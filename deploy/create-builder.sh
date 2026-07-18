#!/usr/bin/env bash
# One-time: create the Scaleway GPU builder, LEFT STOPPED. The controller starts
# it on demand; the systemd service builds, delivers, and self-stops.
#
# Prereq: `scw` CLI installed and `.env` filled in. Model + agent code live on
# the instance's persistent root volume, so the first build is slow (installs +
# ~model download) and every later build is fast.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"

echo "==> Creating stopped GPU builder ${BUILDER_NAME} (${BUILDER_TYPE}) in ${SCW_ZONE}"
# NOTE: root-volume sizing syntax can vary by CLI version; if this is rejected,
# create with the default root volume and resize it to ${MODEL_VOLUME_GB}GB in
# the console, or attach a block volume. b_ssd = persistent block SSD (survives
# stop/start), which is what we need to keep the model between builds.
SERVER_ID=$(scw instance server create \
  type="${BUILDER_TYPE}" image="${BUILDER_IMAGE}" name="${BUILDER_NAME}" \
  root-volume="b_ssd:${MODEL_VOLUME_GB}GB" \
  cloud-init=@deploy/builder-cloud-init.yaml \
  stopped=true zone="${SCW_ZONE}" -o json | jq -r '.id')
echo "    builder id: ${SERVER_ID}"

echo "==> Shipping agent code + config to instance user-data"
AGENT_B64=$(tar czf - agent | base64 -w0)

setk() { scw instance user-data set server-id="${SERVER_ID}" key="$1" content="$2" zone="${SCW_ZONE}" >/dev/null; }
setk agent-tar-b64        "${AGENT_B64}"
setk model                "${MODEL}"
setk email-to             "${EMAIL_TO}"
setk email-from           "${EMAIL_FROM}"
setk smtp-host            "${SMTP_HOST}"
setk smtp-port            "${SMTP_PORT}"
setk smtp-user            "${SMTP_USER}"
setk smtp-password        "${SMTP_PASSWORD}"
setk telegram-bot-token   "${TELEGRAM_BOT_TOKEN}"
setk enable-research      "${ENABLE_RESEARCH}"
setk research-max-results "${RESEARCH_MAX_RESULTS}"
setk guardrail-mode       "${GUARDRAIL_MODE}"
setk guardrail-block      "${GUARDRAIL_BLOCK}"
setk guardrail-policy     "${GUARDRAIL_POLICY}"
setk agent-instructions   "${AGENT_INSTRUCTIONS}"
setk recipient-age-pubkey "${RECIPIENT_AGE_PUBKEY}"
setk max-runtime-min      "${MAX_RUNTIME_MIN}"
setk scw-access-key       "${SCW_ACCESS_KEY}"
setk scw-secret-key       "${SCW_SECRET_KEY}"
setk scw-project-id       "${SCW_DEFAULT_PROJECT_ID}"
setk scw-zone             "${SCW_ZONE}"
setk build-task           ""
setk telegram-chat-id     ""

echo "==> Done. Builder is created and STOPPED."
echo "    Set this for the controller step:  export BUILDER_ID=${SERVER_ID}"