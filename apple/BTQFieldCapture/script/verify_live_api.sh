#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-run}"

usage() {
  cat <<'USAGE'
usage: script/verify_live_api.sh [--check]

Environment:
  BTQ_LIVE_TOKEN       Required for live API verification. Do not commit it.
  BTQ_LIVE_BASE_URL    Optional; defaults to https://fc.gregstoltz.com.
  BTQ_LIVE_SUBMIT=1    Optional; performs a deliberate text-only smoke submit.

Default mode fetches and validates /api/session plus /api/my-submissions.
USAGE
}

case "$MODE" in
  --check)
    swift package describe --type json >/dev/null
    grep -q 'BTQFieldCaptureLiveAPIVerifier' "$ROOT_DIR/Package.swift"
    grep -q 'mySubmissions' "$ROOT_DIR/Sources/BTQFieldCaptureLiveAPIVerifier/main.swift"
    echo "verify-live-api: check passed; set BTQ_LIVE_TOKEN locally to run live session and submitted-history verification"
    ;;
  run)
    if [[ -z "${BTQ_LIVE_TOKEN:-}" ]]; then
      usage >&2
      exit 2
    fi
    swift run BTQFieldCaptureLiveAPIVerifier
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
