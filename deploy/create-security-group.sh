#!/usr/bin/env bash
# One-time: create an egress-locked security group and print its id.
# Default-drop outbound; allow only what the builder/controller actually need:
#   80/443  HTTPS (package mirrors, Telegram, Scaleway API, GitHub, Ollama, Tor-via-443)
#   587     Gmail SMTP submission
#   53      DNS
#   123     NTP (clock sync — TLS breaks on skew)
# Inbound is dropped except SSH (for first-run debugging; tighten ip-range to
# your own IP, or drop the SSH rule once it works).
#
# After running, put the printed id in .env as SECURITY_GROUP_ID, then run
# create-builder.sh / create-controller.sh — they attach it automatically.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"

SG=$(scw instance security-group create name="code-egress" \
  stateful=true inbound-default-policy=drop outbound-default-policy=drop \
  zone="${SCW_ZONE}" -o json | jq -r '.security_group.id // .id')
echo "==> security group: ${SG}"

rule() {  # direction protocol port
  scw instance security-group create-rule security-group-id="${SG}" \
    direction="$1" action=accept protocol="$2" ip-range=0.0.0.0/0 \
    dest-port-from="$3" dest-port-to="$3" zone="${SCW_ZONE}" >/dev/null
}

# Outbound allowlist
rule outbound TCP 80
rule outbound TCP 443
rule outbound TCP 587
rule outbound TCP 53
rule outbound UDP 53
rule outbound UDP 123
# Tailscale (builder → your self-hosted Forgejo). Direct WireGuard + STUN; if these
# are blocked Tailscale still works via DERP relays over 443 (allowed above).
rule outbound UDP 41641
rule outbound UDP 3478
# Inbound: SSH only (scope ip-range in the rule above to your IP for tighter security)
rule inbound  TCP 22

echo "==> Add to .env:  SECURITY_GROUP_ID=${SG}"