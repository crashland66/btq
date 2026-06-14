#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROJECT_FILE="$ROOT_DIR/BTQFieldCapture.xcodeproj/project.pbxproj"
SIGNING_CONFIG="$ROOT_DIR/Signing.xcconfig"
LOCAL_SIGNING_EXAMPLE="$ROOT_DIR/Local.xcconfig.example"
IOS_INFO="$ROOT_DIR/AppResources/iOS/Info.plist"
MAC_INFO="$ROOT_DIR/AppResources/macOS/Info.plist"
IOS_ENTITLEMENTS="$ROOT_DIR/AppResources/iOS/BTQFieldCapture.entitlements"
MAC_ENTITLEMENTS="$ROOT_DIR/AppResources/BTQFieldCapture.entitlements"
PRIVACY_MANIFEST="$ROOT_DIR/AppResources/PrivacyInfo.xcprivacy"
READINESS_DOC="$ROOT_DIR/Release/AppStoreReadiness.md"
MACOS_VERIFIER="$ROOT_DIR/script/verify_macos_app.sh"
MOCK_API_VERIFIER="$ROOT_DIR/script/verify_mock_api_submit.sh"
MOCK_API_SERVER="$ROOT_DIR/script/mock_capture_api_server.py"
LIVE_API_VERIFIER="$ROOT_DIR/script/verify_live_api.sh"
UNIVERSAL_LINK_VERIFIER="$ROOT_DIR/script/verify_universal_links.sh"
FIELD_PILOT_READINESS="$ROOT_DIR/script/field_pilot_readiness.sh"

fail() {
  printf 'release-readiness: %s\n' "$1" >&2
  exit 1
}

require_file() {
  test -f "$1" || fail "missing $1"
}

require_plist_key() {
  /usr/libexec/PlistBuddy -c "Print :$2" "$1" >/dev/null 2>&1 || fail "missing $2 in $1"
}

require_file "$PROJECT_FILE"
require_file "$SIGNING_CONFIG"
require_file "$LOCAL_SIGNING_EXAMPLE"
require_file "$IOS_INFO"
require_file "$MAC_INFO"
require_file "$IOS_ENTITLEMENTS"
require_file "$MAC_ENTITLEMENTS"
require_file "$PRIVACY_MANIFEST"
require_file "$READINESS_DOC"
require_file "$MACOS_VERIFIER"
require_file "$MOCK_API_VERIFIER"
require_file "$MOCK_API_SERVER"
require_file "$LIVE_API_VERIFIER"
require_file "$UNIVERSAL_LINK_VERIFIER"
require_file "$FIELD_PILOT_READINESS"

plutil -lint "$IOS_INFO" "$MAC_INFO" "$IOS_ENTITLEMENTS" "$MAC_ENTITLEMENTS" "$PRIVACY_MANIFEST" >/dev/null

require_plist_key "$IOS_INFO" NSCameraUsageDescription
require_plist_key "$IOS_INFO" NSMicrophoneUsageDescription
require_plist_key "$IOS_INFO" NSPhotoLibraryUsageDescription
require_plist_key "$IOS_INFO" NSUserNotificationsUsageDescription
require_plist_key "$MAC_INFO" NSCameraUsageDescription
require_plist_key "$MAC_INFO" NSMicrophoneUsageDescription
require_plist_key "$MAC_INFO" NSPhotoLibraryUsageDescription
require_plist_key "$MAC_INFO" NSUserNotificationsUsageDescription
require_plist_key "$PRIVACY_MANIFEST" NSPrivacyTracking
require_plist_key "$PRIVACY_MANIFEST" NSPrivacyTrackingDomains
require_plist_key "$PRIVACY_MANIFEST" NSPrivacyAccessedAPITypes

grep -q 'com.btq.fieldcapture' "$PROJECT_FILE" || fail "missing iOS bundle identifier"
grep -q 'com.btq.fieldcapture.mac' "$PROJECT_FILE" || fail "missing macOS bundle identifier"
grep -q 'SUPPORTED_PLATFORMS = "iphoneos iphonesimulator";' "$PROJECT_FILE" || fail "missing iOS supported platforms"
grep -q 'PRODUCT_BUNDLE_IDENTIFIER = com.btq.fieldcapture.mac;' "$PROJECT_FILE" || fail "missing macOS app bundle identifier"
grep -q 'PrivacyInfo.xcprivacy in Resources' "$PROJECT_FILE" || fail "privacy manifest is not included in the Xcode project"
grep -q 'Signing.xcconfig' "$PROJECT_FILE" || fail "Xcode project must use public-safe Signing.xcconfig"
grep -q '^DEVELOPMENT_TEAM = *$' "$SIGNING_CONFIG" || fail "Signing.xcconfig must keep DEVELOPMENT_TEAM empty"
grep -q '#include? "Local.xcconfig"' "$SIGNING_CONFIG" || fail "Signing.xcconfig must optionally include Local.xcconfig"
grep -q 'DEVELOPMENT_TEAM = <team id>' "$LOCAL_SIGNING_EXAMPLE" || fail "Local.xcconfig.example must use a placeholder team id"
! grep -Eq 'DEVELOPMENT_TEAM = "?[A-Z0-9]+' "$PROJECT_FILE" || fail "project.pbxproj must not contain a concrete DEVELOPMENT_TEAM; use Local.xcconfig or BTQ_DEVELOPMENT_TEAM"
grep -q 'applinks:fc.gregstoltz.com' "$IOS_ENTITLEMENTS" || fail "missing associated domain"
grep -q 'verify_macos_app.sh' "$READINESS_DOC" || fail "release readiness doc does not mention macOS verifier"
grep -q 'verify_mock_api_submit.sh' "$READINESS_DOC" || fail "release readiness doc does not mention mock API verifier"
grep -q 'verify_live_api.sh' "$READINESS_DOC" || fail "release readiness doc does not mention live API verifier"
grep -q 'verify_universal_links.sh' "$READINESS_DOC" || fail "release readiness doc does not mention universal link verifier"
grep -q 'field_pilot_readiness.sh' "$READINESS_DOC" || fail "release readiness doc does not mention field pilot readiness verifier"
! grep -q 'sh script/verify_' "$READINESS_DOC" || fail "release readiness doc should execute Bash verifiers directly"
! grep -q 'sh script/verify_' "$FIELD_PILOT_READINESS" || fail "field pilot readiness should execute Bash verifiers directly"

printf 'release-readiness: checks passed\n'
