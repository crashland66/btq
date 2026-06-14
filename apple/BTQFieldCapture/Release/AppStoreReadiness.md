# BTQ Capture App Store Readiness

This file tracks public-safe release setup for the native Apple BTQ field
capture client. Do not commit Apple account secrets, API keys, issuer IDs,
private keys, provisioning profiles, or a personal Team ID unless the project
explicitly decides to make that public.

## Bundle Identifiers

- iOS/iPadOS: `com.btq.fieldcapture`
- macOS: `com.btq.fieldcapture.mac`

## Capabilities

- Associated domains: `applinks:fc.gregstoltz.com`
- Camera access for field photos
- Microphone access for voice notes
- Photo library selection for existing site photos
- Local notifications for sync and upload failure alerts
- Background modes on iOS: audio, fetch, processing
- macOS sandbox with network client, camera, audio input, and user-selected
  read-only files

## Voice Recording Policy

V1 treats voice capture as a foreground-started, background-continuing field
memo workflow. A recording that has already started should continue while the
app is backgrounded or the iPhone is locked, using the iOS audio background
mode and active `playAndRecord` audio session. The app should pause only for
real audio-session interruptions such as phone calls or Siri, then resume only
when iOS says resumption is allowed and the user had not manually paused.

Physical-device validation must include recording, locking/backgrounding the
iPhone, returning to the app, and confirming the saved memo duration/playback.

## App Store Connect Privacy Labels

Expected labels for V1, assuming no third-party analytics or tracking are added:

- Tracking: No
- Data used for tracking: None
- Photos or videos: collected for app functionality
- Audio data: collected for app functionality
- Other user content: field notes and observations, collected for app
  functionality
- User ID: token/session/person identifiers, collected for app functionality
- Diagnostics: none unless crash or analytics tooling is added later

Review this list against the final App Store Connect questionnaire before
submission.

## Local Signing Setup

1. Install the iOS platform in Xcode if it is missing.
2. Sign into the Apple Developer account in Xcode.
3. Copy `Local.xcconfig.example` to `Local.xcconfig` and set your local
   `DEVELOPMENT_TEAM` there. Do not store a personal Team ID by editing the
   Xcode project's Signing & Capabilities pane.
4. On physical iPhones used for testing, enable Developer Mode in
   Settings > Privacy & Security > Developer Mode, then restart/confirm when
   prompted.
5. Let Xcode create or refresh provisioning profiles for:
   - `com.btq.fieldcapture`
   - `com.btq.fieldcapture.mac`
6. Keep automatic signing enabled unless release automation is introduced.
7. Configure the deployed unified capture server with `BTQ_APPLE_TEAM_ID` or
   `BTQ_APPLE_APP_ID` so `fc.gregstoltz.com` can serve the Apple App Site
   Association payload for universal-link onboarding.

## Before TestFlight

### Physical Device Validation Log

- 2026-06-14: Live image capture submitted from the native iPhone app and
  appeared in the BTQ dashboard with vision context. This verifies the real
  device photo capture, upload path, backend ingestion, and dashboard/vision
  handoff for a test capture.
- 2026-06-14: Live audio recording submitted from the native iPhone app and
  appeared in the admin dashboard. This verifies the real device voice memo
  capture, upload path, backend ingestion, and dashboard handoff for a test
  recording.
- 2026-06-14: Settings test alert displayed successfully on the iPhone. This
  verifies local notification permission/presentation for the app-level alert
  path.
- 2026-06-14: Heavy Photos picker capture succeeded on the iPhone: selected 6
  photos, recorded audio, typed an observation, and submitted without the app
  closing. This verifies the Photos picker import path and combined
  photo/audio/text submission path for the current pilot limit.

### Remaining Physical Device Checks

- Voice pause/resume and playback.
- Lock/background voice recording continuation.
- Upload-failure alert behavior.
- Poor-connectivity capture with automatic sync recovery.
- Background sync behavior after app backgrounding.

- Run the aggregate field-pilot readiness script:

```sh
./script/field_pilot_readiness.sh
```

- Build and run on a real iPhone.
- Verify tokenized onboarding link handling from Safari/Mail/Messages.
- Verify camera capture, Photos picker import, voice recording, pause/resume,
  lock/background continuation, phone-call/Siri interruption behavior, and
  playback.
- Verify poor-connectivity capture and automatic sync recovery.
- Verify local notification permission request and upload failure alerts.
- Verify associated-domain universal link configuration on
  `fc.gregstoltz.com`.

```sh
BTQ_AASA_BASE_URL="https://fc.gregstoltz.com" ./script/verify_universal_links.sh --live
```

- Re-run the release readiness script:

```sh
sh script/check_release_readiness.sh
```

- Verify the macOS app target:

```sh
./script/verify_macos_app.sh
```

- Verify the native API client against the local mock capture API:

```sh
./script/verify_mock_api_submit.sh
```

- Verify real deployment read-only routes with a local token. This checks
  `/api/session` and `/api/my-submissions` without mutating backend state:

```sh
BTQ_LIVE_TOKEN="<token>" ./script/verify_live_api.sh
```

- Optionally run a deliberate text-only live submit smoke:

```sh
BTQ_LIVE_TOKEN="<token>" BTQ_LIVE_SUBMIT=1 ./script/verify_live_api.sh
```

- Run the public-safe real-device verifier with local signing values:

```sh
BTQ_DEVELOPMENT_TEAM="<team id>" BTQ_DEVICE_NAME="<device name>" ./script/verify_ios_device.sh
```

For repeated local checks, copy `script/ios_device.env.example` to
`script/ios_device.env` and fill in the same values. The real env file is
gitignored; only the example is public.

For Xcode GUI device runs, copy `Local.xcconfig.example` to
`Local.xcconfig` and set the local `DEVELOPMENT_TEAM` there. The checked-in
project must keep concrete Team IDs out of
`BTQFieldCapture.xcodeproj/project.pbxproj`; release readiness fails if one is
written there.

If the verifier reports Developer Mode is disabled, enable Developer Mode on
the iPhone and rerun the same command.
