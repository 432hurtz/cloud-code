#!/usr/bin/env bash
# Push the CURRENT .env settings to the existing builder without recreating it.
# Use this after editing behavior/guardrails/model/idle in .env — the change
# takes effect on the builder's next boot (session_agent reads config at start).
#
#   bash deploy/update-config.sh
#
# Requires: scw + jq on PATH, .env filled in (incl. BUILDER_ID).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"
export SCW_DEFAULT_ORGANIZATION_ID="${SCW_DEFAULT_ORGANIZATION_ID:-$SCW_DEFAULT_PROJECT_ID}"
: "${BUILDER_ID:?Set BUILDER_ID in .env first}"

CONFIG_JSON=$(jq -n \
  --arg model "$MODEL" --arg email_to "$EMAIL_TO" --arg email_from "$EMAIL_FROM" \
  --arg smtp_host "$SMTP_HOST" --arg smtp_port "$SMTP_PORT" --arg smtp_user "$SMTP_USER" \
  --arg smtp_password "${SMTP_PASSWORD:-}" --arg smtp_password_secret_id "${SMTP_PASSWORD_SECRET_ID:-}" \
  --arg telegram_bot_token "${TELEGRAM_BOT_TOKEN:-}" --arg telegram_token_secret_id "${TELEGRAM_TOKEN_SECRET_ID:-}" \
  --arg enable_research "$ENABLE_RESEARCH" --arg research_max_results "$RESEARCH_MAX_RESULTS" \
  --arg guardrail_mode "$GUARDRAIL_MODE" \
  --arg guardrail_policy "$GUARDRAIL_POLICY" --arg agent_instructions "$AGENT_INSTRUCTIONS" \
  --arg recipient_age_pubkey "$RECIPIENT_AGE_PUBKEY" --arg max_runtime_min "$MAX_RUNTIME_MIN" \
  --arg scw_access_key "$SCW_ACCESS_KEY" --arg scw_secret_key "$SCW_SECRET_KEY" \
  --arg scw_project_id "$SCW_DEFAULT_PROJECT_ID" --arg scw_organization_id "$SCW_DEFAULT_ORGANIZATION_ID" --arg scw_zone "$SCW_ZONE" \
  --arg forgejo_url "${FORGEJO_URL:-}" --arg forgejo_user "${FORGEJO_USER:-}" --arg forgejo_token "${FORGEJO_TOKEN:-}" \
  --arg idle_timeout_min "${IDLE_TIMEOUT_MIN:-8}" --arg tailscale_authkey "${TAILSCALE_AUTHKEY:-}" \
  '{model:$model,"email-to":$email_to,"email-from":$email_from,"smtp-host":$smtp_host,"smtp-port":$smtp_port,"smtp-user":$smtp_user,"smtp-password":$smtp_password,"smtp-password-secret-id":$smtp_password_secret_id,"telegram-bot-token":$telegram_bot_token,"telegram-token-secret-id":$telegram_token_secret_id,"enable-research":$enable_research,"research-max-results":$research_max_results,"guardrail-mode":$guardrail_mode,"guardrail-policy":$guardrail_policy,"agent-instructions":$agent_instructions,"recipient-age-pubkey":$recipient_age_pubkey,"max-runtime-min":$max_runtime_min,"scw-access-key":$scw_access_key,"scw-secret-key":$scw_secret_key,"scw-project-id":$scw_project_id,"scw-organization-id":$scw_organization_id,"scw-zone":$scw_zone,"forgejo-url":$forgejo_url,"forgejo-user":$forgejo_user,"forgejo-token":$forgejo_token,"idle-timeout-min":$idle_timeout_min,"tailscale-authkey":$tailscale_authkey}')

echo "==> Pushing config to builder ${BUILDER_ID}"
# Route through a temp file, not a raw arg — avoids "Argument list too long"
# if config grows (hit live for the agent tarball on 2026-07-21).
CONFIG_TMP=$(mktemp)
printf '%s' "$CONFIG_JSON" > "$CONFIG_TMP"
scw instance user-data set server-id="${BUILDER_ID}" key=config content="@${CONFIG_TMP}" zone="${SCW_ZONE}" >/dev/null
rm -f "$CONFIG_TMP"
echo "==> Done. Takes effect on the builder's next boot."
echo "    agent-instructions now: $(printf '%s' "$CONFIG_JSON" | jq -r '.["agent-instructions"]' | head -c 80)…"
