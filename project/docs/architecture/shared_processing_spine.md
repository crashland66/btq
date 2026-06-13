# Shared Processing Spine

BTQ intake channels should converge on the same processing spine:

```text
raw source artifact
-> metadata artifact
-> local processing
-> raw derived artifact
-> semantic layer
-> action candidates
-> approved queue-job drafts
-> deliberate queue staging
-> deterministic queue processor
-> queue handlers
-> CouchDB canonical state + evidence
```

The current voice inbox and field-capture audio paths already follow most of
this shape. Field-capture audio now uses shared `processing_core` helpers for
artifact IDs, status checks, JSON artifact IO, root containment checks, processor
counts, transcript payload envelopes, semantic payload envelopes, and raw-text
hashing. Voice-inbox transcript metadata and event-pipeline JSON sidecars use
the same low-level artifact helpers where their shapes already matched.
`processing_core.semantic_transform` can also run a caller-provided semantic
engine against an already-discovered transcript object and wrap the outcome in a
semantic artifact payload. `processing_core.action_candidates` now provides a
small review-record envelope for proposed next steps.
`processing_core.approved_job_drafts` provides a draft envelope for approved
candidate-to-queue-job proposals. `processing_core.draft_staging` provides the
deliberate transition from approved draft artifacts into `<runtime_root>/queue/`.
Discovery, routing, extraction, action selection, approval decisions, and
mutation remain channel-owned.

## Current Boundaries

- Raw intake remains evidence-first and non-blocking. Upload and capture paths
  preserve raw media before later processing.
- The field-capture VPS upload path is allowed to accrue raw media and
  `photo_capture` metadata while the Mac is offline. VPS capture storage is not
  the Mac executable mutation queue.
- Transcription is local processing. It may produce imperfect evidence and must
  not directly mutate canonical operational state.
- Semantic cleanup is an interpretation layer. It should preserve provenance to
  raw transcripts and write review-needed artifacts.
- Action candidates are proposed work, not writes. Only approved queue jobs may
  cross the deterministic writer boundary.
- Candidate review artifacts are local runtime records. They do not approve
  anything and do not execute mutation.
- Approved job drafts are still local review artifacts. They are not staged into
  `<runtime_root>/queue/` and are not executable until a later explicit staging
  step.
- Deliberate staging writes queue files only after draft status and
  `queue_spec` validation pass. It does not run the queue processor.
- Canonical mutation belongs to queue processing and handlers and must remain
  deterministic. Markdown is projection/export, not the writer boundary.

## Summit Wire Pilot Surface

For the Summit Wire pilot, the production field-capture SPA is a deployment-branded
tokenized upload surface. A valid individual bearer link resolves one employee
and one site and shows a personalized ready message:

```text
Ready for <First Name> — Summit Wire
```

The shared viewer is site-scoped at `https://photos.example.com/site/7050`.
It returns a friendly empty state for a known site with zero captures, then
shows submitted media as the VPS receives it. This viewer is still evidence
visibility, not approval or publishing. The downstream Mac path pulls VPS
captures into local intake, then transcribes, processes semantics, collects
review candidates, and leaves approval, draft generation, staging, and vault
mutation under deliberate manager control.

## Shared Core

`processing_core` is intentionally small. It currently holds stable,
cross-channel helper concepts:

- `ids.py` for deterministic artifact IDs
- `status.py` for `complete`, `failed`, and terminal status checks
- `artifacts.py` for JSON artifact reads/writes and root containment checks
- `results.py` for processor count dictionaries
- `hashing.py` for raw-text hashes
- `transcripts.py` for generic transcript artifact payload envelopes
- `semantics.py` for generic semantic artifact payload envelopes
- `semantic_transform.py` for payload-level semantic transformation outcomes
- `action_candidates.py` for review-only action-candidate payloads
- `approved_job_drafts.py` for approved draft payloads that are not staged
- `draft_staging.py` for validated staging from approved drafts to queue files

This should not become a framework or orchestration engine. Channel modules can
continue to own discovery and channel-specific metadata while sharing artifact
shape, provenance, status, and writer helpers.

## Staged Direction

1. Done: extract shared helpers without changing artifact locations.
2. Done: move field-capture transcription and semantic artifact writes onto
   those helpers without changing artifact JSON shapes.
3. Done: move voice inbox transcript metadata and event-pipeline JSON sidecars
   onto shared helpers where the shape already matched.
4. Done: add a payload-level semantic transformation helper that can operate on
   completed transcript data supplied by any intake channel.
