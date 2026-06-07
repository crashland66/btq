# Architecture Review Findings

## Strengths

- The deterministic writer boundary is the right core architecture.
- Runtime storage is correctly separated from iCloud transport paths.
- Queue jobs are inspectable JSON and have a validation gate.
- Atomic write and safe move helpers address real filesystem failure modes.
- Idempotency is tested for key handlers and crash-after-write-before-move windows.
- Unknown captures prevent silent loss from incomplete extraction.
- Personal journal routing is separated from operational memory.
- Nightly digest design acknowledges ambiguity and cross-source conflicts.

## Weaknesses

- No global transaction protects vault write plus queue movement.
- No single-writer lock prevents concurrent queue processors.
- Processed-job dedupe relies on scanning retained JSON files.
- Frontmatter parsing is narrow and not YAML-complete.
- Static site registry can drift from vault reality.
- Queue validation is shallow for several payload fields.
- Audit records are distributed across logs, markers, queue files, and target files.
- No automated retention or compaction strategy exists.

## Hidden Assumptions

- Only one queue consumer runs at a time.
- Filename order is good enough as queue order.
- Operators will not delete or corrupt processed files.
- Markdown target files remain parseable enough for custom frontmatter logic.
- iCloud vault sync will not collide with processor writes.
- Site aliases in code are current.
- Human review will catch uncertain or unknown captures.

## Technical Debt

- Static site registry.
- Ad hoc frontmatter parsing.
- File-directory processed index.
- Mixed markdown document model for unknown captures.
- Limited job schema expressiveness.
- No structured run manifest tying source audio, transcript, events, jobs, and writes together.

## Immediate Fixes

- Add a queue processor lock with stale-lock recovery.
- Add a processed-job index or manifest file updated atomically.
- Add validation for unexpected payload fields and stronger field types.
- Document and test manual failed-job recovery.
- Add a health command that reports queue, failed, unknown, and stale claimed files.

## Medium-Term Improvements

- Generate site registry from vault metadata or move it to config/data.
- Store applied-job records with target path, content hash, handler version, and timestamp.
- Add replay tooling with dry-run diff output.
- Add retention policy for processed jobs, temp files, transcripts, and event artifacts.
- Build a structured unknown-review workflow.

## Long-Term Evolution

- Move queue state and processed history into SQLite.
- Treat markdown as a projection of structured operational events.
- Introduce immutable source-event storage for audio/transcript/event/job lineage.
- Consider CouchDB/PouchDB if offline replicated document sync becomes a product requirement.
- Consider a broker only when there are multiple workers, remote producers, or throughput requirements.

## Architecture Classification

BTQ is a hybrid architecture:

- Event-driven characteristics: producers create event-like records, queue jobs represent event notifications, and consumers react to those jobs. This aligns with common EDA layers: producers, channels, processing, and downstream reactions as described in event-driven architecture references.
- Staged event-driven characteristics: transcription, extraction, enrichment, validation, queue conversion, and vault mutation are separate stages with file artifacts between them. It resembles SEDA at the level of decomposition into stages connected by queues, but it lacks SEDA's dynamic resource controllers, admission control, and high-concurrency scheduling model.
- Append-only operational memory system: journals, unknown captures, visit gaps, processed jobs, logs, and digests preserve history rather than only current state.
- Deterministic mutation pipeline: this is the strongest classification. The queue processor is the authority for mutation and constrains AI/probabilistic output.
- Operational knowledge capture system: the domain model is built around preserving field context, unresolved ambiguity, and daily synthesis.

It is not a full distributed event-driven architecture. There is no broker, subscriber topology, stream processor, durable event log, or multi-consumer coordination. The best description is: local-first hybrid operational knowledge capture system with staged event processing and a deterministic mutation pipeline.

References used for classification:

- [Event-driven architecture](https://en.wikipedia.org/wiki/Event-driven_architecture)
- [Staged event-driven architecture](https://en.wikipedia.org/wiki/Staged_event-driven_architecture)
