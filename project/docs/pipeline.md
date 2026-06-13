# Pipeline

## 1. Overview

The current pipeline starts with field inputs and ends with approved mutation jobs written to canonical CouchDB state by the queue processor and handlers.

iCloud is treated as a transport/drop layer only. Watchers may observe configured iCloud ingress directories, but runtime processing must claim files and move them into local non-iCloud storage before transcription, queue validation, or other processing begins. This avoids reading or mutating files while iCloud Drive is still syncing, locking, evicting, or hydrating file contents.

Flow:

`field-capture PWA / voice-memo PWA / voice transcription`  
`-> capture_ingest`  
`-> raw preservation + metadata/sidecars`  
`-> local runtime processing`  
`-> semantic layer`  
`-> action candidates/drafts`  
`-> approved mutation job`  
`-> queue_processor / handlers`  
`-> CouchDB canonical state + evidence`

At runtime, `transcription_pipeline/main.py` coordinates the full flow:

1. Watch the iCloud inbox for stable audio files.
2. Claim and move each ready file into local runtime storage.
3. Transcribe each local file with Whisper into a `.whisper.txt` file.
4. Normalize domain language and write a `.normalized.txt` transcript plus `.corrections.json`.
5. Extract raw events, enrich them, and validate them.
6. Convert valid events into queue job JSON files.
7. Optionally emit a structured missed-capture `append_to_note` job when nothing was extracted or extraction was partial.
8. Stage queue jobs atomically into the runtime queue.
9. Leave runtime queue draining, canonical CouchDB writes, and unknown reclassification to `queue_processor.watch`.
10. Move the local audio into completed storage after transcription-side staging succeeds.

## 2. Entry Point

Entry point: [transcription_pipeline/main.py](/Users/operator/btq/project/transcription_pipeline/main.py)

Default paths:

- iCloud inbox: `~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/inbox/voice`
- local runtime root: `~/btq_runtime`
- local audio archive: `~/btq_runtime/completed/audio`
- local root: `/Users/operator/btq/local`
- local audio staging: `/Users/operator/btq/local/audio_processing`
- local queue jobs: `/Users/operator/btq/local/queue_jobs`

File stability detection:

- Only `.m4a`, `.mp3`, and `.wav` files are considered.
- A file is considered ready when its last modification time is at least `stable_seconds` old. The default is 10 seconds.
- `scan_once()` skips files that are still changing.

Per-file processing flow:

1. Claim the stable ingress file through a temporary `.claiming` path.
2. Move the claimed file into local runtime storage.
3. Transcribe the local audio file into `<audio>.<ext>.whisper.txt`.
4. Run `process_transcript()` from the event pipeline.
5. Convert valid events into queue jobs.
6. Emit a missed-capture journal job if needed.
7. Stage the generated jobs into `<runtime_root>/queue/`.
8. Write a per-audio process log under
   `local/logs/<audio-stem>.<timestamp>.<suffix>.json`.
9. Move the processed audio to `<runtime_root>/completed/audio/`.

## 3. Transcription

Whisper usage:

- `build_transcriber()` imports `whisper` and `torch`, loads the configured Whisper model, and calls `model.transcribe(...)`.
- The enhanced transcription path loads `large-v3`.
- The enhanced profile currently uses:
  - `beam_size=5`
  - `best_of=5`
  - `temperature=0.0`
  - `language="en"`
  - `condition_on_previous_text=True`
  - `patience=2.0`
  - `word_timestamps=True`
- A default `initial_prompt` is passed with known domain terms and names.
- `fp16` is enabled only when CUDA is available.
- If `compare_mode` is enabled, the transcriber also runs a baseline pass using the requested CLI model, writes comparison artifacts under `local/transcripts/YYYY-MM-DD/<audio-stem>/`, and still returns the enhanced transcript to the rest of the pipeline.

Output files:

