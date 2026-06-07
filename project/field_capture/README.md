# Field Capture

Single-page local photo capture app for BTQ.

The app does not write to the vault directly. It exports a `photo_capture`
queue job JSON file. Drop that file into the configured BT Pipeline outbox:

```text
~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/outbox
```

The queue watcher stages it into the runtime queue, and `queue_processor` writes:

- photo attachments to `Journal/Attachments/YYYY-MM-DD/`
- a linked photo-capture entry to `Journal/YYYY-MM-DD.md`

Run locally from the repo root:

```bash
./scripts/field-capture
```

Then open:

```text
http://localhost:8765
```

Field Capture requires a bearer token in the page URL:

```text
http://localhost:8765/?token=<TOKEN>
```

Tokens are stored in SQLite under the configured runtime root and map to a
`person_id`, capability flags, token type, and optional explicit `site_ids`.
Existing capture tokens default to `can_submit: true` and `can_view_site: true`;
when no explicit `site_ids` are stored, site access is resolved at request time
from the vault's `People/*.md` frontmatter. The transitional assignment model
is `job` for the primary site plus `additional_jobs` for secondary sites. Older
`sites: [...]` records remain readable as a fallback.

Create a token:

```bash
./scripts/field-capture token create --person jordan-avery --label "Jordan iPhone"
```

Create a viewer-only token for an internal/client viewer when needed:

```bash
./scripts/field-capture token create --person jordan-avery --viewer-only --token-type client_viewer --site-id 7050 --label "Summit viewer"
```

Create an internal universal viewer/admin token only for trusted managers:

```bash
./scripts/field-capture token create --person jordan-avery --token-type admin_viewer --all-sites --label "Jordan admin viewer"
```

Revoke a token:

```bash
./scripts/field-capture token revoke <token_id>
```

List token metadata:

```bash
./scripts/field-capture token list
```

The raw token is printed only during creation. After that, operations use
`token_id`; the database stores the token hash, not the raw bearer value.
New capture metadata stores safe submitter context for operator review:
`person_id`, `person_name`, and token label when available. Raw bearer tokens
are never written into capture metadata or displayed by dashboards. Older
captures that predate safe submitter metadata may show `Unknown submitter`.

## Summit Wire Pilot Ready State

For the Summit Wire pilot, each team member receives an individual tokenized
capture URL. Tokens are bearer links: treat them like passwords, send each link
only to the assigned person, and do not paste raw token URLs into shared docs,
tickets, or logs. The capture page uses the validated session to confirm the
personal link in the top ready message:

```text
Ready for <First Name> — Summit Wire
```

If a safe person name is not available but the token has exactly one assigned
site, the page falls back to:

```text
Ready to submit for Summit Wire
```

The Summit Wire viewer also requires a valid token:

```text
https://photos.example.com/site/7050?token=<TOKEN>
```

Capture tokens allow submit plus same-site viewing for the employee's assigned
site(s). Viewer-only tokens can view their scoped site(s) but cannot submit
captures. A token scoped to one site must not view another site. Anonymous,
bad, expired, or revoked tokens receive an access-required response. Unknown
sites still return not found.

Viewer pages and media responses send `X-Robots-Tag:
noindex,nofollow,noarchive`, HTML pages include a robots meta tag, and viewer
responses use private/no-store cache headers. These are defense-in-depth only;
authentication is the protection. Media requests are also token-gated: after a
valid site view, the server sets an HttpOnly viewer cookie so image and audio
URLs can remain relative `/media/...` paths without rendering the raw bearer
token in HTML. Do not share tokenized viewer links publicly. A fully
client-safe filtered viewer is future work; this stage gates the existing
internal/raw site viewer.

Pilot guidance for employees:

- Capture completed logical areas and meaningful exceptions.
- Do not capture every toilet, trash can, or repetitive detail.
- Use short voice notes when context matters.
- Voice note formula: `Location. Condition. Action taken or needed.`

Production topology for the pilot:

- The VPS capture app accepts uploads and stores field-capture media/metadata
  even if the Mac is offline.
- The Mac remains the processing and review authority. It can later pull from
  the VPS, transcribe/process audio, collect candidates, and present them for
  review.
- Review, approval/rejection, draft generation, queue staging, and vault
  mutation remain manager-owned. The queue processor remains the only vault
  writer.

## Audio Capture Local Testing

Run the local server from the repo root:

```bash
./scripts/field-capture
```

Open the tokenized capture page in a browser:

```text
http://localhost:8765/?token=<TOKEN>
```

Use an active field-capture token from `./scripts/field-capture token list`, or
create one with `./scripts/field-capture token create --person <PERSON> --label
"Local test"`. The raw bearer token is only printed at creation time.

Manual checks:

- Image-only upload: choose the site and QC category, tap `Add Photo`, select or
  take one image, then tap `Submit capture`.
- Audio-only upload: not supported in the current MVP. A photo is still required;
  submitting without a photo should return `missing_photo`.
- Image plus audio: add one photo, tap `Record voice note`, stop the recording,
  preview it with the audio player, then submit.
- Re-record: use `Clear voice note`, record again, preview, then submit.

After submitting, open the site viewer:

```text
http://localhost:8765/site/<site_id>?token=<TOKEN>
```

For Summit Wire, use:

```text
http://localhost:8765/site/7050?token=<TOKEN>
```

Verify the upload card shows the photo thumbnails, media counts, and a `Voice
note` audio player for submissions with audio. Then inspect the JSON payload:

```text
http://localhost:8765/site/<site_id>?format=json&token=<TOKEN>
```

The JSON should include `summary.total_audio`, each upload's `audio_count`, and
an `audio` array with media URLs, MIME type, filename, size, and optional
duration.

Browser compatibility:

- Android Chrome should generally support `MediaRecorder`.
- iOS Safari support varies by iOS version and audio MIME behavior.
- Unsupported browsers should show the graceful voice-note message while leaving
  photo upload usable.

### Android Emulator / Android Chrome Checklist

Start the local server on the Mac:

```bash
./scripts/field-capture
```

In Android Emulator Chrome, use the emulator host mapping instead of
`localhost`:

```text
http://10.0.2.2:8765/?token=<TOKEN>
```

If testing from a physical Android device on the same network, bind the local
server to the Mac LAN interface and use the Mac LAN IP:

