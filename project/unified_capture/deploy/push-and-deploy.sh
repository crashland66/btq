#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BTQ_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

REMOTE="${BTQ_VPS_SSH_TARGET:-deploy@vps.example.com}" # sanitization-ok: placeholder remote
REMOTE_SOURCE="${UNIFIED_CAPTURE_REMOTE_SOURCE:-/home/deploy/unified-capture-source}" # sanitization-ok: placeholder path

case "${1:-}" in
	--stage-static-only|--python-and-systemd|"")
		deploy_arg="${1:-}"
		;;
	*)
		echo "Usage: $0 [--stage-static-only|--python-and-systemd]" >&2
		exit 2
		;;
esac

cd "${BTQ_ROOT}"

echo "Syncing ${BTQ_ROOT} to ${REMOTE}:${REMOTE_SOURCE}..."
rsync -az --delete \
	--exclude '/.git/' \
	--exclude '/.venv/' \
	--exclude '/apple/' \
	--exclude '/btq_runtime/' \
	--exclude '/runtime/' \
	--exclude '/project/.venv/' \
	--exclude '/project/.runtime/' \
	--exclude '/project/.runtime-dry/' \
	--exclude 'node_modules/' \
	--exclude '__pycache__/' \
	--exclude '*.pyc' \
	"${BTQ_ROOT}/" "${REMOTE}:${REMOTE_SOURCE}/"

echo "Running remote unified-capture deploy..."
if [ -n "${deploy_arg}" ]; then
	ssh "${REMOTE}" "cd '${REMOTE_SOURCE}' && UNIFIED_CAPTURE_SOURCE_DIR='${REMOTE_SOURCE}' ./project/unified_capture/deploy/deploy.sh '${deploy_arg}'"
else
	ssh "${REMOTE}" "cd '${REMOTE_SOURCE}' && UNIFIED_CAPTURE_SOURCE_DIR='${REMOTE_SOURCE}' ./project/unified_capture/deploy/deploy.sh"
fi
