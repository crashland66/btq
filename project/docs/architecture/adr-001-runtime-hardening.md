# ADR-001 Runtime Hardening

## Status

Accepted for Phase 1.

## Context

The architecture review identified operational risks around concurrent queue processors, processed-job dedupe scans, fragmented auditability, weak observability, replay ambiguity, and stale runtime artifacts. The system must remain local-first, markdown-backed, deterministic at the writer boundary, and easy to inspect.

## Decision

Add a local single-processor lock at `<runtime_root>/temp/processor.lock`.

The lock is acquired with exclusive file creation and records pid, hostname, start timestamp, and process command when available. If the recorded process is alive, startup is refused. If the process is dead, the lock is treated as stale and replaced.

Add `<runtime_root>/processed_index.jsonl`.

Successful processed jobs append records containing computed job ID, job type, target path, timestamp, handler version, source queue file, run ID, and capture ID when known. Dedupe prefers the index and falls back to scanning `processed/*.json` when the index is missing or has no matching record, preserving older processed history during rollout.

Add `capture_id` lineage.

New audio captures generate a capture ID and carry it through transcript metadata, event JSON, queue job metadata, processed-index records, process logs, and structured queue logs. Older artifacts can remain without capture IDs.

Add `btq health`.

The health command reports lock status, queue depth, failed count, stale claimed audio, unknown capture count, processed index status, old failed jobs, and runtime disk usage. It exits non-zero for stale locks, queue backlog above threshold, and corrupted processed indexes.

Add local structured JSONL logs.

The queue processor writes lifecycle, lock, dedupe, replay-skip, and processed-index events to `runtime/logs/queue_processor_events.jsonl`.

## Tradeoffs

This keeps the system inspectable and avoids introducing SQLite, distributed queues, leases, or worker coordination before they are needed.

The processor lock is only a local filesystem guard. It is not a distributed lock and does not make vault writes transactional.

The processed index improves lookup and auditability, but it is append-only JSONL. It can lag behind vault markers if a crash happens after mutation and queue movement but before index append.

The capture ID improves forensic reconstruction, but it is not a semantic proof that Whisper, extraction, or routing was correct.

The health command reports stale artifacts and critical conditions but does not repair them automatically.

## Remaining Risks

- No transaction binds target write, frontmatter marker, processed move, processed-index append, and logs.
- Manual edits can still create marker/content divergence.
- Replay remains implicit rather than a first-class dry-run/diff workflow.
- Processed-index corruption requires operator intervention.
- Runtime retention and compaction are still manual.

## Consequences

Phase 1 hardening reduces accidental concurrent processors and makes processed history, run lineage, and health state easier to inspect without changing the markdown vault model or deterministic writer boundary.
