#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="$ROOT_DIR/BTQFieldCapture.xcodeproj"
SCHEME="BTQ Capture"
BUNDLE_ID="com.btq.fieldcapture"
SIMULATOR_FAMILY="${BTQ_SIMULATOR_FAMILY:-iPhone}"
SIMULATOR_NAME_FILTER="${BTQ_SIMULATOR_NAME:-}"
DERIVED_DATA="$ROOT_DIR/.build/xcode-derived-ios-sim-${SIMULATOR_FAMILY// /-}"
APP_BUNDLE="$DERIVED_DATA/Build/Products/Debug-iphonesimulator/BTQ Capture.app"

pick_simulator() {
  xcrun simctl list devices available -j | /usr/bin/python3 -c '
import json
import sys

data = json.load(sys.stdin)
family = sys.argv[1]
name_filter = sys.argv[2]
devices = []
for runtime_devices in data.get("devices", {}).values():
    for device in runtime_devices:
        name = device.get("name", "")
        if not device.get("isAvailable"):
            continue
        if name_filter:
            if name == name_filter:
                devices.append(device)
        elif family in name:
            devices.append(device)

if not devices:
    raise SystemExit(1)

devices.sort(key=lambda device: (device.get("state") != "Booted", device.get("name", "")))
device = devices[0]
print("{}\t{}\t{}".format(device["udid"], device["name"], device["state"]))
' "$SIMULATOR_FAMILY" "$SIMULATOR_NAME_FILTER"
}

if [[ -n "${SIMULATOR_UDID:-}" ]]; then
  SELECTED="$SIMULATOR_UDID	${SIMULATOR_NAME:-selected simulator}	unknown"
else
  if ! SELECTED="$(pick_simulator)"; then
    echo "verify-ios-simulator: no available simulator found for family '$SIMULATOR_FAMILY'." >&2
    echo "verify-ios-simulator: set BTQ_SIMULATOR_FAMILY, BTQ_SIMULATOR_NAME, or SIMULATOR_UDID to choose a device." >&2
    echo "verify-ios-simulator: finish installing an iOS simulator runtime in Xcode, then rerun this script." >&2
    exit 1
  fi
fi

IFS=$'\t' read -r UDID NAME STATE <<<"$SELECTED"

echo "verify-ios-simulator: using $NAME ($UDID)"
if [[ "$STATE" != "Booted" ]]; then
  xcrun simctl boot "$UDID" >/dev/null 2>&1 || true
  xcrun simctl bootstatus "$UDID" -b
fi

xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -destination "id=$UDID" \
  -derivedDataPath "$DERIVED_DATA" \
  CODE_SIGNING_ALLOWED=NO \
  build

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "verify-ios-simulator: expected app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi

xcrun simctl install "$UDID" "$APP_BUNDLE"
xcrun simctl launch "$UDID" "$BUNDLE_ID"

echo "verify-ios-simulator: launched $BUNDLE_ID on $NAME"
