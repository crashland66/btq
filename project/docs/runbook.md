# Operator Runbook

This runbook is for the current BT Pipeline implementation.

It assumes:

- the repository is configured through [config.json](/Users/operator/btq/config.json)
- Python dependencies are installed from [pyproject.toml](/Users/operator/btq/pyproject.toml)
- CouchDB is reachable and provisioned with the canonical entity types documented in [vault_schema.md](/Users/operator/btq/project/docs/vault_schema.md)

If the canonical `btq_vault` seed records (locations, employees) are missing, some jobs will fail even when the pipeline itself is healthy.

## A. Daily Operation

### How audio enters the system

Current expected flow:

1. Record audio on iPhone.
2. Let the memo sync into the configured iCloud inbox.
3. The transcription pipeline picks up stable `.m4a`, `.mp3`, or `.wav` files from `audio_inbox_dir`.
4. The watcher claims and moves the file into `<runtime_root>` before Whisper
   or queue processing touches it.

Configured paths can be inspected with:

```bash
project/.venv/bin/python -m config get audio_inbox_dir
project/.venv/bin/python -m config get audio_archive_dir
```

### Run a single pass

Preflight:

```bash
./scripts/btq-verify-environment
```

Run one end-to-end pass:

```bash
cd project
.venv/bin/python -m transcription_pipeline.main --once
```

This single pass currently does all of the following:

1. scans the audio inbox
2. claims stable audio into local non-iCloud runtime storage
3. transcribes with Whisper
4. normalizes domain language
5. extracts and validates events
6. writes local queue jobs
7. authors those jobs as `btq_queue` CouchDB docs (the unified queue transport)
8. leaves queue draining to the CouchDB queue watcher (`btq watch-couchdb-queue`),
   which materializes each doc into the runtime spool and runs the processing
   pass itself
9. archives the source audio under `<runtime_root>/completed/audio`

### Run watchers

Foreground transcription watcher:

```bash
./scripts/whisper-watch
```

Foreground CouchDB queue watcher (the sole queue processor since the
2026-08-07 file-queue retirement — materializes `btq_queue` docs into the
runtime spool and processes them in the same daemon):

```bash
./scripts/btq watch-couchdb-queue --json
```

The old file-queue watcher (`com.btq.queue-watch` / `queue_processor.watch`)
is retired. `./scripts/queue-watch` remains only as a manual/emergency drain
of the runtime spool (`--once`); nothing should install it as a daemon.

Install macOS `launchd` services:

```bash
./scripts/install-whisper-launch-agent
```

The CouchDB queue watcher installs from the repo-owned template
`project/field_capture/launchagents/com.btq.couchdb-queue-watcher.plist`.

The queue and Whisper installers write and load their service definitions. The
field-capture pipeline watcher uses a repo-owned template that must be copied
and loaded manually when you want it enabled:

```bash
mkdir -p ~/Library/LaunchAgents /Users/operator/btq_runtime/logs
cp @@BTQ_PROJECT_ROOT@@/project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
```

The watchers are macOS-specific service definitions. They are not portable
daemon definitions.

Manual staging is still available:

- `./scripts/btq-stage-outbox`
- `./scripts/btq-run`
- `./scripts/btq-dry`

## B. Verifying Correct Behavior

### Where to look for transcripts

Local staging and transcript artifacts:

- `local/audio_processing/`

Expected files:

- `<audio>.<ext>.whisper.txt`
- `<audio>.<ext>.whisper.normalized.txt`
- `<audio>.<ext>.whisper.corrections.json`

If compare mode is enabled:

- `local/transcripts/YYYY-MM-DD/<audio-stem>/original.txt`
- `local/transcripts/YYYY-MM-DD/<audio-stem>/enhanced.txt`
- `local/transcripts/YYYY-MM-DD/<audio-stem>/diff.txt`

### Where to look for events

Local event artifacts:

- `local/events_raw/`
- `local/events_enriched/`
- `local/events_valid/`
- `local/events_failed/`

Quick checks:

- valid extracted events should appear in `local/events_valid/`
- rejected or malformed events should appear in `local/events_failed/`

### Where queue jobs appear

Local queue job creation from the transcription pipeline:

- `local/queue_jobs/`

Runtime queue processing directories:

- `<runtime_root>/queue/`
- `<runtime_root>/processed/`
- `<runtime_root>/failed/`
- `<runtime_root>/claimed/`
- `<runtime_root>/processing/`
- `<runtime_root>/completed/`

Configured iCloud outbox ingress path, if you use outbox-driven scripts:

- `<pipeline_dir>/outbox/`

Current watcher coverage:

- `<pipeline_dir>/outbox/*.json` is claimed locally and staged into the runtime queue by the queue watcher
- `<runtime_root>/queue/` is then processed by the same watcher

### Cowork read-only queue reader

Cowork queue writes remain one-way through `cowork_drop`; the sandbox must not
receive CouchDB credentials, enqueue/admin tools, or a second write path. Queue
visibility for Cowork is provided by a host-side read-only backend that can run
on AC or PC, depending on operator preference. PC is the more always-on host;
AC is useful for development and debugging.

Backend commands:

```bash
scripts/btq-cowork-reader --tool queue_state
scripts/btq-cowork-reader --tool list_queue_jobs --date YYYY-MM-DD
scripts/btq-cowork-reader --tool get_queue_job --job-id JOB_ID
```

The backend returns JSON and maps CouchDB connection failures to structured
errors such as `{"ok": false, "error": {"code": "couchdb_unavailable", ...}}`.
It only reads `btq_queue` with CouchDB `GET` requests. It exposes these
wrapper tool names for Cowork registration: `btq_queue_state`,
`btq_list_queue_jobs`, and `btq_get_queue_job`.

Configure the host process with either `~/.config/btq/config.json`:

```json
{
  "couchdb_url": "http://HOST:5984",
  "couchdb_user": "USER",
  "couchdb_password": "PASSWORD",
  "queue_database": "btq_queue"
}
```

or environment variables:

```bash
export BTQ_COUCHDB_URL=http://HOST:5984
export BTQ_COUCHDB_USER=USER
export BTQ_COUCHDB_PASSWORD=PASSWORD
export BTQ_COUCHDB_QUEUE_DB=btq_queue
```

Post-merge operator step: register this backend with the Cowork wrapper on the
chosen host, then run a live smoke check from that same host:

```bash
scripts/btq-cowork-reader --tool btq_queue_state
scripts/btq-cowork-reader --tool btq_list_queue_jobs --date "$(date +%F)"
scripts/btq-queue-list --date "$(date +%F)" --format json
```

The first command should return real `btq_queue` counts, and the Cowork
`btq_list_queue_jobs` result for today's date should match
`btq-queue-list --format json` on the same host.

### Field-Capture Review Pipeline

Pilot-ready Summit Wire state:

- Production capture page: `https://photos.example.com/`
- Token-gated Summit Wire viewer: `https://photos.example.com/site/7050?token=<TOKEN>`
- Individual capture URLs include bearer tokens and are assigned to exactly one
  person. Do not share them between employees or paste raw token URLs into
  shared docs.
