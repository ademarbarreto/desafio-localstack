#!/bin/bash
set -e


# ── Configuração ──────────────────────────────────────────────────────
REGION="us-east-1"
ACCOUNT_ID="000000000000"
BUCKET="notas-fiscais-upload"
TABLE="NotasFiscais"
LAMBDA_PROCESSAR="ProcessarNotasFiscais"
LAMBDA_CONSULTAR="ConsultarNotasFiscais"
LAMBDA_RECEBER="ReceberNotasFiscais"
API_NAME="NotaFiscaisAPI"

export AWS_PAGER=""
export AWS_DEFAULT_OUTPUT="json"

# ── Caminhos ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
ZIP_DIR="$SCRIPT_DIR/zips"

mkdir -p "$ZIP_DIR"

log()     { echo ""; echo "==> $1"; }
sucesso() { echo "    ✔ $1"; }

# ── Validação prévia dos arquivos ─────────────────────────────────────
log "Verificando estrutura do projeto..."
ERRO_ESTRUTURA=0
for HANDLER in ingestao consulta receber; do
  if [ ! -f "$ROOT_DIR/lambdas/$HANDLER/handler.py" ]; then
    echo "    ✘ Arquivo não encontrado: lambdas/$HANDLER/handler.py"
    ERRO_ESTRUTURA=1
  else
    sucesso "lambdas/$HANDLER/handler.py encontrado."
  fi
done

if [ $ERRO_ESTRUTURA -eq 1 ]; then
  echo ""
  echo "❌ Corrija a estrutura do projeto antes de continuar."
  exit 1
fi

# ── Limpeza de recursos anteriores ───────────────────────────────────
log "Limpando recursos anteriores (se existirem)..."
awslocal s3 rb s3://$BUCKET --force                                2>/dev/null && sucesso "Bucket removido."              || true
awslocal dynamodb delete-table --table-name $TABLE                 2>/dev/null && sucesso "Tabela removida."              || true
awslocal lambda delete-function --function-name $LAMBDA_PROCESSAR  2>/dev/null && sucesso "Lambda processar removida."    || true
awslocal lambda delete-function --function-name $LAMBDA_CONSULTAR  2>/dev/null && sucesso "Lambda consultar removida."    || true
awslocal lambda delete-function --function-name $LAMBDA_RECEBER    2>/dev/null && sucesso "Lambda receber removida."      || true
awslocal logs delete-log-group \
  --log-group-name /aws/lambda/$LAMBDA_PROCESSAR                   2>/dev/null && sucesso "Log group processar removido." || true
awslocal logs delete-log-group \
  --log-group-name /aws/lambda/$LAMBDA_CONSULTAR                   2>/dev/null && sucesso "Log group consultar removido." || true
awslocal logs delete-log-group \
  --log-group-name /aws/lambda/$LAMBDA_RECEBER                     2>/dev/null && sucesso "Log group receber removido."   || true

# ── S3 ────────────────────────────────────────────────────────────────
log "Criando bucket '$BUCKET'..."
awslocal s3 mb s3://$BUCKET --region $REGION
awslocal s3api put-object --bucket $BUCKET --key "SUCESSO/" --region $REGION
awslocal s3api put-object --bucket $BUCKET --key "ERRO/"    --region $REGION
sucesso "Bucket e diretórios criados."

# ── DynamoDB ──────────────────────────────────────────────────────────
log "Criando tabela DynamoDB '$TABLE'..."
awslocal dynamodb create-table \
  --table-name $TABLE \
  --attribute-definitions AttributeName=id,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region $REGION
sucesso "Tabela '$TABLE' criada."

# ── Empacotamento das Lambdas ─────────────────────────────────────────
log "Empacotando Lambdas..."

python3 -c "
import zipfile, os

