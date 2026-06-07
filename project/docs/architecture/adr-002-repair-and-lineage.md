# ADR-002 Repair And Lineage

## Status

Accepted for the operational repairability phase.

## Context

Phase 1 added a processor lock, processed index, capture IDs, health reporting, and structured queue logs. Those additions improved operational visibility, but they also created new derived artifacts that can drift from vault state, processed queue files, and human-edited markdown.

The next hardening need is not scalability. It is operator trust: being able to inspect, reconcile, and explain the runtime state without pretending the system knows more than it does.

## Decision

Add explicit repair and inspection commands:

- `./scripts/btq repair-index`
- `./scripts/btq inspect-runtime`

`repair-index` is dry-run by default. `--force` is required before it writes derived processed-index records. It scans processed queue files, `processed_index.jsonl`, vault markers, structured lifecycle logs, and lineage manifests. It reports missing index entries, orphaned markers, index drift, lineage breaks, stale artifacts, missing processed references, processed jobs without visible vault markers, and vault markers with no processed or index evidence.

`inspect-runtime` reports stale claimed audio, abandoned temp files, old failed jobs, and manifest references to missing artifacts.

Add observational lineage manifests at:

```text
<runtime_root>/manifests/<capture_id>.json
```

Manifests connect audio, transcript, normalized transcript, events, queue jobs, vault mutations, and processed records when those observations are available.

## Processed Index Is Derived State

The processed index is not canonical truth. It is an append-only lookup and audit convenience derived from queue processing. It can be missing, corrupted, stale, or behind the vault and processed directory after a crash.

Canonical confidence is layered and imperfect:

- `btq_job_ids` markers indicate the processor believed a job was applied or intentionally treated as applied.
- processed queue files indicate a queue artifact was archived as processed.
- structured logs indicate lifecycle observations that may be missing after crashes.
- manifests connect artifacts by `capture_id`, but only when the system observed them.
- vault content can be changed by humans or cloud sync after processing.

No one artifact is sufficient to prove truth in every failure mode.

## Why Ambiguity Reporting Matters

Repair tooling must not turn uncertainty into silent mutation. For example, "marker exists but no processed record found" means there is evidence of a prior processor belief, not proof that the intended content still exists or that every side effect succeeded.

The repair commands therefore report ambiguous states explicitly. They do not auto-fix vault content or markers.

## Manifests Are Observational

Lineage manifests are forensic aids. They are not authority. A manifest can be incomplete if a crash happens before an observation is written, if artifacts are moved, or if older captures predate capture IDs.

## Remaining Integrity Gaps

- There is still no transaction binding vault mutation, marker update, queue movement, processed-index append, manifest update, and logs.
- Marker/content divergence is detectable for simple job types only; complex semantic mutations still require human review.
- `--force` repair only rebuilds missing derived index rows from processed queue files. It does not repair vault content.
- Runtime retention remains manual.
- Replay still lacks a first-class diff and expected-state workflow.