- Existing capture tokens allow submit plus same-site viewing for their scoped
  site(s). Viewer-only tokens can be created with `./scripts/field-capture token
  create --viewer-only --token-type client_viewer --site-id <site_id> ...`; they
  can view but cannot submit.
- A valid Summit token shows `Ready for <First Name> — Summit Wire` at the top
  of the SPA, confirming the employee is using the correct personal link.
- Anonymous, bad, expired, revoked, or cross-site tokens are blocked from
  `/site/<site_id>` and `/site/<site_id>?format=json`; unknown sites still
  return not found. The viewer returns a friendly empty state only after a valid
  token reaches a known site with no captures.
- Viewer HTML and media responses send noindex/noarchive headers and private
  no-store cache controls. Noindex is defense-in-depth, not authentication.
  Media is protected by the same bearer token through an HttpOnly viewer cookie
  after the site page validates the token, so raw tokens are not rendered in
  image/audio URLs.
- Employees should capture completed logical areas and exceptions, not every
  toilet, trash can, or repetitive detail.
- Voice note formula: `Location. Condition. Action taken or needed.`

Per-site capture guidance and display categories are configured through the
admin UI (`/sites`); the SPA reads them from `/api/session` and falls back to
`system_defaults` then to the built-in category list. Pipeline contracts are
unchanged: submit packets always carry the canonical category string.

The VPS upload path is independent of Mac processing. If the Mac watcher is
offline, the VPS still accrues captures under `/srv/btq/runtime/uploads` with
matching `field_capture` documents in CouchDB. The Mac can pull, transcribe,
process semantics, emit `job_draft` documents, and review later. Review approval,
queue materialization, and canonical CouchDB mutation remain manager-owned.
CouchDB `btq_vault` is the authoritative store.

Field-capture audio has a review path between semantic cleanup and queue
processing:

```text
semantic artifact
-> job_draft document in CouchDB
-> review in /swipe or /candidates
-> scripts/job-draft-queue-watch materialization
-> runtime queue job
-> processed or failed queue artifact
```

Artifact locations:

- photo vision sidecars:
  `<runtime_root>/field_capture/photo_vision/`
- semantic artifacts:
  `<runtime_root>/field_capture/audio_semantics/`
- active review records:
  CouchDB `type: job_draft` documents in the field-captures database
- staged queue jobs:
  `<runtime_root>/queue/`
- queue processor archives:
  `<runtime_root>/processed/` and `<runtime_root>/failed/`

The active review emitter is `field_capture.pipeline_watcher`, which calls
`job_draft_emission.collect_job_drafts` after local semantic processing. It
writes CouchDB `job_draft` documents (`couchdb_job_draft_writer.JOB_DRAFT_TYPE`)
with `review_status: pending_approval`. Operators review those records in
`/swipe` or `/candidates`. Approved, unmaterialized drafts are picked up by
`./scripts/job-draft-queue-watch`, which validates the queue job, writes the
runtime queue file, and marks the draft materialized in CouchDB. The deterministic
queue processor remains the canonical writer after that handoff.

When field-capture uploads land on the VPS, the Mac processing runtime needs a
processor-ready copy of exactly one capture bundle. That bundle includes both
media and the matching `photo_capture` queue JSON:

```text
<bundle>/
  uploads/<YYYY-MM-DD>/<capture_id>/...
  queue/<matching-photo-capture-job>.json
```

Field-capture submit now writes a `field_capture` document into CouchDB after
the VPS stores media. The field-capture server requires `BTQ_COUCHDB_URL`,
`BTQ_COUCHDB_USER`, and `BTQ_COUCHDB_PASSWORD` in its service environment. The
Mac CouchDB watcher claims pending documents and materializes them into local
field-capture intake.

The VPS site viewer and `/media/...` authorization now read capture metadata
from CouchDB, not from queue JSON. The `btq_field_captures` design document must
provide `by_site_id` for `/site/<site_id>` and `by_upload_id` for media access
checks. Pre-cutover captures that only exist as old queue files are not visible
through the viewer unless migrated into CouchDB; inspect those historical queue
artifacts directly under `/srv/btq/runtime/queue/` when needed.

For unusual recovery work, preview and import an exported bundle on the Mac:

```bash
./scripts/btq pull-field-capture \
  --capture-id <capture_id> \
  --bundle-path <bundle> \
  --dry-run --json

./scripts/btq pull-field-capture \
  --capture-id <capture_id> \
  --bundle-path <bundle> \
  --json
```

The import command copies media into the local runtime uploads tree and copies
the matching `photo_capture` intake JSON into
`<runtime_root>/field_capture/intake/`. It rewrites copied intake media
`stored_path` values to the Mac runtime so the local field-capture audio
processor can discover the audio safely. It is idempotent, refuses
non-identical local overwrites, does not write imported intake metadata into the
general executable `<runtime_root>/queue/`, and does not run transcription,
semantic processing, queue processing, or canonical mutation.

The deployed VPS stores uploads under `/srv/btq/runtime/uploads`; CouchDB is
the capture metadata ingress. The Mac executable mutation queue remains
`<runtime_root>/queue/`, and only the approved `job_draft` materializer writes
executable jobs there. If CouchDB recovery requires a manual bundle, create a
temporary readable export bundle on the VPS, copy that bundle to the Mac, and use
`pull-field-capture`.

Describe imported field-capture photos with a local vision model:

```bash
./scripts/btq describe-field-photos --channel field_capture --dry-run --json
./scripts/btq describe-field-photos --channel field_capture --json
./scripts/btq describe-field-photos --channel field_capture --capture-id <capture_id> --json
./scripts/btq describe-field-photos --channel field_capture --photo-asset-id <fcp_...> --json
./scripts/btq describe-field-photos --channel field_capture --site-id <site_id> --date YYYY-MM-DD --limit 10 --json
./scripts/btq describe-field-photos --channel field_capture --replace-failed --limit 3 --json
```

The command writes reviewable sidecar artifacts only:
`<runtime_root>/field_capture/photo_vision/<photo_asset_id>.json`. Each sidecar
has `artifact_type: field_capture_photo_vision`, source image hash, model
metadata, visible-content description, area guess, visible objects, possible
conditions, possible issues, confidence, warnings, and provenance back to the
intake JSON and image media.

Existing completed and failed sidecars are skipped unless an explicit
replacement flag is used. Use `--replace-failed` to retry failed sidecars only.
Use `--replace-flagged-judgment-language` to regenerate only completed sidecars
whose model fields contain forbidden wording or whose warnings include the
sanitizer flag. Combine either flag with `--photo-asset-id <fcp_...>` for the
narrowest possible replacement. Dry-run reports `would_replace` and writes
nothing.

Recommended local settings:

```bash
export BTQ_FIELD_CAPTURE_VISION_MODEL=qwen2.5vl:7b
export BTQ_OLLAMA_URL=http://127.0.0.1:11434
export BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS=180
```

The local request timeout defaults to 180 seconds and can also be set per run
with `--timeout-seconds`. Timeout failures create failed sidecars with
structured `error` metadata: `type: timeout`, `message`, `model_name`,
`timeout_seconds`, and `can_retry: true`. Do not auto-retry inside one command;
retry later with `--replace-failed` so serial processing remains predictable.

