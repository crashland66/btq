# Runtime Flow

## Audio Ingestion

`transcription_pipeline.main.scan_once()` creates local runtime directories, scans `audio_inbox_dir`, and considers only `.m4a`, `.mp3`, and `.wav` files. A file is eligible when its modification time is at least `stable_seconds` old. The default is 10 seconds.

For each eligible audio file:

1. If a sibling `.processed` marker exists, the audio is claimed and archived without transcription.
2. Otherwise a fingerprint is recorded from the source path.
3. A `capture_id` is created for this capture run.
4. The file is claimed by moving it through a `.claiming` path into `<runtime_root>/claimed/audio/`.
5. `process_audio_file()` moves it into `local/audio_processing/`.
6. Whisper generates `<audio>.<ext>.whisper.txt`.
7. Transcript metadata is written beside the transcript and includes `capture_id`.
8. The event pipeline writes normalized transcript, correction sidecars, and event JSON carrying `capture_id`.
9. Event JSON receives deterministic epistemic metadata when explicit patterns are present.
10. Valid events are converted to queue jobs with top-level metadata carrying `capture_id`, advisory intent, and epistemic state.
11. Jobs are staged into `<runtime_root>/queue/` through a temp file and `os.replace`.
12. A per-audio log is written with `capture_id`.
13. An observational lineage manifest is written under `<runtime_root>/manifests/<capture_id>.json`.
14. Audio is moved to completed audio archive.

If processing fails after claim, the local audio is moved to `<runtime_root>/failed/audio/` when possible and the exception is logged.

## Transcript Generation

`build_transcriber()` loads Whisper `large-v3` for the enhanced path. It uses beam search and a domain prompt. Compare mode may also run a baseline model and write `original.txt`, `enhanced.txt`, and `diff.txt`, but downstream processing uses the enhanced transcript.

The transcript is not a deterministic artifact in the same sense as queue processing. It depends on model implementation, installed package versions, hardware behavior, and audio quality.

## Event Generation

`event_pipeline.main.process_transcript()`:

1. Reads the raw transcript.
2. Normalizes domain terms via `domain_resolver.normalize_text()`.
3. Writes `.normalized.txt` and `.corrections.json`.
4. Extracts raw events with `extractor.extract_events()`.
5. Enriches and consolidates events.
6. Validates events and separates `events_valid/` from `events_failed/`.
7. Updates `state/last_site.json` when a known site appears with medium or high confidence.

Extraction is deterministic for a given transcript and code version, but it is heuristic. It does not guarantee complete or correct event detection.

## Queue Job Creation

`event_to_queue.adapter.event_to_job()` maps validated events to executable job types:

- `staffing_risk` -> `trigger_recruiting`
- `employee_retention_risk` -> `flag_retention_risk`
- `access_constraint` -> `flag_access_constraint`
- `employee_resigned` -> `remove_from_schedule`
- explicit structured person creation -> `add_person`
- journal-style events -> `append_to_note`
- `site_observation` -> site note or unknown journal append

When event extraction is empty or partial, `emit_missed_capture_job()` preserves
the transcript as a reviewable note. If a known site can be resolved from valid
events or from the transcript text, it creates an `append_to_note` job
containing a `site_audio_memo` block on that site note. If no site can be
resolved, it creates an `unknown_capture` block in
`Journal/YYYY-MM-DD-unknown.md`.

When a transcript begins with a personal-journal trigger, extraction is bypassed and a `personal_journal_entry` job is emitted for `personal_vault_dir`.

## Queue Transport Ingestion

The configured pipeline outbox is a transport drop location, not authoritative runtime state. It may live on iCloud-backed storage, and that storage can temporarily refuse reads. The queue watcher therefore treats `pipeline_dir/outbox/*.json` as retryable ingress only.

For each stable outbox file:

1. The watcher attempts to copy the readable transport file into local runtime intake at `<runtime_root>/intake/outbox/`.
2. Only a verified non-empty local intake copy is staged into the durable local queue at `<runtime_root>/queue/`.
3. If the transport file is unreadable, the source remains in outbox for a later retry and no runtime failed job is created.
4. Once a file is already present in local queue, processed history, or failed processing state, runtime state is authoritative.
5. A stale duplicate transport artifact may be removed safely because it must not resurrect or re-archive an already handled job.

