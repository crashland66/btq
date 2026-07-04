#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT_DIR/BTQFieldCapture.xcodeproj"
SCHEME="BTQ Capture"
CONFIGURATION="Release"
DERIVED_DATA="$ROOT_DIR/.build/xcode-derived-testflight"
OUTPUT_DIR="$ROOT_DIR/.build/testflight"
ARCHIVE_PATH="$OUTPUT_DIR/BTQ Capture.xcarchive"
EXPORT_PATH="$OUTPUT_DIR/export"
EXPORT_OPTIONS="$OUTPUT_DIR/ExportOptions.plist"
MODE="check"

usage() {
  cat <<'USAGE'
Usage: script/build_testflight.sh [--check|--upload]

  --check   Archive and export a local App Store Connect IPA without uploading.
            This is the default and does not require App Store Connect API keys.
  --upload  Archive and upload to App Store Connect/TestFlight. Uses
            BTQ_ASC_KEY_ID, BTQ_ASC_ISSUER_ID, and BTQ_ASC_KEY_PATH when set,
            otherwise falls back to the Apple account signed into Xcode.

Local values may be supplied by environment variables or by untracked files:
  script/ios_device.env       BTQ_DEVELOPMENT_TEAM
  script/testflight.env       BTQ_DEVELOPMENT_TEAM, API key values, build number
                              Set BTQ_TESTFLIGHT_INTERNAL_ONLY=YES only for
                              builds that should never go to external testers.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      MODE="check"
      shift
      ;;
    --upload)
      MODE="upload"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "build-testflight: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

source "$ROOT_DIR/script/load_ios_device_env.sh"
btq_load_ios_device_env

btq_load_env_value() {
  local file="$1"
  local name="$2"
  local current="${!name:-}"
  local line
  local value

  if [[ -n "$current" || ! -f "$file" ]]; then
    return
  fi

  line="$(grep -E "^${name}=" "$file" | tail -1 || true)"
  if [[ -z "$line" ]]; then
    return
  fi

  value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"

  if [[ -n "$value" ]]; then
    export "$name=$value"
  fi
}

TESTFLIGHT_ENV_FILE="${BTQ_TESTFLIGHT_ENV_FILE:-$ROOT_DIR/script/testflight.env}"
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_DEVELOPMENT_TEAM
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_ASC_KEY_ID
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_ASC_ISSUER_ID
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_ASC_KEY_PATH
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_BUILD_NUMBER
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_MARKETING_VERSION
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_TESTFLIGHT_INTERNAL_ONLY
btq_load_env_value "$TESTFLIGHT_ENV_FILE" BTQ_UPLOAD_SYMBOLS

TEAM_ID="${BTQ_DEVELOPMENT_TEAM:-${DEVELOPMENT_TEAM:-}}"
BUILD_NUMBER="${BTQ_BUILD_NUMBER:-$(date -u +%Y%m%d%H%M)}"
MARKETING_VERSION="${BTQ_MARKETING_VERSION:-1.3}"
UPLOAD_SYMBOLS="${BTQ_UPLOAD_SYMBOLS:-NO}"
INTERNAL_ONLY="${BTQ_TESTFLIGHT_INTERNAL_ONLY:-NO}"

if [[ -z "$TEAM_ID" ]]; then
  echo "build-testflight: set BTQ_DEVELOPMENT_TEAM or copy script/testflight.env.example to script/testflight.env." >&2
  exit 1
fi

if [[ "$MODE" == "upload" && ( -n "${BTQ_ASC_KEY_ID:-}" || -n "${BTQ_ASC_ISSUER_ID:-}" || -n "${BTQ_ASC_KEY_PATH:-}" ) ]]; then
  if [[ -z "${BTQ_ASC_KEY_ID:-}" || -z "${BTQ_ASC_ISSUER_ID:-}" || -z "${BTQ_ASC_KEY_PATH:-}" ]]; then
    echo "build-testflight: set all App Store Connect API key values, or unset all of them to use the Apple account signed into Xcode." >&2
    exit 1
  fi
  if [[ ! -f "$BTQ_ASC_KEY_PATH" && -f "$ROOT_DIR/$BTQ_ASC_KEY_PATH" ]]; then
    BTQ_ASC_KEY_PATH="$ROOT_DIR/$BTQ_ASC_KEY_PATH"
  fi
  if [[ ! -f "$BTQ_ASC_KEY_PATH" ]]; then
    echo "build-testflight: App Store Connect API key file not found: $BTQ_ASC_KEY_PATH" >&2
    exit 1
  fi
