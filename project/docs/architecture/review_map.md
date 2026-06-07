# BTQ Architecture Review Map

This directory is a technical review package for the BTQ queue pipeline. It is based on the checked-in code, tests, scripts, and existing docs as of the review, with code treated as the source of truth.

Recommended reading order:

1. [executive_summary.md](executive_summary.md) - short leadership and reviewer summary.
2. [system_overview.md](system_overview.md) - component map, directory map, and high-level runtime diagrams.
3. [runtime_flow.md](runtime_flow.md) - end-to-end behavior from audio ingestion through canonical mutation and retries.
4. [deterministic_contracts.md](deterministic_contracts.md) - guarantees, non-guarantees, idempotency, ordering, and filesystem assumptions.
5. [failure_modes.md](failure_modes.md) - concrete failure scenarios, mitigations, and residual risk.
6. [architecture_decisions.md](architecture_decisions.md) - extracted architectural decisions and tradeoffs.
7. [operational_model.md](operational_model.md) - how the system fits manager, voice memo, review, and nightly workflows.
8. [security_and_integrity_review.md](security_and_integrity_review.md) - trust boundaries, tampering risks, malformed input, and integrity failure points.
9. [scalability_review.md](scalability_review.md) - filesystem, queue, markdown, sync, and migration analysis.
10. [architecture_review_findings.md](architecture_review_findings.md) - final findings, risks, and recommendations.

Classification summary:

BTQ is best described as a hybrid local-first operational knowledge capture system with a deterministic mutation pipeline. It has event-driven and staged-event characteristics, but it is not a full brokered distributed event-driven architecture. Its strongest identity is the separation between probabilistic capture/classification and deterministic, replay-aware canonical mutation.