Runtime archives are provenance records for local processing state. They are not mirrors of transport directories. In particular, `<runtime_root>/processed/`, `<runtime_root>/failed/`, `<runtime_root>/processed_index.jsonl`, idempotency records, and vault `btq_job_ids` are the dedupe surfaces that prevent replay. Transport cleanup must not create new runtime archive entries for jobs that runtime has already handled.

## Queue Processing

`queue_processor.main.process_all()`:

1. Validates project, vault, and personal vault roots.
2. Refuses runtime roots inside `pipeline_dir` or iCloud-looking paths.
3. Creates `queue`, `processed`, `failed`, temp, and log directories.
4. Acquires `runtime/temp/processor.lock`; live locks refuse startup and dead-process locks are recovered.
5. Loads valid site IDs from vault account location files.
6. Builds a site ID -> opportunities directory cache.
7. Processes queue files in sorted filename order.
8. Appends processed-index records for successful processed jobs when possible.
9. Runs unknown reclassification after the queue pass.
10. Releases the processor lock.

Each job:

1. Must be JSON.
2. Must match `queue_spec.validate_job()`.
3. Gets a computed job ID from canonical JSON containing only `job_type` and `payload`.
4. Is skipped if an equivalent computed job ID already exists in `<runtime_root>/processed_index.jsonl`; if the index is missing or has no matching record, `<runtime_root>/processed/` is scanned for backward compatibility.
5. Is routed to a deterministic handler.
6. On success, mutates canonical CouchDB state through the handler and moves the job to `processed/`.
7. A processed-index record is appended to `<runtime_root>/processed_index.jsonl` with computed job ID, job type, target path, timestamp, handler version, source queue file, run ID, and capture ID when known.
8. Supported successful mutations write lightweight evidence snapshots under `<runtime_root>/evidence/<capture_id>/<job_id>.json`.
9. If a capture ID is known, the corresponding manifest records the observed canonical mutation and processed record.
10. On failure, moves the job to `failed/` and writes a log line.

Queue ingestion must be idempotent. A repeated queue file with the same executable payload must either be skipped by processed-index/job-marker checks or fail closed on an idempotency-key conflict. A repeated transport file with a filename already represented in runtime state is transport residue, not a new executable job.

## Repair And Inspection

`btq repair-index` scans processed queue files, processed-index records, canonical `btq_job_ids`, structured queue logs, and manifests. It reports divergence rather than treating any derived index as canonical truth. It is dry-run by default; `--force` is required before rebuilding missing derived index rows.

`btq inspect-runtime` scans stale claimed audio, abandoned temp files, old failed jobs, and manifest references to missing artifacts. It reports findings only.

## Replay Flow

Replay is explicit and operator-mediated:

1. `btq replay-plan` selects candidates from failed jobs, missing-index cases, manifest gaps, specific capture IDs, specific job IDs, or a named queue file.
2. Each candidate receives a replay risk classification and current target state summary.
3. `btq replay-dry-run` produces `BEFORE`, `AFTER`, `DIFF`, and risk notes without mutating canonical state.
4. `btq replay-execute` refuses to mutate unless `--approve` is present.
5. Dangerous replay risks are refused unless `--force-dangerous-replay` is also present.
6. Replay activity is logged to `runtime/logs/replay_events.jsonl`.

Expected-state reconstruction is inferred from queue jobs, manifests, logs, markers, and current canonical CouchDB content. It is an inspection aid, not authoritative truth.

Replay planning also consults mutation evidence snapshots when present. Fingerprint drift can lower replay confidence to `LOW_SEMANTIC_CONFIDENCE`, but this is a heuristic warning rather than semantic proof.

## Semantic Reconciliation

`btq reconciliation-report` reads evidence snapshots, repair findings, manifests, replay candidates, and current canonical state. It reports semantic drift indicators, unresolved ambiguities, orphaned evidence, replay risks, mutation confidence summaries, stale operational references, and lineage gaps.

## Epistemic Narrative

