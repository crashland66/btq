# Executive Summary

BTQ is a local-first field-operations pipeline that turns voice memos, field captures, structured queue jobs, and operational triggers into controlled canonical CouchDB updates with preserved evidence. Its primary purpose is not general automation; it is durable operational memory for Clearpath field management: staffing risks, access constraints, site observations, visit anchors, supply orders, unknown captures, and nightly digests. Markdown is a human-readable projection/export of canonical state, not the authoritative mutation target.

The core architectural philosophy is a strict mutation boundary:

- AI or probabilistic components may generate transcripts, inferred events, or candidate jobs.
- Deterministic Python code validates jobs and performs canonical writes through the queue processor and handlers.
- Canonical writes target CouchDB `btq_vault`; Markdown projection/export is opt-in and downstream of canonical state.

This separation is the system's most important strength. It gives BTQ a reviewable queue contract, deterministic write handlers, replay behavior, and an audit trail through queue files, processed files, failed files, per-run logs, CouchDB evidence, and applied `btq_job_ids` markers.

The current system is mature enough for a single-operator local workflow, with CouchDB as the canonical operational store and local processing for transcription, semantic cleanup, queue validation, and deterministic mutation. It is not yet a high-concurrency architecture. Queue processing remains constrained by the single queue processor / handler boundary, while CouchDB provides the canonical entity store and replication surface.

Major strengths:

- Clear boundary between candidate intent and deterministic mutation.
- Schema-checked queue jobs in `project/queue_spec.py`.
- CouchDB canonical writes through `btq_vault` / `canonical_rmw`.
- Runtime queue directories for `queue`, `processed`, and `failed`.
- Crash-after-write recovery for many handlers through job markers and canonical idempotency checks.
- Raw capture preservation plus metadata/sidecars before local processing.
- Tests cover ingestion stability, queue staging, idempotency, unknown retry behavior, visit gaps, and key write paths.

Major risks:

- No global transaction spans CouchDB mutation plus queue-file movement.
- Processed-job history is a directory scan, not a durable index.
- Multiple queue processors could race because no global processing lock is enforced.
- Markdown projection/frontmatter parsing remains intentionally narrow where projection compatibility is needed.
- Queue schemas validate required fields but permit many optional or semantically weak fields.
- Optional Markdown export/projection can drift from CouchDB until regenerated.
- Event extraction is heuristic and incomplete; confidence is not a formal probabilistic guarantee.
- Site routing depends on configured CouchDB site registry access, with static fallback only when CouchDB is not configured.

Current maturity level: strong prototype / early operational system. The architecture demonstrates good instincts around deterministic mutation and recoverable queues, with CouchDB now serving as canonical operational storage rather than Markdown files.

Future direction should preserve the current boundary while hardening CouchDB backup/restore, replication, queue indexing, and projection tooling.