elif [[ "$MODE" == "upload" ]]; then
  echo "build-testflight: no App Store Connect API key configured; using the Apple account signed into Xcode."
fi

plist_bool() {
  case "${1:-}" in
    1|YES|yes|TRUE|true) echo "true" ;;
    *) echo "false" ;;
  esac
}

write_export_options() {
  local destination="$1"
  local upload_symbols
  local internal_only

  upload_symbols="$(plist_bool "$UPLOAD_SYMBOLS")"
  internal_only="$(plist_bool "$INTERNAL_ONLY")"

  mkdir -p "$OUTPUT_DIR"
  cat > "$EXPORT_OPTIONS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>
    <string>app-store-connect</string>
    <key>destination</key>
    <string>$destination</string>
    <key>signingStyle</key>
    <string>automatic</string>
    <key>teamID</key>
    <string>$TEAM_ID</string>
    <key>uploadSymbols</key>
    <$upload_symbols/>
    <key>manageAppVersionAndBuildNumber</key>
    <false/>
    <key>testFlightInternalTestingOnly</key>
    <$internal_only/>
</dict>
</plist>
PLIST
}

AUTH_ARGS=()
if [[ -n "${BTQ_ASC_KEY_ID:-}" && -n "${BTQ_ASC_ISSUER_ID:-}" && -n "${BTQ_ASC_KEY_PATH:-}" ]]; then
  if [[ ! -f "$BTQ_ASC_KEY_PATH" && -f "$ROOT_DIR/$BTQ_ASC_KEY_PATH" ]]; then
    BTQ_ASC_KEY_PATH="$ROOT_DIR/$BTQ_ASC_KEY_PATH"
  fi
  if [[ -f "$BTQ_ASC_KEY_PATH" ]]; then
    AUTH_ARGS=(
      -authenticationKeyPath "$BTQ_ASC_KEY_PATH"
      -authenticationKeyID "$BTQ_ASC_KEY_ID"
      -authenticationKeyIssuerID "$BTQ_ASC_ISSUER_ID"
    )
  fi
fi

rm -rf "$ARCHIVE_PATH" "$EXPORT_PATH"
mkdir -p "$OUTPUT_DIR"

echo "build-testflight: archiving $SCHEME $MARKETING_VERSION ($BUILD_NUMBER)"
ARCHIVE_CMD=(
  xcodebuild
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration "$CONFIGURATION" \
  -destination "generic/platform=iOS" \
  -archivePath "$ARCHIVE_PATH" \
  -derivedDataPath "$DERIVED_DATA" \
  -allowProvisioningUpdates
)
if [[ ${#AUTH_ARGS[@]} -gt 0 ]]; then
  ARCHIVE_CMD+=("${AUTH_ARGS[@]}")
fi
ARCHIVE_CMD+=(
  DEVELOPMENT_TEAM="$TEAM_ID" \
  MARKETING_VERSION="$MARKETING_VERSION" \
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  archive
)
"${ARCHIVE_CMD[@]}"

if [[ "$MODE" == "upload" ]]; then
  write_export_options "upload"
  echo "build-testflight: uploading archive to App Store Connect"
else
  write_export_options "export"
  echo "build-testflight: exporting local App Store Connect IPA"
fi

EXPORT_CMD=(
  xcodebuild
  -exportArchive \
  -archivePath "$ARCHIVE_PATH" \
  -exportPath "$EXPORT_PATH" \
  -exportOptionsPlist "$EXPORT_OPTIONS" \
  -allowProvisioningUpdates
)
if [[ ${#AUTH_ARGS[@]} -gt 0 ]]; then
  EXPORT_CMD+=("${AUTH_ARGS[@]}")
fi
"${EXPORT_CMD[@]}"

if [[ "$MODE" == "upload" ]]; then
  echo "build-testflight: upload requested; check App Store Connect processing status for build $BUILD_NUMBER."
else
  echo "build-testflight: check passed. Archive: $ARCHIVE_PATH"
  echo "build-testflight: export: $EXPORT_PATH"
fi
