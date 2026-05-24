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
NAMESPACE           = "NotasFiscais/API"
CAMPOS_OBRIGATORIOS = ["id", "cliente", "valor", "data_emissao"]

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


def resposta(status_code: int, corpo: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(corpo, ensure_ascii=False),
    }


def validar_nota(nota: dict) -> None:
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
        raise ValueError(f"Campo 'valor' deve ser numérico, recebido: '{nota['valor']}'")

    if not isinstance(nota["data_emissao"], str) or len(nota["data_emissao"]) != 10:
        raise ValueError("Campo 'data_emissao' deve estar no formato YYYY-MM-DD.")


def lambda_handler(event, context):
    log("info", "requisicao_recebida",
        function=context.function_name,
        request_id=context.aws_request_id,
        method="POST")

    body_raw = event.get("body") or ""

    if not body_raw.strip():
        log("warning", "body_vazio", request_id=context.aws_request_id)
        publicar_metrica("RequisicoesInvalidas")
        return resposta(400, {"erro": "O corpo da requisição está vazio."})

    try:
        nota = json.loads(body_raw)
    except json.JSONDecodeError as e:
        log("error", "json_invalido", erro=str(e))
        publicar_metrica("RequisicoesInvalidas")
        return resposta(400, {"erro": f"JSON inválido: {e}"})

    if not isinstance(nota, dict):
        log("error", "tipo_invalido", tipo=str(type(nota)))
        publicar_metrica("RequisicoesInvalidas")
        return resposta(400, {"erro": "O corpo deve ser um objeto JSON."})

    try:
        validar_nota(nota)
    except ValueError as e:
        log("error", "validacao_falhou", erro=str(e))
        publicar_metrica("RequisicoesInvalidas")
        return resposta(422, {"erro": str(e)})

    try:
        tabela    = dynamodb.Table(TABLE_NAME)
        existente = tabela.get_item(Key={"id": nota["id"]})

        if existente.get("Item"):
            log("warning", "nota_duplicada", id=nota["id"])
            publicar_metrica("NotasDuplicadas")
            return resposta(409, {"erro": f"Nota fiscal '{nota['id']}' já cadastrada."})

        tabela.put_item(Item={
            "id":           nota["id"],
            "cliente":      nota["cliente"],
            "valor":        str(nota["valor"]),
            "data_emissao": nota["data_emissao"],
        })
        log("info", "nota_gravada", id=nota["id"], tabela=TABLE_NAME)
        publicar_metrica("NotasCadastradas")
        return resposta(201, {
            "mensagem": f"Nota fiscal '{nota['id']}' cadastrada com sucesso.",
            "nota":     nota,
        })

    except ClientError as e:
        codigo   = e.response["Error"]["Code"]
        mensagem = e.response["Error"]["Message"]
        log("error", "erro_dynamodb", erro_codigo=codigo, erro_mensagem=mensagem)
        publicar_metrica("ErrosInternos")

        if codigo == "ResourceNotFoundException":
            return resposta(503, {"erro": "Tabela não encontrada. Contate o suporte."})

        return resposta(500, {"erro": "Erro interno ao gravar no banco de dados."})

    except Exception as e:
        log("error", "erro_inesperado", erro=str(e))
        publicar_metrica("ErrosInternos")
        return resposta(500, {"erro": "Erro interno inesperado."})