```bash
./scripts/field-capture serve --host 0.0.0.0 --port 8765
```

```text
http://<MAC_LAN_IP>:8765/?token=<TOKEN>
```

Capture-page checks in Android Chrome:

- Confirm the tokenized page loads and shows the expected site selector.
- Image-only upload: choose the site and QC category, tap `Add Photo`, select or
  take one image, confirm the thumbnail appears, then submit.
- Voice note controls: tap `Record voice note` and confirm the microphone
  permission prompt appears.
- Recording flow: allow microphone access, confirm recording starts, tap `Stop`,
  and confirm the preview audio player appears.
- Playback: play the preview before submitting.
- Clear/re-record: tap `Clear voice note`, record again, stop, and preview.
- Duration cap: start a recording and observe that it stops automatically after
  about 60 seconds.
- Image plus audio: add at least one photo, record a voice note, then submit.
- Unsupported fallback: if a browser lacks `MediaRecorder` or microphone access,
  confirm the voice-note message appears and photo upload remains usable.

Viewer checks:

```text
http://10.0.2.2:8765/site/<site_id>?token=<TOKEN>
```

For Summit Wire:

```text
http://10.0.2.2:8765/site/7050?token=<TOKEN>
```

- Uploaded images appear on the correct upload card.
- The voice-note audio player appears on the same upload card as its photo.
- Per-upload image and audio counts are correct.
- Date summary image and audio totals are correct.
- Area and phase filters still hide/show the expected upload cards.
- `Issues only` still shows issue uploads and hides non-issue uploads.
- Tapping a thumbnail still opens the full-image view.

JSON checks:

```text
http://10.0.2.2:8765/site/<site_id>?format=json&token=<TOKEN>
```

- `summary.total_images` and `summary.total_audio` are present per date.
- Each upload includes `image_count` and `audio_count`.
- Image URLs remain in the `images` list.
- Voice notes appear in the `audio` array with URL, filename, `mime_type`,
  size, and optional duration.
- Browser-recorded audio MIME and filename extension match one of the accepted
  server types.

Manual verification results on 2026-05-02:

- Mac Chrome recorded and submitted an audio test through the capture workflow.
- The audio upload was associated with the correct capture metadata.
- tokenized `/site/<site_id>` displayed the uploaded audio with a playable audio control.
- Android Chrome loaded the tokenized capture app through `10.0.2.2`.
- Site selection worked.
- The emulator media picker could select an image after test media was seeded
  into the emulator.
- Image submission worked.
- Microphone input remained unavailable even after enabling it in emulator
  settings.
- Treat emulator microphone failure as a possible emulator or configuration
  limitation, not proof that app audio recording is broken.
- Prefer physical-device audio testing for final confidence: physical Android
  Chrome and the iPhone Safari spot-check.
- iPhone Safari should still be spot-checked because Safari MIME and
  `MediaRecorder` behavior can vary.

### Current Manual Verification Status

- Mac Chrome audio recording/upload/playback - passed.
- Android Emulator tokenized app load - passed.
- Android Emulator image upload - passed.
- Android Emulator audio recording - blocked by emulator microphone
  availability.
- Physical Android Chrome audio - not yet verified.
- iPhone Safari audio - not yet verified.

### iPhone Safari Spot Check

Use the production URL when testing deployed code, or the Mac LAN IP path when
testing locally from a physical iPhone:

```bash
./scripts/field-capture serve --host 0.0.0.0 --port 8765
```

```text
http://<MAC_LAN_IP>:8765/?token=<TOKEN>
```

Check:

- Tokenized page loads and resolves the expected site access.
- `Add Photo` offers the expected iOS choices, such as camera, photo library, or
  files depending on device settings.
- `Record voice note` either prompts for microphone permission and records, or
  shows the graceful unsupported-browser message.
- Stop, preview playback, and clear/re-record work when recording is supported.
- Image plus audio upload completes when recording is supported.
- Image-only upload still works if recording is not supported.
- tokenized `/site/<site_id>` shows the uploaded image and voice-note player on the same
  card.
- tokenized `/site/<site_id>?format=json` includes the expected image and audio counts.

Known browser notes:

- Android Chrome is the primary emulator test target.
- iOS Safari behavior can vary by iOS version, microphone permissions, and
  browser-selected audio MIME type.
- Observe the submitted audio MIME and extension during manual testing; the
  server only accepts safe audio MIME/extension combinations.
- Unsupported browsers should leave image upload usable.

## Audio Transcription Processing

Field-capture upload does not wait for transcription. Photo and audio submission
still writes the raw media files and queue metadata first; transcription is a
separate local post-upload step.

The field-capture audio processor follows the same broad pattern as the existing
voice inbox/iCloud audio flow:

- discover stable local audio evidence from file metadata
- process audio from local runtime storage, not from iCloud transport
- preserve raw audio as evidence
- write reviewable transcript artifacts
- record terminal success or failure state so failed items do not retry forever

Run one local scan:

```bash
./scripts/btq transcribe-field-audio
```

Field-capture submit now writes a `field_capture` document into CouchDB after
the VPS media files are stored. The field-capture server requires these service
environment variables before startup:

- `BTQ_COUCHDB_URL`
- `BTQ_COUCHDB_USER`
- `BTQ_COUCHDB_PASSWORD`

The VPS `/site/<site_id>` viewer and `/media/...` authorization read capture
metadata from the CouchDB `btq_field_captures` design views, not from archived
queue JSON. The active design document must include `by_site_id` for viewer
listing and `by_upload_id` for media authorization. Captures submitted before
the CouchDB cutover are no longer visible through the viewer unless they were
migrated into CouchDB; operators who need to inspect those historical records
must read the old queue files directly under `/srv/btq/runtime/queue/`.

Production PWA serving now matches the voice-memo app: Caddy serves static files
from `project/field_capture/public/` after deploy staging to
`/srv/btq/apps/field-capture/dist`, and Python serves the API/viewer/media
endpoints only. Deploy with:

```bash
cd /Users/operator/btq/project/field_capture
./deploy/push-and-deploy.sh
```

For the first migration from Python-served static files, use the staged order in
`deploy/DEPLOY.md` so the static `dist` directory is present before the Caddy
route switches.

