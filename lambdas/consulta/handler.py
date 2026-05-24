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

ENDPOINT   = os.environ.get("AWS_ENDPOINT_URL")
TABLE_NAME = os.environ.get("TABLE_NAME", "NotasFiscais")
NAMESPACE  = "NotasFiscais/API"

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


def resposta(status_code: int, corpo: dict | list) -> dict:
    return {
        "statusCode": status_code,
        "headers":    {"Content-Type": "application/json"},
        "body":       json.dumps(corpo, ensure_ascii=False),
    }


def lambda_handler(event, context):
    log("info", "requisicao_recebida",
        function=context.function_name,
        request_id=context.aws_request_id,
        method="GET")

    query_params = event.get("queryStringParameters") or {}
    # Busca por id específico via query string ?id=NF-1
    id_nota = query_params.get("id", "").strip()
    
    try:
        tabela = dynamodb.Table(TABLE_NAME)

        if id_nota:
            log("info", "consulta_por_id", id=id_nota)
            resultado = tabela.get_item(Key={"id": id_nota})
            item      = resultado.get("Item")

            if not item:
                log("warning", "nota_nao_encontrada", id=id_nota)
                publicar_metrica("ConsultasNaoEncontradas")
                return resposta(404, {"erro": f"Nota fiscal '{id_nota}' não encontrada."})

            log("info", "nota_encontrada", id=id_nota)
            publicar_metrica("ConsultasRealizadas")
            return resposta(200, item)

        log("info", "listagem_iniciada")
        resultado = tabela.scan()
        itens     = resultado.get("Items", [])

        while "LastEvaluatedKey" in resultado:
            resultado = tabela.scan(ExclusiveStartKey=resultado["LastEvaluatedKey"])
            itens.extend(resultado.get("Items", []))

        log("info", "listagem_concluida", total=len(itens))
        publicar_metrica("ListagensRealizadas")
        return resposta(200, {"total": len(itens), "notas": itens})

    except ClientError as e:
        codigo   = e.response["Error"]["Code"]
        mensagem = e.response["Error"]["Message"]
        log("error", "erro_dynamodb", erro_codigo=codigo, erro_mensagem=mensagem)
        publicar_metrica("ErrosInternos")

        if codigo == "ResourceNotFoundException":
            return resposta(503, {"erro": "Tabela não encontrada. Contate o suporte."})

        return resposta(500, {"erro": "Erro interno ao acessar o banco de dados."})

    except Exception as e:
        log("error", "erro_inesperado", erro=str(e))
        publicar_metrica("ErrosInternos")
        return resposta(500, {"erro": "Erro interno inesperado."})