When a site is known and CouchDB site registry access is configured, the prompt
includes safe site background from the `btq_sites` document `vision_context`.
This is advisory background only. The model must still describe visible facts
only and use uncertain language when the image does not match the context. New
or explicitly regenerated sidecars record `site_context_used`,
`site_context_id`, `site_context_name`, and `site_context_summary`. If
`BTQ_COUCHDB_URL` is unset, photo vision continues without site-specific
background. If CouchDB is configured but unavailable, photo vision logs
`CouchDB site vision context unavailable` and continues without that context.

Vision enrichment is local-only and descriptive. It identifies visible contents
and visible conditions only. It does not judge work quality, score cleanliness,
choose best photos, publish to clients, emit `job_draft` records,
approve/reject anything, materialize queue jobs, invoke the queue processor, or
mutate canonical state. Treat the raw image as evidence and the model output as
advisory interpretation; human review remains authoritative. Original images
remain immutable. Later processed/derived images may be added as separate
artifacts, but they must not replace the original evidence.

Use only local Ollama at the configured `BTQ_OLLAMA_URL`. Do not call OpenAI,
Google Vision, Anthropic, Gemini, hosted OCR, hosted image captioning, or any
external AI service, and do not send images, thumbnails, transcripts, or
metadata to external AI services. If local Ollama is unavailable, fail clearly;
do not fall back to cloud.

Run a read-only pilot audit after local intake and optional photo vision
sidecars exist:

```bash
./scripts/btq audit-field-capture-pilot --site-id 7050 --date 2026-05-05
./scripts/btq audit-field-capture-pilot --site-id 7050 --date 2026-05-05 --json
```

The audit reads only the Mac runtime. It summarizes capture/media totals,
submitters, submitted areas and phases, photo-only and large-batch behavior
signals, existing photo vision sidecars, metadata integrity, and review state.
Submitter breakdowns use safe person metadata when available and never expose
raw bearer tokens.
It is useful before changing UI guidance, area lists, Continental rollout behavior,
or any future client-visible layer.

By default the audit writes no files. To preserve a local operator report under
the runtime report tree, pass explicit output paths:

```bash
./scripts/btq audit-field-capture-pilot \
  --site-id 7050 \
  --date 2026-05-05 \
  --output-md <runtime_root>/reports/field_capture_pilot/7050/2026-05-05.md \
  --output-json <runtime_root>/reports/field_capture_pilot/7050/2026-05-05.json
```

The audit does not pull from the VPS, run vision, run transcription, process
semantics, emit `job_draft` records, approve/reject drafts, materialize queue
jobs, invoke the queue processor, mutate canonical state, delete/archive files,
or publish client-facing content. Its recommendations are non-mutating review
prompts such as browsing `/captures`, retrying failed local vision sidecars,
tuning area choices, and reinforcing voice-tag guidance.

Inspect active review state:

```bash
./scripts/btq ops-dashboard
# open http://127.0.0.1:8765/swipe
# open http://127.0.0.1:8765/candidates
```

`/swipe` is the fast approval surface for pending `job_draft` records.
`/candidates` is the table/detail surface for the same CouchDB-backed review
records, with filters and capture context. These surfaces update the
`job_draft.review_status` and reviewer metadata in CouchDB; they do not write
runtime queue files or mutate canonical state. Queue materialization is a
separate step handled by `./scripts/job-draft-queue-watch`.

Failed queue files in `<runtime_root>/failed/` are retained as historical
evidence. If the same draft/job is replayed and later appears in
`<runtime_root>/processed/` or the processed index, review reporting treats the
current state as processed and counts the failed archive under replayed
historical failures rather than unresolved failures.

The local ops dashboard includes a narrow field-capture review UI:

```bash
./scripts/btq ops-dashboard
# open http://127.0.0.1:8765/swipe
# open http://127.0.0.1:8765/candidates
# open http://127.0.0.1:8765/captures
```

`/captures` is the read-only capture browser. Each row groups one upload
session by capture id with site, submitter, time, file counts, review-draft
count, and sidecar count. Filter on site, date range, has-photo, has-audio,
has-draft, and has-vision-sidecar. Open a capture to see the rich
per-photo vision card inline: photo asset id, submitter, site, submitted
area/phase, captured time, image preview, sidecar status, area guess,
description, visible objects, possible conditions, possible issues, warnings,
model, confidence, and retry metadata for failed sidecars — plus any audio or
other uploads and `job_draft` records tied to that capture. Submitter
comes from safe intake metadata such as `person_name` or `person_id`; raw
bearer tokens are never displayed. Older captures without safe submitter
metadata may show `Unknown submitter`.
It shows a small image preview when the safe `/media/...` route can resolve the
photo under the configured upload directory; otherwise it only notes that the
image path is available locally. It does not generate derivatives, watermark,
redact, strip EXIF, create client-ready copies, mutate images, run vision, call
Ollama, create sidecars, emit `job_draft` records, materialize queue jobs, run
processors, publish to clients, or mutate canonical state.

The review pages render readable `job_draft` cards and let the operator approve
or reject pending drafts with reviewer metadata. They also display existing
local photo vision sidecars for the same capture as advisory context:
description, area guess, visible objects, possible conditions, possible issues,
warnings, model, and confidence. The dashboard does not run vision, call Ollama,
create sidecars, score/rank/judge photos, select photos, materialize queue jobs,
run processors, sync VPS captures, or mutate canonical state. Use it only over
localhost, Tailscale, or another trusted private path.

To surface reviewed/contextual captures in the internal VPS site viewer, export
the Mac review state as a static viewer artifact:

```bash
./scripts/btq export-field-capture-site-status --site-id 7050 --json
./scripts/btq export-field-capture-site-status --site-id 7050 --include-issues --json
```

Default output:

```text
<runtime_root>/field_capture/site_viewer_exports/site_7050.json
```

Copy that JSON to the same runtime-relative path on the VPS:

```text
/srv/btq/runtime/field_capture/site_viewer_exports/site_7050.json
```

`/site/<site_id>?token=<TOKEN>` continues to render all raw captures from the
VPS runtime for tokens scoped to that site. When the export exists, it also
renders an internal-only **Reviewed / Important Items** section above the raw
stream and marks captures with `Reviewed`, `Voice note`, `Text note`, and
transcript/context badges. The export is curated from approved Mac review
candidates and omits bearer tokens, auth records, raw token labels, queue
processor internals, and canonical mutation jobs. Deterministic categories and
priorities are display aids only; a filtered client-safe portal remains a later
stage.

With `--include-issues`, the same export includes safe read-only fields from
structured `type: site_issue` files for that site. The viewer can then render an
internal **Open Site Issues** section. This export path does not change issue
status, mark Client Informed, stage queue jobs, invoke the queue processor, or
mutate canonical state.

When a reviewed issue/request has been communicated to the client, mark the
candidate as Client Informed:

```bash
./scripts/btq mark-client-informed \
  --channel field_capture \
  --candidate-id <ac_...> \
  --method email \
  --by "Jordan" \
  --note "Emailed client with photo/context."
```