5. Done: add an action-candidate review artifact layer that records proposed
   next steps without approval, queue-job generation, or canonical mutation.
6. Done: convert explicitly approved candidates into approved queue-job draft
   artifacts without staging, execution, or canonical mutation.
7. Done: stage selected approved drafts into `<runtime_root>/queue/` only after
   validation and duplicate checks, without executing the queue processor.
8. Done: add read-only review/status observability across semantic artifacts,
   candidates, drafts, staging status, queue files, processed/failed files, and
   the processed index.
9. Done: add deterministic fixture coverage for the field-capture semantic to
   candidate to approved draft to staged queue path.
10. Done: add operator-facing documentation and runbook coverage for the
   field-capture review pipeline.
11. Done: expose operator CLI commands for collecting action candidates and
   generating approved drafts without adding automatic approval.
12. Done: expose one-candidate-at-a-time manual approval/rejection ergonomics
   without generating drafts or staging jobs.
13. Done: add command-level smoke coverage for the full field-capture review
   workflow up to deliberate queue staging.
14. Done: add dry-run planning for approved draft staging so operators can
    validate queue-spec compatibility, deterministic queue filenames, job IDs,
    and duplicate evidence without writing queue or staging artifacts.
15. Done: add dry-run planning for approved job draft generation so operators
    can preview approved candidate to draft mapping without writing approved or
    failed draft artifacts.
16. Done: add dry-run planning for field-capture action candidate collection so
    operators can preview semantic artifact to candidate mapping without
    writing pending or failed candidate artifacts.
17. Done: add a read-only field-capture action candidate listing command so
    operators can inspect candidate IDs, summaries, status, source previews,
    and review metadata before approving or rejecting one candidate.
18. Done: add a read-only field-capture approved draft listing command so
    operators can inspect draft IDs, candidate provenance, proposed queue job
    previews, and staging/queue state before deliberate staging.
19. Done: add a read-only detail command for one field-capture review item by
    candidate ID or draft ID.
20. Done: add read-only maintenance/status visibility for accumulated
    field-capture review artifacts before any cleanup or archive strategy.
21. Done: add a read-only review dashboard that summarizes existing review
    state and suggests the next operator command without taking action.
22. Done: add an explicit non-destructive export/import bridge for moving one
    VPS field-capture upload bundle into the Mac runtime.
23. Done: add a local read-only Mac ops dashboard for runtime and review
    visibility.
24. Later, process staged queue files through the existing deterministic writer
   boundary.

## Stage 5 Candidate Review Artifacts

Stage 5 creates inspectable local artifacts under channel-owned review
directories such as:

```text
<runtime_root>/reviews/action_candidates/field_capture/*.json
```

These records preserve provenance to the semantic artifact and source
transcript when available. They default to `pending_review`; malformed
candidates are recorded as `failed` rather than being silently treated as
actionable work. The field-capture collector reads existing semantic
`action_candidates` and emits review artifacts without changing semantic JSON,
upload behavior, queue files, viewer output, or vault content.

The candidate layer intentionally stops before approval. A candidate is a
review prompt, not a mutation request. Later work may add an explicit approval
step that converts selected candidates into queue jobs through the existing
deterministic writer boundary.

## Stage 6 Approved Job Drafts

Stage 6 adds another review-only artifact directory:

```text
<runtime_root>/reviews/approved_job_drafts/field_capture/*.json
```

Only candidates already marked `approved` can produce drafts. `pending_review`,
`rejected`, and `failed` candidates are skipped. Approved candidates that do not
contain enough channel-owned mapping information produce failed draft artifacts
instead of queue jobs.

For `field_capture`, the channel-owned default mapping is an `append_to_note`
draft for the associated site note. Explicit `proposed_queue_job` metadata still
wins when present. Otherwise, the draft builder resolves `site_id` from channel
metadata, candidate provenance, or the semantic artifact and maps it through the
active site registry. Missing site context fails closed.

Draft records preserve provenance to the candidate, semantic artifact, and
source transcript when available. They include the proposed queue job type and
payload as inspectable JSON, but those fields remain draft data only. Stage 6
does not write `<runtime_root>/queue/`, does not call the queue processor, and
does not mutate vault state.

## Stage 7 Deliberate Queue Staging

Stage 7 is the explicit handoff from review artifacts to the existing queue
transport. It reads approved draft artifacts and writes validated queue jobs
under:

```text
<runtime_root>/queue/*.json
```

The staging command also writes review status artifacts under:

