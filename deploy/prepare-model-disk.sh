#!/usr/bin/env bash
# One-time: create a persistent disk and download the model onto it using a
# cheap temporary CPU VM (no GPU needed just to download weights). The disk
# survives so the A100 never has to re-download ~20GB on each boot.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

TMP_VM="model-prep-$$"

echo "==> Creating model disk ${MODEL_DISK_NAME} (${MODEL_DISK_GB}GB)"
gcloud compute disks create "${MODEL_DISK_NAME}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --size="${MODEL_DISK_GB}GB" --type=pd-balanced 2>/dev/null || \
  echo "    (disk already exists, reusing)"

echo "==> Launching temporary CPU VM to download the model"
gcloud compute instances create "${TMP_VM}" \
  --project="${PROJECT_ID}" --zone="${ZONE}" \
  --machine-type=e2-standard-4 \
  --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
  --disk="name=${MODEL_DISK_NAME},device-name=models,mode=rw,boot=no"

cleanup() {
  echo "==> Deleting temporary VM (disk is kept)"
  gcloud compute instances delete "${TMP_VM}" \
    --project="${PROJECT_ID}" --zone="${ZONE}" --quiet || true
}
trap cleanup EXIT

echo "==> Waiting for SSH..."
sleep 30

echo "==> Formatting/mounting disk and pulling model: ${MODEL}"
gcloud compute ssh "${TMP_VM}" --project="${PROJECT_ID}" --zone="${ZONE}" \
  --command="
    set -e
    DEV=/dev/disk/by-id/google-models
    sudo blkid \$DEV || sudo mkfs.ext4 -F \$DEV
    sudo mkdir -p /mnt/models && sudo mount \$DEV /mnt/models
    sudo mkdir -p /mnt/models/ollama && sudo chown -R \$USER /mnt/models/ollama
    curl -fsSL https://ollama.com/install.sh | sh
    sudo systemctl stop ollama || true
    export OLLAMA_MODELS=/mnt/models/ollama
    OLLAMA_MODELS=/mnt/models/ollama nohup ollama serve >/tmp/ollama.log 2>&1 &
    sleep 5
    OLLAMA_MODELS=/mnt/models/ollama ollama pull '${MODEL}'
    sync
    echo 'Model downloaded to persistent disk.'
  "

echo "==> Done. Model '${MODEL}' is on disk '${MODEL_DISK_NAME}'."