The Mac CouchDB watcher claims pending capture documents and materializes them
into the Mac runtime intake path. For unusual recovery work, an operator may
still import one exported capture bundle into the Mac runtime. The
processor-ready bundle must include both the media directory and its matching
`photo_capture` queue JSON:

```text
<bundle>/
  uploads/<YYYY-MM-DD>/<capture_id>/...
  queue/<matching-photo-capture-job>.json
```

Preview and import the bundle on the Mac:

```bash
./scripts/btq pull-field-capture \
  --capture-id cap-photo-2026-05-03T18-25-20-04-00 \
  --bundle-path /tmp/btq-field-capture-export/cap-photo-2026-05-03T18-25-20-04-00 \
  --dry-run --json

./scripts/btq pull-field-capture \
  --capture-id cap-photo-2026-05-03T18-25-20-04-00 \
  --bundle-path /tmp/btq-field-capture-export/cap-photo-2026-05-03T18-25-20-04-00 \
  --json
```

The import command is copy-only and non-destructive. It copies media into
`<runtime_root>/uploads/<YYYY-MM-DD>/<capture_id>/`, copies the matching
`photo_capture` intake JSON into `<runtime_root>/field_capture/intake/`,
rewrites copied intake media `stored_path` values to the local Mac runtime,
verifies the intake JSON `capture_id`, and verifies referenced media exists. It
refuses to overwrite non-identical local files, does not write imported intake
metadata into `<runtime_root>/queue/`, does not delete remote or local files,
and does not run transcription, semantic processing, queue processing, or vault
mutation.

On the VPS, `/srv/btq/runtime` is owned for the `btq-field` service account.
If the operator account cannot read it directly, create a temporary readable
export bundle with sudo, then pull that bundle to the Mac. Example VPS-side
shape:

```bash
CAPTURE_ID=cap-photo-2026-05-03T18-25-20-04-00
CAPTURE_DATE=2026-05-03
EXPORT_ROOT=/tmp/btq-field-capture-export/${CAPTURE_ID}
sudo mkdir -p "${EXPORT_ROOT}/uploads/${CAPTURE_DATE}" "${EXPORT_ROOT}/queue"
sudo rsync -a "/srv/btq/runtime/uploads/${CAPTURE_DATE}/${CAPTURE_ID}/" \
  "${EXPORT_ROOT}/uploads/${CAPTURE_DATE}/${CAPTURE_ID}/"
sudo sh -c "grep -rl '\"capture_id\": \"${CAPTURE_ID}\"' /srv/btq/runtime/queue/*.json | head -1 | xargs -I{} cp {} '${EXPORT_ROOT}/queue/'"
sudo chown -R deploy:deploy "${EXPORT_ROOT}"
```

Then copy `/tmp/btq-field-capture-export/<capture_id>/` to the Mac and run
`pull-field-capture` against that local bundle path.

The Mac non-executable field-capture intake path is
`<runtime_root>/field_capture/intake/`. The Mac executable mutation queue
remains `<runtime_root>/queue/`. Only approved draft staging writes executable
jobs into `<runtime_root>/queue/`.

## Photo Vision Sidecars

Field-capture photos can be enriched locally with advisory vision descriptions:

```bash
./scripts/btq describe-field-photos --channel field_capture --dry-run --json
./scripts/btq describe-field-photos --channel field_capture --json
./scripts/btq describe-field-photos --channel field_capture --site-id 7050 --limit 10 --json
./scripts/btq describe-field-photos --channel field_capture --photo-asset-id <fcp_...> --json
```

The `mlx` backend requires `pip install 'bt-pipeline[vision-mlx]'`.

The command reads imported intake metadata from
`<runtime_root>/field_capture/intake/`, resolves image media under
`<runtime_root>/uploads`, and writes one sidecar per image under:

```text
<runtime_root>/field_capture/photo_vision/<photo_asset_id>.json
```

Each sidecar uses `artifact_type: field_capture_photo_vision` and includes the
capture id, stable photo asset id, source image path and SHA-256 hash, site id,
submitted area/phase, local model/provider, generated timestamp, description,
area guess, visible objects, possible conditions, possible issues, confidence,
human-review flag, warnings, and provenance back to the intake JSON and source
image.

This is descriptive enrichment only. The raw image is evidence; the vision
sidecar is interpretation. Vision can help reviewers identify visible contents
and visible conditions when photos lack voice notes, but it does not judge work
quality, assign visual quality ratings, select best photos, approve/reject
candidates, generate drafts, stage queue jobs, publish to clients, call the
queue processor, or mutate the vault. Human review remains authoritative.

The local ops dashboard displays existing sidecars as advisory context only. Its
status view reports photo vision sidecar counts, completed/failed/skipped
counts, malformed sidecars, backlog, and recent warnings. Open a capture from
`/captures` to see a rich per-photo vision card inline even when there are no
action candidates. Each card shows capture id, photo asset id, submitter,
site, submitted area/phase, captured time, source image path, sidecar status,
area guess, description, visible objects, possible conditions, possible
issues, warnings, model, confidence, and retry metadata for failed items. The
card also shows a small preview image when the existing safe `/media/...`
route can resolve the photo under the configured upload directory; otherwise
it only notes that the image path is available locally. No thumbnails or other image
derivatives are generated in this stage, and previews are for operator
comparison only, not client-facing publishing. The field-capture review page
separately matches sidecars to candidate captures by
capture id when candidates exist. Dashboard and review routes do not run vision,
call Ollama, create sidecars, score/rank/judge photos, create candidates, stage
queue jobs, invoke processors, publish to clients, or mutate the vault.

Completed sidecars are sanitized before writing. If model output drifts into
judgment language, the writer neutralizes common phrasing into visible facts and
adds `possible_judgment_language_removed` to `warnings`; if output cannot be
made safe, the item fails closed. Original image files remain immutable. Later
derived/processed images may be added as separate artifacts, but they must not
replace the original evidence.

Existing completed and failed sidecars are terminal skips by default. Targeted
regeneration requires explicit flags:

```bash
./scripts/btq describe-field-photos --channel field_capture \
  --photo-asset-id <fcp_...> --replace-flagged-judgment-language --json

./scripts/btq describe-field-photos --channel field_capture \
  --replace-failed --limit 3 --json
```