- Raw transcript: `<audio>.<ext>.whisper.txt`
- Normalized transcript: `<audio>.<ext>.whisper.normalized.txt`
- Normalization sidecar: `<audio>.<ext>.whisper.corrections.json`

The raw Whisper text is written first. The normalized transcript is then generated inside `event_pipeline/main.py` by `normalize_text(...)` from `domain_resolver.py`.

## 4. Domain Language Resolver

Module: [event_pipeline/domain_resolver.py](/Users/operator/btq/project/event_pipeline/domain_resolver.py)

This module applies rule-based terminology normalization before event extraction.

Canonical term mapping currently includes:

- `VCT`
- `LVP`
- `BCT`

Implemented mappings:

- `vinyl composition tile` -> `VCT`
- `vct` and spaced variants such as `v c t` -> `VCT`
- `vinyl tile` -> `VCT`
- `luxury vinyl plank` -> `LVP`
- `vinyl plank` -> `LVP`
- `lvp` -> `LVP`

Context-aware correction:

- `bct` is converted to `VCT` when nearby context contains flooring terms such as `tile`, `tiles`, `floor`, `floors`, or `flooring`.
- This is implemented by scanning a small context window around each `bct` match.

Dual output:

- The original Whisper transcript is preserved in `.whisper.txt`.
- The normalized transcript is written to `.normalized.txt`.
- A correction list is written to `.corrections.json`.
- The transcription process log also records `domain_corrections`.

## 5. Event Pipeline

Modules:

- [event_pipeline/extractor.py](/Users/operator/btq/project/event_pipeline/extractor.py)
- [event_pipeline/enricher.py](/Users/operator/btq/project/event_pipeline/enricher.py)
- [event_pipeline/validator.py](/Users/operator/btq/project/event_pipeline/validator.py)

Orchestration:

- `event_pipeline/main.py` reads the normalized transcript.
- Raw events are written to `events_raw/`.
- Enriched events are written to `events_enriched/`.
- Valid events are written to `events_valid/`.
- Failed events are written to `events_failed/`.

Supported event types in the current extractor:

- `staffing_risk`
- `employee_retention_risk`
- `access_constraint`
- `site_observation`
- `employee_onboarding`
- `interview_note`

The current schema also allows:

- `employee_callout`
- `employee_resigned`
- `incident`

The fallback extractor is rule-based and sentence-oriented. It can emit multiple events from one transcript and multiple events from one sentence.

Multi-event extraction:

- Badge, key, and dependency statements can produce separate `access_constraint` events.
- Mixed spoken input can produce `access_constraint`, `site_observation`, and `employee_retention_risk` events from the same transcript.
- `employee_onboarding` is emitted when onboarding/training phrases are present.
- `interview_note` is emitted when the transcript contains explicit interview or call-interaction context plus observation-only artifacts such as unsolicited disclaimers or explicit call-cutoff statements.

Tunable extraction terms:

- The phrase vocabulary that drives the extractor and the visit classifiers
  lives in
  [event_pipeline/extraction_terms.yaml](/Users/operator/btq/project/event_pipeline/extraction_terms.yaml),
  loaded and validated by `event_pipeline/extraction_terms.py`.
- `global:` defines system-wide named phrase lists. `extractor.py` and
  `field_capture/audio_semantics.py` match transcript text against them.
- `sites.<site_id>:` adjusts those lists per site: `extend` appends phrases,
  `remove` drops phrases, `replace` swaps a list entirely (`[]` disables it).
- The empty `site_obs_extra` and `access_extra` lists are per-site extension
  points — extend them to add new site-specific terms with no code change.
- The file controls only *which phrases* trigger each detector. Event
  `category`, `confidence`, and emitted detail text remain in `extractor.py`.
- Per-site lookups key off the resolved `site_id`; a list with no site
  override falls back to its global definition.

Observation handling:

- Events may now include an optional `observations` field.
- `observations` is a list of objects with:
  - `type`: string
  - `confidence`: fixed literal `observed`
