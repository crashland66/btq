#!/usr/bin/env bash
set -euo pipefail

ARCHIVE="${1:-/tmp/btq-admin-portal-public.tar}"
ADMIN_HOST="${BTQ_ADMIN_HOST:-admin.example.com}"
DEST="${BTQ_ADMIN_DEST:-/srv/www/${ADMIN_HOST}}"
CADDYFILE="${BTQ_ADMIN_CADDYFILE:-/etc/caddy/Caddyfile}"
ADMIN_USER="${BTQ_ADMIN_USER:-jordan}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MARKER_BEGIN="# BEGIN BTQ ADMIN PORTAL"
MARKER_END="# END BTQ ADMIN PORTAL"

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Missing portal archive: ${ARCHIVE}" >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script with sudo on the VPS." >&2
  exit 1
fi

command -v caddy >/dev/null
command -v tar >/dev/null
command -v awk >/dev/null

printf "Basic auth user [%s]: " "${ADMIN_USER}" >&2
read -r INPUT_USER
if [[ -n "${INPUT_USER}" ]]; then
  ADMIN_USER="${INPUT_USER}"
fi

printf "Temporary admin password: " >&2
read -rs ADMIN_PASSWORD
printf "\n" >&2
printf "Confirm temporary admin password: " >&2
read -rs ADMIN_PASSWORD_CONFIRM
printf "\n" >&2

if [[ -z "${ADMIN_PASSWORD}" ]]; then
  echo "Password cannot be empty." >&2
  exit 1
fi

if [[ "${ADMIN_PASSWORD}" != "${ADMIN_PASSWORD_CONFIRM}" ]]; then
  echo "Passwords did not match." >&2
  exit 1
fi

ADMIN_HASH="$(caddy hash-password --plaintext "${ADMIN_PASSWORD}")"
unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM

install -d -o root -g root -m 0755 "${DEST}"
find "${DEST}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
tar -C "${DEST}" -xf "${ARCHIVE}"
chown -R root:root "${DEST}"
find "${DEST}" -type d -exec chmod 0755 {} +
find "${DEST}" -type f -exec chmod 0644 {} +

cp -a "${CADDYFILE}" "${CADDYFILE}.before-btq-admin-${STAMP}"
TMP_CADDY="$(mktemp)"

awk -v begin="${MARKER_BEGIN}" -v end="${MARKER_END}" '
  $0 == begin { skip = 1; next }
  $0 == end { skip = 0; next }
  skip != 1 { print }
' "${CADDYFILE}" > "${TMP_CADDY}"

cat >> "${TMP_CADDY}" <<EOF

${MARKER_BEGIN}
${ADMIN_HOST} {
	encode gzip
	root * ${DEST}

	basicauth {
		${ADMIN_USER} ${ADMIN_HASH}
	}

	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "same-origin"
		Permissions-Policy "camera=(), microphone=(), geolocation=()"
		Cache-Control "no-store"
	}

	file_server
}
${MARKER_END}
EOF

caddy validate --config "${TMP_CADDY}" --adapter caddyfile
install -o root -g root -m 0644 "${TMP_CADDY}" "${CADDYFILE}"
rm -f "${TMP_CADDY}"

systemctl reload caddy
systemctl is-active caddy

echo "BTQ admin portal installed."
echo "Destination: ${DEST}"
echo "Caddyfile backup: ${CADDYFILE}.before-btq-admin-${STAMP}"
echo "Protection: Caddy basic auth user ${ADMIN_USER}"
