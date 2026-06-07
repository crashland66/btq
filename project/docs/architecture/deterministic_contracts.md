# Deterministic Contracts

## Guaranteed

For a fixed code version and fixed input files:

- Queue files are processed in sorted filename order within one `process_all()` pass.
- A real queue processor pass must acquire `runtime/temp/processor.lock` before reading the queue.
- An existing live processor lock refuses startup; a dead-process lock is treated as stale and replaced.
- Job payloads must satisfy `queue_spec.validate_job()` before mutation.
- Canonical targets are resolved by queue handlers before CouchDB mutation.
- Runtime root is rejected if it is under `pipeline_dir` or appears to be iCloud-managed.
- Projection text writes use temp-write, fsync, and `os.replace`.
- Moves use `os.replace` where possible and copy/fsync/replace/unlink for `EXDEV` and `EDEADLK`.
- Successfully applied canonical mutations record computed job IDs in `btq_job_ids`.
- Successful supported mutations write lightweight observational evidence snapshots under `<runtime_root>/evidence/<capture_id>/<job_id>.json`.
- Successfully processed queue files append a record to `<runtime_root>/processed_index.jsonl` when possible.
- New capture flows write observational manifests under `<runtime_root>/manifests/<capture_id>.json`.
- Reprocessing an already marked job should not duplicate most supported mutations.
- Failed jobs are moved to `failed/` unless the failure also prevents the move.
- Personal journal jobs write to `personal_vault_dir`, not the operational vault.
- Replay execution requires explicit `--approve`; dangerous replay requires `--force-dangerous-replay`.
- Epistemic metadata is preserved when provided or deterministically classified from explicit patterns.

## Explicitly Not Guaranteed

- Whisper transcript correctness.
- Complete event extraction.
- Correct site resolution for all spoken variants.
- Exactly-once queue processing across crashes or machines.
- Global transactional atomicity across CouchDB write and queue-file movement.
- Always-current Markdown projection/export files.
- Durable queue ordering beyond filename sort.
- Safe behavior for arbitrary YAML frontmatter.
- Recovery from manually edited or corrupted `processed/` history when neither the processed index nor target markers remain useful.
- Automatic repair of ambiguous marker/content/index divergence.
- Automatic retry of failed queue jobs.
- Autonomous replay or hidden replay execution.
- Semantic correctness of replayed operational facts.
- Semantic certainty from markers, evidence snapshots, fingerprints, or replay previews.
- Epistemic certainty from narrative reports or contradiction links.
- Cross-machine consistency.

## Idempotency Mechanisms

The main idempotency key is `compute_job_id(job)`, a SHA-256 hash of canonical JSON containing:

```text
job_type
payload
```

The top-level `job_id` field is ignored by the computed key. This is good for deduplicating equivalent jobs generated with different labels, but it can surprise authors who expect `job_id` to be authoritative.

Idempotency is enforced through:

- scanning `processed/*.json` for equivalent computed job IDs
- checking `<runtime_root>/processed_index.jsonl` for equivalent computed job IDs
- checking `<runtime_root>/idempotency_keys.jsonl` for completed keyed `add_person` mutations
- checking canonical CouchDB employee docs for `add_person` duplicate normalized names and employee IDs
- checking canonical CouchDB target docs for `btq_job_ids`
- duplicate content/evidence checks for some append and visit cases
- checking unknown `source_unknown_id` across `queue`, `processed`, and `failed`

The processed index is preferred over scanning and keeps the common lookup path from opening every processed job file. If the index is absent or has no matching record, the processor falls back to scanning `processed/*.json` for backward compatibility. Corrupted historical processed files are skipped during fallback scans.

`add_person` also supports an optional top-level `idempotency_key`. A completed key is appended only after a successful mutation. Replaying the same key with the same payload is treated as a safe no-op before duplicate checks; replaying the same key with a different payload fails closed. Failed jobs do not complete keys. For new executions, CouchDB employee documents are the duplicate authority; Markdown person-file checks remain projection defense-in-depth.

Weakness: the processed index is append-only JSONL, not a transaction log. A crash after canonical CouchDB write and queue movement but before index append can still leave the index behind the canonical marker and processed file.

`btq repair-index` treats the index as derived state. Dry-run is the default. `--force` may rebuild missing index entries from processed queue files, but the command does not silently repair canonical content or markers.

## Runtime Lineage

Audio ingestion creates a `capture_id` and persists it in transcript metadata, event JSON, generated queue-job metadata, process logs, processed-index records, structured queue logs, and observational manifests. Older artifacts may not have a `capture_id`; this is expected during migration.

## Semantic Evidence

Evidence snapshots and target fingerprints are observational. They preserve lightweight context around a mutation and help detect drift, but they do not prove operational meaning. Queue-job `intent` metadata is advisory and is not part of the computed idempotency key.

## Epistemic State

Epistemic state separates observations, human reports, inferences, assumptions, contradictions, stale truths, and disputed claims. These labels are deterministic metadata and review aids. They do not become canonical truth and do not override raw observations.

## Marker Semantics

`btq_job_ids` means "this computed job payload was applied to this canonical target or intentionally treated as applied." It is not a cryptographic proof that all intended side effects occurred. It also does not record handler version, timestamp, actor, or previous content hash.

Markdown projection marker compatibility depends on the custom frontmatter parser in `queue_processor/idempotency.py`. The parser supports simple scalar keys and simple two-space-indented list items, and tolerates ignored continuation lines in writer-created person assignment blocks. It is not a full YAML parser.

## Queue Ordering Assumptions

Queue order is lexicographic filename order. The system assumes producers use timestamped filenames when ordering matters. The runtime does not maintain an enqueue timestamp index, monotonic sequence, lease, visibility timeout, or broker acknowledgement protocol.

## Atomicity Assumptions

File content replacement is atomic at the target path if the underlying filesystem honors `os.replace`. The code fsyncs file contents but does not fsync parent directories after rename. A sudden power loss may still expose filesystem-specific edge cases.

The system does not atomically bind:

- queue claim
- target read
- target write
- marker update
- processed move
- processed index append
- evidence snapshot write
- replay audit log append
- log write

Those are separate operations.

## Filesystem Dependency Assumptions

BTQ assumes:

- local runtime storage is more reliable than iCloud transport storage
- single-process queue draining is the normal operating mode
- CouchDB canonical documents are the authoritative operational records
- path containment via `resolve()` and `commonpath()` is sufficient for configured projection/media roots
- Markdown can be regenerated as projection/export from canonical state
- queue jobs are small JSON files
- processed/failed files are durable enough for audit and dedupe

These assumptions are reasonable for a local-first prototype. They are not sufficient for a multi-writer distributed system.
