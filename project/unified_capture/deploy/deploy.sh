#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${UNIFIED_CAPTURE_SOURCE_DIR:-/home/deploy/unified-capture-source}" # sanitization-ok: public-safe deploy-host placeholder; operator overrides UNIFIED_CAPTURE_SOURCE_DIR
APP_ROOT="${UNIFIED_CAPTURE_APP_ROOT:-/srv/btq/apps/unified-capture}"
DIST_DIR="${APP_ROOT}/dist"
ENV_FILE="${UNIFIED_CAPTURE_ENV_FILE:-/etc/btq/unified-capture.env}"
# Runtime CouchDB secrets file the systemd unit loads (BTQ_COUCHDB_USER/PASSWORD).
# Distinct from ENV_FILE: runtime secrets stay in the /etc/gregstoltz drop-in.
RUNTIME_SECRETS_ENV_FILE="${UNIFIED_CAPTURE_RUNTIME_SECRETS_ENV_FILE:-/etc/gregstoltz/unified-capture.env}"
SERVICE_NAME="${UNIFIED_CAPTURE_SERVICE_NAME:-btq-unified-capture.service}"

run_static=1
run_python=1
case "${1:-}" in
	--stage-static-only)
		run_python=0
		;;
	--python-and-systemd)
		run_static=0
		;;
	"")
		;;
	*)
		echo "Usage: $0 [--stage-static-only|--python-and-systemd]" >&2
		exit 2
		;;
esac

cd "${SOURCE_DIR}"

if [ -f "${ENV_FILE}" ]; then
	set -a
	. "${ENV_FILE}"
	set +a
fi

if [ "${run_static}" = "1" ]; then
	echo "Staging unified-capture static assets in ${DIST_DIR}..."
	sudo mkdir -p "${DIST_DIR}"
	sudo rsync -a --delete "${SOURCE_DIR}/project/unified_capture/public/" "${DIST_DIR}/"
	# db.js and sw.js are served dynamically by server.py, so they are NOT in
	# public/. Caddy serves the SPA from dist/, so they must be materialized here.
	# The unified shell loads /static/db.js; keep root db.js too for parity with
	# the field-capture deploy pattern.
	echo "Materializing unified-capture db.js + sw.js into ${DIST_DIR}..."
	sudo env PYTHONPATH="${SOURCE_DIR}/project" python3 - "${DIST_DIR}" <<'PY'
import sys
from pathlib import Path
from shared_pwa.assets import render_service_worker, shared_db_bytes
dist = Path(sys.argv[1])
dist.mkdir(parents=True, exist_ok=True)
db_bytes = shared_db_bytes()
(dist / "db.js").write_bytes(db_bytes)
(dist / "static").mkdir(parents=True, exist_ok=True)
(dist / "static" / "db.js").write_bytes(db_bytes)
(dist / "sw.js").write_text(render_service_worker("unified_capture"), encoding="utf-8")
print(f"materialized db.js + static/db.js + sw.js into {dist}")
PY
fi

if [ "${run_python}" = "1" ]; then
	echo "Creating unified-capture app root ${APP_ROOT}..."
	sudo mkdir -p "${APP_ROOT}"
	sudo install -d -o btq-field -g btq-field -m 0750 /srv/btq/runtime /srv/btq/runtime/uploads /srv/btq/runtime/logs

	echo "Installing unified-capture source..."
	sudo rsync -az --delete \
		--exclude '/.git/' \
		--exclude '/.venv/' \
		--exclude '/btq_runtime/' \
		--exclude '/project/.runtime/' \
		--exclude '/project/.runtime-dry/' \
		--exclude '/project/unified_capture/public/' \
		--exclude '/project/unified_capture/deploy/' \
		--exclude '/dist/' \
		--exclude '/config.json' \
		--exclude '__pycache__/' \
		--exclude '*.pyc' \
		"${SOURCE_DIR}/" "${APP_ROOT}/"
	sudo chown -R linuxuser:linuxuser "${APP_ROOT}"

	if [ ! -f "${APP_ROOT}/config.json" ]; then
		if [ -f "${APP_ROOT}/project/unified_capture/config.production.json" ]; then
			sudo cp "${APP_ROOT}/project/unified_capture/config.production.json" "${APP_ROOT}/config.json"
			sudo chown linuxuser:linuxuser "${APP_ROOT}/config.json"
		else
			echo "ERROR: ${APP_ROOT}/config.json is missing and unified_capture has no config.production.json template." >&2
			echo "Create the machine-local config.json before restarting ${SERVICE_NAME}." >&2
			exit 1
		fi
	else
		echo "Keeping existing unified-capture config.json."
	fi

	echo "Installing unified-capture systemd unit..."
	sudo cp "${SOURCE_DIR}/project/unified_capture/deploy/btq-unified-capture.service" "/etc/systemd/system/${SERVICE_NAME}"
	sudo systemctl daemon-reload
	sudo systemctl enable --now "${SERVICE_NAME}"
	sudo systemctl reload-or-restart "${SERVICE_NAME}"

	echo "Smoke checking local unified-capture API auth gate..."
	session_code=""
	for attempt in 1 2 3 4 5; do
		session_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Accept: application/json" http://127.0.0.1:8081/api/session || true)"
		if [ "${session_code}" = "401" ]; then
			local_session_gate_ok=1
			break
		fi
		sleep 1
	done
	if [ "${local_session_gate_ok:-0}" != "1" ]; then
		echo "ERROR: local unified-capture /api/session gate failed; expected HTTP 401, got HTTP ${session_code:-000}." >&2
		sudo systemctl --no-pager -l status "${SERVICE_NAME}" >&2 || true
		exit 1
	fi
	echo "Unified-capture auth gate OK (/api/session -> 401 without token)."

	if ! sudo systemctl cat "${SERVICE_NAME}" 2>/dev/null | grep -Fq -- "${RUNTIME_SECRETS_ENV_FILE}"; then
		echo "WARNING: ${SERVICE_NAME} does not reference ${RUNTIME_SECRETS_ENV_FILE}; CouchDB auth may rely on another env source." >&2
	fi
fi

echo "Unified-capture deploy phase complete."
