#!/usr/bin/env bash
# Runs on every boot of the GPU VM. Reads the build task from instance
# metadata, runs the coding agent, delivers the result, and ALWAYS self-stops
# so a Spot A100 can never be left billing.
set -uo pipefail

MD="http://metadata.google.internal/computeMetadata/v1"
meta() { curl -s -H "Metadata-Flavor: Google" "${MD}/$1"; }

ZONE=$(meta "instance/zone" | awk -F/ '{print $NF}')
NAME=$(meta "instance/name")
PROJECT=$(meta "project/project-id")
BUCKET="gs://${PROJECT}-code-builder"

# Always self-stop, even if anything below fails.
self_stop() {
  gcloud compute instances stop "${NAME}" --zone="${ZONE}" --quiet
}
trap self_stop EXIT

BUILD_TASK=$(meta "instance/attributes/build-task")
if [ -z "${BUILD_TASK}" ]; then
  echo "No build-task set; nothing to do. Shutting down."
  exit 0
fi

export MODEL=$(meta "instance/attributes/model")
export EMAIL_TO=$(meta "instance/attributes/email-to")
export EMAIL_FROM=$(meta "instance/attributes/email-from")
export SMTP_HOST=$(meta "instance/attributes/smtp-host")
export SMTP_PORT=$(meta "instance/attributes/smtp-port")
export SMTP_USER=$(meta "instance/attributes/smtp-user")
export SMTP_PASSWORD=$(meta "instance/attributes/smtp-password")
export TELEGRAM_BOT_TOKEN=$(meta "instance/attributes/telegram-bot-token")
export TELEGRAM_CHAT_ID=$(meta "instance/attributes/telegram-chat-id")
export ENABLE_RESEARCH=$(meta "instance/attributes/enable-research")
export RESEARCH_MAX_RESULTS=$(meta "instance/attributes/research-max-results")
export GUARDRAIL_MODE=$(meta "instance/attributes/guardrail-mode")
export GUARDRAIL_POLICY=$(meta "instance/attributes/guardrail-policy")
export GUARDRAIL_BLOCK=$(meta "instance/attributes/guardrail-block")
export BUILD_TASK

# ── Anti-runaway watchdog ───────────────────────────────────────────────────
# Force-stops the VM after MAX_RUNTIME_MIN no matter what — a hung build, a
# wedged model server, anything — and alerts you, so it can never run 24/7.
MAX_RUNTIME_MIN=$(meta "instance/attributes/max-runtime-min")
MAX_RUNTIME_MIN=${MAX_RUNTIME_MIN:-60}
(
  sleep $(( MAX_RUNTIME_MIN * 60 ))
  if [ -n "${TELEGRAM_BOT_TOKEN}" ] && [ -n "${TELEGRAM_CHAT_ID}" ]; then
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=⏱️ Hard timeout: ${MAX_RUNTIME_MIN} min reached — force-stopping the VM so it doesn't keep billing." >/dev/null || true
  fi
  gcloud compute instances stop "${NAME}" --zone="${ZONE}" --quiet
) &

# ── Tor (for anonymized web search during builds) ───────────────────────────
if ! command -v tor >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tor
fi
systemctl enable --now tor 2>/dev/null || service tor start || true

# ── Mount the persistent model disk ─────────────────────────────────────────
DEV=/dev/disk/by-id/google-models
mkdir -p /mnt/models
mount "${DEV}" /mnt/models || echo "model disk already mounted?"
export OLLAMA_MODELS=/mnt/models/ollama

# ── Start the model server ──────────────────────────────────────────────────
# Ollama is used for setup simplicity and runs the model on the A100 GPU.
# UPGRADE PATH: for higher throughput, replace this with vLLM serving an
# OpenAI-compatible endpoint on :11434 and point the agent at it unchanged.
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
systemctl stop ollama 2>/dev/null || true
OLLAMA_MODELS=/mnt/models/ollama nohup ollama serve >/var/log/ollama.log 2>&1 &
# Wait for the API to come up.
for _ in $(seq 1 60); do
  curl -sf http://localhost:11434/api/tags >/dev/null && break; sleep 2
done

# ── Fetch and run the agent ─────────────────────────────────────────────────
mkdir -p /opt/agent
gcloud storage rsync "${BUCKET}/agent" /opt/agent --recursive
python3 -m pip install --quiet -r /opt/agent/requirements.txt
python3 /opt/agent/build_and_send.py

echo "Build complete. Shutting down."
# trap self_stop runs on exit.
