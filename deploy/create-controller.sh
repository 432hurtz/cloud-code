#!/usr/bin/env bash
# One-time: create the always-on controller that long-polls Telegram and starts
# the builder. Run create-builder.sh FIRST and export the BUILDER_ID it prints.
#
#   export BUILDER_ID=<id from create-builder.sh>
#   bash deploy/create-controller.sh
#
# Prefer privacy over convenience? Skip this and run controller/poller.py on your
# OWN always-on machine instead (set the same env vars) — then the Scaleway API
# key never touches the cloud.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"

BUILDER_ID="${BUILDER_ID:-${1:-}}"
: "${BUILDER_ID:?Set BUILDER_ID (printed by create-builder.sh) or pass it as arg 1}"

EXTRA=()
[ -n "${SECURITY_GROUP_ID:-}" ] && EXTRA+=("security-group-id=${SECURITY_GROUP_ID}")

echo "==> Creating controller ${CONTROLLER_NAME} (${CONTROLLER_TYPE}) in ${SCW_ZONE}"
CID=$(scw instance server create \
  type="${CONTROLLER_TYPE}" image=ubuntu_jammy name="${CONTROLLER_NAME}" \
  cloud-init=@deploy/controller-cloud-init.yaml \
  stopped=true zone="${SCW_ZONE}" "${EXTRA[@]}" -o json | jq -r '.id')
echo "    controller id: ${CID}"

echo "==> Shipping poller + config to instance user-data"
POLLER_B64=$(base64 -w0 controller/poller.py)
setk() { scw instance user-data set server-id="${CID}" key="$1" content="$2" zone="${SCW_ZONE}" >/dev/null; }
setk poller-b64         "${POLLER_B64}"
setk telegram-chat-id   "${TELEGRAM_CHAT_ID}"
# Secret Manager (optional) takes precedence; otherwise the token rides in user-data.
if [ -n "${TELEGRAM_TOKEN_SECRET_ID:-}" ]; then
  setk telegram-token-secret-id "${TELEGRAM_TOKEN_SECRET_ID}"; setk telegram-bot-token ""
else
  setk telegram-bot-token "${TELEGRAM_BOT_TOKEN}"
fi
setk builder-id         "${BUILDER_ID}"
setk guardrail-block    "${GUARDRAIL_BLOCK}"
setk scw-access-key     "${SCW_ACCESS_KEY}"
setk scw-secret-key     "${SCW_SECRET_KEY}"
setk scw-project-id     "${SCW_DEFAULT_PROJECT_ID}"
setk scw-zone           "${SCW_ZONE}"

echo "==> Starting controller"
scw instance server start "${CID}" zone="${SCW_ZONE}" >/dev/null
echo "==> Done. Text your bot to trigger a build."