#!/usr/bin/env bash
# VPS-side install for the read-only admin reporting app (admin.gregstoltz.com).
# Runs from the rsync'd source dir (see push-and-deploy.sh) via push-and-deploy.sh.
set -euo pipefail

# Derive the source dir from this script's own location (no hardcoded home path):
# this file lives at <SOURCE_DIR>/admin_reporting/deploy/deploy.sh.
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE_ROOT="/srv/btq/apps/admin"
ENV_FILE="/etc/gregstoltz/admin.env"
TOKEN_DB="/srv/btq/data/field_capture_tokens.sqlite3"
PORT=8083

cd "${SOURCE_DIR}"

echo "Ensuring btq-admin service user..."
id btq-admin >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin btq-admin
# The app authenticates against the shared field-capture token DB (group btq-field,
# mode 0660 — the store writes last_used_at on auth). Grant the service user access.
sudo usermod -aG btq-field btq-admin

echo "Verifying shared token database..."
if [ ! -f "${TOKEN_DB}" ]; then
	echo "ERROR: token database not found: ${TOKEN_DB}" >&2
	echo "The admin app authenticates against the shared field-capture token store." >&2
	exit 1
fi

# READ-ONLY: verify btq_references is reachable + readable with the reporting creds.
# admin.env is root-owned (secrets); source it + curl inside a single root subshell
# so this deploy (non-root) never holds the CouchDB creds in its own environment.
# Do NOT create btq_references (this app never writes; it is replicated Pro->VPS).
echo "Verifying read access to btq_references via ${ENV_FILE}..."
if ! sudo test -f "${ENV_FILE}"; then
	echo "ERROR: ${ENV_FILE} not found. Create it with the read-only reporting CouchDB creds." >&2
	exit 1
fi
status="$(sudo sh -c '. "'"${ENV_FILE}"'" 2>/dev/null
	url="${BTQ_COUCHDB_URL:-http://127.0.0.1:5984}"; db="${BTQ_REFERENCES_DB:-btq_references}"
	if [ -n "${BTQ_COUCHDB_USER:-}" ]; then
		curl -s -o /dev/null -w "%{http_code}" -u "$BTQ_COUCHDB_USER:$BTQ_COUCHDB_PASSWORD" "$url/$db"
	else
		curl -s -o /dev/null -w "%{http_code}" "$url/$db"
	fi')"
if [ "${status}" != "200" ]; then
	echo "ERROR: cannot read btq_references with the creds in ${ENV_FILE} (HTTP ${status})." >&2
	echo "  Ensure btq_references replicated to the VPS and the reporting user can read it." >&2
	exit 1
fi
echo "  btq_references readable (HTTP ${status})."

echo "Installing curated source to ${SITE_ROOT}/source..."
sudo mkdir -p "${SITE_ROOT}/source"
sudo rsync -a --delete \
	--exclude '__pycache__/' \
	--exclude '*.pyc' \
	--exclude 'admin_reporting/deploy/' \
	--exclude 'admin_reporting/tests/' \
	"${SOURCE_DIR}/" "${SITE_ROOT}/source/"
sudo chown -R btq-admin:btq-admin "${SITE_ROOT}"

echo "Installing systemd unit..."
sudo cp "${SOURCE_DIR}/admin_reporting/deploy/btq-admin.service" /etc/systemd/system/btq-admin.service
sudo systemctl daemon-reload
sudo systemctl enable --now btq-admin.service
sudo systemctl reload-or-restart btq-admin.service

echo "Smoke checking local service..."
ok=0
for attempt in 1 2 3 4 5; do
	if curl -fsS "http://127.0.0.1:${PORT}/api/health" | grep -q '"status"'; then
		ok=1
		break
	fi
	sleep 1
done
if [ "${ok}" != "1" ]; then
	echo "ERROR: local admin reporting health check failed (127.0.0.1:${PORT}/api/health)." >&2
	sudo systemctl --no-pager status btq-admin.service | tail -20 >&2 || true
	exit 1
fi

echo "Admin reporting deploy complete (local 127.0.0.1:${PORT})."
echo "Live admin.gregstoltz.com checks require the DNS A record + the Caddy block"
echo "(gregstoltz.com deploy/sites-enabled/admin.gregstoltz.com.caddy)."
