# ADR-005 Epistemic Modeling

## Status

Accepted for the epistemic modeling phase.

## Context

BTQ preserves raw captures, queue jobs, deterministic mutations, lineage, evidence, drift indicators, and replay plans. Those artifacts still do not distinguish clearly enough between observation, inference, assumption, contradiction, and stale historical truth.

Operational memory becomes dangerous when inference is flattened into fact. "No response" is an observation. "Resigned" may be an inference. "Storm caused damage" may be an assumption later contradicted by inspection.

## Decision

Add explicit epistemic metadata:

```json
{
  "epistemic_state": {
    "classification": "...",
    "source_type": "...",
    "confidence": "...",
    "derived_from": "...",
    "timestamp_context": "...",
    "confidence_basis": []
  }
}
```

Supported classifications include `OBSERVED`, `REPORTED_BY_HUMAN`, `INFERRED`, `ASSUMED`, `UNCONFIRMED`, `CONTRADICTED`, `HISTORICALLY_TRUE`, `CURRENTLY_VALID`, `STALE`, and `DISPUTED`.

Classification is deterministic and pattern/metadata driven. It is not AI judgment.

Add contradiction relationship artifacts under:

```text
runtime/contradictions/
```

Add temporal transition helpers so records can become stale, contradicted, disputed, or historically true without erasing the earlier interpretation.

Add `./scripts/btq narrative-report` to generate operational timelines that preserve observations, human reports, inferences, assumptions, contradictions, and unresolved ambiguity.

ADR-006 extends this model with human governance records. Epistemic classification describes the claim. Governance records describe who reviewed or disputed the interpretation, why, and with what evidence.

## Observation Versus Inference

The system preserves the difference between direct observation and derived interpretation. It does not convert "did not respond" into "resigned" unless explicit metadata or deterministic language indicates inference. Even then, the result is marked as inference, not truth.

## Why Contradictions Are Preserved

Contradictions are operationally valuable. They show how understanding changed. A later finding does not erase the earlier assumption; it links to it and changes its epistemic status.

## Temporal Truth Evolution

Some statements are historically relevant but no longer current. Temporal state transitions make that visible without deleting the old statement.

## Risks

- Deterministic phrase rules can misclassify terse human language.
- Contradiction links may be incomplete or themselves disputed.
- Narrative reports can feel more authoritative than they are.
- Stale truth may remain textually unchanged.
- Operators must still decide which interpretation governs current action.
- Governance acknowledgments can be mistaken for truth resolution if operators do not preserve the distinction.

## Consequences

BTQ now models evolving operational understanding more explicitly. This improves review and narrative reconstruction, but does not create epistemic certainty.
