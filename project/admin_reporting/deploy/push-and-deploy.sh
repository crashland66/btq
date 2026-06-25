#!/usr/bin/env bash
# Push + deploy the read-only admin reporting app to the VPS (admin.gregstoltz.com).
#
# Ships ONLY the curated dependency closure — NO private surfaces (no ops_dashboard,
# btq_vault, queue, etc.). The admin app imports: admin_reporting (local),
# token_store, voice_memo.couchdb, event_pipeline.couchdb_config, instance_config —
# all stdlib at the leaves.
#
#   BTQ_VPS_SSH_TARGET=linuxuser@vps ./project/admin_reporting/deploy/push-and-deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../admin_reporting/deploy
PKG_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"                     # .../admin_reporting
PROJECT="$(cd "${PKG_DIR}/.." && pwd)"                        # .../project

REMOTE="${BTQ_VPS_SSH_TARGET:-deploy@vps.example.com}"
REMOTE_SOURCE="${BTQ_VPS_REMOTE_SOURCE:-/home/deploy/admin-reporting-source}"  # sanitization-ok: generic deploy-host placeholder, real path via BTQ_VPS_REMOTE_SOURCE

echo "Syncing admin_reporting package to ${REMOTE}:${REMOTE_SOURCE}..."
rsync -az --delete \
	--exclude '__pycache__/' \
	--exclude '*.pyc' \
	"${PKG_DIR}/" "${REMOTE}:${REMOTE_SOURCE}/admin_reporting/"

echo "Syncing shared leaf modules (token_store, instance_config, couchdb client)..."
rsync -az "${PROJECT}/token_store.py"   "${REMOTE}:${REMOTE_SOURCE}/token_store.py"
rsync -az "${PROJECT}/instance_config.py" "${REMOTE}:${REMOTE_SOURCE}/instance_config.py"
ssh "${REMOTE}" "mkdir -p '${REMOTE_SOURCE}/voice_memo' '${REMOTE_SOURCE}/event_pipeline'"
rsync -az "${PROJECT}/voice_memo/__init__.py"            "${REMOTE}:${REMOTE_SOURCE}/voice_memo/__init__.py"
rsync -az "${PROJECT}/voice_memo/couchdb.py"             "${REMOTE}:${REMOTE_SOURCE}/voice_memo/couchdb.py"
rsync -az "${PROJECT}/event_pipeline/__init__.py"        "${REMOTE}:${REMOTE_SOURCE}/event_pipeline/__init__.py"
rsync -az "${PROJECT}/event_pipeline/couchdb_config.py"  "${REMOTE}:${REMOTE_SOURCE}/event_pipeline/couchdb_config.py"

echo "Running remote deploy..."
ssh "${REMOTE}" "cd '${REMOTE_SOURCE}' && ./admin_reporting/deploy/deploy.sh"