- Observations preserve raw interaction context for journals only.
- Observations are not structured facts.
- Observations must describe what was said or what happened in the interaction, not an interpretation.
- Examples:
  - `Candidate volunteered denial of drug and alcohol issues without prompting`
  - `Phone battery died mid-call and candidate said he would call back`

Consolidation behavior in `enricher.py`:

- `aggregate_staffing_risk_events()` merges `staffing_risk` events by site.
- `open_positions` values are summed across merged staffing events.
- Highest severity wins, with `critical` forced when merged `open_positions >= 2`.
- `consolidate_site_observations()` groups `site_observation` events by `(site, category)`.
- Material observations are further bucketed into `flooring`, `surface`, or `general`.
- Consolidated events combine details and source excerpts into one event record.

Validation rules in `validator.py`:

- Every event must be a JSON object.
- Required fields must be present.
- Extra fields are rejected unless explicitly allowed.
- `type` must be one of `ALLOWED_EVENT_TYPES`.
- `confidence` must be `high`, `medium`, or `low`.
- `details` and `site` must be non-empty strings.
- `blocking` must be boolean when present.
- `severity` must be one of `low`, `medium`, `high`, `critical`.
- `open_positions` must be a positive integer when present.
- `site_observation.category` must be `condition`, `material`, or `layout`.
- `observations` must be a list when present.
- Each observation must contain only:
  - `type`
  - `confidence`
- `observation.confidence` must always be `observed`.

## 6. Site Resolution

Module: [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py)

Site detection is alias-based.

Current registry entries include:

- Lakeshore Community Health
- Western Gas Transmission
- Summit Wire
- Glenwood Elementary School
- Glenwood High School

Resolution behavior:

- Exact alias match returns the canonical site with `high` confidence.
- Substring match returns the canonical site with `medium` confidence.
- Word-overlap ratio of at least `0.7` also returns the canonical site with `medium` confidence.
- Otherwise the site resolves to `"unknown"` with `low` confidence.

Current-site inheritance:

- `fallback_extract_events()` tracks `current_site` while iterating sentences.
- Once a sentence resolves a site, following sentences inherit that `current_site` until another site is detected.

Last-known-site fallback:

- `event_pipeline/main.py` writes `state/last_site.json` when a valid event has a known site and confidence `medium` or `high`.
- `extractor.py` loads that file only if it is not older than 2 hours.
- The fallback is only used when the current transcript has no explicit site at all.

Fallback behavior:

- If no alias match or inherited site is available, the site is `"unknown"`.

## 7. Event -> Queue Mapping

Module: [event_to_queue/adapter.py](/Users/operator/btq/project/event_to_queue/adapter.py)

Mapping from event types to job types:

- `staffing_risk` -> `trigger_recruiting`
- `employee_retention_risk` -> `flag_retention_risk`
- `access_constraint` -> `flag_access_constraint`
- `employee_resigned` -> `remove_from_schedule`
- explicit structured person creation -> `add_person`
- `site_observation` -> `append_to_note`
- `employee_callout` -> `append_to_note`
- `incident` -> `append_to_note`
- `interview_note` -> `append_to_note`

Routing rules:

- `site_observation` with a known site routes to that site's `location` document via the site registry.
- `site_observation` with `site == "unknown"` routes to an `unknown_capture` record.
- `employee_callout`, `incident`, and `interview_note` route to the dated `journal` entry.
- onboarding or new-person requests must not route through `append_to_note`; they require `add_person` with enough structured fields or remain unqueued.
- Action-oriented events map to queue jobs for later interpretation by `queue_processor`.

Journal rendering rules:

- Journal-bound events use factual `details` as the primary line.
- If the event includes `observations`, they are rendered in a dedicated section:
  - `**Observations:**`
  - `- <text>`
- This framing is deliberate and marks the line as observed interaction context rather than structured fact.
- The queue contract does not expose a separate `observation` job field. Observations are carried inside event data and rendered only when the destination is the journal.

Idempotency:

