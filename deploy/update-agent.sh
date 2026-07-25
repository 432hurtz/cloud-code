#!/usr/bin/env bash
# Ship the CURRENT local agent/ code to the existing builder's user-data, WITHOUT
# recreating the instance. This only updates the `agent-tar-b64` key; it does NOT
# re-extract onto an already-provisioned builder by itself (cloud-init's unpack
# step is first-boot-only). After running this you must boot the builder and
# re-extract onto /opt/agent — see the printed next-steps, or deploy/redeploy notes.
#
#   bash deploy/update-agent.sh
#
# Requires: scw + jq on PATH, .env filled in (incl. BUILDER_ID).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"
export SCW_DEFAULT_ORGANIZATION_ID="${SCW_DEFAULT_ORGANIZATION_ID:-$SCW_DEFAULT_PROJECT_ID}"
: "${BUILDER_ID:?Set BUILDER_ID in .env first}"

echo "==> Compiling agent locally before shipping"
python -m py_compile agent/session_agent.py

echo "==> Packing local agent/ into a base64 tarball"
AGENT_B64=$(tar czf - agent | base64 -w0)

# Route through a temp file (content=@file), not a raw arg — the agent tarball
# exceeds the shell's max argument length ("Argument list too long", hit live).
TMP=$(mktemp)
printf '%s' "$AGENT_B64" > "$TMP"
echo "==> Pushing agent-tar-b64 to builder ${BUILDER_ID}"
scw instance user-data set server-id="${BUILDER_ID}" key=agent-tar-b64 content="@${TMP}" zone="${SCW_ZONE}" >/dev/null
rm -f "$TMP"

echo "==> Done. New agent code is staged in the builder's user-data."
echo "    It is NOT live yet — boot the builder, SIGKILL code-builder.service,"
echo "    then re-extract onto /opt/agent (see redeploy steps)."
