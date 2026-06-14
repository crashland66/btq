#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT_FILE="$(mktemp "${TMPDIR:-/tmp}/btq-mock-api-port.XXXXXX")"
SERVER_LOG="$(mktemp "${TMPDIR:-/tmp}/btq-mock-api-log.XXXXXX")"
SERVER_PID=""
TOKEN="btq-smoke-token"

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$PORT_FILE" "$SERVER_LOG"
}
trap cleanup EXIT

/usr/bin/python3 "$ROOT_DIR/script/mock_capture_api_server.py" "$PORT_FILE" >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

for _ in $(seq 1 100); do
  if [[ -s "$PORT_FILE" ]]; then
    break
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "verify-mock-api-submit: mock server exited before writing a port" >&2
    cat "$SERVER_LOG" >&2 || true
    exit 1
  fi
  sleep 0.05
done

if [[ ! -s "$PORT_FILE" ]]; then
  echo "verify-mock-api-submit: mock server did not start" >&2
  cat "$SERVER_LOG" >&2 || true
  exit 1
fi

PORT="$(cat "$PORT_FILE")"
BASE_URL="http://127.0.0.1:$PORT"

echo "verify-mock-api-submit: running Swift client against $BASE_URL"
swift run BTQFieldCaptureAPISmoke "$BASE_URL" "$TOKEN"
echo "verify-mock-api-submit: passed"