```text
<runtime_root>/reviews/staging/field_capture/*.json
```

Only drafts with `status: approved_draft` are eligible. The proposed job is
validated with `queue_spec.validate_job()` before any queue file is written.
The staged queue job preserves draft, candidate, semantic, and transcript
provenance in top-level `metadata`; the executable `payload` remains unchanged
from the approved draft.

Duplicate prevention checks the staging status artifact, existing
`<runtime_root>/queue/`, `<runtime_root>/processed/`, and
`<runtime_root>/failed/` JSON files for the same draft metadata or equivalent
computed job ID. It also consults the processed index when available. This
keeps restaging conservative without changing `compute_job_id()` semantics.

Stage 7 does not call the queue processor and does not mutate vault state.
Vault mutation still only happens later, when the deterministic queue processor
handles staged queue files.

## Stage 8 Review Status

Stage 8 adds a read-only operator report:

```text
./scripts/btq review-status --channel field_capture
```

The command reports counts across completed semantic artifacts with action
candidates, candidate review statuses, approved and failed drafts, staging
results, queued staged jobs, processed staged jobs, failed staged jobs, and
processed-index records. `--json` returns the same report as stable JSON.

The report also surfaces broken lineage:

- candidates pointing to missing semantic artifacts
- drafts pointing to missing candidates
- staging results pointing to missing drafts
- queue files pointing to missing drafts
- approved candidates without drafts
- approved drafts without staging status
- staged drafts without queue, processed, failed, or index evidence

Stage 8 is observational only. It does not generate candidates, create drafts,
stage queue files, call the queue processor, repair artifacts, or mutate vault
state.

## Stage 9 Fixture Coverage

Stage 9 adds deterministic test coverage for the field-capture review path up
to deliberate queue staging. The fixture creates a completed semantic artifact,
collects pending review candidates, simulates explicit human approval inside
test data only, generates an approved queue-job draft, stages the draft into
`<runtime_root>/queue/`, and verifies review-status counts and lineage.

This fixture does not introduce production auto-approval. Approval remains an
explicit candidate state required before draft generation. The fixture also
asserts that restaging does not duplicate queue files, that staged queue
metadata preserves semantic/candidate/draft provenance, that the executable
queue payload remains unchanged from the approved draft, and that no queue
processor invocation or canonical mutation occurs.

## Stage 10 Operator Documentation

Stage 10 documents the field-capture review workflow in the field-capture README
and operator runbook. The docs describe artifact meanings and locations,
inspection commands, explicit approval boundaries, deliberate staging, and
common lineage gaps reported by `review-status`.

The documented operator command surface is intentionally honest. Candidate
collection, approved draft generation, review status, and deliberate staging
are available as CLI commands. Candidate approval itself remains manual and
explicit; there is no production approval UI, automatic approval, or bulk
approval command. Vault mutation still only happens later through the
deterministic queue processor.

## Stage 11 Operator Commands

Stage 11 exposes the helper-backed review steps as operator commands:

```text
./scripts/btq collect-action-candidates --channel field_capture
./scripts/btq generate-approved-drafts --channel field_capture
```

`collect-action-candidates` reads completed field-capture semantic artifacts and
writes review records under
`<runtime_root>/reviews/action_candidates/field_capture/`. It does not change
semantic artifacts, write `<runtime_root>/queue/`, approve anything, call the
queue processor, or mutate canonical state.

`generate-approved-drafts` reads action candidate artifacts and only processes
candidates already marked `approved`. It writes approved or failed draft
artifacts under `<runtime_root>/reviews/approved_job_drafts/field_capture/`, skips
`pending_review`, `rejected`, and `failed` candidates, and does not write
`<runtime_root>/queue/`, call the queue processor, or mutate canonical state.
For `field_capture`, approved candidates default to append-to-associated-site
note drafts when site context can be resolved; missing site context fails
closed.

## Stage 12 Candidate Review Command

Stage 12 adds explicit one-candidate review ergonomics:

```text
./scripts/btq review-candidate --channel field_capture --candidate-id <ac_...> --status approved --reviewer "Jordan" --rationale "..."
./scripts/btq review-candidate --channel field_capture --candidate-id <ac_...> --status rejected --reviewer "Jordan" --rationale "..."
```

The command locates exactly one candidate artifact, allows only `approved` or
`rejected`, preserves provenance and source fields, records reviewer,
`reviewed_at`, `review_rationale`, `prior_status`, and appends a
`review_history` entry. It fails closed when the candidate is missing,
duplicated, failed, malformed, or not an action-candidate review artifact.