`btq narrative-report` reads mutation evidence and contradiction artifacts to produce timelines grouped by observations, human reports, inferences, assumptions, unresolved ambiguity, and other epistemic states. It preserves contradiction links when requested. It is a contextual reconstruction, not an authoritative truth generator.

## Epistemic Governance

`btq unresolved-report` reads contradiction artifacts, evidence snapshots, replay/reconciliation risk, and governance records under `runtime/reviews/`. It surfaces unresolved contradictions, stale assumptions, unreviewed inferences, high-risk unresolved narratives, and disputed operational states.

Review, dispute, escalation, and acknowledgment records are provenance. They do not mutate the underlying evidence and do not adjudicate truth. Acknowledgment can reduce repeated surfacing for a target, but it does not delete the original ambiguity.

Important detail: input `job_id` is not the authoritative idempotency key. `compute_job_id()` ignores the top-level `job_id` and hashes `job_type + payload`. Top-level `metadata`, including `capture_id`, is also outside the computed key. This means two queue files with different top-level `job_id` values but identical executable payloads are treated as the same job.

## Canonical Mutation

Mutation handlers use:

- canonical target resolution
- job-type-specific target resolution
- `has_job_been_applied()` checks against `btq_job_ids`
- duplicate-content/evidence checks for some append cases
- CouchDB read-modify-write through `btq_vault` / `canonical_rmw`
- optional Markdown projection writes after canonical mutation
- `move_job_file()` for queue archival

There is no transaction across the CouchDB write and queue-file movement. The intended crash-safe behavior is:

```text
write canonical target with job marker succeeds
crash before move to processed
queue file remains
next run sees marker or processed equivalent
moves/skips without duplicate mutation
```

Tests explicitly cover crash-after-write-before-move for append and visit creation.

## Marker Creation

Successful canonical writes add `btq_job_ids` to the target CouchDB document. The marker contains the computed job ID. Markdown projection may also render compatible marker/frontmatter data for human-readable exports.

Legacy Markdown projection handling for files whose existing frontmatter has `type: unknown_capture` may prepend a new outer frontmatter block rather than modifying the unknown-capture block directly. This is pragmatic but structurally awkward because unknown capture projections can contain multiple frontmatter blocks.

## Retries and Failure States

## Add Person Identity and Replay

`add_person` is a deterministic entity-creation job. The job payload never supplies a vault path or `person_id`.

- the writer generates `People/<Name>.md` as the initial presentation path
- the writer generates a permanent `person_id` with a `per_` prefix
- `person_id` is the stable identity anchor for future cross-linking; filenames and names are not identity
- duplicate `employee_id` or normalized name collisions fail closed against canonical CouchDB employee docs; Markdown duplicate checks remain projection defense-in-depth
- optional top-level `idempotency_key` values are recorded only after successful mutation in `<runtime_root>/idempotency_keys.jsonl`
- replay with the same idempotency key and same payload is a safe no-op
- replay with the same idempotency key and different payload fails closed
- failed jobs do not complete idempotency keys

There is no automatic merge behavior and no implicit mutation of existing person records.

Queue-job failure is simple: the job file is moved to `<runtime_root>/failed/`. There is no automatic retry loop for failed queue jobs. Human operators can inspect and requeue or correct failed jobs manually.

Unknown captures have built-in retry semantics:

- unresolved entries have `retry_count` and `last_attempted`
- retry stops at 3 attempts
- delay is exponential: `2 ** retry_count` hours
- manual edits or detected site signals can trigger reclassification
- successful reclassification stages new jobs and marks the unknown entry resolved

Audio ingestion retries are indirect. If a file is still in the inbox and processing failed before claim, it remains eligible. If failure occurs after claim, the local failed audio is moved under runtime failed storage; it is not automatically reprocessed from the original inbox path.

## Ordering Guarantees

The queue processor uses sorted filesystem iteration over queue files. This is deterministic for a directory snapshot but is not a broker-level ordering guarantee. Job filenames often include timestamps, so operational ordering depends on producer naming discipline.

There is no cross-file transaction and no formal dependency scheduler. When order matters, the producer must encode it in filenames or avoid concurrent conflicting jobs.
