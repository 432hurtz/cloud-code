#!/usr/bin/env bash
# Clear the Telegram update backlog so the controller won't replay a pile of
# queued messages (each of which cold-boots the GPU builder). The controller
# polls getUpdates with a fresh offset on every restart, so anything Telegram is
# still holding gets processed on its next poll — this drops that backlog.
#
#   bash deploy/flush-telegram.sh
#
# Safe to run anytime, even with the controller running (deleteWebhook doesn't
# conflict with an active getUpdates long-poll). It only discards messages
# Telegram hasn't delivered yet — it can't delete already-processed ones.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN in .env}"
API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

pending() { curl -s -m 15 "${API}/getWebhookInfo" | jq -r '.result.pending_update_count // "?"'; }

echo "==> Pending updates before: $(pending)"
echo "==> Dropping pending updates"
curl -s -m 15 "${API}/deleteWebhook?drop_pending_updates=true" | jq -c '{ok, result, description}'
echo "==> Pending updates after:  $(pending)"
echo "==> Done."
