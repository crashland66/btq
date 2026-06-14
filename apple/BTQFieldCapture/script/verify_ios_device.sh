#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT_DIR/BTQFieldCapture.xcodeproj"
SCHEME="BTQ Capture"
BUNDLE_ID="com.btq.fieldcapture"
DERIVED_DATA="$ROOT_DIR/.build/xcode-derived-ios-device"
APP_BUNDLE="$DERIVED_DATA/Build/Products/Debug-iphoneos/BTQ Capture.app"

source "$ROOT_DIR/script/load_ios_device_env.sh"
btq_load_ios_device_env

TEAM_ID="${BTQ_DEVELOPMENT_TEAM:-${DEVELOPMENT_TEAM:-}}"
DEVICE_NAME="${BTQ_DEVICE_NAME:-${DEVICE_NAME:-}}"
DEVICE_SELECTOR="${BTQ_DEVICE_SELECTOR:-${DEVICE_NAME:-}}"
XCODE_DESTINATION="${BTQ_XCODE_DESTINATION:-}"

if [[ -z "$TEAM_ID" ]]; then
  echo "verify-ios-device: set BTQ_DEVELOPMENT_TEAM to your Apple Developer Team ID." >&2
  echo "verify-ios-device: or copy script/ios_device.env.example to script/ios_device.env and fill in local values." >&2
  echo "verify-ios-device: the public project intentionally keeps DEVELOPMENT_TEAM empty." >&2
  exit 1
fi

if [[ -z "$XCODE_DESTINATION" ]]; then
  if [[ -z "$DEVICE_NAME" ]]; then
    echo "verify-ios-device: set BTQ_DEVICE_NAME or BTQ_XCODE_DESTINATION." >&2
    echo "verify-ios-device: local defaults may be stored in untracked script/ios_device.env." >&2
    echo "verify-ios-device: available devices can be listed with: xcrun devicectl list devices" >&2
    exit 1
  fi
  XCODE_DESTINATION="platform=iOS,name=$DEVICE_NAME"
fi

if [[ -z "$DEVICE_SELECTOR" ]]; then
  DEVICE_SELECTOR="$DEVICE_NAME"
fi

DEVICE_DETAILS="$(xcrun devicectl device info details --device "$DEVICE_SELECTOR" 2>&1 || true)"
if echo "$DEVICE_DETAILS" | grep -q "developerModeStatus: disabled"; then
  echo "verify-ios-device: Developer Mode is disabled on '$DEVICE_SELECTOR'." >&2
  echo "verify-ios-device: enable it on the iPhone in Settings > Privacy & Security > Developer Mode, restart/confirm when prompted, then rerun." >&2
  exit 1
fi
if echo "$DEVICE_DETAILS" | grep -q "Developer Mode is disabled"; then
  echo "verify-ios-device: Developer Mode is disabled on '$DEVICE_SELECTOR'." >&2
  echo "verify-ios-device: enable it on the iPhone in Settings > Privacy & Security > Developer Mode, restart/confirm when prompted, then rerun." >&2
  exit 1
fi

echo "verify-ios-device: building for destination '$XCODE_DESTINATION'"
if ! xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -destination "$XCODE_DESTINATION" \
  -derivedDataPath "$DERIVED_DATA" \
  -allowProvisioningUpdates \
  DEVELOPMENT_TEAM="$TEAM_ID" \
  build; then
  echo "verify-ios-device: device build failed." >&2
  echo "verify-ios-device: if Xcode reports Developer Mode disabled, enable it on the iPhone in Settings > Privacy & Security > Developer Mode, then reconnect and rerun." >&2
  exit 1
fi

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "verify-ios-device: expected app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi

echo "verify-ios-device: installing on '$DEVICE_SELECTOR'"
xcrun devicectl device install app \
  --device "$DEVICE_SELECTOR" \
  "$APP_BUNDLE"

echo "verify-ios-device: launching $BUNDLE_ID on '$DEVICE_SELECTOR'"
xcrun devicectl device process launch \
  --device "$DEVICE_SELECTOR" \
  --terminate-existing \
  "$BUNDLE_ID"

echo "verify-ios-device: launched $BUNDLE_ID on '$DEVICE_SELECTOR'"
