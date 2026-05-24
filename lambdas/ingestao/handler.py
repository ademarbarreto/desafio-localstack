import json
import boto3
import os
import logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '{"timestamp":"%(asctime)s","level":"%(levelname)s","message":%(message)s}'
    ))
    logger.addHandler(handler)

ENDPOINT            = os.environ.get("AWS_ENDPOINT_URL")
TABLE_NAME          = os.environ.get("TABLE_NAME", "NotasFiscais")
NAMESPACE           = "NotasFiscais/Processamento"
CAMPOS_OBRIGATORIOS = ["id", "cliente", "valor", "data_emissao"]

s3         = boto3.client("s3",         endpoint_url=ENDPOINT)
dynamodb   = boto3.resource("dynamodb", endpoint_url=ENDPOINT)
cloudwatch = boto3.client("cloudwatch", endpoint_url=ENDPOINT)


def log(nivel: str, acao: str, **kwargs) -> None:
    payload = json.dumps({"acao": acao, **kwargs})
    getattr(logger, nivel)(payload)


def publicar_metrica(nome: str, valor: float = 1, unidade: str = "Count") -> None:
    try:
        cloudwatch.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{
                "MetricName": nome,
                "Value":      valor,
                "Unit":       unidade,
                "Timestamp":  datetime.now(timezone.utc),
            }]
        )
    except Exception as e:
        log("warning", "metrica_nao_publicada", metrica=nome, erro=str(e))


def mover_arquivo(bucket: str, key_origem: str, destino: str) -> None:
    datahora      = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome_original = os.path.basename(key_origem)
    nome_sem_ext  = os.path.splitext(nome_original)[0]
    key_destino   = f"{destino}/{datahora}_{nome_sem_ext}.json"

    try:
        s3.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": key_origem},
            Key=key_destino,
        )
        log("info", "arquivo_copiado", origem=key_origem, destino=key_destino)
        s3.delete_object(Bucket=bucket, Key=key_origem)
        log("info", "arquivo_removido", key=key_origem)
    except ClientError as e:
        codigo   = e.response["Error"]["Code"]
        mensagem = e.response["Error"]["Message"]
        log("error", "falha_mover_arquivo",
            origem=key_origem, destino=destino,
            erro_codigo=codigo, erro_mensagem=mensagem)
        raise


def validar_item(nota: dict) -> None:
    faltando = [c for c in CAMPOS_OBRIGATORIOS if c not in nota]
    if faltando:
        raise ValueError(f"Campos obrigatórios ausentes: {faltando}")

    if not isinstance(nota["id"], str) or not nota["id"].strip():
        raise ValueError("Campo 'id' deve ser uma string não vazia.")

    if not isinstance(nota["cliente"], str) or not nota["cliente"].strip():
        raise ValueError("Campo 'cliente' deve ser uma string não vazia.")

    try:
        float(nota["valor"])
    except (ValueError, TypeError):
        raise ValueError(f"Campo 'valor' inválido: '{nota['valor']}'")

    if not isinstance(nota["data_emissao"], str) or len(nota["data_emissao"]) != 10:
        raise ValueError("Campo 'data_emissao' deve estar no formato YYYY-MM-DD.")


