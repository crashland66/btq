# ADR-006 Epistemic Governance

## Status

Accepted for the human epistemic governance phase.

## Context

BTQ now preserves observations, human reports, inferences, assumptions, contradictions, temporal drift, evidence snapshots, replay risk, reconciliation findings, and narrative reports.

That preservation reduces flattening, but it introduces a new risk: once the system can produce coherent timelines, operators may treat those timelines as more authoritative than the underlying evidence permits. A narrative can sound resolved even when it is only an organized view of uncertainty.

The system therefore needs explicit governance records for human review, disagreement, escalation, acknowledgment, and unresolved ambiguity. The goal is not truth adjudication. The goal is to keep interpretation attributable:

```text
who believed what
when
why
with what evidence
under what uncertainty
```

## Decision

Add human governance records under:

```text
runtime/reviews/
```

Review records include reviewer, timestamp, target artifact, reviewed epistemic classification, prior state, proposed updated state, rationale, supporting evidence, confidence rationale, contradictory evidence visible at review time, optional escalation state, optional dispute state, and resolution status.

Add explicit dispute states:

```text
DISPUTED_BY_MANAGER
DISPUTED_BY_CLIENT
DISPUTED_BY_FIELD_REPORT
UNRESOLVED_CONFLICT
```

Disputes remain visible as open governance records. They are not collapsed automatically, even when another reviewer disagrees with the dispute.

Add escalation states:

```text
NEEDS_REVIEW
HIGH_OPERATIONAL_RISK
UNVERIFIED_BUT_ACTIONABLE
REQUIRES_CONFIRMATION
```

Escalation requests attention. It does not convert an inferred, assumed, or unconfirmed claim into confirmed truth.

Add lightweight acknowledgment records for cases such as:

```text
manager reviewed contradiction
manager acknowledged unresolved ambiguity
manager accepted replay risk
```

Acknowledgment means the uncertainty was seen and accepted for operator workflow purposes. It does not resolve the underlying ambiguity.

Add:

```bash
./scripts/btq unresolved-report
```

The report surfaces unresolved contradictions, stale assumptions, unreviewed inferences, high-risk unresolved narratives, and disputed operational states. It supports `--since`, `--account`, `--high-risk-only`, and `--json`.

## Review Provenance Philosophy

Governance records are append-only local artifacts. They are not canonical truth. They are evidence that a human reviewed an interpretation at a point in time, with named rationale and evidence references.

This preserves the difference between:

```text
"Damon presumed resigned"
```

and:

```text
"HR confirmed resignation"
```

A review may escalate the first claim to `REQUIRES_CONFIRMATION`; it must not silently rewrite it as the second.

## Disagreement Preservation

Multiple reviewers can record conflicting states for the same target artifact. The system preserves both records. It does not choose a winner.

This is intentional. Operational memory often contains live disagreement: a client report, a manager interpretation, a field follow-up, and a later HR confirmation may all matter. Losing the disagreement would erase the path of interpretation.

## Unresolved Ambiguity Handling

`unresolved-report` is a surfacing tool. It does not repair, replay, reclassify, or recommend operational action.

Acknowledgments can reduce repeated surfacing for a target, but they do not delete contradictions, disputes, evidence, or narrative entries. Operators remain responsible for deciding whether a reviewed ambiguity still requires action.

## Risks

- Review records can create a false sense that a claim was resolved when it was only acknowledged.
- Operators may overuse acknowledgments to silence uncomfortable ambiguity.
- Conflicting reviewer records can increase cognitive load.
- Disputes may persist after the business reality is resolved unless someone records that resolution.
- The system still cannot verify whether a human rationale is accurate.
- Governance records are not transactionally bound to vault changes, evidence snapshots, or replay execution.

## Consequences

BTQ now has first-class human epistemic governance. It can preserve disagreement, attribution, escalation, and acknowledgment without performing autonomous adjudication.

The next architectural pressure point is review ergonomics: the system can surface more uncertainty than a busy operator can responsibly process.