`--photo-asset-id` limits discovery to exactly one stable photo asset id and
fails closed if the asset does not exist. `--replace-failed` retries only
existing failed sidecars. `--replace-flagged-judgment-language` replaces only
completed sidecars whose model fields contain forbidden wording or whose
warnings include the sanitizer flag. Replacement sidecars include
`replaced_existing_sidecar`, `replaced_at`, `replacement_reason`,
`previous_status`, `previous_generated_at`, and `previous_model_name`.

Photo vision prompts include safe site background when the site is known. For
example, Summit Wire (`site_id: 1337`) is described as an industrial
manufacturing / wire facility with possible locker rooms, restrooms, admin
offices, entryways, break rooms, supply areas, mill/plant-adjacent spaces, and
maintenance areas. Glenwood school sites are described as school
facilities, and Continental is described as an industrial manufacturing site. This
context is advisory background only: the model must still describe visible facts
only, use uncertain language when the image does not match the context, and
avoid scoring, ranking, employee-performance language, or approval decisions.
Sidecars record whether site context was used with `site_context_used`,
`site_context_id`, `site_context_name`, and `site_context_summary`.

The current Ollama integration sends one generated prompt string to
`/api/generate`, not a multi-message chat prompt. The prompt is assembled in
layers inside that string: visible-only task framing, optional site background,
descriptive-only boundaries, forbidden wording, strict JSON keys, and submitted
area/phase metadata. `format: json` requests structured output, and BTQ still
normalizes the returned JSON before writing a sidecar. The
`possible_judgment_language_removed` warning is produced by this post-response
sanitizer when model fields required neutralization; it is not treated as proof
that the raw model output was safe.

Vision runs locally via one of two backends, selected with
`BTQ_FIELD_CAPTURE_VISION_BACKEND` (or `--vision-backend`): `mlx` (default) runs a
HuggingFace model on-device via mlx-vlm (Apple Silicon), and `ollama` calls a
local Ollama endpoint (`BTQ_OLLAMA_URL` or `--ollama-url`). The model is
configurable with `BTQ_FIELD_CAPTURE_VISION_MODEL` or `--model`. The current
primary model is **Qwen2.5-VL-7B** (`mlx-community/Qwen2.5-VL-7B-Instruct-4bit` on
MLX, equivalently `qwen2.5vl:7b` on Ollama) because BTQ needs structured visual
extraction rather than creative scoring. This matches the cat-capture demo
(your-org `cat_pipeline/vision_runner.py`). The field-capture vision
clients delegate backend mechanics to `project/vision_backends.py`, the shared
single source of truth vendored by the cat-capture demo. Recommended local
environment (mirrors the production launchd plist on `pro`):

```bash
export BTQ_FIELD_CAPTURE_VISION_BACKEND=mlx
export BTQ_FIELD_CAPTURE_VISION_MODEL=mlx-community/Qwen2.5-VL-7B-Instruct-4bit
export BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS=180
```

To run against Ollama instead, set `BTQ_FIELD_CAPTURE_VISION_BACKEND=ollama`,
`BTQ_FIELD_CAPTURE_VISION_MODEL=qwen2.5vl:7b`, and
`BTQ_OLLAMA_URL=http://127.0.0.1:11434`. `llama3.2-vision:11b` may be used later
for comparison only when comparison artifacts are model-aware and cannot
overwrite useful primary sidecars.

Process photos serially and idempotently. Slow one-photo-at-a-time processing is
preferred over parallel throughput because these artifacts are operational
evidence. The default local request timeout is 180 seconds and can be adjusted
with `BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS` or `--timeout-seconds`. Timeout
failures write failed sidecars with structured `error` metadata, including
`type: timeout`, `timeout_seconds`, and `can_retry: true`; retry them later with
`--replace-failed`. Do not configure cloud vision APIs for this stage. The
vision client accepts only local Ollama endpoints such as `127.0.0.1`,
`localhost`, or `::1`; it must not call OpenAI, Google Vision, Anthropic,
Gemini, hosted OCR, hosted image captioning, or any external AI service.
Images, thumbnails, transcripts, and metadata must not be sent to external AI
services. If local Ollama is unavailable, the command fails clearly and does
not fall back to cloud. Tests mock the vision call and do not require network
access.

## Pilot Audit Report

After local intake and photo vision sidecars exist, use the pilot audit command
to summarize real field-capture behavior without processing anything:

```bash
./scripts/btq audit-field-capture-pilot --site-id 7050 --date 2026-05-05
./scripts/btq audit-field-capture-pilot --site-id 7050 --date 2026-05-05 --json
```

The audit reads only local Mac runtime data: imported intake metadata, uploaded
media references, existing photo vision sidecars, review artifacts, and queue
state. It reports capture/media totals, submitter counts, area and phase
breakdowns, photo-only and large-batch behavior signals, photo vision
completed/failed/missing counts, metadata integrity issues, and current review
state. It does not pull from the VPS, run vision, run transcription, process
semantics, create candidates, approve/reject candidates, generate drafts, stage
queue jobs, invoke the queue processor, mutate the vault, delete files, or
publish client-facing content.

Submitter breakdowns use safe person metadata from intake records when
available. Raw bearer tokens are never included; older records without safe
person fields are grouped as `Unknown submitter`.

By default the command prints a human-readable report to stdout and writes no
files. Write explicit report artifacts only when requested:

```bash
./scripts/btq audit-field-capture-pilot \
  --site-id 7050 \
  --date 2026-05-05 \
  --output-md <runtime_root>/reports/field_capture_pilot/7050/2026-05-05.md \
  --output-json <runtime_root>/reports/field_capture_pilot/7050/2026-05-05.json
```

Markdown reports include an operator observations checklist for what worked,
what confused employees, whether photos were intentional, whether voice notes
were used, area-list gaps, Continental prep, and later client-visible candidates.
Recommendations are non-mutating review prompts such as browsing `/captures`,
retrying failed local vision sidecars with `--replace-failed`, adjusting area
choices, and reinforcing short voice tags.

By default the command reads field-capture intake metadata from
`<runtime_root>/field_capture/intake/`, resolves audio under
`<runtime_root>/uploads`, and writes transcript artifacts under:

```text
<runtime_root>/field_capture/audio_transcripts/
```

