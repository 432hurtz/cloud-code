#!/usr/bin/env bash
# Bake a fully-provisioned builder into a bootable image, so future builders start
# in ~1-2 min with the model + deps already on disk (no 34 GB pull, no long first
# boot). Run this ONCE, after a builder has booted, pulled the model, and stopped.
#
#   export BUILDER_ID=<provisioned builder id>
#   bash deploy/snapshot-builder.sh
#
# It prints an image id — put it in .env as BUILDER_IMAGE and future
# create-builder.sh runs boot from it (create-builder.sh omits root-volume
# automatically when BUILDER_IMAGE is an image UUID).
#
# NOTE: this builder's root volume is SBS (Scaleway Block Storage), which lives
# in the newer `scw block` API, not `scw instance snapshot` (the latter errors
# "cannot find resource 'instance_volume'" for an SBS volume id — verified live
# 2026-07-21). `scw instance image create` DOES accept a block snapshot id
# directly as snapshot-id, so no separate "instance snapshot" step is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
export SCW_ACCESS_KEY SCW_SECRET_KEY SCW_DEFAULT_PROJECT_ID
export SCW_DEFAULT_ZONE="${SCW_ZONE}"
export SCW_DEFAULT_ORGANIZATION_ID="${SCW_DEFAULT_ORGANIZATION_ID:-$SCW_DEFAULT_PROJECT_ID}"
BID="${BUILDER_ID:?Set BUILDER_ID to a provisioned builder}"

echo "==> Stopping the builder (snapshotting a live volume risks an inconsistent image)"
scw instance server stop "$BID" zone="$SCW_ZONE" -w 2>/dev/null || true

echo "==> Locating the root (SBS) volume"
VOL=$(scw instance server get "$BID" zone="$SCW_ZONE" -o json | jq -r '.volumes["0"].id')
echo "    root volume: ${VOL}"

echo "==> Snapshotting the volume (scw block, not scw instance — SBS lives there)"
SNAP=$(scw block snapshot create "$VOL" name="code-builder-snap" zone="$SCW_ZONE" -o json | jq -r '.id')
echo "    snapshot: ${SNAP}  (waiting until available…)"
for _ in $(seq 1 90); do
  st=$(scw block snapshot get "$SNAP" zone="$SCW_ZONE" -o json 2>/dev/null | jq -r '.status // "?"')
  [ "$st" = "available" ] && break
  sleep 10
done

echo "==> Creating a bootable image from the snapshot"
IMG=$(scw instance image create snapshot-id="$SNAP" name="code-builder-image" \
      arch="x86_64" zone="$SCW_ZONE" -o json | jq -r '.image.id // .id')

echo "==> Done."
echo "    Set this in .env to boot future builders fast (no model pull):"
echo "        BUILDER_IMAGE=${IMG}"
echo "    Then: bash deploy/create-builder.sh   (boots in ~1-2 min)"
