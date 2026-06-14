#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKIPPED=()

run() {
  echo "field-pilot-readiness: running $*"
  "$@"
}

run_with_env() {
  echo "field-pilot-readiness: running $*"
  env "$@"
}

skip() {
  SKIPPED+=("$1")
  echo "field-pilot-readiness: skipped $1"
}

cd "$ROOT_DIR"

source "$ROOT_DIR/script/load_ios_device_env.sh"
btq_load_ios_device_env

run swift test
run sh script/check_release_readiness.sh
run ./script/verify_live_api.sh --check
run ./script/verify_universal_links.sh --check
run ./script/verify_mock_api_submit.sh
run ./script/verify_macos_app.sh
run ./script/verify_ios_simulator.sh
run_with_env BTQ_SIMULATOR_FAMILY=iPad ./script/verify_ios_simulator.sh

if [[ -n "${BTQ_LIVE_TOKEN:-}" ]]; then
  run ./script/verify_live_api.sh
else
  skip "live backend token run (set BTQ_LIVE_TOKEN)"
fi

if [[ -n "${BTQ_DEVELOPMENT_TEAM:-${DEVELOPMENT_TEAM:-}}" ]] &&
   [[ -n "${BTQ_DEVICE_NAME:-${BTQ_XCODE_DESTINATION:-}}" ]]; then
  run ./script/verify_ios_device.sh
else
  skip "physical iPhone install/launch (set BTQ_DEVELOPMENT_TEAM and BTQ_DEVICE_NAME or BTQ_XCODE_DESTINATION, or fill script/ios_device.env)"
fi

if ((${#SKIPPED[@]} > 0)); then
  echo "field-pilot-readiness: required public-safe gates passed; skipped optional gates:"
  for item in "${SKIPPED[@]}"; do
    echo "field-pilot-readiness: - $item"
  done
else
  echo "field-pilot-readiness: all field-pilot gates passed"
fi