Each transcript artifact is JSON with `type: field_audio_transcript`, site/upload
metadata, audio filename and media URL, transcription engine, `status`, raw text
for successful transcriptions, and error details for failed transcriptions.

This pass reuses the local Whisper subprocess transcriber abstraction from the
voice inbox pipeline. It does not call an external transcription API. Raw
transcripts are evidence and may be imperfect; semantic cleanup, client-safe
summaries, and vault mutations are future layers.

## Audio Intelligence Local Verification

Use this checklist to verify the full local field-capture audio intelligence
chain without deploying or mutating the vault.

1. Start the local field-capture server:

   ```bash
   ./scripts/field-capture
   ```

1. Open the tokenized capture page:

   ```text
   http://localhost:8765/?token=<TOKEN>
   ```

   For Android Emulator Chrome, use:

   ```text
   http://10.0.2.2:8765/?token=<TOKEN>
   ```

1. Submit a capture with at least one photo and one voice note. Upload remains
   non-blocking; transcription and semantic cleanup do not run during submit.

1. Confirm raw audio playback in the site viewer:

   ```text
   http://localhost:8765/site/<site_id>?token=<TOKEN>
   ```

   For Summit Wire:

   ```text
   http://localhost:8765/site/7050?token=<TOKEN>
   ```

   Verify the upload card shows the image, the voice-note audio player, and the
   expected image/audio counts.

1. Confirm raw audio metadata in JSON:

   ```text
   http://localhost:8765/site/<site_id>?format=json&token=<TOKEN>
   ```

   Verify the upload includes an `audio` entry with `url`, `filename`,
   `mime_type`, `audio_count`, and no transcript yet unless processing already
   ran.

1. Run local transcription after upload:

   ```bash
   ./scripts/btq transcribe-field-audio
   ```

   Expected output reports pending/transcribed/failed/skipped counts.
   Successful transcript artifacts are written under:

   ```text
   <runtime_root>/field_capture/audio_transcripts/
   ```

1. Reload tokenized `/site/<site_id>` and verify the upload still shows the raw audio
   player plus an `Internal transcript - raw/unreviewed` section.

1. Run local semantic cleanup after transcription:

   ```bash
   ./scripts/btq process-field-audio-semantics
   ```

   Expected output reports discovered/skipped/completed/failed counts. Semantic
   artifacts are written under:

   ```text
   <runtime_root>/field_capture/audio_semantics/
   ```

1. Reload tokenized `/site/<site_id>` and verify the raw transcript remains visible and a
   separate `AI-assisted semantic cleanup - review needed` section appears with
   internal note, client-safe prepared note, operational summary, and suggested
   actions.

1. Reopen tokenized `/site/<site_id>?format=json` and verify transcript and semantic
   status fields are present for the audio asset.

This workflow must not create vault mutations, client-facing reports, database
records, or external API calls. Raw audio and raw transcripts remain preserved as
evidence.

## Audio Semantic Cleanup Processing

Semantic cleanup is a separate post-transcription layer. It reads completed raw
field-audio transcript artifacts and writes review-needed semantic artifacts
without changing the raw transcript.

Run one local scan:

```bash
./scripts/btq process-field-audio-semantics
```

By default the command reads raw transcript artifacts from:

```text
<runtime_root>/field_capture/audio_transcripts/
```

and writes semantic artifacts under:

```text
<runtime_root>/field_capture/audio_semantics/
```

Each semantic artifact is JSON with `type: field_audio_semantic_summary`,
provenance back to the transcript/audio asset, a hash of the raw transcript,
review-needed internal cleanup fields, issue classification, urgency, suggested
tags, and action candidates.

The current backend is a local deterministic rule/stub engine. It does not call
an external or cloud LLM API. Semantic artifacts are AI-assisted/review-needed
interpretation layers, not final truth. The `client_safe_note` field is prepared
text only; it is not automatically sent to a client. This pass creates no vault
mutations and no client-facing reports.

## Audio Review Pipeline

Field-capture audio review now has an explicit artifact chain after semantic
cleanup:

```text
field_audio_semantic_summary
-> action_candidate_review
-> approved_queue_job_draft
-> approved_draft_staging_result
-> runtime queue job
-> processed or failed queue artifact
```

Artifact locations:

- semantic artifacts:
  `<runtime_root>/field_capture/audio_semantics/`
- action candidates:
  `<runtime_root>/reviews/action_candidates/field_capture/`
- approved queue-job drafts:
  `<runtime_root>/reviews/approved_job_drafts/field_capture/`
- staging status:
  `<runtime_root>/reviews/staging/field_capture/`
- staged queue jobs:
  `<runtime_root>/queue/`
- queue processor archives:
  `<runtime_root>/processed/` and `<runtime_root>/failed/`

Meanings:

- A semantic artifact is an interpretation of a completed transcript. It is
  review-needed and does not mutate the vault.
- An action candidate is a proposed next step from a semantic artifact. It
  defaults to `pending_review` and is not executable.
- An approved job draft is a reviewed proposal for a queue job. It is still a
  review artifact, not a staged queue file.
- A staging status records whether an approved draft was staged, skipped, or
  failed validation.
- A runtime queue job is ready for the existing deterministic queue processor.
  It still does not mutate the vault until that processor runs.
- Processed and failed queue artifacts are written later by the queue
  processor, which remains the only vault writer.
- Failed queue artifacts are historical evidence. If the same draft/job is
  later replayed successfully and has processed evidence, the current
  operational state is `processed`; the old failed archive remains visible as
  historical failure evidence instead of an unresolved failure.

Inspect the current review state:

```bash
./scripts/btq review-dashboard --channel field_capture
./scripts/btq review-dashboard --channel field_capture --json
./scripts/btq review-dashboard --channel field_capture --stale-days 14 --limit 5
./scripts/btq review-status --channel field_capture
./scripts/btq review-status --channel field_capture --json
```

The CLI dashboard is read-only. It combines the most useful review workflow state:
candidate counts and pending previews, approved draft counts and queue-state
previews, maintenance finding counts, review disk usage, and a suggested next
operator command. It does not collect candidates, approve or reject candidates,
generate drafts, stage queue jobs, clean artifacts, invoke the queue processor,
or mutate the vault. Use the detailed commands below for inspection and action.
Failure counts distinguish unresolved failures from replayed historical
failures so archived failed queue files do not imply a successfully replayed job
is still currently failed.