This stores internal operational status at:

```text
<runtime_root>/reviews/client_notifications/field_capture/<candidate_id>.json
```

Client Informed means communication happened; it does not mean resolved or
fixed. It is review/action sidecar metadata, separate from raw captures and
separate from canonical mutation. The command must not generate drafts, stage
queue jobs, invoke the queue processor, or mutate canonical state. A refreshed
site-viewer export can surface the Client Informed badge in the internal
`/site/<site_id>` viewer.

Structured operational issues can be written through the deterministic queue job
type `log_site_issue`. The job creates or updates a canonical `site_issue`
document in `btq_vault`, keyed by `issue_id`.

Use `log_site_issue` when a reviewed field capture or daily log item should
become first-class issue state. Required contract fields include `site_id`,
`title`, `reported_by`, `client_notified`, `resolution_trigger`, and either
`summary` or `observations`. Issue status is limited to `open`, `monitoring`,
and `resolved`. `client_notified` means the client was told; it does not close
the issue by itself.

Open/current site issues are visible in the ops dashboard and the read-only
`/field-capture/issues` page. Supply needs and equipment requests are visible in
the read-only `/supplies` and `/equipment` browse views. Status changes and
resolution should be handled by deterministic queue jobs, not by dashboard
display code.

Backlog schema gaps surfaced by field-capture review:

- `log_supply_need`: proposed future job type for consumable restock or order
  workflow, such as paper towels, toilet paper, mop heads, BrightWash cleaner,
  trash liners, and soap. Likely fields include `site_id`, `item_name`,
  `quantity_needed`, `urgency`, `requested_by`, `observed_at`, `source`,
  `related_capture_ids`, `related_candidate_ids`, `status`, and `notes`.
  Proposed statuses are `open`, `ordered`, `delivered`, `stocked`, and
  `no_action_needed`.
- `log_equipment_request`: proposed future job type for durable tool or
  equipment workflow, such as a vacuum, SweepMate, mop bucket, extension pole,
  replacement dispenser, or floor machine. Likely fields include `site_id`,
  `equipment_name`, `reason`, `priority`, `requested_by`, `observed_at`,
  `source`, `related_capture_ids`, `related_candidate_ids`, `status`, and
  `notes`. Proposed statuses are `open`, `approved`, `ordered`, `provided`,
  `denied`, and `no_action_needed`.

These are separate from `log_site_issue`. Maintenance, damage, and client
issues remain `log_site_issue`; consumable supply needs and durable equipment
requests should not be buried as `append_to_note` long term. Until those job
types are implemented, `append_to_note` remains the fallback.

The Mac watcher can automate intake through `job_draft` emission:

```bash
./scripts/btq watch-field-capture-pipeline --poll-seconds 60
./scripts/btq watch-field-capture-pipeline --once --json
```

Each cycle pulls new VPS field captures, transcribes at most one pending audio
file, processes completed transcripts into semantic artifacts, describes at
most one not-yet-terminal photo with local Ollama vision, and calls
`job_draft_emission.collect_job_drafts` to emit CouchDB `job_draft` records. It
loads the Whisper transcriber only for the transcription pass and runs photo
vision as a serial sidecar-only pass. Defaults are intentionally slow:
`--transcribe-limit 1` and `--vision-limit 1`. The vision model and
endpoint come from `BTQ_FIELD_CAPTURE_VISION_MODEL` or `qwen2.5vl:7b`,
`BTQ_OLLAMA_URL` or `http://127.0.0.1:11434`, and
`BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS` or `--vision-timeout-seconds`.
It does not approve/reject, materialize queue jobs, run the queue processor,
delete local or remote files, clean VPS uploads, call cloud vision APIs, publish
client-facing content, or mutate canonical state.

The optional LaunchAgent template at
`project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist`
runs the same watcher automatically:

```bash
/Users/operator/btq/scripts/btq watch-field-capture-pipeline --poll-seconds 60 --vision-limit 3 --json
```

It logs to:

```text
/Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.out.log
/Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.err.log
```

Ollama should be running separately, such as through the Homebrew Ollama
service. Unload, restart, inspect status, and tail logs with:

```bash
launchctl unload ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl unload ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl load ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist
launchctl list | grep com.btq.field-capture-pipeline-watcher
tail -f /Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.out.log
tail -f /Users/operator/btq_runtime/logs/field-capture-pipeline-watcher.err.log
```

Materialize approved `job_draft` records into the local runtime queue:

```bash
./scripts/job-draft-queue-watch --once --json
./scripts/job-draft-queue-watch --poll-seconds 5 --json
```

The watcher reads approved, unmaterialized CouchDB `job_draft` documents,
validates each proposed queue job, writes a queue JSON file under
`<runtime_root>/queue/`, and marks `queue_materialized_at` on the source draft.
Use `--dry-run --once --json` to inspect what would be written. It does not run
the queue processor and does not mutate canonical state.

### Legacy (pre-job_draft) Review Flow — Migration Only

The following action-candidate, approved-draft, and staging material is retained
for migration and historical recovery. It is not the active operator review
pipeline. New field-capture review should use CouchDB `job_draft` records,
`/swipe` or `/candidates`, and `./scripts/job-draft-queue-watch`.

Inspect review artifact maintenance status:

```bash
./scripts/btq review-maintenance-status --channel field_capture
./scripts/btq review-maintenance-status --channel field_capture --stale-days 14 --json
./scripts/btq review-maintenance-status --channel field_capture --include-paths
```

The maintenance report is read-only. It reports accumulated candidates, drafts,
and staging artifacts; stale pending candidates; old reviewed items; failed
artifacts; orphaned records; unresolved staged drafts; queue files pointing to
missing drafts; approximate review directory disk usage; and oldest/newest file
timestamps. `stale` means needing human attention, not that the artifact is
wrong or safe to delete. This stage implements no retention, deletion,
archiving, repair, approval, rejection, or restaging behavior.

Collect legacy candidates from completed semantic artifacts:

```bash
./scripts/btq collect-action-candidates --channel field_capture
```

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
artifact path, and reviewer metadata when present. Output sorts by
`candidate_id` for deterministic review. Use `--include-source` for longer
source text and context.

Preview candidate collection without writing review artifacts:

```bash
./scripts/btq collect-action-candidates --channel field_capture --dry-run
./scripts/btq collect-action-candidates --channel field_capture --dry-run --json
```

The collection dry-run reads completed semantic artifacts, builds candidate
payloads with the same deterministic IDs as real collection, computes review
artifact paths, detects equivalent existing candidates, and reports
`would_create`, `skipped`, or `failed` results. It does not write pending or
failed candidate artifacts, queue files, or canonical state.

Generate approved drafts from already-approved candidates:

```bash
./scripts/btq generate-approved-drafts --channel field_capture
```

For `field_capture`, approved candidates without an explicit
`proposed_queue_job` default to `append_to_note` drafts for the associated site
note. Site context must resolve from channel metadata, candidate provenance, or
the semantic artifact through the active site registry. If site context is
missing, draft generation fails closed.

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
evidence when visible. Output sorts by `draft_id`. Use `--include-payload` for
the full proposed queue payload and `--include-source` for source context and
provenance details.

