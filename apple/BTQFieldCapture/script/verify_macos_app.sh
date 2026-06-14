#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT_DIR/BTQFieldCapture.xcodeproj"
SCHEME="BTQ Capture Mac"
BUNDLE_ID="com.btq.fieldcapture.mac"
PRODUCT_NAME="BTQ Capture Mac"
DERIVED_DATA="$ROOT_DIR/.build/xcode-derived-mac"
APP_BUNDLE="$DERIVED_DATA/Build/Products/Debug/$PRODUCT_NAME.app"
APP_EXECUTABLE="$APP_BUNDLE/Contents/MacOS/$PRODUCT_NAME"
INFO_PLIST="$APP_BUNDLE/Contents/Info.plist"
PRIVACY_MANIFEST="$APP_BUNDLE/Contents/Resources/PrivacyInfo.xcprivacy"
MODE="${1:-build}"

if [[ "$MODE" != "build" && "$MODE" != "--launch" && "$MODE" != "launch" ]]; then
  echo "usage: $0 [--launch]" >&2
  exit 2
fi

echo "verify-macos-app: building scheme '$SCHEME'"
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -destination "platform=macOS" \
  -derivedDataPath "$DERIVED_DATA" \
  build

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "verify-macos-app: expected app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi

if [[ ! -x "$APP_EXECUTABLE" ]]; then
  echo "verify-macos-app: expected executable not found or not executable: $APP_EXECUTABLE" >&2
  exit 1
fi

if [[ ! -f "$INFO_PLIST" ]]; then
  echo "verify-macos-app: expected Info.plist not found: $INFO_PLIST" >&2
  exit 1
fi

if [[ ! -f "$PRIVACY_MANIFEST" ]]; then
  echo "verify-macos-app: expected privacy manifest not found: $PRIVACY_MANIFEST" >&2
  exit 1
fi

plutil -lint "$INFO_PLIST" "$PRIVACY_MANIFEST" >/dev/null

ACTUAL_BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$INFO_PLIST")"
ACTUAL_EXECUTABLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$INFO_PLIST")"
TRACKING="$(/usr/libexec/PlistBuddy -c 'Print :NSPrivacyTracking' "$PRIVACY_MANIFEST")"

if [[ "$ACTUAL_BUNDLE_ID" != "$BUNDLE_ID" ]]; then
  echo "verify-macos-app: expected bundle id '$BUNDLE_ID', found '$ACTUAL_BUNDLE_ID'" >&2
  exit 1
fi

if [[ "$ACTUAL_EXECUTABLE" != "$PRODUCT_NAME" ]]; then
  echo "verify-macos-app: expected executable '$PRODUCT_NAME', found '$ACTUAL_EXECUTABLE'" >&2
  exit 1
fi

if [[ "$TRACKING" != "false" ]]; then
  echo "verify-macos-app: expected privacy manifest NSPrivacyTracking=false, found '$TRACKING'" >&2
  exit 1
fi

if [[ "$MODE" == "--launch" || "$MODE" == "launch" ]]; then
  pkill -x "$PRODUCT_NAME" >/dev/null 2>&1 || true
  echo "verify-macos-app: launching $APP_BUNDLE"
  /usr/bin/open -n "$APP_BUNDLE"
  sleep 1
  if ! pgrep -x "$PRODUCT_NAME" >/dev/null; then
    echo "verify-macos-app: app launch did not leave a running '$PRODUCT_NAME' process" >&2
    exit 1
  fi
  echo "verify-macos-app: launched $BUNDLE_ID"
else
  echo "verify-macos-app: build and bundle metadata checks passed"
fi