The local ops dashboard also includes a human review page:

```bash
./scripts/btq ops-dashboard
# open http://127.0.0.1:8765/field-capture/review
```

`GET /field-capture/review` renders pending, approved, rejected, or failed
candidate cards with source context, provenance, and any existing local photo
vision sidecars for the same capture. Vision is shown only as advisory visual
context; the original image remains the evidence. `POST
/field-capture/review/approve` and `POST /field-capture/review/reject` update
exactly one `pending_review` candidate by calling the same review helper as
`review-candidate`. These routes only mutate candidate review artifacts. They
do not generate drafts, stage queue jobs, invoke processors, sync VPS captures,
or mutate the vault. Use this dashboard only over localhost, Tailscale, or
another trusted private path.

The ops dashboard also has `/batch-images` for importing WhatsApp fallback
photos as normal field captures. It is a client of field-capture `POST
/api/submit`; it does not write capture documents directly. Configure these
environment variables in the ops-dashboard launchd environment before deploy:

- `BTQ_BATCH_IMAGE_IMPORT_TOKEN`: raw field-capture token with `token_type=import`
  or equivalent admin import privileges.
- `BTQ_FIELD_CAPTURE_INTERNAL_URL`: private field-capture origin, such as a
  loopback or tailnet URL. Do not point batch imports at the public Cloudflare
  URL because large multi-photo batches can exceed the proxy body cap.

Automate the Mac intake-to-review path:

```bash
./scripts/btq watch-field-capture-pipeline --poll-seconds 60
./scripts/btq watch-field-capture-pipeline --once --json
```

One watcher cycle transcribes at most one pending audio file from local
field-capture intake, processes completed transcripts into semantic artifacts,
describes at most one not-yet-terminal photo with local Ollama vision, and
collects action candidates for human review. It loads the
Whisper transcriber only for the transcription pass and invokes photo vision as
a serial sidecar-only pass, so slow local processing can chew through backlog
without competing workers. The default backend is
`BTQ_FIELD_CAPTURE_VISION_BACKEND` or `mlx`, the default vision model is
`BTQ_FIELD_CAPTURE_VISION_MODEL` or `mlx-community/Qwen2.5-VL-7B-Instruct-4bit`,
the Ollama endpoint (when that backend is selected) is
`BTQ_OLLAMA_URL` or `http://127.0.0.1:11434`, and the timeout comes from
`BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS` or the watcher
`--vision-timeout-seconds` option. Use `--transcribe-limit` and
`--vision-limit` to tune per-cycle work; both default to `1`. Use
`--no-transcribe`, `--no-semantics`, `--no-vision`, or `--no-candidates` for
narrow maintenance runs.

Cat-demo vision is intentionally disabled by default. To enable it for a live
verification window, set `BTQ_CAT_VISION_ENABLED=1`, `BTQ_CAT_VISION_REPO` to the
cat site repository, and `BTQ_FIELD_CAPTURE_VISION_ISOLATED=1`. The watcher runs
field-photo vision first, then starts cat vision in a short-lived child process
bounded by `BTQ_CAT_VISION_TIMEOUT_SECONDS` (default `120`). If the isolated
field-vision precondition is missing, if the parent watcher has already warmed an
MLX client, or if the child times out, the cycle records `process_cat_vision` as
skipped/failed and continues; do not re-enable the old cat vision LaunchAgent.

The watcher does not approve or reject candidates, generate approved drafts,
stage queue jobs, invoke the queue processor, delete local or remote files,
clean up VPS uploads, publish client-facing content, call cloud vision APIs, or
mutate the vault. Candidate review remains a human UI/CLI step owned by Jordan.

Export reviewed site-viewer status for the VPS viewer:

```bash
./scripts/btq export-field-capture-site-status --site-id 7050 --json
./scripts/btq export-field-capture-site-status --site-id 7050 --include-issues --json
```

The command writes a static, internal-only viewer artifact to:

```text
<runtime_root>/field_capture/site_viewer_exports/site_7050.json
```

This export is curated from the Mac review workflow and is safe to copy to the
same runtime-relative path on the VPS. It contains reviewed capture IDs,
candidate IDs, approved status, reviewer/rationale metadata, deterministic
display category, context flags, safe `/media/...` references, and counts. It
does not include bearer tokens, private auth records, raw token labels, queue
processor internals, draft/staging state, or vault mutation jobs. Categories
such as `maintenance_issue`, `supply_request`, `staff_request`,
`site_reference`, and `other` are deterministic display hints only; they do not
mutate the vault and are not client-facing claims.

When `--include-issues` is supplied, the export also reads structured
`type: site_issue` files from the vault for the requested site and includes
safe issue fields for the internal viewer. This is read-only: it does not
update issue status, mark the client informed, stage queue jobs, invoke the
queue processor, or mutate the vault. `client_notified` means communication
happened and is separate from `resolved`.

Reviewed candidates can also be marked **Client Informed** after Jordan or the
team communicates a reviewed issue/request to a client:

```bash
./scripts/btq mark-client-informed \
  --channel field_capture \
  --candidate-id <ac_...> \
  --method email \
  --by "Jordan" \
  --note "Emailed client with photo/context."
```

This writes review/action sidecar metadata only:

```text
<runtime_root>/reviews/client_notifications/field_capture/<candidate_id>.json
```

Client Informed means communication happened. It does not mean the issue is
resolved, fixed, cleaned, or closed. The marker is internal operational status,
is separate from raw captures, and does not generate drafts, stage queue jobs,
invoke the queue processor, or mutate the vault. The site-viewer export includes
safe Client Informed status fields so internal reviewed cards can show whether
the client has already been notified.

The token-gated VPS `/site/<site_id>` viewer reads raw capture metadata from
CouchDB plus this optional export. If the export exists, reviewed/contextual
items appear in a top **Reviewed / Important Items** section above the raw
stream. Raw captures remain visible separately, and captures with voice notes or
text notes are marked with context badges. Mac-side transcript, semantic, and
photo-vision sidecars are not fetched by the VPS viewer; reviewed visual context
must be carried by the exported viewer artifact. The viewer is still
internal-only; a public client-ready portal is a later stage.