pares = [
    ('$ROOT_DIR/lambdas/ingestao/handler.py', '$ZIP_DIR/processar.zip'),
    ('$ROOT_DIR/lambdas/consulta/handler.py',  '$ZIP_DIR/consultar.zip'),
    ('$ROOT_DIR/lambdas/receber/handler.py',   '$ZIP_DIR/receber.zip'),
]

for origem, destino in pares:
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(origem, 'handler.py')
    print(f'    ✔ {os.path.basename(destino)} gerado.')
"



# ── IP do container LocalStack na rede bridge ─────────────────────────
DOCKER_HOST_IP=$(docker inspect localstack-main \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

LAMBDA_ENDPOINT="http://${DOCKER_HOST_IP}:4566"

log "Endpoint Lambda → LocalStack: $LAMBDA_ENDPOINT"

# ── Lambdas ───────────────────────────────────────────────────────────
log "Criando Lambda '$LAMBDA_PROCESSAR'..."
awslocal lambda create-function \
  --function-name $LAMBDA_PROCESSAR \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-role \
  --handler handler.lambda_handler \
  --zip-file fileb://$ZIP_DIR/processar.zip \
  --environment Variables="{TABLE_NAME=$TABLE,AWS_ENDPOINT_URL=$LAMBDA_ENDPOINT}" \
  --timeout 30 \
  --region $REGION
sucesso "Lambda '$LAMBDA_PROCESSAR' criada."

log "Criando Lambda '$LAMBDA_CONSULTAR'..."
awslocal lambda create-function \
  --function-name $LAMBDA_CONSULTAR \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-role \
  --handler handler.lambda_handler \
  --zip-file fileb://$ZIP_DIR/consultar.zip \
  --environment Variables="{TABLE_NAME=$TABLE,AWS_ENDPOINT_URL=$LAMBDA_ENDPOINT}" \
  --timeout 30 \
  --region $REGION
sucesso "Lambda '$LAMBDA_CONSULTAR' criada."

log "Criando Lambda '$LAMBDA_RECEBER'..."
awslocal lambda create-function \
  --function-name $LAMBDA_RECEBER \
  --runtime python3.12 \
  --role arn:aws:iam::${ACCOUNT_ID}:role/lambda-role \
  --handler handler.lambda_handler \
  --zip-file fileb://$ZIP_DIR/receber.zip \
  --environment Variables="{TABLE_NAME=$TABLE,AWS_ENDPOINT_URL=$LAMBDA_ENDPOINT}" \
  --timeout 30 \
  --region $REGION
sucesso "Lambda '$LAMBDA_RECEBER' criada."

# ── Trigger S3 → ProcessarNotasFiscais ───────────────────────────────
log "Configurando trigger S3 → '$LAMBDA_PROCESSAR'..."
LAMBDA_ARN_PROCESSAR="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_PROCESSAR}"

# Permissão para o S3 invocar a Lambda
awslocal lambda add-permission \
  --function-name $LAMBDA_PROCESSAR \
  --statement-id  "s3-invoke-processar" \
  --action        "lambda:InvokeFunction" \
  --principal     "s3.amazonaws.com" \
  --source-arn    "arn:aws:s3:::${BUCKET}" \
  --region $REGION
sucesso "Permissão S3 → Lambda concedida."

# Notificação do bucket apontando para a Lambda
awslocal s3api put-bucket-notification-configuration \
  --bucket $BUCKET \
  --notification-configuration "{
    \"LambdaFunctionConfigurations\": [{
      \"LambdaFunctionArn\": \"${LAMBDA_ARN_PROCESSAR}\",
      \"Events\": [\"s3:ObjectCreated:*\"],
      \"Filter\": {
        \"Key\": {
          \"FilterRules\": [
            {\"Name\": \"suffix\", \"Value\": \".json\"}
          ]
        }
      }
    }]
  }" \
  --region $REGION
sucesso "Trigger S3 configurado."