Preview approved draft generation without writing draft artifacts:

```bash
./scripts/btq generate-approved-drafts --channel field_capture --dry-run
./scripts/btq generate-approved-drafts --channel field_capture --dry-run --json
```

The draft-generation dry-run skips non-approved candidates, maps approved
candidates through the same draft builder as real generation, detects an
equivalent existing draft by deterministic draft path, and reports
`would_create`, `skipped`, or `failed` results. It does not write approved or
failed draft artifacts, queue files, or canonical state.

Default field-capture drafts append internal-safe, provenance-preserving notes
with available capture timestamp, site ID, area, candidate summary, reviewed
context, capture/audio IDs, and semantic/transcript artifact paths. Draft
generation does not mutate canonical state. Staging is still explicit through
`stage-approved-drafts`, and the queue processor remains the only canonical writer.

Approve or reject exactly one candidate:

```bash
./scripts/btq review-candidate --channel field_capture \
  --candidate-id <ac_...> \
  --status approved \
  --reviewer "Jordan" \
  --rationale "Verified against the field note."
```

Use `--status rejected` to reject a candidate. The command updates only the
matching candidate artifact and records reviewer, timestamp, rationale, prior
status, and review history.

Stage already-approved drafts into the runtime queue:

```bash
./scripts/btq stage-approved-drafts --channel field_capture
```

Preview staging without writing queue or review artifacts:

```bash
./scripts/btq stage-approved-drafts --channel field_capture --dry-run
./scripts/btq stage-approved-drafts --channel field_capture --dry-run --json
```

The staging dry-run validates proposed queue jobs, computes deterministic queue
filenames and job IDs, checks duplicate evidence in `<runtime_root>/queue`,
`<runtime_root>/processed`, `<runtime_root>/failed`, and the processed index,
and reports whether each approved draft would stage, skip, or fail. It does not
write `<runtime_root>/queue` files, staging status artifacts, or canonical state.

Approval rules:

- semantic artifacts and action candidates are review artifacts, not vault
  mutations
- action candidates default to `pending_review`
- production approval is explicit; no automatic approval exists
- `pull-field-capture` is copy-only and imports exactly one exported capture
  bundle without running processors
- imported `photo_capture` metadata lands in `<runtime_root>/field_capture/intake/`,
  not the general executable `<runtime_root>/queue/`
- `review-dashboard` is read-only and only summarizes existing review state
- `review-maintenance-status` is read-only visibility before any future cleanup
  or archive strategy
- `list-action-candidates` is read-only and does not generate drafts or stage
  queue jobs
- `list-approved-drafts` is read-only and does not write staging artifacts or
  queue jobs
- `show-review-item` is read-only and requires exactly one candidate or draft ID
- candidate collection dry-run reports intended review artifacts without
  writing pending or failed candidate artifacts
- only `approved` candidates can become approved drafts
- draft-generation dry-run reports intended draft artifacts without writing
  approved or failed draft artifacts
- only `approved_draft` drafts can be staged into `<runtime_root>/queue/`
- staging dry-run reports intended effects without writing review or queue
  artifacts
- staged queue jobs still do not mutate canonical state until the deterministic queue
  processor runs
- dry-run support is currently implemented for candidate collection, draft
  generation, and staging; candidate review remains an explicit write command

Legacy migration workflow:

1. Historical only: this flow predates CouchDB `job_draft` review and should be
   used only for migration or recovery of old runtime artifacts.
2. Preferred active flow: run `./scripts/btq watch-field-capture-pipeline
   --poll-seconds 60` on the Mac to automate VPS pull through local processing,
   serial photo vision sidecars, and `job_draft` emission.
3. If running manually and the upload landed on the VPS, export one capture bundle and import it on
   the Mac with `./scripts/btq pull-field-capture --capture-id <capture_id>
   --bundle-path <bundle> --dry-run`, then run the same command without
   `--dry-run`.
4. Run `./scripts/btq transcribe-field-audio --json --limit 1`.
5. Run semantic cleanup with `./scripts/btq process-field-audio-semantics
   --json`.
6. Start with `./scripts/btq review-dashboard --channel field_capture` to see
   what needs attention and the suggested next command.
7. Inspect detailed status with `./scripts/btq review-status --channel
   field_capture`.
8. Preview candidate collection with `./scripts/btq collect-action-candidates
   --channel field_capture --dry-run`.
9. Collect candidates with `./scripts/btq collect-action-candidates --channel
   field_capture`.
10. List pending candidates with `./scripts/btq list-action-candidates
   --channel field_capture --status pending_review`.
11. Show full candidate detail with `./scripts/btq show-review-item --channel
   field_capture --candidate-id <ac_...>` if more detail is needed.
12. Approve or reject one candidate with `./scripts/btq review-candidate
   --channel field_capture --candidate-id <ac_...> --status approved
   --reviewer "Jordan" --rationale "..."`, or use the private ops dashboard page
   at `/field-capture/review`. No bulk approval command exists.
13. Preview approved draft generation with `./scripts/btq
   generate-approved-drafts --channel field_capture --dry-run`.
14. Generate approved drafts with `./scripts/btq generate-approved-drafts
   --channel field_capture`.
15. List approved drafts with `./scripts/btq list-approved-drafts --channel
   field_capture`.
16. Show full draft detail with `./scripts/btq show-review-item --channel
   field_capture --draft-id <ajd_...>` if more detail is needed.
17. Preview staging with `./scripts/btq stage-approved-drafts --channel
   field_capture --dry-run`.
18. Run `./scripts/btq stage-approved-drafts --channel field_capture`.
19. Run `./scripts/btq review-dashboard --channel field_capture` or
   `./scripts/btq review-status --channel field_capture` again.
20. Later, run the existing queue watcher or queue processor deliberately.

Troubleshooting review-status gaps:

- `approved_candidate_missing_draft`: generate an approved draft for the
  reviewed candidate or confirm approval was accidental.
- `approved_draft_missing_staging_status`: staging has not been attempted or
  the staging result artifact is missing.
- `staged_draft_missing_queue_processed_failed_evidence`: staging says the
  draft was staged, but no queue, processed, failed, or processed-index evidence
  is visible.
- `queue_file_missing_draft_artifact`: a queue file points to a draft artifact
  that is missing or was moved.
- queue file in `<runtime_root>/failed/`: inspect the failed job JSON and queue
  processor logs; the review pipeline has handed off to deterministic queue
  validation/processing.

Safety boundaries:

- `ops-dashboard` visibility routes are read-only
- `ops-dashboard` review POST routes only approve/reject one pending
  field-capture candidate artifact
- `review-status` is read-only
- `review-dashboard` is read-only
- `review-maintenance-status` is read-only
- `list-action-candidates` is read-only
- `list-approved-drafts` is read-only
- `show-review-item` is read-only
- `pull-field-capture --dry-run` reports bundle import actions without writing
  media or intake artifacts
- `pull-field-capture` does not run transcription, semantic processing, queue
  processing, or canonical mutation