The local ops dashboard also shows a read-only **Open Site Issues** summary and
`/field-capture/issues` page so Jordan can see current operational issues without
manually browsing the vault.

Install the optional macOS LaunchAgent template only when you want this watcher
to run automatically on the processing node. The repo-owned template is:

```text
project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist
```

It runs:

```bash
/Users/operator/btq/scripts/btq watch-field-capture-pipeline --poll-seconds 60 --vision-limit 3 --json
```

Install and load it manually:

```bash
mkdir -p ~/Library/LaunchAgents /Users/operator/btq_runtime/logs
cp @@BTQ_PROJECT_ROOT@@/project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
```

Unload, restart, inspect status, and tail logs:

```bash
launchctl unload ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl unload ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl load ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl list | grep com.btq.field-capture-pipeline-watcher
tail -f /Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.out.log
tail -f /Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.err.log
```

Ollama should run separately, for example through the Homebrew Ollama service,
so the watcher can reach `http://127.0.0.1:11434`. The LaunchAgent only starts
the non-vault field-capture watcher and stops at candidate collection.

Inspect review artifact maintenance status:

```bash
./scripts/btq review-maintenance-status --channel field_capture
./scripts/btq review-maintenance-status --channel field_capture --stale-days 14 --json
./scripts/btq review-maintenance-status --channel field_capture --include-paths
```

The maintenance report is read-only. It reports review artifact accumulation,
stale pending candidates, old reviewed candidates, failed artifacts, approved
candidates without drafts, approved drafts without staging status, staged drafts
without queue/processed/failed/index evidence, orphaned staging statuses, queue
files pointing to missing drafts, approximate disk usage, and oldest/newest file
timestamps. `stale` means the item may need human attention; it does not mean
the item is wrong or safe to delete. No retention, deletion, archiving, repair,
approval, rejection, or restaging is implemented here.

Collect candidates from completed semantic artifacts:

```bash
./scripts/btq collect-action-candidates --channel field_capture
```

Candidate collection is intentionally conservative. It keeps provenance to the
semantic artifact and source transcript, but filters noisy field-audio actions
before writing review records:

- supply/order notes produce one supply review candidate
- generic after-photo candidates are suppressed for supply notes
- after-photo candidates are kept only when the semantic text clearly indicates
  correction, remediation, or before/after photo workflow
- obvious test, junk, or unclear audio snippets are skipped instead of becoming
  generic operational candidates
- duplicate generic candidates from the same semantic artifact are suppressed

List candidates for review:

```bash
./scripts/btq list-action-candidates --channel field_capture
./scripts/btq list-action-candidates --channel field_capture --status pending_review
./scripts/btq list-action-candidates --channel field_capture --status pending_review --json
```

Show full detail for one candidate:

```bash
./scripts/btq show-review-item --channel field_capture --candidate-id <ac_...>
./scripts/btq show-review-item --channel field_capture --candidate-id <ac_...> --json
```

The list command is read-only. It shows candidate IDs, status, type, summary,
confidence, rationale, source/context preview, semantic artifact path, review
artifact path, and reviewer metadata when present. It sorts by `candidate_id`
for deterministic output. Use `--include-source` to include longer source text
and context in JSON or expanded human output.

Preview candidate collection without writing review artifacts:

```bash
./scripts/btq collect-action-candidates --channel field_capture --dry-run
./scripts/btq collect-action-candidates --channel field_capture --dry-run --json
```

The collection dry-run reads completed semantic artifacts, builds candidate
payloads with the same deterministic IDs as real collection, computes the
review artifact path, and reports `would_create`, `skipped`, or `failed`
results. It detects an equivalent existing candidate artifact by deterministic
candidate path and does not write pending or failed candidate artifacts.

Generate approved drafts from already-approved candidates:

```bash
./scripts/btq generate-approved-drafts --channel field_capture
```

For `field_capture`, an approved candidate with explicit
`approval_metadata.proposed_queue_job` uses that job unchanged. Without an
explicit job, deterministic maintenance/client issue candidates map to
`log_site_issue` drafts; site-reference/documentation notes and non-issue
candidates continue to map to `append_to_note` drafts for the associated site
note. The draft builder resolves `site_id` from channel metadata, candidate
provenance, or the semantic artifact, then maps it through the active site
registry. Missing site context fails closed and produces a failed draft instead
of a queue job draft.

List approved drafts before staging:

```bash
./scripts/btq list-approved-drafts --channel field_capture
./scripts/btq list-approved-drafts --channel field_capture --status approved_draft
./scripts/btq list-approved-drafts --channel field_capture --status approved_draft --json
```

Show full detail for one approved draft:

```bash
./scripts/btq show-review-item --channel field_capture --draft-id <ajd_...>
./scripts/btq show-review-item --channel field_capture --draft-id <ajd_...> --json
```

The draft list command is read-only. It shows draft IDs, candidate IDs, draft
status, proposed queue job type, proposed payload preview, rationale,
confidence, draft/candidate/semantic/transcript artifact paths, and queue state
evidence when visible. It sorts by `draft_id` for deterministic output. Use
`--include-payload` for the full proposed queue payload and `--include-source`
for source context and provenance details.

Preview approved draft generation without writing draft artifacts:

```bash
./scripts/btq generate-approved-drafts --channel field_capture --dry-run
./scripts/btq generate-approved-drafts --channel field_capture --dry-run --json
```

The draft-generation dry-run reads the same candidate artifacts, skips
non-approved candidates, maps approved candidates through the same draft builder,
and reports `would_create`, `skipped`, or `failed` results. It detects an
equivalent existing draft by deterministic draft path and does not write
approved or failed draft artifacts.

Generated field-capture site-note content is internal-safe and
provenance-preserving by default. It appends as a separated markdown block under
`## Field Capture Reviews`, with `---` before each new entry so consecutive
captures do not run together. It includes available capture timestamp, site ID,
safe area, reviewed summary/rationale, capture/audio IDs, reviewer, and
semantic/transcript artifact paths. Generic review labels stay out of the final
site-note content. Maintenance issue drafts include site ID, issue title,
summary, observations, category, priority, status, submitter/reporter,
capture/candidate IDs, source artifacts, and `client_notified` status from
review notification sidecars when present. Missing or uncertain area values
render as `Unknown`. Draft generation does not mutate the vault; staging
remains explicit through `stage-approved-drafts`, and the queue processor
remains the only vault writer.