This command does not generate approved drafts, stage queue jobs, call the queue
processor, approve multiple candidates, or mutate canonical state.

## Stage 13 Command Smoke Test

Stage 13 adds command-level test coverage for the operator workflow using the
same `btq` CLI dispatch layer:

```text
collect-action-candidates
review-status --json
review-candidate
generate-approved-drafts
stage-approved-drafts
review-status --json
```

The smoke fixture runs against a temporary runtime and verifies clean lineage,
staged queue metadata provenance, unchanged executable payload, no processed or
failed queue processor artifacts, no queue processor invocation, and no vault
mutation.

## Stage 14 Staging Dry Run

Stage 14 adds `stage-approved-drafts --dry-run` for field-capture approved
draft staging. The command reads the same approved draft artifacts as real
staging, validates proposed queue jobs with `queue_spec.validate_job()`,
computes the deterministic queue filename and computed job ID, and checks for
duplicates in `<runtime_root>/queue/`, `<runtime_root>/processed/`,
`<runtime_root>/failed/`, and the processed index.

Dry-run output reports whether each draft would stage, skip, or fail. It does
not write `<runtime_root>/queue/` files, staging status artifacts, review
artifacts, or vault files, and it does not invoke the queue processor. Real
staging remains a separate explicit command without `--dry-run`.

## Stage 15 Draft-Generation Dry Run

Stage 15 adds `generate-approved-drafts --dry-run` for field-capture approved
draft generation. The command reads action-candidate review artifacts, skips
non-approved candidates, maps approved candidates through the same channel-owned
draft builder used by real generation, and reports whether each candidate would
create a draft, skip, or fail.

The dry run detects an equivalent existing draft through the deterministic draft
ID and path. It does not write approved draft artifacts, failed draft artifacts,
queue files, staging status artifacts, or vault files, and it does not invoke
the queue processor. Approval remains explicit and one candidate at a time.

## Stage 16 Candidate-Collection Dry Run

Stage 16 adds `collect-action-candidates --dry-run` for field-capture candidate
collection. The command reads completed semantic artifacts, builds candidate
payloads through the same channel-owned mapper used by real collection,
computes deterministic candidate IDs and review artifact paths, and reports
whether each candidate would create, skip, or fail.

The dry run detects an equivalent existing candidate through the deterministic
candidate path. It does not write pending candidate artifacts, failed candidate
artifacts, approved drafts, queue files, or vault files, and it does not invoke
the queue processor.

## Stage 17 Candidate Listing

Stage 17 adds `list-action-candidates` as a read-only operator command for
field-capture review. It reads candidate review artifacts and reports
candidate ID, status, type, summary, confidence, rationale, source/context
preview, semantic artifact path, artifact path, and reviewer metadata when
present. It supports status filtering, JSON output, limiting, and optional
source inclusion.

The command only reads review artifacts. It does not approve candidates,
generate drafts, stage queue jobs, invoke the queue processor, or mutate the
vault.

## Stage 18 Approved Draft Listing

Stage 18 adds `list-approved-drafts` as a read-only operator command for
field-capture staging review. It reads approved job draft artifacts and reports
draft ID, status, candidate ID, proposed queue job type, proposed payload
preview, rationale, confidence, artifact provenance, and queue state evidence
when visible.

The command supports status filtering, JSON output, limiting, full payload
inclusion, and source/provenance inclusion. It only reads review and runtime
evidence; it does not generate drafts, write staging artifacts, stage queue
jobs, invoke the queue processor, or mutate canonical state.

## Stage 19 Review Item Detail

Stage 19 adds `show-review-item` as a read-only detail command for one
field-capture review artifact. Operators can inspect a candidate by
`candidate_id` before approval or a draft by `draft_id` before staging.

The candidate view includes full source text/context, provenance, channel
metadata, review metadata, and review history. The draft view includes the full
proposed queue payload, candidate/semantic/transcript provenance, approval
metadata, queue state evidence, and staging status details when present.

The command requires exactly one ID and fails closed when the target is missing
or duplicated. It does not approve candidates, generate drafts, stage queue
jobs, invoke the queue processor, or mutate canonical state.

## Stage 20 Maintenance Visibility

Stage 20 adds `review-maintenance-status` as a read-only report for
field-capture review artifact accumulation. It counts candidates, drafts, and
staging artifacts; reports stale pending candidates, old rejected candidates,
failed artifacts, approved candidates without drafts, approved drafts without
staging status, staged drafts without queue/processed/failed/index evidence,
orphaned staging statuses, queue files pointing to missing drafts, approximate
disk usage, and oldest/newest artifact timestamps.

