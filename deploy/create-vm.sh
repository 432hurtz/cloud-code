#!/usr/bin/env bash
# One-time: create the Spot A100 GPU VM, LEFT STOPPED. The Telegram trigger
# starts it on demand; the startup script builds, delivers, and self-stops.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

BUCKET="gs://${PROJECT_ID}-code-builder"

echo "==> Ensuring staging bucket ${BUCKET} exists"
gcloud storage buckets create "${BUCKET}" --project="${PROJECT_ID}" \
  --location="${REGION}" 2>/dev/null || echo "    (bucket exists, reusing)"

echo "==> Uploading agent code to ${BUCKET}/agent"
gcloud storage rsync agent "${BUCKET}/agent" --recursive

echo "==> Creating Spot GPU VM ${VM_NAME} (${MACHINE_TYPE}, ${GPU_TYPE} x${GPU_COUNT})"

# Secrets/config are passed via instance metadata for simplicity.
# HARDENING: move SMTP_PASSWORD / TELEGRAM_BOT_TOKEN into Secret Manager and
# fetch them in startup-script.sh instead of reading them from metadata.
gcloud compute instances create "${VM_NAME}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --machine-type="${MACHINE_TYPE}" \
  --accelerator="type=${GPU_TYPE},count=${GPU_COUNT}" \
  --provisioning-model=SPOT \
  --instance-termination-action=STOP \
  --maintenance-policy=TERMINATE \
  --image-family=common-cu123-ubuntu-2204 \
  --image-project=deeplearning-platform-release \
  --boot-disk-size="${BOOT_DISK_GB}GB" \
  --boot-disk-type=pd-balanced \
  --disk="name=${MODEL_DISK_NAME},device-name=models,mode=rw,boot=no" \
  --scopes=cloud-platform \
  --metadata-from-file=startup-script=deploy/startup-script.sh \
  --metadata="\
model=${MODEL},\
email-to=${EMAIL_TO},\
email-from=${EMAIL_FROM},\
smtp-host=${SMTP_HOST},\
smtp-port=${SMTP_PORT},\
smtp-user=${SMTP_USER},\
smtp-password=${SMTP_PASSWORD},\
telegram-bot-token=${TELEGRAM_BOT_TOKEN},\
build-task=,\
telegram-chat-id="

echo "==> Stopping VM so it sits idle (\$0 compute) until a Telegram message wakes it"
gcloud compute instances stop "${VM_NAME}" \
  --project="${PROJECT_ID}" --zone="${ZONE}"

echo "==> Done. VM '${VM_NAME}' is created and stopped."