- `event_to_queue/main.py` writes job files as `job_<event_id>.json`.
- If that file already exists in `queue_jobs`, conversion skips it.

## 8. Queue Spec (Single Source of Truth)

Module: [queue_spec.py](/Users/operator/btq/project/queue_spec.py)

Job types:

- `append_to_note`
- `trigger_recruiting`
- `remove_from_schedule`
- `flag_access_constraint`
- `flag_retention_risk`
- `add_person`
- `reclassify_unknown`
- `visit_create`
- `parse_supply_email`
- `personal_journal_entry`
- `photo_capture`

`JOB_SCHEMAS` defines the required payload keys for each job type:

- `append_to_note`: `path`, `content`, `destination`
- `trigger_recruiting`: `site`, `priority`, `details`
- `remove_from_schedule`: `employee`, `site`
- `flag_access_constraint`: `site`, `details`
- `flag_retention_risk`: `employee`, `site`, `details`
- `add_person`: `name`, `role`
- `reclassify_unknown`: `path`
- `visit_create`: `site`, `confidence`, `source`, `evidence`
- `parse_supply_email`: `html_path`, `subject`, `source_email_date`
- `personal_journal_entry`: `date`, `timestamp`, `audio_file`, `body`, `raw_transcript_path`
- `photo_capture`: `site`, `qc_category`, `note`, `photos`, `captured_at`, `exported_at`

`validate_job(job)`:

- Rejects non-dict jobs.
- Rejects unknown `job_type` values.
- Rejects non-dict payloads.
- Requires all fields listed in `JOB_SCHEMAS`.
- For `append_to_note`, requires `destination` to be one of:
  - `missed`
  - `journal_unknown`
  - `site_note`
  - `journal`
- For `visit_create`, requires:
  - `confidence` to be `high` or `medium`
  - `site`, `source`, and `evidence` to be non-empty strings

THIS IS THE ONLY JOB CONTRACT IN THE SYSTEM.

`queue_processor` calls `validate_job(...)` before it executes any queue file.

## 9. Queue Processor

Module: [queue_processor/main.py](/Users/operator/btq/project/queue_processor/main.py)

Queue loading:

- `process_all(...)` reads jobs from `<runtime_root>/queue`.
- In the transcription flow, jobs are staged atomically into `<runtime_root>/queue/`; `queue_processor.watch` is the queue-draining owner.
- Processed jobs are moved to `<runtime_root>/processed`.
- Failed jobs are moved to `<runtime_root>/failed`.
- The processor refuses runtime roots inside the iCloud `pipeline_dir` or another iCloud-managed path.

Validation:

- `load_job(...)` reads JSON, assigns `job_id` from the payload or filename stem, and calls `validate_job(...)` from `queue_spec.py`.

Execution behavior:

- `append_to_note` / unknown-capture handlers record canonical unknown-capture state and evidence in `btq_vault`.
- Site operational jobs such as `flag_access_constraint` and `trigger_recruiting` resolve canonical site targets and write CouchDB `btq_vault` documents through handlers.
- People and staffing jobs such as `add_person`, `remove_from_schedule`, and `flag_retention_risk` update canonical person/personnel-event documents; duplicate employee ID or normalized name collisions fail safely against CouchDB employee docs.
- `reclassify_unknown` reuses stored normalized transcript and notes/evidence to produce reviewed queue jobs.
- `visit_create` writes the canonical visit entity and evidence.
- `parse_supply_email` parses a source HTML supply email, resolves the site, and persists canonical supply/equipment records.
- `personal_journal_entry` writes through the personal journal store.
- `photo_capture` preserves media evidence and writes canonical capture/visit-related state, linked from a canonical journal entry.

Unknown reclassification:

- After the normal queue loop finishes, `process_all(...)` calls `process_unknowns(...)`.
- `process_unknowns(...)` scans `Journal/*-unknown.md` for unresolved `unknown_capture` entries.
- An entry is eligible when `should_reclassify(...)` or `should_retry(...)` returns true.
- `should_reclassify(...)` uses:
  - file modification time newer than the entry timestamp
  - known site aliases in the entry body
  - explicit `#site:` text in the entry body