`stale` means the item may need human attention. It does not mean the artifact
is wrong or safe to delete. This stage does not delete, archive, repair,
approve, reject, restage, invoke the queue processor, or mutate canonical state.

## Stage 21 Review Dashboard

Stage 21 adds `review-dashboard` as a read-only operator summary for the
field-capture review workflow:

```text
./scripts/btq review-dashboard --channel field_capture
./scripts/btq review-dashboard --channel field_capture --json
```

The dashboard combines existing read-only reports into one fast orientation
view. It shows candidate counts and pending previews, approved draft counts and
queue-state previews, maintenance finding counts, review disk usage, and a
suggested next command for the operator.

The suggested next command is advisory only. It points operators toward
candidate collection when semantic artifacts have uncollected action
candidates, candidate listing/review when pending candidates exist,
draft-generation dry-run when approved candidates have no draft, staging
dry-run when approved drafts are not staged, maintenance status when failed or
unresolved review artifacts need attention, or a reminder that queue processing
is separate when queue jobs are already staged.

The dashboard does not collect candidates, approve or reject candidates,
generate drafts, stage queue jobs, clean or repair artifacts, invoke the queue
processor, or mutate canonical state. Detailed commands remain the action and
inspection surface.

## Stage 22 VPS To Mac Bundle Bridge

Stage 22 adds a copy-only import command for one field-capture capture bundle:

```text
./scripts/btq pull-field-capture --capture-id <capture_id> --bundle-path <bundle>
./scripts/btq pull-field-capture --capture-id <capture_id> --bundle-path <bundle> --dry-run --json
```

The command imports a local exported bundle with this shape:

```text
<bundle>/
  uploads/<YYYY-MM-DD>/<capture_id>/...
  queue/<matching-photo-capture-job>.json
```

This reflects the production topology: the deployed field-capture SPA writes
media under `/srv/btq/runtime/uploads`, while the Mac review pipeline reads
`/Users/operator/btq_runtime`. A processor-ready pull requires both the media
directory and the matching `photo_capture` queue JSON because field-capture
audio transcription discovers audio through imported intake metadata.

The import command verifies the queue JSON references the requested
`capture_id`, verifies referenced media exists in the bundle, copies media to
`<runtime_root>/uploads/<date>/<capture_id>/`, copies the `photo_capture`
intake JSON to `<runtime_root>/field_capture/intake/`, and rewrites copied
intake media `stored_path` values to the local Mac runtime. It is idempotent and
refuses non-identical local overwrites. `--dry-run` reports the planned copies
without writing.

Imported `photo_capture` metadata is not an executable canonical mutation job and
must not be written to the Mac general `<runtime_root>/queue/`. That queue
remains reserved for validated executable mutation jobs, including the jobs
produced later by explicit approved-draft staging.

Stage 22 originally kept this as a local bundle import boundary. The current
operator bridge also includes a direct VPS pull command:

```text
./scripts/btq pull-vps-field-captures --dry-run --json
./scripts/btq pull-vps-field-captures --json
./scripts/btq pull-vps-field-captures --capture-id <capture_id> --json
./scripts/btq pull-vps-field-captures --watch --poll-seconds 30
```

The direct puller discovers remote `photo_capture` JSON under
`/srv/btq/runtime/queue`, copies matching media from `/srv/btq/runtime/uploads`,
and feeds the same safe local bundle importer. On the VPS,
`/srv/btq/runtime/queue` is the remote field-capture upload metadata source. It
is not treated as the Mac executable mutation queue. It supports `--dry-run`,
`--capture-id`, `--limit`, `--watch`, `--poll-seconds`, and `--json`.

The direct puller deliberately imports remote metadata into the Mac
non-executable intake path, `<runtime_root>/field_capture/intake/`. The Mac
executable mutation queue remains `<runtime_root>/queue/`, and only approved
draft staging writes executable jobs there. `<runtime_root>/queue/` is not
field-capture intake. `<runtime_root>/field_capture/intake/` is not executable
mutation work. The bridge does not run transcription, semantic processing,
candidate collection, queue staging, queue processing, cleanup, remote
deletion, or canonical mutation.

## Stage 23 Local Ops Dashboard

Stage 23 adds a local read-only Mac dashboard:

```text
./scripts/btq ops-dashboard
https://workstation.example.ts.net/
./scripts/btq ops-dashboard --host 127.0.0.1 --port 8765
```

