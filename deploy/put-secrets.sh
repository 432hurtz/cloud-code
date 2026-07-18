#!/usr/bin/env bash
# Optional hardening: move the two real secrets (Gmail app password, Telegram bot
# token) out of instance user-data and into Scaleway Secret Manager. Prints their
# secret IDs — put those in .env, then re-run create-builder.sh / create-controller.sh.
# The builder/controller fetch the values at boot using the Scaleway API key they
# already carry (scope that key to Secret Manager read).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"

put() {  # name value -> prints secret id
  local id
  id=$(scw secret secret create name="$1" -o json 2>/dev/null | jq -r '.id') \
    || id=$(scw secret secret list name="$1" -o json | jq -r '.[0].id')
  scw secret version create "$id" data="$2" >/dev/null
  echo "$id"
}

SMTP_ID=$(put code-smtp-password "${SMTP_PASSWORD}")
TG_ID=$(put code-telegram-token "${TELEGRAM_BOT_TOKEN}")

echo "==> Stored. Add these to .env, then re-run create-builder.sh / create-controller.sh:"
echo "SMTP_PASSWORD_SECRET_ID=${SMTP_ID}"
echo "TELEGRAM_TOKEN_SECRET_ID=${TG_ID}"