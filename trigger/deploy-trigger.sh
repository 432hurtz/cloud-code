#!/usr/bin/env bash
# Deploy the Telegram webhook Cloud Function and register the webhook so
# messages to your bot start the GPU VM.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

echo "==> Deploying Cloud Function ${FUNCTION_NAME}"
gcloud functions deploy "${FUNCTION_NAME}" \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --gen2 --runtime=python312 \
  --source=trigger --entry-point=telegram_webhook \
  --trigger-http --allow-unauthenticated \
  --memory=256Mi --timeout=60s \
  --set-env-vars="^@^PROJECT_ID=${PROJECT_ID}@ZONE=${ZONE}@VM_NAME=${VM_NAME}@TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}@TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}@GUARDRAIL_BLOCK=${GUARDRAIL_BLOCK}"

URL=$(gcloud functions describe "${FUNCTION_NAME}" \
  --project="${PROJECT_ID}" --region="${REGION}" \
  --gen2 --format='value(serviceConfig.uri)')

echo "==> Function URL: ${URL}"

# The function's runtime service account needs to start/stop the VM.
SA=$(gcloud functions describe "${FUNCTION_NAME}" --project="${PROJECT_ID}" \
  --region="${REGION}" --gen2 --format='value(serviceConfig.serviceAccountEmail)')
echo "==> Granting ${SA} compute.instanceAdmin.v1"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA}" \
  --role="roles/compute.instanceAdmin.v1" --quiet >/dev/null

echo "==> Registering Telegram webhook"
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook?url=${URL}"
echo

echo "==> Done. Text your bot to trigger a build."