- `collect-action-candidates --dry-run` validates and reports without writing
  pending or failed candidate artifacts
- `review-candidate` updates exactly one candidate and does not generate drafts
- `generate-approved-drafts --dry-run` validates and reports without writing
  approved or failed draft artifacts
- `stage-approved-drafts --dry-run` validates and reports without writing queue
  or staging artifacts
- `stage-approved-drafts` does not run the queue processor
- semantic cleanup, candidate review, draft generation, and staging do not
  mutate canonical state
- the queue processor remains the only canonical writer

## Local Ops Dashboard

Run the local operator dashboard on the processing node:

```bash
./scripts/btq ops-dashboard
```

Local URL:

```text
http://127.0.0.1:8765/
```

### Routes

- `/` renders the Inbox triage hub: pending `job_draft` records, approved
  drafts awaiting queue materialization, failed jobs, failed photo-vision
  sidecars, unknown captures, recent uploads with no draft yet, open site issues,
  open supply needs, and open equipment requests
- `/swipe` renders the fast CouchDB `job_draft` approval surface
- `/health` renders the runtime health dashboard that previously lived at `/`
- `/candidates` renders the CouchDB `job_draft` triage view with filters,
  capture grouping, proposed queue-job payloads, and read-only
  transcript/semantic/photo-vision context
- `/field-capture/review` remains a legacy alias for the Candidates view
- `/captures` renders the read-only capture browser with per-capture detail
  pages that show inline photo-vision cards (preview + AI metadata), other
  uploads, and related review drafts
- `/field-capture/issues` renders structured site issues, detail/source views,
  and queue-job-driven status transition forms
- `/supplies` renders a read-only list of canonical `supply_need` records from
  `btq_vault`. Filters by `?site_id=<id>` and `?status=<open|ordered|...>`
- `/equipment` renders a read-only list of canonical `equipment_request` records
  from `btq_vault`. Filters by `?site_id=<id>` and `?status=<open|approved|...>`
- `/drafts` and `/drafts/stage-preview` are legacy pre-`job_draft` approved-draft
  staging views retained for migration/recovery
- `/failed` renders failed queue jobs and failed photo-vision sidecars with
  constrained runtime artifact links
- `/captures` renders a read-only capture browser with site/date/media/artifact
  filters and capture details
- `/sites` renders the CouchDB site list and one-site edit forms
- `/sites/new` renders the new-site form
- `/tokens` renders field-capture token metadata and one-time issuance reveals
- `/tokens/new` renders the token issuance form
- `/system` renders the system defaults form backed by CouchDB
- `/help` renders the local dashboard operator guide from
  `project/ops_dashboard/HELP.md`
- `/api/status` returns the same core status as JSON
- `/api/inbox.json` returns the same Inbox card counts and top-row links as JSON
- `/healthz` returns a simple health JSON payload
- `/media/*` serves constrained upload media under the configured upload
  directory
- `/runtime-file` serves constrained runtime files under the configured runtime
  root for failed-job and sidecar inspection
- `/static/*` serves local dashboard static assets

The Health page summarizes runtime health, field-capture uploads and intake
records, transcript and semantic artifact counts, field-capture review status,
photo vision sidecar counts/backlog/recent warnings, maintenance findings, and
recent log warnings/errors. The Inbox is the default operator landing page.

### Mutation Surfaces

- `/field-capture/review/approve` approves one CouchDB `job_draft`
- `/field-capture/review/reject` rejects one CouchDB `job_draft`
- `/field-capture/review/approve-set` approves/rejects a checked set of
  CouchDB `job_draft` records
- `/field-capture/review/edit` edits one pending CouchDB `job_draft` payload
- `/field-capture/review/client-informed` mutates one client-notification
  artifact
- `/drafts/generate` and `/drafts/stage` are legacy pre-`job_draft` mutation
  surfaces retained for migration/recovery
- `/supplies/mark-ordered` stages a `mark_supply_ordered` queue job; the queue
  processor advances the matching canonical `supply_need` record from `open` to
  `ordered`
- `/supplies/mark-delivered` stages a `mark_supply_delivered` queue job;
  `ordered` to `delivered`
- `/supplies/mark-stocked` stages a `mark_supply_stocked` queue job;
  `delivered` to `stocked`
- `/supplies/mark-no-action-needed` stages a
  `mark_supply_no_action_needed` queue job; any non-terminal supply status to
  `no_action_needed`
- `/equipment/mark-approved` stages a `mark_equipment_approved` queue job;
  `open` to `approved`
- `/equipment/mark-denied` stages a `mark_equipment_denied` queue job; `open`
  or `approved` to `denied`
- `/equipment/mark-ordered` stages a `mark_equipment_ordered` queue job;
  `approved` to `ordered`
- `/equipment/mark-provided` stages a `mark_equipment_provided` queue job;
  `ordered` to `provided`
- `/equipment/mark-no-action-needed` stages a
  `mark_equipment_no_action_needed` queue job; any non-terminal equipment
  status to `no_action_needed`
- `/field-capture/issues/mark-monitoring` stages a
  `mark_issue_monitoring` queue job; `open` to `monitoring`
- `/field-capture/issues/mark-resolved` stages a
  `mark_issue_resolved` queue job; `open` or `monitoring` to `resolved`
- `/field-capture/issues/reopen` stages a `mark_issue_open` queue job;
  `monitoring` or `resolved` to `open`
- `/failed/retry-sidecar` mutates one retry-intent file consumed by
  `pipeline_watcher`
- `/sites/save` mutates one `btq_sites` document
- `/sites/new` mutates one `btq_sites` document
- `/tokens/new` mutates one row in the field-capture token SQLite store
- `/tokens/revoke` mutates one row in the same store by marking it revoked
- `/system/save` mutates one `system_defaults` CouchDB document

Supply and equipment transitions use a two-step browser pattern: a detail page
links to a confirmation page, then the POST writes one queue file under
`<runtime_root>/queue/` and appends one audit line. The dashboard stages the
job only; the queue processor and handlers perform the deterministic canonical
CouchDB mutation.

### Safety Boundaries

Every POST appends one JSON line to:

```text
<runtime_root>/logs/admin_audit.log
```

- Localhost and Tailscale only; no public exposure.
- Every POST is single-action; there is no bulk mutation UI.
- Every POST writes exactly one audit-log line at
  `<runtime_root>/logs/admin_audit.log`.
- Raw token values are never logged.
- The dashboard does not write canonical state; the queue processor remains the
  only canonical writer.
- The dashboard does not invoke the queue processor.
- The dashboard does not invoke transcription, semantic cleanup, or
  photo-vision runs. Vision retries write an intent file consumed by the
  existing pipeline watcher.
- The dashboard does not change the SPA; Stage G1 site-aware SPA tuning is
  explicitly deferred.
- The dashboard adds no CLI flags.
- The dashboard adds no authentication layer.

### Forward-Compat Fields

Sites accepts `capture_guidance` and `display_categories` fields. The System
defaults page edits the same shape for pipeline-wide defaults. Both are present
for a future Stage G1 SPA wiring prompt.