def processar_arquivo(bucket: str, key: str) -> dict:
    if not key.endswith(".json"):
        raise ValueError(f"Extensão inválida: '{key}'")

    # Leitura do S3
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        log("info", "arquivo_lido", bucket=bucket, key=key)
    except ClientError as e:
        codigo = e.response["Error"]["Code"]
        if codigo == "NoSuchKey":
            raise FileNotFoundError(f"Arquivo não encontrado: '{key}'")
        raise RuntimeError(f"Erro S3 [{codigo}]: {e.response['Error']['Message']}")

    # Parse do JSON
    try:
        body  = obj["Body"].read().decode("utf-8")
        dados = json.loads(body)
        log("info", "json_parseado", key=key)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"JSON inválido em '{key}': {e}")

    # Normaliza para lista
    if isinstance(dados, dict):
        notas = [dados]
    elif isinstance(dados, list):
        notas = dados
    else:
        raise ValueError("JSON deve ser um objeto {} ou uma lista de objetos [{}].")

    if len(notas) == 0:
        raise ValueError("Lista de notas está vazia.")

    log("info", "notas_encontradas", total=len(notas), key=key)

    tabela     = dynamodb.Table(TABLE_NAME)
    sucessos   = 0
    duplicatas = 0
    erros      = 0

    for nota in notas:
        try:
            validar_item(nota)

            # Verifica duplicata antes de gravar
            existente = tabela.get_item(Key={"id": nota["id"]})
            if existente.get("Item"):
                log("warning", "item_duplicado", id=nota["id"])
                publicar_metrica("NotasDuplicadas")
                duplicatas += 1
                continue

            tabela.put_item(Item={
                "id":           nota["id"],
                "cliente":      nota["cliente"],
                "valor":        str(nota["valor"]),
                "data_emissao": nota["data_emissao"],
            })
            log("info", "item_gravado", id=nota["id"], tabela=TABLE_NAME)
            sucessos += 1

        except ValueError as e:
            log("error", "erro_validacao_item", id=nota.get("id", "?"), erro=str(e))
            erros += 1
        except ClientError as e:
            codigo   = e.response["Error"]["Code"]
            mensagem = e.response["Error"]["Message"]
            log("error", "erro_dynamodb_item",
                id=nota.get("id", "?"),
                erro_codigo=codigo,
                erro_mensagem=mensagem)
            erros += 1
        except Exception as e:
            log("error", "erro_inesperado_item", id=nota.get("id", "?"), erro=str(e))
            erros += 1

    log("info", "resultado_processamento",
        total=len(notas),
        sucessos=sucessos,
        duplicatas=duplicatas,
        erros=erros)

    # Nenhum item novo gravado → arquivo vai para ERRO
    if sucessos == 0:
        raise RuntimeError(
            f"Nenhum item gravado. "
            f"Duplicatas: {duplicatas}, Erros: {erros}"
        )

    return {"sucessos": sucessos, "duplicatas": duplicatas, "erros": erros}


def lambda_handler(event, context):
    log("info", "lambda_iniciada",
        function=context.function_name,
        request_id=context.aws_request_id,
        records_count=len(event.get("Records", [])))

    total_sucessos   = 0
    total_duplicatas = 0
    total_erros      = 0

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]

        if key.startswith("SUCESSO/") or key.startswith("ERRO/"):
            log("info", "arquivo_ignorado", key=key, motivo="diretorio_destino")
            continue

        log("info", "processamento_iniciado", bucket=bucket, key=key)
        try:
            resultado = processar_arquivo(bucket, key)
            mover_arquivo(bucket, key, "SUCESSO")
            publicar_metrica("NotasProcessadas", valor=resultado["sucessos"])
            log("info", "processamento_concluido",
                key=key,
                status="SUCESSO",
                sucessos=resultado["sucessos"],
                duplicatas=resultado["duplicatas"],
                erros=resultado["erros"])
            total_sucessos   += resultado["sucessos"]
            total_duplicatas += resultado["duplicatas"]
            total_erros      += resultado["erros"]

        except (ValueError, FileNotFoundError) as e:
            log("error", "erro_validacao", key=key, erro=str(e))
            publicar_metrica("NotasComErroValidacao")
            mover_arquivo(bucket, key, "ERRO")
            total_erros += 1

        except RuntimeError as e:
            log("error", "erro_processamento", key=key, erro=str(e))
            publicar_metrica("NotasComErroInesperado")
            mover_arquivo(bucket, key, "ERRO")
            total_erros += 1

        except Exception as e:
            log("error", "erro_inesperado", key=key, erro=str(e))
            publicar_metrica("NotasComErroInesperado")
            mover_arquivo(bucket, key, "ERRO")
            total_erros += 1

    log("info", "lambda_finalizada",
        function=context.function_name,
        request_id=context.aws_request_id,
        total_sucessos=total_sucessos,
        total_duplicatas=total_duplicatas,
        total_erros=total_erros)

    return {
        "statusCode": 200,
        "body": json.dumps({
            "mensagem":         "Processamento concluído.",
            "total_sucessos":   total_sucessos,
            "total_duplicatas": total_duplicatas,
            "total_erros":      total_erros,
        })
    }