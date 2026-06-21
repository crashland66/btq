# BTQ

Deterministic field-operations pipeline for turning recorded voice notes and field captures into validated queue jobs and controlled canonical writes.

Current end-to-end flow:

`audio -> transcription -> event extraction -> queue -> canonical write (CouchDB)`

This repository is local-first in its processing model (Whisper, deterministic extraction, and the queue processor all run locally), with **CouchDB as the canonical operational data store** for both ingress and entity state. Canonical operational entities — journals, visit notes, site issues, supplies, equipment, people, and approved summaries — live as typed documents in the `btq_vault` CouchDB database; CouchDB is the sole source of truth. Field/voice captures arrive via the capture SPAs into CouchDB and replicate to the processing node; the macOS-oriented watcher setup remains the current pilot deployment shape.

Development workflow, prompts, and execution history live in the sister repository [`ai-methodology`](https://github.com/your-org/ai-methodology) under `projects/btq/`.

## Purpose

BTQ ingests field audio notes and captures, transcribes audio with Whisper, normalizes domain language, extracts deterministic operational events, converts those events into validated queue jobs, and writes approved updates to the canonical CouchDB store through a single writer boundary.

Author entry point:

- Repo-side authoring reference: [project/docs/queue_authoring_guide.md](project/docs/queue_authoring_guide.md)

It is useful when you want:

- deterministic extraction rather than free-form summarization
- explicit queue contracts between pipeline stages
- all canonical writes to flow through one processor
- a reviewable local-first workflow for field operations notes

## Core Architecture

The implemented runtime path is:

1. `transcription_pipeline` watches or scans an audio inbox for stable `.m4a`, `.mp3`, or `.wav` files.
2. Audio is moved into local staging and transcribed to `<audio>.<ext>.whisper.txt`.
3. `event_pipeline` normalizes domain terms and writes:
   - `<audio>.<ext>.whisper.normalized.txt`
   - `<audio>.<ext>.whisper.corrections.json`
   - event artifacts under `events_raw/`, `events_enriched/`, `events_valid/`, and `events_failed/`
4. `event_to_queue` converts validated events into queue-spec job JSON files.
5. `queue_processor` validates each job against `project/queue_spec.py`, executes supported jobs, and performs the only canonical writes in the system — to the CouchDB `btq_vault` store.

Practical stage boundaries:

- Transcription contract:
  raw text file at `*.whisper.txt`
- Normalization / extraction contract:
  normalized transcript plus validated event JSON files
- Event-to-queue contract:
  queue job JSON matching `project/queue_spec.py`
- Queue validation contract:
  `validate_job(job)` in `project/queue_spec.py`
- Canonical writer boundary:
  `project/queue_processor/main.py` only (writes to CouchDB `btq_vault`)

Observation boundary:

- The event pipeline can now preserve journal-only `observations` alongside factual event details.
- Observations are rendered into journal output for human review.
- They are not part of the executable queue contract.
- They do not drive structured writes to People, Sites, State, or other automation targets.

### Queue Executor

The guarded queue executor can preview or execute queue-shaped jobs produced by the skill bridge. It is not part of the normal canonical writer path and it does not execute external calls.

Modes:

- `dry-run` prints planned actions and logs the preview without writing targets.
- `auto-safe` executes only safe local file operations.
- `approve-required` executes only when `--approve` is also present.

Safety model:

- Safe job types: `create_file`, `update_file`, `generate_agents_txt`.
- Restricted job types: `update_existing_file`, `delete_file`, `external_call`.
- Unknown job types are rejected before execution.
- `external_call` is never executed.
- File targets must resolve under the repository root by default; path traversal and system-level paths are blocked.

Skill bridge usage:

```bash
./scripts/btq skill run web-review \
  --version v2 \
  --input project/skills/web-review/fixtures/sample_input.md \
  --structured \
  --to-queue-dry-run \
  --execute \
  --mode auto-safe
```

Every executor decision is logged to `runtime/logs/queue_executor.log` with timestamp, mode, job, and result. Human approval is still required for restricted operations, and no queue files or canonical writes are produced by this bridge.

### Action Validation

Structured skill actions are validated before they can be mapped into queue-shaped jobs. The validator rejects vague or non-actionable actions so weak model output cannot drift into the executor path.

Rejected actions include:

- descriptions shorter than 10 characters
- vague descriptions such as `improve`, `optimize`, `fix things`, or `make better`
- generic targets such as `/`, `*`, or `site`
- empty payloads
- payloads without required actionable fields, such as `change` for `update_file`, `content` for `create_file`, or `capabilities` for `generate_agents_txt`
- duplicate actions with the same type and target

To fix a failing action, make the description concrete, name a real file path or endpoint, and include payload fields that a deterministic writer could apply or preview.

### Execution Journal

Every structured skill run writes a replayable JSON journal under `runtime/journal/`. The journal captures the timestamp, skill id, version, original input, parsed `agents.txt` context, composed prompt, raw structured output, parsed actions, mapped queue jobs, execution mode, execution results, code version, and raw-output hash.

This artifact exists so a skill run can be inspected later without relying on memory, terminal scrollback, or another model call. The journal is evidence for what prompt was composed, what structured actions were seen, and what queue-shaped jobs were produced.

### Replay

Replay loads a journal file and remaps jobs from the stored raw output. It does not call a model and it does not refetch `agents.txt`.

```bash
./scripts/btq replay runtime/journal/<journal>.json --diff
./scripts/btq replay runtime/journal/<journal>.json --execute --mode auto-safe
```

Replay verifies that the referenced skill and version still exist, warns if the current code version differs from the journal, and can diff the original mapped jobs against newly mapped jobs. Use it to debug mapper changes, confirm deterministic behavior, and rerun guarded executor previews before any approved mutation path.

Personal journal boundary:

- A voice memo that begins with `personal journal`, `this is a personal journal`, or `personal note` is routed as personal-only.
- The trigger phrase is stripped from the stored body, while the raw transcript path, timestamp, and source audio filename are preserved in the queued job.
- Personal entries are staged as `personal_journal_entry` jobs and written by the queue watcher to the separate personal journal store, isolated from operational state.
- Personal entries do not enter operational extraction, account journals, staffing events, shift reports, unknown captures, or nightly ops digest.
- This first pass performs separation only. It does not add semantic analysis, pattern extraction, mood analysis, tagging, summaries, or personal insight generation.

## Repository Layout

- `project/`
  Python application code for the pipeline itself.
- `project/transcription_pipeline/`
  Audio ingestion, Whisper transcription, compare mode, and queue handoff.
- `project/event_pipeline/`
  Domain normalization, site resolution, extraction, enrichment, and validation.
- `project/event_to_queue/`
  Mapping from validated events into queue jobs.
- `project/queue_processor/`
  Queue execution (registry-dispatched domain handlers under `handlers/`), the CouchDB `btq_queue` watcher, unknown reclassification, visit handling, and the canonical CouchDB writes.
- `project/field_capture/`
  Field-capture PWA/API service. Caddy serves `public/` static assets from the
  deployed `dist` directory while Python handles `/api/*`, `/site/*`, and media.
- `project/voice_memo/`
  Voice memo PWA intake service, public assets, and deployment scripts. It uses
  the same Caddy-static plus Python-API deployment pattern as field capture.
- `project/docs/pipeline.md`
  Detailed description of what the pipeline currently does.
- `project/docs/queue_authoring_guide.md`
  Authoring rules for executable queue jobs, including what is explicitly not part of the queue contract.
- `project/docs/docs_export_manifest.json`
  Explicit repo-to-BTDocs export map for AI/bootstrap/configuration documents.
- `project/docs/nightly_digest.md`
  Recommended nightly review artifact shape based on current system outputs.
- `project/WHISPER_SETUP.md`
  Existing notes for the Whisper watcher and macOS background service setup.
- `scripts/`
  Shell wrappers for queue processing, queue authoring (`btq-enqueue`), nightly digest generation, dry runs, deploy (`btq-deploy-pro`, `btq-deploy-vps`), and `launchd` installers.
- `tests/`
  Pytest coverage for the current active pipeline behavior.
- `local/`
  Generated runtime data and logs. Not checked into git.
- `logs/`
  Queue processor run logs. Not checked into git.

## Requirements

The repository now includes a minimal root `pyproject.toml` for the Python packages the current code actually imports.

Current Python/runtime requirements inferred from active code paths:

- Python 3.9 or newer
- `openai-whisper`
  imported as `whisper` by `project/transcription_pipeline/main.py`
- `torch`
  imported by `project/transcription_pipeline/main.py`
- `pytest`
  only needed for the test suite, installed through the `dev` extra
- `pymarkdownlnt`
  markdown linting for operational docs, installed through the `dev` extra
- `ruff`
  Python linting for first-party code, installed through the `dev` extra
- `boto3`
  only needed on hosts running the media store with `media_store="s3"` for Cloudflare R2, installed through the `r2` extra
- `ffmpeg`
  expected on `PATH` for Whisper audio handling

Current platform assumptions:

- macOS
- iCloud Drive paths for ingress/drop locations only
- local non-iCloud runtime storage for all claimed and processed files
- a reachable CouchDB instance for canonical operational state
- `launchd` for background watchers

Apple Silicon note:

- The checked-in manifest lists `torch`, but the correct wheel/build can still vary by platform.
- If a default `pip install` fails on a fresh macOS machine, treat `torch` as the first dependency to verify against the local Python and hardware combination.

If you want this to run on another machine, update `config.json`, set the CouchDB connection variables, and, if needed, override `base_dir` with an environment variable.

## Setup

Fresh-machine bootstrap:

1. Clone the repository to a local path such as `/path/to/bt-pipeline`.
2. Create a virtual environment in `project/.venv`.
3. Install the repository dependencies and the test extra from the root manifest.
4. Ensure `ffmpeg` is installed and available on `PATH`.
5. Review and update `config.json`.
6. Point the configured iCloud ingress and local runtime paths at real directories, and set the CouchDB connection (`BTQ_COUCHDB_URL`, `BTQ_COUCHDB_USER`, `BTQ_COUCHDB_PASSWORD`).
7. Run the environment verifier before the first pipeline pass.

From the repository root:

```bash
python3 -m venv project/.venv
project/.venv/bin/python -m pip install --upgrade pip
project/.venv/bin/python -m pip install ".[dev]"
```

On serving or ingest hosts using the Cloudflare R2 media store (`media_store="s3"`), install the
optional `r2` extra in the same venv too:

```bash
project/.venv/bin/python -m pip install ".[r2]"
```

The `r2` install is idempotent and safe to re-run. Hosts using the default `local` media store do
not need this extra.

If that install fails very early with build-backend or setuptools errors, upgrade `setuptools` inside the venv and retry.

What this install does here:

- installs the runtime dependencies declared in `pyproject.toml`
- installs `pytest` through the `dev` extra
- installs `pymarkdownlnt` and `ruff` through the `dev` extra
- makes the modules under `project/` importable without changing the current source layout

What it does not do:

- it does not make this a polished distributable package
- it does not remove the need for CouchDB, an iCloud ingress directory, a local runtime directory, or `ffmpeg`
- it does not resolve machine-specific `torch` wheel issues for you

Before the first real run:

```bash
./scripts/btq-verify-environment
```

Clean generated local caches with `./scripts/btq-clean-local` (dry-run default; use `--apply`, and `--venv` only when intentionally recreating `project/.venv`).

Lint operational markdown docs:

```bash
./scripts/lint-markdown
```

The markdown lint gate is strict for `project/field_capture` deployment docs.
It also scans `project/docs`, but intentionally ignores line length and duplicate
heading checks there because the legacy architecture docs contain long narrative
paragraphs and the queue authoring guide repeats field headings by job type.

Lint first-party Python:

```bash
./scripts/lint-python
```

## Configuration

Runtime paths are defined in the repository root at:

- `config.json`

The config loader lives at:

- `project/config.py`

Current config keys:

- `base_dir`
- `project_dir`
- `pipeline_dir`
- `audio_inbox_dir`
- `audio_archive_dir`
- `local_root`
- `transcription_output_dir`
- `event_output_dir`
- `queue_dir`
- `local_runtime_dir`
- `runtime_root`
- `project_runtime_root`
- `project_runtime_dry_root`
- `logs_dir`
- `queue_processor_logs_dir`
- `transcription_log_path`
- `queue_watch_log_path`
- `whisper_launchd_stdout_log`
- `whisper_launchd_stderr_log`
- `queue_watch_launchd_stdout_log`
- `queue_watch_launchd_stderr_log`
- `whisper_model`
- `ffmpeg_path_prefix`

Minimal example:

```json
{
  "base_dir": "/path/to/bt-pipeline",
  "project_dir": "{base_dir}/project",
  "pipeline_dir": "/path/to/BTpipeline",
  "audio_inbox_dir": "/path/to/BTpipeline/inbox/voice",
  "audio_archive_dir": "/path/to/local-runtime/completed/audio",
  "local_root": "{base_dir}/local",
  "local_runtime_dir": "/path/to/local-runtime",
  "runtime_root": "/path/to/local-runtime",
  "project_runtime_root": "/path/to/local-runtime",
  "project_runtime_dry_root": "/path/to/local-runtime/dry-runs",
  "whisper_model": "large-v3",
  "ffmpeg_path_prefix": "/opt/homebrew/bin:/usr/local/bin"
}
```

`{base_dir}` placeholders are expanded by the config loader.

Runtime directory layout:

```text
<runtime_root>/
├── claimed/
├── processing/
├── completed/
├── failed/
├── temp/
├── logs/
├── metrics/
├── alerts/
└── queue/
```

`metrics/` holds the rolling pipeline-observability sidecar
(`pipeline_metrics.jsonl`); see the `/health` "Pipeline trend" panel and
`btq pipeline-metrics-sample`.

`alerts/` holds unattended health-monitor artifacts. `btq health --monitor`
uses the same health model as `btq health`; when status is critical it appends
a throttled alert to `alerts/health_alerts.jsonl` and updates
`alerts/latest_health_alert.json` for the `/health` dashboard. Install the
LaunchAgent with `./scripts/install-btq-health-monitor-launch-agent`.

`pipeline_dir` is iCloud transport only. Watchers may detect files there, but they must claim and move files into `local_runtime_dir` before processing. This boundary exists because iCloud can lock, hydrate, evict, or sync files while they are visible on disk; processing synced files directly has produced `Resource deadlock avoided` and similar contention failures.

Environment override:

- `BT_PIPELINE_BASE_DIR`

If set, it overrides `base_dir` from `config.json` and re-resolves all derived `{base_dir}` paths. This is the intended way to move the repository without editing every path value immediately.

### Engine vs Instance Configuration

`project/config.py` keeps engine-agnostic machine settings in `PipelineConfig`.
These are runtime paths, queue paths, log paths, `whisper_model`, and
`ffmpeg_path_prefix`. Do not put company/operator-specific values in that
dataclass.

`project/instance_config.py` defines the instance-specific surface in
`InstanceConfig`. It preserves the existing environment variable names and
defaults, and `config.example.json` carries synthetic fallback values only.
Real instance values belong in gitignored local config/data files or environment
variables.

Instance-specific fields:

- CouchDB URL and credentials:
  `BTQ_COUCHDB_URL`, `BTQ_COUCHDB_USER`, `BTQ_COUCHDB_PASSWORD`
- CouchDB database-name overrides:
  `BTQ_COUCHDB_SITES_DB`, `BTQ_COUCHDB_FIELD_CAPTURES_DB`,
  `BTQ_COUCHDB_PEOPLE_DB`, `BTQ_COUCHDB_PHOTO_VISION_DB`,
  `BTQ_COUCHDB_QUEUE_DB`, `BTQ_COUCHDB_VAULT_DB`,
  `BTQ_COUCHDB_PERSONAL_JOURNAL_DB`, `BTQ_COUCHDB_VOICE_MEMOS_DB`
- Instance data-file paths:
  `BTQ_SITE_REGISTRY_PATH`, `BTQ_BRAND_KEYWORDS_PATH`
- Media-store selection and inert R2 slots:
  `BTQ_MEDIA_STORE` defaults to `local`; `BTQ_R2_ENDPOINT_URL`,
  `BTQ_R2_BUCKET`, `BTQ_R2_REGION`, `BTQ_R2_ACCESS_KEY_ID`, and
  `BTQ_R2_SECRET_ACCESS_KEY` are present for the future R2 backend but are not
  used by current storage code.

The committed examples are intentionally synthetic:
`project/event_pipeline/site_registry.example.json`,
`project/event_pipeline/brand_keywords.example.json`, and the `instance` block
in `config.example.json`. Real site registry and brand keyword files remain
gitignored as `project/event_pipeline/site_registry.json` and
`project/event_pipeline/brand_keywords.json`.

Startup validation:

- `transcription_pipeline.main` fails clearly if the configured audio inbox does not exist, and creates local archive/runtime directories as needed.
- `queue_processor.main` and `queue_processor.watch` fail clearly if the configured project root does not exist or CouchDB is not reachable.
- `queue_processor.main` refuses runtime roots inside `pipeline_dir` or another iCloud-managed path.

## Canonical Entity Store

Canonical operational state lives as typed documents in the `btq_vault` CouchDB
database — `location`, `account`, `employee`, `visit`, `site_issue`,
`supply_need`, `equipment_request`, `personnel_event`, `journal`,
`unknown_capture`, `shift_report`, and related types. CouchDB is the sole source
of truth; there is no Obsidian markdown projection.

Important current coupling:

- runtime site routing uses the active site registry — the `btq_sites` CouchDB
  database when `BTQ_COUCHDB_URL` is set, falling back to
  [project/event_pipeline/sites.py](project/event_pipeline/sites.py) in
  local/dev when CouchDB is not configured
- `location` documents must carry valid enough fields for site validation
- visit and visit-gap records are written against each resolved site

Site-routing lifecycle:

- a `location` document can exist and still not be routable yet
- runtime event routing only uses the entries currently present in the active
  registry
- the checked-in repository does not contain a nightly registry-refresh
  implementation
- if your operational environment runs an external registry refresh, new sites
  will not become routable until that refresh has completed and the runtime is
  using the refreshed registry

Read this for the canonical entity schema and write targets:

- [project/docs/vault_schema.md](project/docs/vault_schema.md)

## Environment Verification

Before running the pipeline on a fresh machine, verify:

```bash
./scripts/btq-verify-environment
```

Practical checks:

- the Python version is at least 3.9
- `torch` and `whisper` import successfully from `project/.venv`
- `ffmpeg` is on `PATH`
- the configured inbox and archive paths exist on disk
- `base_dir` and `project_dir` point at the checkout you intend to run
- runtime queue backlog count, oldest queued job age, and recent queue watcher or processing activity

Because transcription only stages jobs, a `queue_backlog` failure means jobs are waiting in `<runtime_root>/queue/` and the queue watcher has not shown recent activity.

If you want to inspect individual configured paths directly:

```bash
project/.venv/bin/python -m config get base_dir
project/.venv/bin/python -m config get audio_inbox_dir
project/.venv/bin/python -m config get audio_archive_dir
```

(The `outbox_dir` and `working_dir` config keys were removed when the iCloud
outbox was retired; `config.json` now rejects unknown keys.)

## Known-Good Local Versions

These are the versions currently installed in the maintainer's existing local venv. They are not pinned by the repository, but they are a concrete reference point for a working setup:

- Python `3.9`
- `openai-whisper` `20250625`
- `torch` `2.8.0`
- `pytest` `8.4.2`
- `setuptools` `58.0.4`

## Running The Pipeline Once

From the repository root:

```bash
cd project
.venv/bin/python -m transcription_pipeline.main --once
```

What that actually does today:

1. scans the configured inbox for stable audio files
2. claims a ready file out of iCloud into local runtime storage
3. starts a short-lived transcription worker process
4. the worker loads Whisper, transcribes with the enhanced profile, writes the transcript result, and exits
5. the parent process continues after the worker has exited, keeping idle RAM low
6. normalizes domain terms
7. extracts, enriches, and validates events
8. writes queue jobs into local queue staging
9. stages those jobs into the local runtime queue
10. leaves runtime queue draining to `queue_processor.watch`
11. archives the source audio file under local runtime `completed/audio`

The parent watcher does not keep a global Whisper model instance alive. This is deliberate on macOS Apple Silicon: process teardown is a more reliable memory release boundary than trying to unload large PyTorch/Whisper allocations inside a long-running Python process.

To keep the enhanced transcript but also write baseline-vs-enhanced comparison artifacts:

```bash
cd project
.venv/bin/python -m transcription_pipeline.main --once --compare-mode
```

## Running Tests

From the repository root:

```bash
cd project
.venv/bin/python -m pytest ../tests -q
```

The main test suites are:

- `tests/test_transcription_pipeline.py`
- `tests/test_event_pipeline.py`
- `tests/test_queue_spec.py`
- `tests/test_queue_processor.py`

## Authoring And Processing Queue Jobs

Jobs are authored into the CouchDB `btq_queue` database, not dropped as files.
The legacy iCloud `BTpipeline/outbox/` JSON-drop and its `btq-stage-outbox`
staging script have been **retired** (machinery removed; CouchDB cutover
2026-05). Author a job by piping its JSON to `scripts/btq-enqueue`:

```bash
cat job.json | ./scripts/btq-enqueue --dry-run   # validate against queue_spec.py only, no write
cat job.json | ./scripts/btq-enqueue             # PUT into btq_queue
```

The CouchDB queue watcher (`project/queue_processor/couchdb_queue_watcher.py`)
then materializes each pending `btq_queue` document through the queue processor.
See [project/docs/queue_authoring_guide.md](project/docs/queue_authoring_guide.md).

To run the queue processor directly against the local runtime queue:

```bash
./scripts/btq-run    # process the local runtime queue and write canonical state
./scripts/btq-dry    # same, with --dry-run into a timestamped dry runtime root
```

What these do today:

- `scripts/btq-run` runs `queue_processor.main` against the configured runtime root and CouchDB (real canonical writes).
- `scripts/btq-dry` runs `queue_processor.main --dry-run` into `<project_runtime_dry_root>/<timestamp>/`.

Both resolve repository-relative paths from their own location and read runtime paths from `config.json`.

## Background Watchers

Long-running watchers in production today:

Transcription watcher:

- module: `project/transcription_pipeline/main.py`
- worker module: `project/transcription_pipeline/worker.py`
- script: `scripts/whisper-watch`
- installer: `scripts/install-whisper-launch-agent`

CouchDB queue watcher (the production deterministic-mutation path):

- module: `project/queue_processor/couchdb_queue_watcher.py`
- subscribes to the CouchDB `btq_queue` `_changes` feed and materializes each pending job through the queue processor
- LaunchAgent: `project/field_capture/launchagents/com.btq.couchdb-queue-watcher.plist`

CouchDB capture watchers (ingress from the SPAs):

- field captures: `project/field_capture/couchdb_watcher.py` (`btq_field_captures`)
- voice memos: `project/voice_memo/couchdb_watcher.py` (`btq_voice_memos`)

Local file-queue watcher (`project/queue_processor/watch.py`, `scripts/queue-watch`)
still exists for locally staged runtime-queue jobs and dev use; the retired
iCloud-outbox staging it once did is gone.

These are macOS `launchd` services. Linux `systemd` unit templates ship under
`project/deploy/systemd/` for the server deployment.

Run the local file-queue watcher once:

```bash
cd project
.venv/bin/python -m queue_processor.watch --once
```

Run it continuously in the foreground:

```bash
cd project
.venv/bin/python -m queue_processor.watch
```

Recommended order on a fresh machine:

1. `./scripts/btq-verify-environment`
2. `cd project && .venv/bin/python -m transcription_pipeline.main --once`
3. `cd project && .venv/bin/python -m queue_processor.watch --once`
4. optionally use `./scripts/btq-run` for an explicit one-shot queue processing pass

## Exporting BTDocs

Canonical operational data and AI/bootstrap/configuration documents are separate.

- The `btq_vault` CouchDB database stores mutable operational truth only.
- Repository files are the source of truth for AI instructions, queue authoring docs, architecture notes, bootstrap docs, and setup guidance.
- `~/Library/Mobile Documents/com~apple~CloudDocs/BTDocs/` is an exported iCloud projection from the repository.
- Do not place AI/bootstrap/configuration documents inside the canonical operational store.

Export docs:

```bash
./scripts/btq-export-docs
```

The exporter reads `project/docs/docs_export_manifest.json`, validates every source and target path, creates target directories, and only rewrites files whose content changed.

Each exported markdown file receives a projection-only metadata header:

```markdown
<!--
BTQ_DOC_VERSION: v1
BTQ_EXPORT_TIME: 2026-05-01T22:14:11Z
BTQ_GIT_COMMIT: a1b2c3d
BTQ_SOURCE_PATH: project/docs/ai/README_FIRST.md
BTQ_EXPORTER_VERSION: 1
-->
```

Source files in the repository are not modified. `BTQ_EXPORT_TIME` is preserved on no-op exports so repeated syncs still report `written=0 unchanged=N`. The exporter also writes `BTDocs/export_manifest.json` with document hashes, commit, schema version, and export metadata for stale-doc detection.

## Golden Path

One realistic minimal flow supported today:

1. A voice memo is captured through the `voice.example.com` SPA into the CouchDB `btq_voice_memos` database and replicated to the processing node. (The iCloud inbox is retained only for operator drag-and-drop of long pre-recorded audio; fresh capture goes through the SPA.)
2. The transcription pipeline claims the audio into local runtime storage.
3. The transcription pipeline transcribes it into `*.whisper.txt`.
4. The event pipeline produces one or more validated event JSON files.
5. `event_to_queue` writes queue jobs that match `project/queue_spec.py`.
6. `queue_processor` validates those jobs and writes the canonical entity to CouchDB `btq_vault` as one of:
   - a `location` update (operational notes, issues, supplies, equipment)
   - a `journal` entry
   - an `unknown_capture` record
   - a new `employee` document
   - a `photo_capture` journal entry with saved photo media

Example outcomes that exist in the current code:

- `access_constraint` -> `location` document update
- `staffing_risk` -> recruiting trigger on the `location` document
- explicit new-employee/person creation request -> `add_person` writer-created `employee` document
- `photo_capture` -> journal entry with saved photo media
- unresolved or partial extraction -> structured `unknown_capture`

## Design Principles / Invariants

These are implementation truths in the current codebase:

- Deterministic extraction:
  event extraction and enrichment are rule-based, not LLM-driven.
- Single job contract:
  `project/queue_spec.py` is the only supported queue job contract.
- Single writer boundary:
  canonical writes happen only in `project/queue_processor/main.py` — to the CouchDB `btq_vault` store, which is the source of truth.
- Entity creation boundary:
  `append_to_note` must not be used for onboarding or entity creation. New people must use `add_person`; the writer generates the canonical `employee` document and permanent `person_id`.
- Additive processing:
  transcription, event artifacts, and queue jobs are persisted as intermediate files.
- Idempotency:
  the system uses processed-file fingerprints, job-file existence checks, processed job IDs, duplicate-content guards, and an append-only mutation-key ledger for keyed `add_person` replay protection.
- Unknown is not failure:
  unresolved captures are stored structurally and can be retried or reclassified later.

## Current Limitations / Local Environment Assumptions

- Default path values in `config.example.json` are placeholders; installation-specific paths belong in local config or environment variables.
- The root `pyproject.toml` is intentionally minimal and only covers the dependencies the active code imports directly.
- There is still no lockfile or pinned environment export for reproducible installs across machines.
- The queue watcher and transcription watcher are macOS `launchd` oriented.
- Canonical entity shapes are assumed to match the current `btq_vault` document conventions.
- Site resolution is alias-based and finite.
- The pipeline is more portable than before, but not yet fully environment-agnostic because directory structure and CouchDB conventions are still assumed by code.

## Deeper Documentation

- Detailed pipeline behavior:
  [project/docs/pipeline.md](project/docs/pipeline.md)
- Nightly review artifact shape:
  [project/docs/nightly_digest.md](project/docs/nightly_digest.md)
- Canonical entity schema and write targets:
  [project/docs/vault_schema.md](project/docs/vault_schema.md)
- Operator runbook:
  [project/docs/runbook.md](project/docs/runbook.md)
- Whisper watcher notes:
  [project/WHISPER_SETUP.md](project/WHISPER_SETUP.md)

Nightly digest command:

```bash
./scripts/btq-build-nightly-digest --date 2026-04-20
```

Default output path:

- `Journal/YYYY-MM-DD-digest.md`

## Documentation TODOs

- Continue removing machine-specific assumptions from deeper docs and supporting scripts.
- Add more explicit examples of real `btq_vault` seed documents for new operators.