By default the dashboard binds to localhost. For Tailscale access, prefer
running `scripts/btq-setup-tailscale-serve` on the host once. It fronts the
local dashboard with HTTPS via Tailscale Serve, using the tailnet's Let's
Encrypt cert. After setup:

```bash
./scripts/btq ops-dashboard --host 127.0.0.1 --port 8765
```

Tailnet HTTPS URL (after `btq-setup-tailscale-serve`):

```text
https://workstation.example.ts.net/
```

Tailnet JSON status URL:

```text
https://workstation.example.ts.net/api/status
```

The HTTPS surface is required for `MediaRecorder` (the Capture Observation
card's tape-deck recorder) because `navigator.mediaDevices` is gated to secure
contexts. Plain private-network dashboard URLs still work for read-only use but
recording will be disabled.

Do not bind this dashboard to a public interface. V1 has no in-app
authentication; the Tailscale Serve front is tailnet-gated, which is the
security boundary.

### After rotating the CouchDB admin password

Run on the host once:

```bash
./scripts/btq-rotate-replicator-auth
```

It refreshes the embedded `Basic <base64>` Authorization headers on every
`_replicator` doc whose source or target carries one. Without this step, the
VPS→Pro replicators crash with `replication_auth_error` and the field-capture
pipeline silently stalls. See the 2026-05-26 incident under
`ai-methodology:projects/btq/inbox/archived/2026-05-26_incident_silent-watcher-outage.md`.

### CouchDB database topology

Every BTQ CouchDB node must have the full database set:

- `btq_field_captures`
- `btq_photo_vision`
- `btq_queue`
- `btq_sites`
- `btq_vault`
- `btq_voice_memos`

The queue processor writes canonical operational state to `btq_vault`; if one
node lacks that database or lacks the `location_*`/`employee_*` docs, fail-closed
jobs will reject instead of mutating projection-only files. That is intentional,
but it means Pro, VPS, and Dell must be provisioned as database peers before any
watcher points at them.
Existing `location` and `employee` docs are never overwritten by migration; the dashboard is the authoritative source for these types.

Run setup on each CouchDB host, then configure mesh replication from the Pro
checkout with Pro/VPS credentials and optional Dell credentials:

```bash
BTQ_PRO_COUCHDB_URL=http://127.0.0.1:5984 \
BTQ_PRO_COUCHDB_USER=... \
BTQ_PRO_COUCHDB_PASSWORD=... \
BTQ_VPS_COUCHDB_URL=http://203.0.113.10:5984 \
BTQ_VPS_COUCHDB_USER=... \
BTQ_VPS_COUCHDB_PASSWORD=... \
BTQ_DELL_COUCHDB_URL=http://10.0.0.10:5984 \
BTQ_DELL_COUCHDB_USER=... \
BTQ_DELL_COUCHDB_PASSWORD=... \
./scripts/btq setup-couchdb --with-replication --skip-migrate
```

Omit the three `BTQ_DELL_*` variables until the Dell CouchDB instance exists.
After any topology or credential change, check `/health/pipeline` and the
`_replicator` docs before starting an additional queue watcher.

### Repairing malformed CouchDB scalar fields

If `_string()` rejects a field as multi-element list or you spot bracketed
Python repr in vault projections, run:

```bash
./scripts/btq-migrate-malformed-scalars --discover
```

This produces `proposed_fixes.json`. Review it, then either run
`./scripts/btq-migrate-malformed-scalars --apply proposed_fixes.json` to apply
the auto-fixable changes, or hand-fix the `manual_review` entries via CouchDB
Fauxton.

The script is idempotent. Re-running `--apply` on the same proposal after a
clean run is a no-op.

This is a successor to `_string()`'s defensive rendering (per
`ai-methodology:projects/btq/108_string_coercion_hardening/`). The renderer
suppresses the symptom; this script fixes the underlying data.

### Where canonical writes land

Canonical queue writes land in the CouchDB `btq_vault` database as typed
documents: `location` updates (issues, supplies, equipment, recruiting state),
`visit` and `visit_gap` records, `journal` and `unknown_capture` entries, and
`employee` documents. There is no markdown projection; CouchDB is the sole
source of truth.

### Personal journal mode

To keep a voice memo separate from operational memory, begin the memo with one of these explicit triggers:

- `personal journal`
- `this is a personal journal`
- `personal note`

The transcription pipeline strips that trigger from the stored body, preserves the raw transcript path plus source audio filename and timestamp, and stages a `personal_journal_entry` job. The queue watcher writes it to the separate personal journal store, isolated from operational state.

Personal journal jobs do not run through operational event extraction and do not feed shift reports, account journals, staffing events, unknown captures, or nightly ops digest. This first pass is separation only; it does not perform semantic analysis, tagging, summaries, mood analysis, or personal insight generation.

## Site Routing Lifecycle

### What runtime routing uses

Current runtime routing uses the active site registry through
[event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py).
When `BTQ_COUCHDB_URL` is set, `sites.py` reads the CouchDB `btq_sites`
registry. When that environment variable is unset, local/dev routing falls back
to the checked-in hardcoded registry.

`project/event_pipeline/couchdb/push_design_doc.py` provisions both supported
design documents by default: `btq_sites` for site registry lookups and
`btq_field_captures` for field-capture viewer/media lookups.

The active registry currently provides:

- canonical site name
- string `site_id`
- alias list used by transcript site resolution

Runtime consequence:

- a `location` document can already exist in `btq_vault`, but runtime event routing will not use it unless the site is also present in the active registry
- if CouchDB is configured but unavailable, runtime site routing fails closed
  and logs `CouchDB site registry unavailable; failing closed`; do not edit
  `sites.py` as an operational workaround

### What makes a site routable

A site is only fully routable when all of these are true:

1. a valid canonical `location` document exists for the site
2. the site appears in the active runtime registry
3. transcript text can resolve to that canonical site through the registry aliases or matching logic

### Nightly refresh behavior

I did not find a checked-in nightly site-refresh implementation in this repository.

That means this repository shows:

- the runtime registry consumer
- the queue-processor side site validation logic

It does not show code that rebuilds the active `btq_sites` registry on a
schedule. Site seed/migration code writes CouchDB documents with IDs like
`site_<site_id>`, for example `site_7050`, including aliases and optional vision
context.

If your deployed environment has a nightly process that refreshes the registry, treat that as an external operational step not represented in the current checked-in code.

### Transition state for new sites

A newly created site can be in one of these states:

1. canonical `location` document present but structurally invalid
2. structurally valid `location` document but absent from the active runtime registry
3. present in the active runtime registry but not yet resolved by spoken aliases
4. fully routable

The most common transition problem is state 2:

- the `location` document exists
- its fields are good enough
- but runtime processing still treats the site as `unknown` because the active registry has not been refreshed yet

## C. Handling Failures

### If transcription fails

Check:

1. `./scripts/btq-verify-environment`
2. `ffmpeg` availability
3. whether `torch` and `whisper` import from `project/.venv`
4. transcription logs under `local/logs/`

Useful files:

- `local/logs/transcription_pipeline.log`
- `local/logs/<audio-stem>.<timestamp>.<suffix>.json`

