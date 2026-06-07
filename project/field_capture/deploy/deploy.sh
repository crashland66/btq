#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${FIELD_CAPTURE_SOURCE_DIR:-/home/deploy/field-capture-source}"
APP_ROOT="${FIELD_CAPTURE_APP_ROOT:-/srv/btq/apps/field-capture}"
DIST_DIR="${APP_ROOT}/dist"
ENV_FILE="${FIELD_CAPTURE_ENV_FILE:-/etc/btq/field-capture.env}"
SERVICE_NAME="${FIELD_CAPTURE_SERVICE_NAME:-btq-field-capture.service}"

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

FIELD_CAPTURE_COUCHDB_URL="${BTQ_COUCHDB_URL:-${FIELD_CAPTURE_COUCHDB_URL:-http://127.0.0.1:5984}}"
FIELD_CAPTURE_COUCHDB_DB="${BTQ_FIELD_CAPTURES_DB:-${FIELD_CAPTURE_COUCHDB_DB:-btq_field_captures}}"
FIELD_CAPTURE_COUCHDB_USER="${BTQ_COUCHDB_USER:-${FIELD_CAPTURE_COUCHDB_USER:-}}"
FIELD_CAPTURE_COUCHDB_PASSWORD="${BTQ_COUCHDB_PASSWORD:-${FIELD_CAPTURE_COUCHDB_PASSWORD:-}}"

curl_auth=()
if [ -n "${FIELD_CAPTURE_COUCHDB_USER}" ] && [ -n "${FIELD_CAPTURE_COUCHDB_PASSWORD}" ]; then
	curl_auth=(-u "${FIELD_CAPTURE_COUCHDB_USER}:${FIELD_CAPTURE_COUCHDB_PASSWORD}")
fi

ensure_couchdb_database() {
	local db_name="$1"
	local status
	status="$(curl -sS -o /dev/null -w '%{http_code}' "${curl_auth[@]}" "${FIELD_CAPTURE_COUCHDB_URL}/${db_name}")"
	if [ "${status}" = "200" ]; then
		echo "CouchDB database ${db_name} ready (status ${status})"
	elif [ "${status}" = "404" ]; then
		status="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "${curl_auth[@]}" "${FIELD_CAPTURE_COUCHDB_URL}/${db_name}")"
		if [ "${status}" = "201" ] || [ "${status}" = "412" ]; then
			echo "CouchDB database ${db_name} ready (status ${status})"
		elif [ "${status}" = "401" ] || [ "${status}" = "403" ]; then
			echo "ERROR: CouchDB rejected credentials for ${db_name}. Set BTQ_COUCHDB_USER / BTQ_COUCHDB_PASSWORD in ${ENV_FILE}." >&2
			exit 1
		else
			echo "ERROR: Could not ensure CouchDB database ${db_name} (HTTP ${status})" >&2
			exit 1
		fi
	elif [ "${status}" = "401" ] || [ "${status}" = "403" ]; then
		echo "ERROR: CouchDB rejected credentials for ${db_name}. Set BTQ_COUCHDB_USER / BTQ_COUCHDB_PASSWORD in ${ENV_FILE}." >&2
		exit 1
	else
		echo "ERROR: Could not ensure CouchDB database ${db_name} (HTTP ${status})" >&2
		exit 1
	fi
}

if [ "${run_static}" = "1" ]; then
	echo "Staging field-capture static assets in ${DIST_DIR}..."
	sudo mkdir -p "${DIST_DIR}"
	sudo rsync -a --delete "${SOURCE_DIR}/project/field_capture/public/" "${DIST_DIR}/"
fi

if [ "${run_python}" = "1" ]; then
	echo "Ensuring CouchDB database is ready..."
	if [ "${#curl_auth[@]}" -gt 0 ]; then
		ensure_couchdb_database "${FIELD_CAPTURE_COUCHDB_DB}"
	else
		echo "No deploy-time CouchDB credentials found in ${ENV_FILE}; skipping database ensure."
		echo "The systemd service may still receive credentials from an existing drop-in or environment file."
	fi

	echo "Creating field-capture app root ${APP_ROOT}..."
	sudo mkdir -p "${APP_ROOT}"
	sudo install -d -o btq-field -g btq-field -m 0750 /srv/btq/runtime /srv/btq/runtime/uploads /srv/btq/runtime/logs

	echo "Installing field-capture source..."
	sudo rsync -az --delete \
		--exclude '/.git/' \
		--exclude '/.venv/' \
		--exclude '/btq_runtime/' \
		--exclude '/project/.runtime/' \
		--exclude '/project/.runtime-dry/' \
		--exclude '/project/field_capture/public/' \
		--exclude '/project/field_capture/deploy/' \
		--exclude '/dist/' \
		--exclude '__pycache__/' \
		--exclude '*.pyc' \
		"${SOURCE_DIR}/" "${APP_ROOT}/"
	sudo cp "${APP_ROOT}/project/field_capture/config.production.json" "${APP_ROOT}/config.json"

	echo "Installing field-capture systemd unit..."
	sudo cp "${SOURCE_DIR}/project/field_capture/deploy/btq-field-capture.service" "/etc/systemd/system/${SERVICE_NAME}"
	sudo systemctl daemon-reload
	sudo systemctl enable --now "${SERVICE_NAME}"
	sudo systemctl reload-or-restart "${SERVICE_NAME}"

	echo "Smoke checking local API service..."
	for attempt in 1 2 3 4 5; do
		if curl -fsS http://127.0.0.1:8080/api/health | grep -q '"status": "ok"'; then
			local_health_ok=1
			break
		fi
		sleep 1
	done
	if [ "${local_health_ok:-0}" != "1" ]; then
		echo "ERROR: local field-capture API health check failed." >&2
		exit 1
	fi
fi

echo "Field-capture deploy phase complete."
