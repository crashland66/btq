# Architecture Decisions

## Markdown Vault as Primary Store

Decision: store operational memory in a file-based markdown vault.

Rationale: human-readable, Obsidian-compatible, easy to inspect, easy to edit, low infrastructure burden.

Tradeoffs: weak transactional semantics, difficult indexing, sync conflicts, ad hoc schema enforcement.

Alternatives rejected or deferred: SQLite, CouchDB/PouchDB, full event store, hosted database.

Future implication: migration will require extracting structured records from markdown plus preserving human narrative context.

Status (2026-05-20): superseded as direction by ADR-007. CouchDB becomes the source of truth and the vault becomes a generated, read-only projection. The migration is staged and incremental; this decision holds for each slice until that slice is moved. See `adr-007-couchdb-source-of-truth.md`.

## Deterministic Writer Boundary

Decision: AI/probabilistic stages do not directly mutate the vault.

Rationale: vault changes should be schema-checked, path-constrained, idempotent, and replayable.

Tradeoffs: more code and queue plumbing; slower feature iteration than free-form agent edits.

Future implication: this boundary should be retained even if the datastore changes.

## External BTDocs Projection

Decision: AI bootstrap, configuration, instruction, and architecture documents are source-controlled repository artifacts exported to an iCloud `BTDocs` directory outside the operational vault.

Rationale: the operational vault should contain operational truth only. AI setup documents are configuration context, not business records. Keeping them outside the vault prevents prompts, schemas, bootstrap notes, and agent instructions from being mistaken for operational state.

Tradeoffs: one explicit export step is required after docs change; the iCloud copy is a projection and can drift until `./scripts/btq-export-docs` is run.

Provenance: exported markdown files receive a machine-readable `BTQ_*` metadata header with docs schema version, export timestamp, source path, exporter version, and repository commit. `BTDocs/export_manifest.json` records exported document hashes. The exporter preserves existing timestamps on no-op exports to avoid rewrite churn while still making stale projections detectable.

Future implication: add new AI/bootstrap docs by updating `project/docs/docs_export_manifest.json`; do not add vault mirror writers.

## File Queue Semantics

Decision: use JSON files in runtime queue directories.

Rationale: simple local operation, inspectable jobs, natural manual recovery, no broker dependency.

Tradeoffs: no leases, no visibility timeouts, no built-in concurrency control, O(n) scans.

Future implication: queue files can become an interchange format for SQLite/message-queue migration.

## Append-Oriented Operational Memory

Decision: many mutations append sections or blocks instead of rewriting canonical state.

Rationale: preserves operational history and human context.

Tradeoffs: derived state can become ambiguous; cleanup and compaction are manual.

Future implication: nightly digest and future indexes should treat markdown as an event-like log, not just current state.

## Filesystem Over Database

Decision: use local filesystem primitives for runtime and vault writes.

Rationale: works offline, integrates with the vault, minimal deployment burden.

Tradeoffs: hard to scale concurrency, audit, and indexing; relies on platform-specific rename behavior.

Future implication: SQLite is the lowest-friction next step for job indexes, leases, and processed history.

## Sidecar and In-File Markers

Decision: use sidecar artifacts for transcripts/events/logs and in-file `btq_job_ids` for applied jobs.

Rationale: makes each stage inspectable and supports crash recovery.

Tradeoffs: markers can become stale; sidecars can accumulate; state is distributed across files.

Future implication: a manifest or event index should eventually connect artifacts, jobs, target writes, and source audio.

## Unknown Capture Reclassification

Decision: preserve missed/partial captures as markdown blocks with retry metadata.

Rationale: prevents silent loss when extraction fails and allows human edits to improve later routing.

Tradeoffs: unknown files contain multiple frontmatter-like blocks and are not clean YAML documents.

Future implication: unknown captures should become structured records if the system moves to a database.
