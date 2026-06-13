# BTQ README First

Start every AI-assisted BTQ session here.

BTQ separates operational truth from bootstrap, configuration, and instruction documents.

## Boundaries

- The CouchDB `btq_vault` database stores operational state and records only.
- AI instructions, queue-authoring guidance, architecture notes, bootstrap prompts, and setup documents live in the repository.
- iCloud `BTDocs` is an exported projection from the repository, not the source of truth.
- Do not create or update AI/bootstrap/config documents inside the canonical operational store.
- Every exported BTDocs markdown file starts with a `BTQ_*` provenance header. Use it to check schema version, export time, source path, exporter version, and repository commit.
- `BTDocs/export_manifest.json` records exported document hashes for stale-doc and mismatch detection.

## Writer Model

AI and other probabilistic tools may author queue jobs, but they do not directly
mutate canonical CouchDB state.

The deterministic writer owns:

- queue-job validation
- canonical target resolution
- idempotency and replay checks
- CouchDB canonical mutation through queue handlers
- failed-job quarantine

For entity creation, use first-class queue jobs such as `add_person`. Do not convert onboarding or entity creation into `append_to_note`.

## Session Checklist

1. Read the queue authoring guide before creating executable jobs.
2. Use supported job types only.
3. Prefer clarification or unresolved capture over guessing.
4. Never invent storage paths for people records.
5. Treat `BTDocs` files as read-only exported context unless explicitly running the repo exporter.