- `should_retry(...)` uses retry/backoff on unresolved entries:
  - max 3 retries
  - first retry with no prior attempt is allowed
  - exponential backoff of 1 hour, 2 hours, then 4 hours based on `retry_count`

Reclassification behavior:

- `reclassify_unknown(...)` does not rerun Whisper or the domain resolver.
- It reuses the stored normalized transcript plus any user-added notes text.
- It runs `extract_events(...)`, `enrich_events(...)`, and `validate_events(...)`.
- If valid events are produced, they are mapped through `event_to_queue.adapter`.
- Derived queue jobs include `source_unknown_id` in the payload for traceability and idempotency checks.
- The source unknown entry is marked `resolved` with `resolved_at` and `resolved_site`.
- If no valid events are produced, the entry stays `unresolved` and only `retry_count` / `last_attempted` are updated.

Additional processor behavior:

- `append_to_note` preserves provided content and does not interpret an `**Observations:**` section or its bullet items as structured metadata.
- Observation text is therefore journal-visible but has no effect on People files, site files, state reconstruction, hiring workflows, scheduling, or queue-derived automation.

- Processed job IDs are checked by scanning files already present in `processed/`.
- Duplicate processed `job_id` values are skipped.
- Canonical handlers apply idempotency markers before mutating CouchDB state.

## 10. Canonical Write Behavior

All canonical operational writes in this pipeline go through `queue_processor` and its handlers.

There are no direct canonical writes in:

- `transcription_pipeline`
- `event_pipeline`
- `event_to_queue`

Current canonical destinations written by queue jobs:

- CouchDB `btq_vault` documents for sites, people, visits, personnel events, unknown captures, supply/equipment records, and other operational entities.
- Associated evidence references preserved with the canonical documents.

CouchDB `btq_vault` is the sole source of truth; there is no Markdown projection.

## 11. Missed / Unknown Capture

Missed capture is created in `transcription_pipeline/main.py`.

When it happens:

- `events_created == 0`
- Partial extraction, where some significant transcript sentences do not match any event `source_excerpt`

Behavior:

- The pipeline creates an `append_to_note` / unknown-capture job named `job_missed_<audio-file>.json`.
- The canonical destination is a CouchDB `btq_vault` `unknown_capture` document with evidence.

Current unknown entry format:

- Frontmatter fields include:
  - `type: unknown_capture`
  - `timestamp`
  - `audio_file`
  - `status`
  - `retry_count`
  - `last_attempted`
  - `events_created`
  - `capture_status`
  - `reason_heading`
  - `reasons`
- Body sections include:
  - `## Original Transcript`
  - `## Normalized Transcript`
  - `## Notes`

Status currently used:

- `#unknown #needs-review` when no events were extracted
- `#partial #needs-review` when extraction was partial

Unknown lifecycle:

- New entries start with `status: unresolved`.
- `retry_count` starts at `0`.
- `last_attempted` starts as `null`.
- On reclassification attempt, `last_attempted` is updated and `retry_count` is incremented before extraction runs.
- On successful reclassification, the entry is updated with:
  - `status: resolved`
  - `resolved_at`
  - `resolved_site`
- The body also gets a resolution marker:
  - `RESOLUTION: Reclassified and routed to structured events.`

Important meaning:

- `unknown` is not failure.
- It means the data was captured but has not yet been fully classified into a known event or site route.
- Unknown entries are now reprocessable, not terminal.

## 12. Idempotency

Current idempotency mechanisms:

- local completed/failed audio placement in the transcription pipeline
- queue job file existence checks in `event_to_queue`
- processed `job_id` checks plus canonical idempotency markers in `queue_processor`
- `source_unknown_id` checks for second-pass unknown reclassification
- duplicate content checks for `append_to_note`

Per-audio claim and cleanup:

