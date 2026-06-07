# ADR-003 Replay And State Reconstruction

## Status

Accepted for the replay planning phase.

## Context

BTQ now has locks, processed indexes, capture lineage, manifests, repair inspection, and structured logs. Those tools improve reconstruction, but they do not make historical replay safe.

Replay can reintroduce outdated operational facts, overwrite or obscure human edits, duplicate content, or add markers that make later recovery harder. The system therefore treats replay as an operator-mediated workflow:

```text
proposal -> inspection -> approval -> execution
```

It must not become hidden recovery automation.

## Decision

Add explicit replay commands:

- `./scripts/btq replay-plan`
- `./scripts/btq replay-dry-run`
- `./scripts/btq replay-execute`

`replay-plan` generates candidates without mutation. Candidates include capture ID, computed job ID, original queue file, target path, original mutation timestamp when known, current target state summary, replay risk, mutation type, and replay reason.

`replay-dry-run` generates before/after/diff previews and risk notes. It never mutates vault state.

`replay-execute` requires `--approve`. Dangerous candidates are refused unless `--force-dangerous-replay` is also supplied. Execution writes structured audit events to `runtime/logs/replay_events.jsonl`.

## Risk Classifications

- `SAFE`: current target exists, expected content and marker are absent, and a structural preview is supported.
- `LOW_CONFIDENCE`: marker and content are already present; replay is likely a no-op but still not proof of semantic correctness.
- `TARGET_DRIFTED`: content appears present without the expected marker, or another structural drift is visible.
- `MARKER_CONFLICT`: marker exists while expected content is missing, or processed history conflicts with replay source.
- `MISSING_CONTEXT`: target or mutation preview cannot be reconstructed with enough structure.
- `UNKNOWN_STATE`: target file is missing or the current state cannot be inspected.

## Structural Replay Versus Semantic Correctness

Replay can reconstruct a deterministic file mutation from a queue job. It cannot prove the underlying operational fact remains true, that a human edit should be overwritten, or that the original extraction was correct.

The preview diff is structural. It answers "what text would this replay write now?" It does not answer "should the business state contain this fact?"

Epistemic metadata can make replay review safer by marking claims as observed, inferred, assumed, contradicted, or stale. It still does not decide whether replay should proceed.

## Expected State Reconstruction

Expected state is inferred from processed queue files, manifests, logs, markers, and current vault content. This is probabilistic. For append-oriented jobs, the expected prior state is usually "target exists without this content and without this marker." If content or marker already exists, the replay is flagged.

This reconstruction is not authoritative truth. It is an inspection aid.

## Operator Responsibilities

Operators must inspect replay plans and dry-run diffs before approval. `--approve` means the operator accepts the proposed structural mutation. `--force-dangerous-replay` means the operator also accepts explicitly flagged drift, marker conflict, missing context, or unknown state risks.

ADR-006 adds governance acknowledgments for replay risk. An acknowledgment records that a human accepted visibility of a replay risk. It does not prove semantic correctness and does not make future replay safe by default.

## Remaining Replay Risks

- Human edits may have intentionally removed or changed prior content.
- Markers can outlive content or content can outlive markers.
- Some job types require context-heavy target resolution and are classified conservatively.
- Replay does not validate whether the original spoken capture was accurate.
- Replay audit logs can be missing if the process crashes after mutation but before logging.
