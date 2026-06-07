# BT Pipeline

Deterministic field-operations pipeline for turning recorded voice notes and field captures into validated queue jobs and controlled canonical writes.

Current end-to-end flow:

`audio -> transcription -> event extraction -> queue -> canonical write (CouchDB) [+ optional Markdown projection]`

This repository is local-first in its processing model (Whisper, deterministic extraction, and the queue processor all run locally), with **CouchDB as the canonical operational data store** for both ingress and entity state. The Obsidian vault is a human-readable projection of that data — journals, digests, visit notes, and approved summaries — not the authoritative store (decided 2026-05-31; the legacy Markdown dual-write defaults off in production and is being removed). Field/voice captures arrive via the capture SPAs into CouchDB and replicate to the processing node; the macOS-oriented watcher setup remains the current pilot deployment shape.

Development workflow, prompts, and execution history live in the sister repository [`ai-methodology`](https://github.com/your-org/ai-methodology) under `projects/btq/`.

## Purpose

BT Pipeline ingests field audio notes and captures, transcribes audio with Whisper, normalizes domain language, extracts deterministic operational events, converts those events into validated queue jobs, and writes approved updates to the canonical CouchDB store through a single writer boundary (with an optional, off-by-default human-readable Markdown projection into an Obsidian vault).

Author entry point:

- Start with `SYSTEM_CONTEXT.md` in the configured vault root for session orientation and authoring rules.
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
5. `queue_processor` validates each job against `project/queue_spec.py`, executes supported jobs, and performs the only canonical writes in the system — to the CouchDB `btq_vault` store (with an optional Markdown projection into the Obsidian vault, off by default in production).

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
  `project/queue_processor/main.py` only (writes to CouchDB `btq_vault`; Markdown projection is optional and off by default)

Observation boundary:

- The event pipeline can now preserve journal-only `observations` alongside factual event details.
- Observations are rendered into journal output for human review.
- They are not part of the executable queue contract.
- They do not drive structured writes to People, Sites, State, or other automation targets.

### Queue Executor

The guarded queue executor can preview or execute queue-shaped jobs produced by the skill bridge. It is not part of the normal vault writer path and it does not execute external calls.

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

Every executor decision is logged to `runtime/logs/queue_executor.log` with timestamp, mode, job, and result. Human approval is still required for restricted operations, and no queue files, canonical writes, or Markdown projection changes are produced by this bridge.

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
- Personal entries are staged as `personal_journal_entry` jobs and written by the queue watcher to `Journal/YYYY-MM-DD.md` inside `personal_vault_dir`.
- Personal entries do not enter Clearpath operational extraction, account journals, staffing events, shift reports, unknown captures, or nightly ops digest.
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
- `ffmpeg`
  expected on `PATH` for Whisper audio handling

Current platform assumptions:

- macOS
- iCloud Drive paths for ingress/drop locations only
- local non-iCloud runtime storage for all claimed and processed files
- an Obsidian vault on the local filesystem
- `launchd` for background watchers

Apple Silicon note:

- The checked-in manifest lists `torch`, but the correct wheel/build can still vary by platform.
- If a default `pip install` fails on a fresh macOS machine, treat `torch` as the first dependency to verify against the local Python and hardware combination.

If you want this to run on another machine, update `config.json` and, if needed, override `base_dir` with an environment variable.

## Setup

Fresh-machine bootstrap:

1. Clone the repository to a local path such as `/path/to/bt-pipeline`.
2. Create a virtual environment in `project/.venv`.
3. Install the repository dependencies and the test extra from the root manifest.
4. Ensure `ffmpeg` is installed and available on `PATH`.
5. Review and update `config.json`.
6. Point the configured iCloud ingress, local runtime, and vault paths at real directories.
7. Run the environment verifier before the first pipeline pass.

From the repository root:

```bash
python3 -m venv project/.venv
project/.venv/bin/python -m pip install --upgrade pip
project/.venv/bin/python -m pip install ".[dev]"
```

If that install fails very early with build-backend or setuptools errors, upgrade `setuptools` inside the venv and retry.

What this install does here:

- installs the runtime dependencies declared in `pyproject.toml`
- installs `pytest` through the `dev` extra
- installs `pymarkdownlnt` and `ruff` through the `dev` extra
- makes the modules under `project/` importable without changing the current source layout

What it does not do:

- it does not make this a polished distributable package
- it does not remove the need for a vault, iCloud ingress directory, local runtime directory, or `ffmpeg`
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
Vault example notes under `project/docs/examples/vault` are excluded because
they intentionally start with frontmatter.

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
- `vault_dir`
- `personal_vault_dir`
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
  "vault_dir": "/path/to/bt-vault",
  "personal_vault_dir": "/path/to/personal-vault",
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

Startup validation:

- `transcription_pipeline.main` fails clearly if the configured audio inbox or vault directory does not exist, and creates local archive/runtime directories as needed.
- `queue_processor.main` and `queue_processor.watch` fail clearly if the configured project root or vault directory does not exist.
- `queue_processor.main` refuses runtime roots inside `pipeline_dir` or another iCloud-managed path.

## Vault Structure

The pipeline assumes a specific Obsidian vault shape. The code does not fully validate that schema up front, so an incorrect vault layout can break routing, site resolution, employee lookups, visit linking, or queue processing.

Key expectations:

- site notes live under `Accounts/<Account>/Locations/<Site>/about.md`
- daily journal notes live under `Journal/YYYY-MM-DD.md`
- unresolved captures live under `Journal/YYYY-MM-DD-unknown.md`
- employee-targeted jobs resolve files under `People/`

These paths describe the Obsidian vault projection. The canonical store is
CouchDB (`btq_vault`); the vault layout below applies when the Markdown
projection is enabled.

Important current coupling:

- runtime site routing uses the currently loaded site registry in [project/event_pipeline/sites.py](project/event_pipeline/sites.py)
- site `about.md` files are expected to contain valid location frontmatter
- visit files are written under `Visits/YYYY-MM-DD.md` beside each site `about.md`

Site-routing lifecycle:

- a site note can be structurally valid in the vault and still not be routable yet
- runtime event routing only uses the registry entries currently present in `project/event_pipeline/sites.py`
- the checked-in repository does not contain a nightly registry-refresh implementation
- if your operational environment has an external nightly refresh that updates the registry from the vault, new vault sites will not become routable until that refresh has completed and the runtime is using the refreshed registry

Read this before changing vault folder names or frontmatter conventions:

- [project/docs/vault_schema.md](project/docs/vault_schema.md)

Concrete checked-in examples:

- [project/docs/examples/vault/](project/docs/examples/vault)

## Environment Verification

Before running the pipeline on a fresh machine, verify:

```bash
./scripts/btq-verify-environment
```

Practical checks:

- the Python version is at least 3.9
- `torch` and `whisper` import successfully from `project/.venv`
- `ffmpeg` is on `PATH`
- the configured inbox, archive, Clearpath vault, and personal vault paths exist on disk
- `base_dir` and `project_dir` point at the checkout you intend to run
- runtime queue backlog count, oldest queued job age, and recent queue watcher or processing activity

Because transcription only stages jobs, a `queue_backlog` failure means jobs are waiting in `<runtime_root>/queue/` and the queue watcher has not shown recent activity.

If you want to inspect individual configured paths directly:

```bash
project/.venv/bin/python -m config get base_dir
project/.venv/bin/python -m config get audio_inbox_dir
project/.venv/bin/python -m config get audio_archive_dir
project/.venv/bin/python -m config get vault_dir
project/.venv/bin/python -m config get personal_vault_dir
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

- `scripts/btq-run` runs `queue_processor.main` against the configured vault and runtime roots (real canonical writes).
- `scripts/btq-dry` runs `queue_processor.main --dry-run` into `<project_runtime_dry_root>/<timestamp>/`.

Both resolve repository-relative paths from their own location and read vault/runtime paths from `config.json`.

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

Operational vault data and AI/bootstrap/configuration documents are separate.

- The operational vault stores mutable operational truth only.
- Repository files are the source of truth for AI instructions, queue authoring docs, architecture notes, bootstrap docs, and setup guidance.
- `~/Library/Mobile Documents/com~apple~CloudDocs/BTDocs/` is an exported iCloud projection from the repository.
- Do not place AI/bootstrap/configuration documents inside the operational vault.

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
6. `queue_processor` validates those jobs and writes the canonical entity to CouchDB `btq_vault` (projected, when enabled, to one of):
   - a site note in `Accounts/.../about.md`
   - a daily journal note in `Journal/YYYY-MM-DD.md`
   - an unresolved capture in `Journal/YYYY-MM-DD-unknown.md`
   - a new person note in `People/<Name>.md`
   - photo attachments in `Journal/Attachments/YYYY-MM-DD/` plus linked journal entries

Example outcomes that exist in the current code:

- `access_constraint` -> site `about.md`
- `staffing_risk` -> recruiting trigger note in site `about.md`
- explicit new-employee/person creation request -> `add_person` writer-created `People/<Name>.md`
- `photo_capture` -> daily journal append with saved photo attachments
- unresolved or partial extraction -> structured `unknown_capture`

## Design Principles / Invariants

These are implementation truths in the current codebase:

- Deterministic extraction:
  event extraction and enrichment are rule-based, not LLM-driven.
- Single job contract:
  `project/queue_spec.py` is the only supported queue job contract.
- Single writer boundary:
  canonical writes happen only in `project/queue_processor/main.py` — to the CouchDB `btq_vault` store. The Markdown projection into the Obsidian vault is optional and off by default in production; CouchDB is the canonical store.
- Entity creation boundary:
  `append_to_note` must not be used for onboarding or entity creation. New people must use `add_person`; the writer generates the `People/<Name>.md` path and permanent `person_id`.
- Additive processing:
  transcription, event artifacts, and queue jobs are persisted as intermediate files.
- Idempotency:
  the system uses processed-file fingerprints, job-file existence checks, processed job IDs, duplicate-content guards, and an append-only mutation-key ledger for keyed `add_person` replay protection.
- Unknown is not failure:
  unresolved captures are stored structurally and can be retried or reclassified later.

## Current Limitations / Local Environment Assumptions

- Default path values in `config.json` still reflect the author's current machine layout until you change them.
- The root `pyproject.toml` is intentionally minimal and only covers the dependencies the active code imports directly.
- There is still no lockfile or pinned environment export for reproducible installs across machines.
- The queue watcher and transcription watcher are macOS `launchd` oriented.
- The vault shape is assumed to match the current Obsidian layout and account/location note conventions.
- Site resolution is alias-based and finite.
- The pipeline is more portable than before, but not yet fully environment-agnostic because directory structure and vault conventions are still assumed by code.

## Deeper Documentation

- Detailed pipeline behavior:
  [project/docs/pipeline.md](project/docs/pipeline.md)
- Nightly review artifact shape:
  [project/docs/nightly_digest.md](project/docs/nightly_digest.md)
- Vault structure and write targets:
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
- Add more explicit examples of real vault seed files for new operators.