- Stable ingress audio is moved through `<runtime_root>/claimed/audio/` before processing.
- Whisper reads from local storage, not the iCloud inbox.
- Successful audio is moved to `<runtime_root>/completed/audio/`.
- Failed audio is moved to `<runtime_root>/failed/audio/` for manual inspection.

Queue job file existence checks:

- `convert_event_paths(...)` skips writing `job_<event_id>.json` if it already exists in `queue_jobs`.
- Missed-capture jobs also skip creation if `job_missed_<audio-file>.json` already exists.
- Runtime staging into `<runtime_root>/queue/` uses temp-file staging plus atomic replace, and skips files already staged.
- Reclassified unknown jobs include `source_unknown_id`, and `queue_processor` skips re-emitting jobs when that source has already produced queue, processed, or failed jobs.
- canonical handlers skip when the job marker already exists on the target document.

Reprocessing behavior:

- If the source audio file changes size or `mtime_ns`, the transcription pipeline treats it as a new version and processes it again.
- If a queue file with the same `job_id` is already in `processed/`, `queue_processor` skips it.
- Unresolved unknown entries may be retried automatically over time even without human edits, subject to retry/backoff limits.

## 13. Directory Structure

Key paths in the current system:

- iCloud inbox: `~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/inbox/voice`
- iCloud outbox ingress: `~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/outbox`
- local runtime root: `~/btq_runtime`
- local root: `/Users/operator/btq/local`
- local audio staging: `/Users/operator/btq/local/audio_processing`
- local raw events: `/Users/operator/btq/local/events_raw`
- local enriched events: `/Users/operator/btq/local/events_enriched`
- local valid events: `/Users/operator/btq/local/events_valid`
- local failed events: `/Users/operator/btq/local/events_failed`
- local queue jobs: `/Users/operator/btq/local/queue_jobs`
- runtime claimed files: `/Users/operator/btq_runtime/claimed`
- runtime processing files: `/Users/operator/btq_runtime/processing`
- runtime queue: `/Users/operator/btq_runtime/queue`
- runtime completed files: `/Users/operator/btq_runtime/completed`
- runtime failed files: `/Users/operator/btq_runtime/failed`
- runtime temp files: `/Users/operator/btq_runtime/temp`
- runtime logs: `/Users/operator/btq_runtime/logs`
- canonical store: CouchDB `btq_vault` via `BTQ_COUCHDB_URL`

## 14. Testing

Tests currently live under `/Users/operator/btq/tests`.

Pytest usage:

- Intended runner: `pytest`

Key suites:

- [/Users/operator/btq/tests/test_event_pipeline.py](/Users/operator/btq/tests/test_event_pipeline.py)
- [/Users/operator/btq/tests/test_extraction_terms.py](/Users/operator/btq/tests/test_extraction_terms.py)
- [/Users/operator/btq/tests/test_transcription_pipeline.py](/Users/operator/btq/tests/test_transcription_pipeline.py)
- [/Users/operator/btq/tests/test_queue_spec.py](/Users/operator/btq/tests/test_queue_spec.py)
- [/Users/operator/btq/tests/test_queue_processor.py](/Users/operator/btq/tests/test_queue_processor.py)

Current shell status:

- The active test suite has been run from `project/.venv` and is the expected validation path for current changes.

## 15. Current Limitations

- Site resolution depends on a spoken site alias, an inherited `current_site`, or a recent `last_site.json` fallback.
- If a site is not spoken and cannot be resolved from those mechanisms, the event site becomes `"unknown"`.
- Cross-event reasoning is not implemented. Extraction is rule-based and sentence-driven.
- Site observation deduplication and consolidation are basic heuristic merges in `enricher.py`.
- The domain resolver is rule-based and limited to the explicit replacement patterns in `domain_resolver.py`.
- Unknown reclassification only reuses the stored normalized transcript plus user-added notes; it does not rerun Whisper or domain normalization.
- Retry/backoff for unknowns is capped at 3 attempts.
