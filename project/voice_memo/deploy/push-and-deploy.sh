#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_PROJECT="$(cd "${PROJECT_DIR}/.." && pwd)"

REMOTE="${BTQ_VPS_SSH_TARGET:-deploy@vps.example.com}"
REMOTE_SOURCE="${BTQ_VPS_REMOTE_SOURCE:-/home/deploy/voice-memo-source}"

cd "${PROJECT_DIR}"

echo "Syncing ${PROJECT_DIR} to ${REMOTE}:${REMOTE_SOURCE}..."
rsync -az --delete \
	--exclude '/tests/' \
	--exclude '/__pycache__/' \
	--exclude '*/__pycache__/' \
	--exclude '*.pyc' \
	--exclude '/token_store.py' \
	--exclude '/capture_ingest.py' \
	"${PROJECT_DIR}/" "${REMOTE}:${REMOTE_SOURCE}/"

# The voice memo server imports the shared, dependency-free token store; ship
# it alongside the package so the VPS source tree stays self-contained.
echo "Syncing token_store.py..."
rsync -az "${REPO_PROJECT}/token_store.py" "${REMOTE}:${REMOTE_SOURCE}/token_store.py"

# Same for the shared, dependency-free capture ingestion module (prompt 168).
echo "Syncing capture_ingest.py..."
rsync -az "${REPO_PROJECT}/capture_ingest.py" "${REMOTE}:${REMOTE_SOURCE}/capture_ingest.py"

echo "Running remote deploy..."
ssh "${REMOTE}" "cd '${REMOTE_SOURCE}' && ./deploy/deploy.sh"