HTTPS via `scripts/btq-setup-tailscale-serve` (see `docs/runbook.md` §
Dashboard URLs).

The dashboard exposes `GET /`, `GET /api/status`, and `GET /healthz`. It shows
runtime health, field-capture uploads and intake records, transcript and
semantic artifact counts, review-dashboard state, maintenance findings, and
recent log warning/error lines.

This is visibility only. V1 has no POST mutation routes, no approval controls,
no draft generation, no queue staging, no queue processor invocation, no remote
VPS sync/export operation, and no canonical mutation. It should bind to localhost by
default; Tailscale access should use a trusted private bind or tunnel, not a
public interface.

## Stage 30 Field-Capture Review UI

Stage 30 adds a narrow human review surface to the local ops dashboard:

```text
GET /field-capture/review
POST /field-capture/review/approve
POST /field-capture/review/reject
```

The page renders field-capture action candidates as readable cards with status,
site/capture metadata, summary, context, review history, and source artifact
paths. The POST routes require `candidate_id`, `reviewer`, and `rationale`, and
only approve or reject exactly one `pending_review` candidate. They call the
same candidate review helper used by `review-candidate`.

These routes mutate candidate review artifacts only. They do not generate
approved drafts, stage queue jobs, invoke the queue processor, run
transcription or semantic processing, sync VPS captures, delete/repair
artifacts, or mutate canonical state. The dashboard remains for localhost, Tailscale,
or another trusted private path only.

## Stage 31 Field-Capture Pipeline Watcher

Stage 31 adds a Mac-side watcher command for the non-vault intake path:

```text
./scripts/btq watch-field-capture-pipeline --poll-seconds 60
./scripts/btq watch-field-capture-pipeline --once --json
```

One cycle runs the safe intake-to-review stages in order: VPS pull, local field
audio transcription, local semantic processing, local photo vision sidecar
generation, and action candidate collection. Transcription is limited to one
not-yet-terminal audio asset per cycle by default, and photo vision is limited
to one not-yet-terminal image asset per cycle by default. The watcher creates
the transcriber only for the transcription pass and invokes the local Ollama
vision client as a serial sidecar-only pass, so slow Mac processing can chew
through backlog without parallel workers.

The watcher is idempotent through the existing stage boundaries: already
imported captures, terminal transcripts, terminal semantic artifacts, terminal
photo vision sidecars, and existing action candidates are skipped by their
owning stage. Failure in one stage is reported in the cycle summary and logged;
photo vision failures remain failed sidecars or failed cycle steps and can be
retried later with explicit `describe-field-photos --replace-failed` commands.
The watcher does not advance to approval, draft generation, staging, or queue
processing.

The watcher never approves or rejects candidates, never generates approved
drafts, never stages queue jobs, never invokes the queue processor, never
deletes local or remote files, never cleans VPS uploads, never calls cloud
vision APIs, never publishes client-facing content, and never mutates the vault.
Candidate review remains a human UI/CLI step, and draft generation, staging,
and deterministic vault writing remain deliberate later steps.

## Stage 37 Photo Vision Sidecars

Stage 37 adds a local-only photo description layer for field-capture images:

```text
raw image media + intake JSON
-> photo vision sidecar
```

The sidecars live under
`<runtime_root>/field_capture/photo_vision/<photo_asset_id>.json` and use
`artifact_type: field_capture_photo_vision`. They preserve provenance to the
imported intake JSON, capture id, stable photo asset id, source image path,
source image hash, submitted area/phase, model name/provider, generated time,
and the advisory fields `description`, `area_guess`, `visible_objects`,
`possible_conditions`, `possible_issues`, `confidence`,
`needs_human_review`, and `warnings`.

```text
./scripts/btq describe-field-photos --channel field_capture --dry-run --json
./scripts/btq describe-field-photos --channel field_capture --json
```

The command discovers images from `<runtime_root>/field_capture/intake/`,
resolves media under `<runtime_root>/uploads`, skips existing terminal sidecars,
and writes only photo vision sidecars. Dry-run reports what would be created and
writes nothing. It supports filtering by capture id, site id, date, and limit.

This layer is intentionally outside the mutation path. It does not create review
candidates, approve or reject candidates, generate approved drafts, stage queue
jobs, invoke the queue processor, publish to clients, or mutate canonical state. It
does not score cleanliness, rank employees, judge work quality, or select best
photos. The local vision model describes what appears visible, using cautious
language, and human review remains authoritative.
