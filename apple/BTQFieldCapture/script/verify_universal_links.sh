#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BTQ_ROOT="$(cd "$ROOT_DIR/../.." && pwd)"
MODE="${1:---check}"
ENTITLEMENTS="$ROOT_DIR/AppResources/iOS/BTQFieldCapture.entitlements"
SERVER="$BTQ_ROOT/project/unified_capture/server.py"

usage() {
  cat <<'USAGE'
usage: script/verify_universal_links.sh [--check|--live]

Modes:
  --check  Validate local associated-domain entitlement and backend AASA route.
  --live   Fetch and validate the deployed AASA file from BTQ_AASA_BASE_URL.

Environment:
  BTQ_AASA_BASE_URL    Required for --live; for example https://fc.gregstoltz.com.
USAGE
}

case "$MODE" in
  --check)
    grep -q 'applinks:fc.gregstoltz.com' "$ENTITLEMENTS"
    grep -q 'apple_app_site_association_payload' "$SERVER"
    grep -q '/.well-known/apple-app-site-association' "$SERVER"
    grep -q '/apple-app-site-association' "$SERVER"
    echo "verify-universal-links: check passed; configure BTQ_APPLE_TEAM_ID on the deployed server and run --live"
    ;;
  --live)
    if [[ -z "${BTQ_AASA_BASE_URL:-}" ]]; then
      usage >&2
      exit 2
    fi
    /usr/bin/python3 - "$BTQ_AASA_BASE_URL" <<'PY'
import json
import sys
import urllib.request
from urllib.parse import urljoin

base_url = sys.argv[1].rstrip("/") + "/"
url = urljoin(base_url, ".well-known/apple-app-site-association")
request = urllib.request.Request(url, headers={"Accept": "application/json"})
with urllib.request.urlopen(request, timeout=10) as response:
    content_type = response.headers.get("Content-Type", "")
    body = response.read()
if "application/json" not in content_type:
    raise SystemExit(f"unexpected Content-Type for {url}: {content_type}")
payload = json.loads(body.decode("utf-8"))
details = payload.get("applinks", {}).get("details", [])
if not details:
    raise SystemExit("AASA payload has no applinks.details")
app_ids = [str(detail.get("appID", "")) for detail in details]
if not any(app_id.endswith(".com.btq.fieldcapture") for app_id in app_ids):
    raise SystemExit(f"AASA payload does not include com.btq.fieldcapture appID: {app_ids}")
print(f"verify-universal-links: live AASA OK at {url}")
PY
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
