# BTQ Field Capture Apple Client

Native Apple client for BTQ field capture.

This package is the public application code. Private prompts, plans, executor
logs, and verification notes live in the private `ai-methodology` repository.

## Targets

- `BTQFieldCaptureApp`: shared SwiftUI application module for iOS 18+ and
  macOS 15+.
- `BTQFieldCaptureMac`: macOS executable wrapper used for local build/run
  verification while Xcode app targets are added.
- `BTQFieldCapture.xcodeproj`:
  - `BTQ Capture`: iPhone/iPad app target.
  - `BTQ Capture Mac`: macOS app target.

## v1 Scope

The app is an offline-first ingress producer for the existing unified capture
backend:

- token/session model compatible with `/api/session`
- site selection and favorites
- visit timeline
- photo/voice/text capture draft model
- local queue status model
- multipart `/api/submit` client

The backend remains authoritative for authorization, workflow processing,
CouchDB persistence, and approval decisions.

## Verification

From this directory:

```bash
./script/field_pilot_readiness.sh
swift test
xcodebuild -list -project BTQFieldCapture.xcodeproj
xcodebuild -project BTQFieldCapture.xcodeproj -scheme 'BTQ Capture Mac' -destination 'platform=macOS' -derivedDataPath .build/xcode-derived build
./script/build_and_run.sh --verify
./script/verify_macos_app.sh
./script/verify_mock_api_submit.sh
./script/verify_live_api.sh --check
./script/verify_universal_links.sh --check
./script/verify_ios_simulator.sh
BTQ_SIMULATOR_FAMILY=iPad ./script/verify_ios_simulator.sh
BTQ_DEVELOPMENT_TEAM="<team id>" BTQ_DEVICE_NAME="<device name>" ./script/verify_ios_device.sh
```

The field-pilot readiness script runs the repeatable public-safe gates in one
pass: Swift tests, release metadata checks, mock API verification, macOS build
verification, iPhone simulator verification, and iPad simulator verification.
It also runs the live backend verifier and physical-device verifier when the
required local environment variables are present; otherwise it reports those as
skipped without failing the public-safe run.

The iOS target requires an installed iOS platform SDK in Xcode. If Xcode reports
that iOS is not installed, install the platform from Xcode Settings before
building the `BTQ Capture` scheme. The simulator verification script defaults
to iPhone and can target iPad with `BTQ_SIMULATOR_FAMILY=iPad`; it requires at
least one installed matching simulator runtime/device.

The device verification script uses runtime environment variables so personal
Apple Developer Team IDs and device names do not need to be committed. If the
device name is ambiguous, set `BTQ_XCODE_DESTINATION` for `xcodebuild` and
`BTQ_DEVICE_SELECTOR` for `devicectl` explicitly.

For repeated physical-iPhone checks, copy
`script/ios_device.env.example` to `script/ios_device.env` and fill in local
values. The real env file is gitignored and is also read by
`./script/field_pilot_readiness.sh`.

For Xcode GUI device runs, copy `Local.xcconfig.example` to `Local.xcconfig`
and set `DEVELOPMENT_TEAM` there. The shared project references
`Signing.xcconfig`, which keeps the public team empty and optionally includes
the ignored local file. Do not use Xcode project edits to store a personal Team
ID in `BTQFieldCapture.xcodeproj/project.pbxproj`.

The macOS verifier builds the real Xcode `BTQ Capture Mac` app target and
checks the generated bundle metadata. Add `--launch` to perform a local process
smoke test:

```bash
./script/verify_macos_app.sh --launch
```

The mock API verifier starts a local public-safe server and exercises the real
Swift HTTP client against `/api/session` and multipart `/api/submit` with
photo, audio, native metadata, and per-photo notes:

```bash
./script/verify_mock_api_submit.sh
```

The live API verifier uses local environment variables and does not commit
tokens. By default it validates `/api/session` and read-only
`/api/my-submissions`:

```bash
BTQ_LIVE_TOKEN="<token>" ./script/verify_live_api.sh
```

For a deliberate text-only smoke submit against the live backend:

```bash
BTQ_LIVE_TOKEN="<token>" BTQ_LIVE_SUBMIT=1 ./script/verify_live_api.sh
```

Universal-link onboarding requires the deployed unified capture server to serve
Apple's App Site Association payload. Configure `BTQ_APPLE_TEAM_ID` or
`BTQ_APPLE_APP_ID` on the deployed server, then run:

```bash
BTQ_AASA_BASE_URL="https://fc.gregstoltz.com" ./script/verify_universal_links.sh --live
```

## App Metadata

The app resource templates live under `AppResources/`:

- iOS and macOS `Info.plist` files include camera, microphone, photo library,
  notification, and background-mode declarations.
- `AppResources/iOS/BTQFieldCapture.entitlements` declares associated-domain
  readiness for tokenized `fc.gregstoltz.com` onboarding links.
- `AppResources/BTQFieldCapture.entitlements` enables sandboxed network,
  camera, microphone, and selected-file access for the macOS target.
