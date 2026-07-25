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
export SCW_DEFAULT_ORGANIZATION_ID="${SCW_DEFAULT_ORGANIZATION_ID:-$SCW_DEFAULT_PROJECT_ID}"
export SCW_DEFAULT_ZONE="${SCW_ZONE}"

EXTRA=()
[ -n "${SECURITY_GROUP_ID:-}" ] && EXTRA+=("security-group-id=${SECURITY_GROUP_ID}")

echo "==> Creating stopped GPU builder ${BUILDER_NAME} (${BUILDER_TYPE}) in ${SCW_ZONE}"
# NOTE: root-volume sizing syntax can vary by CLI version; if this is rejected,
# create with the default root volume and resize it to ${MODEL_VOLUME_GB}GB in
# the console, or attach a block volume. b_ssd = persistent block SSD (survives
# stop/start), which is what we need to keep the model between builds.
# ip=dynamic: a dynamic IP is allocated at boot and released on stop — it's free,
# doesn't consume the flexible-IP (cp_ips) quota, and doesn't orphan on delete
# (unlike a `new` flexible IP). The builder only needs outbound access anyway.
# When BUILDER_IMAGE is a snapshot image (a UUID), the root volume comes from the
# image (model baked in) — don't pass root-volume. For a marketplace label, size it.
ROOTVOL=(root-volume="sbs:${MODEL_VOLUME_GB}GB")
if [[ "${BUILDER_IMAGE}" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}- ]]; then
  ROOTVOL=(); echo "    booting from snapshot image (model baked in — fast boot)"
fi
SERVER_ID=$(scw instance server create \
  type="${BUILDER_TYPE}" image="${BUILDER_IMAGE}" name="${BUILDER_NAME}" \
  "${ROOTVOL[@]}" \
  ip=dynamic dynamic-ip-required=true \
  cloud-init=@deploy/builder-cloud-init.yaml \
  stopped=true zone="${SCW_ZONE}" "${EXTRA[@]}" -o json | jq -r '.id')
echo "    builder id: ${SERVER_ID}"

echo "==> Shipping agent code + config to instance user-data"
AGENT_B64=$(tar czf - agent | base64 -w0)

# Route through a temp file (content=@file), not a raw arg — the agent tarball
# has grown enough to exceed the shell's max argument length ("Argument list
# too long", hit live 2026-07-21). A temp file has no such limit.
setk() {
  [ -n "$2" ] || return 0
  local tmp; tmp=$(mktemp)
  printf '%s' "$2" > "$tmp"
  scw instance user-data set server-id="${SERVER_ID}" key="$1" content="@${tmp}" zone="${SCW_ZONE}" >/dev/null
  rm -f "$tmp"
}

# Scaleway caps user-data at ~15 keys per server, so we can't ship one key per
# setting. Pack ALL config into a single JSON "config" key (run.sh parses it with
# jq). build-task and telegram-chat-id stay separate — the controller sets them
# at trigger time. Empty content is also rejected (400), so build-task/chat-id
# are simply left unset until then (read back empty = the intended idle state).
# jq -n --arg keeps every value safely quoted regardless of spaces / punctuation.
CONFIG_JSON=$(jq -n \
  --arg model                     "${MODEL}" \
  --arg email_to                  "${EMAIL_TO}" \
  --arg email_from                "${EMAIL_FROM}" \
  --arg smtp_host                 "${SMTP_HOST}" \
  --arg smtp_port                 "${SMTP_PORT}" \
  --arg smtp_user                 "${SMTP_USER}" \
  --arg smtp_password             "${SMTP_PASSWORD:-}" \
  --arg smtp_password_secret_id   "${SMTP_PASSWORD_SECRET_ID:-}" \
  --arg telegram_bot_token        "${TELEGRAM_BOT_TOKEN:-}" \
  --arg telegram_token_secret_id  "${TELEGRAM_TOKEN_SECRET_ID:-}" \
  --arg enable_research           "${ENABLE_RESEARCH}" \
  --arg research_max_results      "${RESEARCH_MAX_RESULTS}" \
  --arg guardrail_mode            "${GUARDRAIL_MODE}" \
  --arg guardrail_policy          "${GUARDRAIL_POLICY}" \
  --arg agent_instructions        "${AGENT_INSTRUCTIONS}" \
  --arg recipient_age_pubkey      "${RECIPIENT_AGE_PUBKEY}" \
  --arg max_runtime_min           "${MAX_RUNTIME_MIN}" \
  --arg scw_access_key            "${SCW_ACCESS_KEY}" \
  --arg scw_secret_key            "${SCW_SECRET_KEY}" \
  --arg scw_project_id            "${SCW_DEFAULT_PROJECT_ID}" \
  --arg scw_organization_id       "${SCW_DEFAULT_ORGANIZATION_ID}" \
  --arg scw_zone                  "${SCW_ZONE}" \
  --arg forgejo_url               "${FORGEJO_URL:-}" \
  --arg forgejo_user              "${FORGEJO_USER:-}" \
  --arg forgejo_token             "${FORGEJO_TOKEN:-}" \
  --arg idle_timeout_min          "${IDLE_TIMEOUT_MIN:-8}" \
  --arg tailscale_authkey         "${TAILSCALE_AUTHKEY:-}" \
  '{model:$model, "email-to":$email_to, "email-from":$email_from,
    "smtp-host":$smtp_host, "smtp-port":$smtp_port, "smtp-user":$smtp_user,
    "smtp-password":$smtp_password, "smtp-password-secret-id":$smtp_password_secret_id,
    "telegram-bot-token":$telegram_bot_token, "telegram-token-secret-id":$telegram_token_secret_id,
    "enable-research":$enable_research, "research-max-results":$research_max_results,
    "guardrail-mode":$guardrail_mode,
    "guardrail-policy":$guardrail_policy, "agent-instructions":$agent_instructions,
    "recipient-age-pubkey":$recipient_age_pubkey, "max-runtime-min":$max_runtime_min,
    "scw-access-key":$scw_access_key, "scw-secret-key":$scw_secret_key,
    "scw-project-id":$scw_project_id, "scw-organization-id":$scw_organization_id, "scw-zone":$scw_zone,
    "forgejo-url":$forgejo_url, "forgejo-user":$forgejo_user, "forgejo-token":$forgejo_token,
    "idle-timeout-min":$idle_timeout_min, "tailscale-authkey":$tailscale_authkey}')

setk agent-tar-b64 "${AGENT_B64}"
setk config        "${CONFIG_JSON}"

echo "==> Done. Builder is created and STOPPED."
echo "    Set this for the controller step:  export BUILDER_ID=${SERVER_ID}"