Typical causes:

- missing `ffmpeg`
- missing Python dependency
- inbox/archive path misconfiguration or unreachable CouchDB
- audio file still changing and not yet considered stable

### If no events are generated

Check:

1. the normalized transcript in `local/audio_processing/`
2. `local/events_raw/`, `local/events_valid/`, and `local/events_failed/`
3. whether the transcript actually contains a resolvable site or supported event language

Important current behavior:

- no valid events is not always an error
- the pipeline may instead create an `unknown_capture`
- partial extraction can also create an unknown capture alongside valid events
- if a `location` document exists but the site is not yet in the active runtime registry, extraction may still resolve the site as `unknown`

### If queue jobs are not processed

Check:

1. `<runtime_root>/queue/`
2. `<runtime_root>/processed/`
3. `<runtime_root>/failed/`
4. queue processor logs under `<runtime_root>/logs/`
5. `./scripts/btq-verify-environment` for `queue_backlog`

The `queue_backlog` check reports queued job count, oldest queued job age, recent queue watcher log activity, and recent processed/failed queue activity. A failure means jobs are queued but no recent watcher or processing activity was detected.

Useful commands:

```bash
./scripts/btq process-durable-queue
./scripts/btq-run
./scripts/btq-dry
cd project && .venv/bin/python -m queue_processor.watch --once
```

Use `./scripts/btq process-durable-queue` when you want the narrowest supported
local queue pass. It processes only files already present in
`<runtime_root>/queue/`, uses the normal deterministic queue processor lock,
validation, idempotency, evidence, processed-index, and processed/failed
movement, and skips unknown reclassification by default. It does not stage
`<pipeline_dir>/outbox/*.json`, does not process watcher working/nightly
triggers, and does not touch iCloud transport backlog. Use `--no-skip-unknowns`
only when you intentionally want the processor to scan and reclassify
unresolved `unknown_capture` records after the queue pass.

Typical causes:

- invalid queue payload rejected by `queue_spec.py`
- missing or malformed `location` document
- missing `employee` document for employee-targeted jobs
- duplicate job ID already processed
- queue watcher not running
- a `location` document exists but the site is not present in the active runtime registry

### If a valid new site is still not routing

Check in this order:

1. confirm a canonical `location` document exists for the site
2. confirm the `location` document carries `type: location` and either `job` or `site_id`
3. check whether the site exists in the active `btq_sites` registry (or [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py) in local/dev fallback)
4. confirm the spoken or typed site reference matches a canonical name or alias in that registry
5. if your environment uses an external registry refresh, verify that it has actually run and updated the runtime registry used by the current process

Important distinction:

- a structurally valid `location` document is not the same thing as a currently routable site
- the queue processor can validate site IDs from the canonical store, but event routing still depends on the active runtime registry

### Troubleshooting stale registry behavior

Symptoms of stale registry behavior:

- a new `location` document exists
- queue-processor validation finds other existing sites correctly
- transcript events for the new site still route to `unknown`
- `site_observation` falls back to an `unknown_capture` record instead of a `location` document

What the repository can confirm:

- whether the site is present in the checked-in runtime registry
- whether the `location` document is structurally valid enough for queue-processor side validation

What this repository cannot confirm from code:

- whether an external nightly refresh is configured in production
- how that external refresh updates the active registry
- whether the currently running process has picked up a refreshed registry after such a run

### How to inspect logs

Transcription logs:

- `local/logs/transcription_pipeline.log`
- `local/logs/<audio-stem>.<timestamp>.<suffix>.json`
- `local/logs/queue_watch.log`

Queue processor logs:

- `logs/run-<timestamp>.log`

Launch-agent stdout/stderr paths are also configured in [config.json](/Users/operator/btq/config.json).

## D. Unknown Captures

### What they mean

Unknown capture means:

- the transcript did not produce supported valid events
- or the transcript was only partially classified and some content was preserved for later review

Unknown is not the same as failure.

### How they are stored

Unknown captures are stored as canonical `unknown_capture` documents in
`btq_vault`. Each record carries:

- `type: unknown_capture`
- `timestamp`
- `audio_file`
- `status`
- `retry_count`
- `last_attempted`

The record body keeps:

- original transcript
- normalized transcript
- notes for human review

### How they are reprocessed or resolved

The queue processor scans unresolved unknowns after the normal queue pass.

It will attempt reclassification when:

- the record was updated after the original capture timestamp
- the notes/body now contain a resolvable site alias
- the notes/body contain `#site:`
- retry backoff allows another attempt

Reclassification does not rerun Whisper. It reuses the stored normalized transcript plus any user-added text.

If reclassification succeeds:

- standard queue jobs are created
- the unknown entry is marked `resolved`
- `resolved_at` and `resolved_site` are written

If it fails:

- the entry stays `unresolved`
- `retry_count` and `last_attempted` are still updated

## E. Safe Reprocessing

### How to rerun pipeline steps without duplicating data

Single-pass rerun:

```bash
cd project
.venv/bin/python -m transcription_pipeline.main --once
```

Queue-only rerun:

```bash
./scripts/btq process-durable-queue
```

Transport-aware queue rerun:

```bash
./scripts/btq-run
```

`./scripts/btq-run` stages outbox transport before processing. Do not use it
when old iCloud pending/retry backlog should remain untouched.

Dry-run queue inspection:

```bash
./scripts/btq-dry
```

### How idempotency is enforced

Current idempotency protections:

- the transcription pipeline claims audio locally, archives successful audio under completed storage, and moves failed audio under failed storage
- `event_to_queue` skips existing `job_<event_id>.json` files
- queue processor skips already processed `job_id`s and records `btq_job_ids`
  on canonical CouchDB documents
- canonical handlers skip already-applied job markers
- `visit_create` skips duplicate evidence already present in the canonical
  visit document
- `visit_gap` is only appended once per site/date block
- unknown reclassification adds `source_unknown_id` and skips duplicate derived jobs

Practical rule:

- rerunning the pipeline is usually safe when the source file and queue files are unchanged
- editing canonical documents directly can affect unknown reclassification eligibility and visit-gap behavior

### When to be careful

Be careful when:

- manually editing `location` document fields
- renaming or re-keying sites without updating the site registry
- editing `unknown_capture` records in ways that break their structure
- deleting `processed/` queue files or local completed/failed runtime files

Those actions can change how the processor resolves entities or whether it thinks work has already been handled.

## F. Nightly Digest

The current system can now build a nightly digest on demand, but it does not yet generate one automatically.

If you want one nightly review artifact that shows:

- what happened today
- what executed successfully
- what failed
- what remained unresolved
- what patterns suggest a new queue job type would help

use:

- [nightly_digest.md](/Users/operator/btq/project/docs/nightly_digest.md)
- `./scripts/btq-build-nightly-digest`
- or create `nightly-digest-YYYY-MM-DD.trigger` in the configured runtime `working_dir` and let the queue watcher build it automatically

Default output:

- `Journal/YYYY-MM-DD-digest.md`

The builder reads artifacts the system already writes today and creates a separate digest file without changing the daily journal itself.