Approve or reject exactly one candidate:

```bash
./scripts/btq review-candidate --channel field_capture \
  --candidate-id <ac_...> \
  --status approved \
  --reviewer "Jordan" \
  --rationale "Verified against the field note."
```

Use `--status rejected` to reject a candidate. The command records reviewer,
review timestamp, review rationale, prior status, and review history. It does
not generate drafts or stage queue jobs.

Stage already-approved drafts into the runtime queue:

```bash
./scripts/btq stage-approved-drafts --channel field_capture
```

Preview staging without writing queue or staging artifacts:

```bash
./scripts/btq stage-approved-drafts --channel field_capture --dry-run
./scripts/btq stage-approved-drafts --channel field_capture --dry-run --json
```

The staging dry-run reads the same approved draft artifacts, validates the
proposed queue jobs, computes deterministic queue filenames and job IDs, and
checks queue, processed, failed, and processed-index duplicate evidence. It
reports `would_stage`, `skipped`, or `failed` results without writing
`<runtime_root>/queue/` files or staging status artifacts.

Safety boundaries:

- `ops-dashboard` is read-only local visibility only; it does not run field
  capture import, transcription, semantic processing, review actions, queue
  staging, queue processing, or vault mutation
- no automatic approval exists in production
- pending candidates are never executable
- semantic cleanup never mutates the vault
- `review-dashboard` is read-only and only summarizes existing review state
- `review-status` is read-only and does not invoke the queue processor
- `review-maintenance-status` is read-only visibility only; it does not clean
  up, archive, repair, approve, reject, or restage artifacts
- `list-action-candidates` is read-only and does not generate drafts or stage
  queue jobs
- `list-approved-drafts` is read-only and does not write staging artifacts or
  queue jobs
- `show-review-item` is read-only and requires exactly one candidate or draft ID
- `pull-field-capture --dry-run` validates one exported capture bundle without
  writing media or intake artifacts
- `pull-field-capture` imports one exported capture bundle without running
  transcription, semantic processing, queue processing, or vault mutation
- `pull-field-capture` does not write imported `photo_capture` metadata into
  the general executable `<runtime_root>/queue/`
- `collect-action-candidates --dry-run` validates and reports intended
  candidate collection without writing pending or failed candidate artifacts
- `generate-approved-drafts --dry-run` validates and reports intended draft
  generation without writing approved or failed draft artifacts
- `stage-approved-drafts --dry-run` validates and reports intended staging
  effects without writing review or queue artifacts
- `stage-approved-drafts` writes `<runtime_root>/queue/` only for valid approved
  drafts
- vault mutation only happens later through the deterministic queue processor
- dry-run support is currently implemented for candidate collection, draft
  generation, and staging; candidate review remains an explicit write command

Current operator workflow:

1. Preferred: run `./scripts/btq watch-field-capture-pipeline --poll-seconds
   60` on the Mac to automate VPS pull through local processing, one-photo
   vision sidecars, and candidate collection.
2. If running manually and the upload landed on the VPS, export the single capture bundle and import
   it with `./scripts/btq pull-field-capture --capture-id <capture_id>
   --bundle-path <bundle> --dry-run`, then run the same command without
   `--dry-run`.
3. Run `./scripts/btq transcribe-field-audio --json --limit 1`.
4. Run semantic cleanup with `./scripts/btq process-field-audio-semantics
   --json`.
5. Start with `./scripts/btq review-dashboard --channel field_capture` to see
   what needs attention and the suggested next command.
6. Inspect detailed review state with `./scripts/btq review-status --channel
   field_capture`.
7. Preview candidate collection with `./scripts/btq collect-action-candidates
   --channel field_capture --dry-run`.
8. Collect review candidates with `./scripts/btq collect-action-candidates
   --channel field_capture`.
9. List pending candidates with `./scripts/btq list-action-candidates
   --channel field_capture --status pending_review`.
10. Show full candidate detail with `./scripts/btq show-review-item --channel
   field_capture --candidate-id <ac_...>` if more detail is needed.
11. Approve or reject one candidate with `./scripts/btq review-candidate
   --channel field_capture --candidate-id <ac_...> --status approved
   --reviewer "Jordan" --rationale "..."`. No bulk approval command exists.
12. Preview approved draft generation with `./scripts/btq
   generate-approved-drafts --channel field_capture --dry-run`.
13. Generate approved draft artifacts with `./scripts/btq
   generate-approved-drafts --channel field_capture`.
14. List approved drafts with `./scripts/btq list-approved-drafts --channel
   field_capture`.
15. Show full draft detail with `./scripts/btq show-review-item --channel
   field_capture --draft-id <ajd_...>` if more detail is needed.
16. Preview staging with `./scripts/btq stage-approved-drafts --channel
   field_capture --dry-run`.
17. Stage approved drafts with `./scripts/btq stage-approved-drafts --channel
   field_capture`.
18. Inspect status again. Clean staged lineage should show queued staged jobs
   and no unexpected lineage gaps.
19. Later, run the existing queue watcher or queue processor deliberately.

Troubleshooting:

- `approved_candidate_missing_draft`: the candidate was approved, but no
  approved draft artifact exists yet.
- `approved_draft_missing_staging_status`: the draft exists, but staging has not
  been attempted or no staging status was recorded.
- `staged_draft_missing_queue_processed_failed_evidence`: staging claims the
  draft was staged, but no queue, processed, failed, or processed-index evidence
  is visible.
- `queue_file_missing_draft_artifact`: a runtime queue file points back to a
  draft artifact that cannot be found.
- queue job in `<runtime_root>/failed/`: inspect the failed job JSON and queue
  processor logs; the review pipeline has already handed off to deterministic
  queue validation/processing.
- any lineage gap in `review-status`: treat it as an audit prompt. The report
  does not repair artifacts or restage jobs.

For the VPS rollout, see [PRODUCTION_AUTH_DATA.md](PRODUCTION_AUTH_DATA.md) for
the minimal readonly vault mirror and token database bootstrap, and
[field-capture-production-deployment.md](field-capture-production-deployment.md)
for the safe production deployment process.
