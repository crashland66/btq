# ADR-004 Semantic Drift And Evidence

## Status

Accepted for the semantic state awareness phase.

## Context

BTQ can now lock processors, index processed jobs, reconstruct lineage, repair derived state, and plan replay. Those capabilities are still mostly structural. They can say that a file changed, a marker exists, or a queue job was processed. They cannot prove that the intended operational meaning still exists.

This phase adds lightweight evidence to help operators reason about semantic drift without pretending the system understands semantics like a human.

## Decision

Add mutation evidence snapshots at:

```text
<runtime_root>/evidence/<capture_id>/<job_id>.json
```

Snapshots are written by deterministic mutation handlers and include:

- capture ID and computed job ID
- mutation timestamp
- target path and handler type
- advisory mutation intent summary
- pre/post target fingerprints
- nearby content excerpt
- mutation location hints
- marker state at mutation time

Add optional queue-job `intent` metadata:

```json
{
  "intent": {
    "category": "...",
    "reason": "...",
    "source_context": "...",
    "operator_relevance": "...",
    "confidence": "..."
  }
}
```

Intent metadata is advisory. It is not part of the computed idempotency key and is not authoritative truth.

Add deterministic drift indicators based on fingerprints:

- nearby mutation region removed or substantially altered
- frontmatter changed
- target line count changed
- nearby excerpt hash changed

Add confidence categories:

- `STRUCTURALLY_SAFE`
- `STRUCTURALLY_UNCERTAIN`
- `SEMANTICALLY_DRIFTED`
- `SEMANTICALLY_UNKNOWN`
- `REPLAY_HIGH_RISK`

Add `./scripts/btq reconciliation-report` for operator-facing integrity summaries.

## Structural Versus Semantic Correctness

Structural correctness means a deterministic handler can produce an expected file mutation.

Semantic correctness means the operational meaning remains valid in context. BTQ cannot prove semantic correctness from hashes, markers, or excerpts. The new evidence only helps identify when the local context changed enough that replay or reconciliation deserves extra scrutiny.

## Fingerprint Limits

Fingerprints are heuristic. They can detect text drift, frontmatter drift, line-count changes, and missing nearby excerpts. They cannot detect all paraphrases, human-intended edits, stale business facts, or context changes elsewhere in the vault.

False confidence remains possible when text is unchanged but meaning is stale.

False alarms remain possible when harmless formatting changes alter fingerprints.

## Operator Responsibilities

Operators must treat semantic drift findings as prompts for review, not verdicts. Evidence snapshots are observational artifacts and reconstruction aids. They do not override vault content, processed queue files, manifests, or human judgment.

## Remaining Risks

- Evidence snapshots are not transactionally bound to vault writes.
- Snapshots can be missing, corrupted, or stale.
- Nearby excerpts are intentionally lightweight and may omit important context.
- Semantic drift detection can miss meaning changes that preserve text.
- Replay confidence improves, but replay is still not semantically safe by default.

## Relationship To Epistemic Modeling

Semantic evidence answers "what changed around this mutation?" Epistemic modeling answers "what kind of claim was this: observation, report, inference, assumption, or later-contradicted interpretation?" Both remain advisory.