# ── API Gateway ───────────────────────────────────────────────────────
log "Criando API Gateway '$API_NAME'..."
API_ID=$(awslocal apigateway create-rest-api \
  --name "$API_NAME" \
  --description "API de gerenciamento de Notas Fiscais" \
  --region $REGION \
  --query "id" --output text)
sucesso "API criada com ID: $API_ID"

ROOT_ID=$(awslocal apigateway get-resources \
  --rest-api-id $API_ID \
  --region $REGION \
  --query "items[?path=='/'].id" --output text)

log "Criando recurso /notas..."
NOTAS_ID=$(awslocal apigateway create-resource \
  --rest-api-id $API_ID \
  --parent-id $ROOT_ID \
  --path-part "notas" \
  --region $REGION \
  --query "id" --output text)
sucesso "Recurso /notas criado: $NOTAS_ID"

LAMBDA_ARN_CONSULTAR="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_CONSULTAR}"
LAMBDA_ARN_RECEBER="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${LAMBDA_RECEBER}"

log "Configurando GET /notas → '$LAMBDA_CONSULTAR'..."
awslocal apigateway put-method \
  --rest-api-id $API_ID \
  --resource-id $NOTAS_ID \
  --http-method GET \
  --authorization-type NONE \
  --request-parameters "method.request.querystring.numero=false" \
  --region $REGION

awslocal apigateway put-integration \
  --rest-api-id $API_ID \
  --resource-id $NOTAS_ID \
  --http-method GET \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN_CONSULTAR}/invocations" \
  --region $REGION

for STATUS in 200 404 500 503; do
  awslocal apigateway put-method-response \
    --rest-api-id $API_ID \
    --resource-id $NOTAS_ID \
    --http-method GET \
    --status-code $STATUS \
    --response-models '{"application/json": "Empty"}' \
    --region $REGION
done
sucesso "GET /notas configurado."

log "Configurando POST /notas → '$LAMBDA_RECEBER'..."
awslocal apigateway put-method \
  --rest-api-id $API_ID \
  --resource-id $NOTAS_ID \
  --http-method POST \
  --authorization-type NONE \
  --region $REGION

awslocal apigateway put-integration \
  --rest-api-id $API_ID \
  --resource-id $NOTAS_ID \
  --http-method POST \
  --type AWS_PROXY \
  --integration-http-method POST \
  --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${LAMBDA_ARN_RECEBER}/invocations" \
  --region $REGION

for STATUS in 201 400 409 422 500 503; do
  awslocal apigateway put-method-response \
    --rest-api-id $API_ID \
    --resource-id $NOTAS_ID \
    --http-method POST \
    --status-code $STATUS \
    --response-models '{"application/json": "Empty"}' \
    --region $REGION
done
sucesso "POST /notas configurado."

log "Fazendo deploy no stage 'dev'..."
awslocal apigateway create-deployment \
  --rest-api-id $API_ID \
  --stage-name dev \
  --region $REGION
sucesso "Deploy concluído."

# ── CloudWatch Log Groups ─────────────────────────────────────────────
log "Criando Log Groups no CloudWatch..."
for LAMBDA_NAME in $LAMBDA_PROCESSAR $LAMBDA_CONSULTAR $LAMBDA_RECEBER; do
  LOG_GROUP="/aws/lambda/${LAMBDA_NAME}"
  awslocal logs create-log-group \
    --log-group-name "$LOG_GROUP" \
    --region $REGION
  awslocal logs put-retention-policy \
    --log-group-name "$LOG_GROUP" \
    --retention-in-days 30 \
    --region $REGION
  sucesso "Log group '$LOG_GROUP' criado (retenção: 30 dias)."
done

# ── Metric Filters ────────────────────────────────────────────────────
log "Criando filtros de métricas..."
awslocal logs put-metric-filter \
  --log-group-name "/aws/lambda/$LAMBDA_PROCESSAR" \
  --filter-name    "ErrosProcessamento" \
  --filter-pattern '{ $.level = "ERROR" }' \
  --metric-transformations \
    metricName=ErrosProcessamento,metricNamespace=NotasFiscais/Processamento,metricValue=1,defaultValue=0 \
  --region $REGION
