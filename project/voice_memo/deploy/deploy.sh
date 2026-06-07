#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="/home/deploy/voice-memo-source"
SITE_ROOT="/srv/btq/voice"
ENV_FILE="/etc/btq/voice-memo.env"
VOICE_PUBLIC_URL="${VOICE_PUBLIC_URL:-https://voice.example.com}"

cd "${SOURCE_DIR}"

if [ -f "${ENV_FILE}" ]; then
	set -a
	. "${ENV_FILE}"
	set +a
fi

VOICE_COUCHDB_URL="${VOICE_COUCHDB_URL:-http://127.0.0.1:5984}"
VOICE_COUCHDB_DB="${VOICE_COUCHDB_DB:-btq_voice_memos}"
VOICE_COUCHDB_USER="${VOICE_COUCHDB_USER:-}"
VOICE_COUCHDB_PASSWORD="${VOICE_COUCHDB_PASSWORD:-}"
VOICE_MEMO_TOKEN_DB="${VOICE_MEMO_TOKEN_DB:-/srv/btq/data/field_capture_tokens.sqlite3}"

echo "Ensuring CouchDB database is ready..."
curl_auth=()
if [ -n "${VOICE_COUCHDB_USER}" ] && [ -n "${VOICE_COUCHDB_PASSWORD}" ]; then
	curl_auth=(-u "${VOICE_COUCHDB_USER}:${VOICE_COUCHDB_PASSWORD}")
fi
status="$(curl -sS -o /dev/null -w '%{http_code}' "${curl_auth[@]}" "${VOICE_COUCHDB_URL}/${VOICE_COUCHDB_DB}")"
if [ "${status}" = "200" ]; then
	echo "CouchDB database ${VOICE_COUCHDB_DB} ready (status ${status})"
elif [ "${status}" = "404" ]; then
	status="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "${curl_auth[@]}" "${VOICE_COUCHDB_URL}/${VOICE_COUCHDB_DB}")"
	if [ "${status}" = "201" ] || [ "${status}" = "412" ]; then
		echo "CouchDB database ${VOICE_COUCHDB_DB} ready (status ${status})"
	elif [ "${status}" = "401" ] || [ "${status}" = "403" ]; then
		echo "ERROR: CouchDB rejected credentials for ${VOICE_COUCHDB_DB}. Set VOICE_COUCHDB_USER / VOICE_COUCHDB_PASSWORD in ${ENV_FILE}." >&2
		exit 1
	else
		echo "ERROR: Could not ensure CouchDB database ${VOICE_COUCHDB_DB} (HTTP ${status})" >&2
		exit 1
	fi
elif [ "${status}" = "401" ] || [ "${status}" = "403" ]; then
	echo "ERROR: CouchDB rejected credentials for ${VOICE_COUCHDB_DB}. Set VOICE_COUCHDB_USER / VOICE_COUCHDB_PASSWORD in ${ENV_FILE}." >&2
	exit 1
else
	echo "ERROR: Could not ensure CouchDB database ${VOICE_COUCHDB_DB} (HTTP ${status})" >&2
	exit 1
fi

if [ ! -f "${VOICE_MEMO_TOKEN_DB}" ]; then
	echo "ERROR: token database not found: ${VOICE_MEMO_TOKEN_DB}" >&2
	echo "Voice memo authenticates against the shared field-capture token store." >&2
	exit 1
fi

echo "Creating voice memo web root ${SITE_ROOT}..."
sudo mkdir -p "${SITE_ROOT}/source" "${SITE_ROOT}/dist" "${SITE_ROOT}/data"
sudo chown -R btq-voice:btq-voice "${SITE_ROOT}"

echo "Installing voice memo source and public assets..."
rsync -az --delete --exclude='public/' --exclude='deploy/' --exclude='README.md' --exclude='__pycache__/' --exclude='/token_store.py' --exclude='/capture_ingest.py' "${SOURCE_DIR}/" "${SITE_ROOT}/source/voice_memo/"
rsync -a "${SOURCE_DIR}/token_store.py" "${SITE_ROOT}/source/token_store.py"
rsync -a "${SOURCE_DIR}/capture_ingest.py" "${SITE_ROOT}/source/capture_ingest.py"
rsync -a --delete "${SOURCE_DIR}/public/" "${SITE_ROOT}/dist/"

echo "Installing voice memo systemd unit..."
sudo cp "${SOURCE_DIR}/deploy/btq-voice-memo.service" /etc/systemd/system/btq-voice-memo.service
sudo systemctl daemon-reload
sudo systemctl enable --now btq-voice-memo.service
sudo systemctl reload-or-restart btq-voice-memo.service

echo "Smoke checking local service..."
for attempt in 1 2 3 4 5; do
	if curl -fsS http://127.0.0.1:8092/api/health | grep -q '"status": "ok"'; then
		local_health_ok=1
		break
	fi
	sleep 1
done
if [ "${local_health_ok:-0}" != "1" ]; then
	echo "ERROR: local voice memo health check failed." >&2
	exit 1
fi

echo "Smoke checking live PWA..."
if ! curl -fsS ${VOICE_PUBLIC_URL}/ | grep -q 'id="recordVoiceButton"'; then
	echo "ERROR: live voice memo PWA is missing the recorder button." >&2
	exit 1
fi

echo "Smoke checking live API proxy..."
if ! curl -fsS ${VOICE_PUBLIC_URL}/api/health | grep -q '"status": "ok"'; then
	echo "ERROR: live voice memo API health check failed." >&2
	exit 1
fi

echo "Voice memo deploy complete."