sucesso "Filtro 'ErrosProcessamento' criado."

awslocal logs put-metric-filter \
  --log-group-name "/aws/lambda/$LAMBDA_CONSULTAR" \
  --filter-name    "ErrosConsulta" \
  --filter-pattern '{ $.level = "ERROR" }' \
  --metric-transformations \
    metricName=ErrosConsulta,metricNamespace=NotasFiscais/API,metricValue=1,defaultValue=0 \
  --region $REGION
sucesso "Filtro 'ErrosConsulta' criado."

awslocal logs put-metric-filter \
  --log-group-name "/aws/lambda/$LAMBDA_RECEBER" \
  --filter-name    "ErrosRecebimento" \
  --filter-pattern '{ $.level = "ERROR" }' \
  --metric-transformations \
    metricName=ErrosRecebimento,metricNamespace=NotasFiscais/API,metricValue=1,defaultValue=0 \
  --region $REGION
sucesso "Filtro 'ErrosRecebimento' criado."

# ── Alarmes ───────────────────────────────────────────────────────────
log "Criando alarmes no CloudWatch..."
awslocal cloudwatch put-metric-alarm \
  --alarm-name          "AlarmErrosProcessamento" \
  --alarm-description   "Erros na Lambda ProcessarNotasFiscais" \
  --namespace           "NotasFiscais/Processamento" \
  --metric-name         "ErrosProcessamento" \
  --statistic           Sum \
  --period              60 \
  --evaluation-periods  1 \
  --threshold           1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data  notBreaching \
  --region $REGION
sucesso "Alarme 'AlarmErrosProcessamento' criado."

awslocal cloudwatch put-metric-alarm \
  --alarm-name          "AlarmErrosAPI" \
  --alarm-description   "Erros internos nas Lambdas de API" \
  --namespace           "NotasFiscais/API" \
  --metric-name         "ErrosInternos" \
  --statistic           Sum \
  --period              60 \
  --evaluation-periods  1 \
  --threshold           3 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data  notBreaching \
  --region $REGION
sucesso "Alarme 'AlarmErrosAPI' criado."

awslocal cloudwatch put-metric-alarm \
  --alarm-name          "AlarmNotasDuplicadas" \
  --alarm-description   "Tentativas de cadastro duplicado" \
  --namespace           "NotasFiscais/API" \
  --metric-name         "NotasDuplicadas" \
  --statistic           Sum \
  --period              300 \
  --evaluation-periods  1 \
  --threshold           5 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data  notBreaching \
  --region $REGION
sucesso "Alarme 'AlarmNotasDuplicadas' criado."

# ── Resumo ────────────────────────────────────────────────────────────
BASE_URL="http://localhost:4566/restapis/${API_ID}/dev/_user_request_/notas"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅  Infraestrutura criada com sucesso!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Bucket S3  :  s3://$BUCKET"
echo "    ├─ SUCESSO/"
echo "    └─ ERRO/"
echo ""
echo "  Tabela     :  $TABLE"
echo ""
echo "  Lambdas    :"
echo "    ├─ $LAMBDA_PROCESSAR   ← trigger S3"
echo "    ├─ $LAMBDA_CONSULTAR   ← GET /notas"
echo "    └─ $LAMBDA_RECEBER     ← POST /notas"
echo ""
echo "  API        :  $API_NAME (ID: $API_ID)"
echo "    ├─ GET  $BASE_URL"
echo "    ├─ GET  $BASE_URL?id=NF-999"
echo "    └─ POST $BASE_URL"
echo ""
echo "  Logs       :"
echo "    ├─ /aws/lambda/$LAMBDA_PROCESSAR"
echo "    ├─ /aws/lambda/$LAMBDA_CONSULTAR"
echo "    └─ /aws/lambda/$LAMBDA_RECEBER"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"