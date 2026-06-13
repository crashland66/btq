# Failure Modes

## Partial Vault Write

Impact: target markdown could be truncated or missing if write atomicity fails.

Current mitigation: `atomic_write_text()` writes to a temp suffix, flushes, fsyncs, and replaces the target.

Remaining risk: parent directory is not fsynced; cloud-sync tools may observe replacement as delete/create; no backup snapshot is written by the processor.

## Crash After Vault Write Before Queue Move

Impact: queue file remains and may be retried.

Current mitigation: target `btq_job_ids` marker lets reruns skip without duplicate mutation. Tests cover append and visit creation.

Remaining risk: if marker write succeeded but not all intended side effects completed, rerun may treat partial work as complete.

## Crash After Queue Move Before Log Write

Impact: job may be processed but log line missing.

Current mitigation: processed job file remains as evidence; `processed_index.jsonl` is appended on successful processing when the process reaches the post-handler bookkeeping step.

Remaining risk: audit trail is split across logs, target markers, processed files, manifests, and the processed index. A crash can still leave one artifact behind the others.

## Queue Corruption

Impact: malformed JSON or schema-invalid jobs are rejected.

Current mitigation: invalid jobs move to `failed/` and log an error.

Remaining risk: corruption in `processed_index.jsonl` is a critical health condition and can block confident dedupe until repaired. Corruption in `failed/` can hide prior failed attempts.

## iCloud Sync Conflicts

Impact: duplicated files, conflict copies, stale reads, or `EDEADLK` on the iCloud transport directories.

Current mitigation: iCloud is used as transport only for inbox/outbox/working; runtime root is forced local; `safe_move()` handles `EDEADLK`. Canonical operational state lives in CouchDB `btq_vault`, not on an iCloud-synced filesystem, so canonical mutation no longer depends on cloud-sync behavior.

Remaining risk: the personal journal store and iCloud transport directories still depend on cloud-sync behavior for ingress/transport.

## Duplicate Job Processing

Impact: duplicate markdown entries or repeated operational actions.

Current mitigation: computed job IDs, processed scanning, target markers, and duplicate-content checks.

Remaining risk: two processors can race before either writes markers; jobs with semantically identical but textually different payloads are not deduplicated.

## Race Conditions

Impact: lost updates when two handlers read the same target file, compute independent updates, and both replace the file.

Current mitigation: `runtime/temp/processor.lock` uses exclusive file creation, records pid/hostname/start/command, refuses live concurrent processors, and recovers dead-process stale locks.

Remaining risk: the lock is local-filesystem coordination, not a distributed lock. It does not protect against manual vault edits, non-BTQ writers, cross-machine runtime sharing, or crashes inside individual mutation windows.

## Stale Markers

Impact: a marker can cause future jobs to skip even if humans removed the actual content.

Current mitigation: `btq repair-index --mode markers-only` reports orphaned markers, content without markers, and simple marker/content divergence when processed payloads make that inspection possible.

Remaining risk: marker/content divergence is not detected by content hash, and complex semantic mutations still require human review.

## Orphaned Files

Impact: temp files, claimed files, local queue_jobs, failed audio, or unknown reclassification workdirs may accumulate.

Current mitigation: many temp files are cleaned on exception; failed directories preserve problematic inputs; `btq health` reports stale claimed audio and runtime usage.

Additional mitigation: `btq inspect-runtime` reports stale claimed audio, abandoned temp files, old failed jobs, and manifests that reference missing artifacts.

Remaining risk: no retention policy, compaction, or automatic orphan cleanup.

## Replay Risks

Impact: requeueing historical files can recreate outdated facts or duplicate state if markers are missing.

Current mitigation: processed index, processed history fallback, target markers, duplicate-content checks, explicit replay planning, dry-run diffs, approval gates, dangerous-replay refusal, and replay audit logs.

Remaining risk: replay can still be structurally correct and semantically wrong. `capture_id` and diffs improve forensic reconstruction but do not prove that the replayed fact remains valid.

## Replay Approval Failure

Impact: an operator may approve a replay that conflicts with human edits or stale operational reality.

Current mitigation: replay plans classify risk, dry-run previews show before/after/diff, and execution refuses target drift, marker conflicts, missing context, and unknown state unless `--force-dangerous-replay` is supplied.

Remaining risk: approval is a human control, not a mathematical proof. Forced dangerous replay can still damage operational memory.

## Semantic Drift

Impact: target text can remain structurally valid while the operational meaning has changed or become stale.

Current mitigation: mutation evidence snapshots record lightweight pre/post fingerprints, nearby excerpts, intent metadata, and marker state. Replay and reconciliation reports surface deterministic drift indicators.

Remaining risk: fingerprints are heuristic. They can miss semantic drift when text is unchanged and can over-warn when harmless formatting changes occur.

## Evidence Corruption Or Absence

Impact: replay and reconciliation lose useful context and may classify state as semantically unknown.

Current mitigation: evidence is advisory only; repair, replay, and reconciliation continue to surface missing evidence as uncertainty rather than treating it as proof.

Remaining risk: evidence snapshots are not transactionally bound to canonical CouchDB writes and can be deleted or corrupted.

## Inference Flattening

Impact: an inferred or assumed operational interpretation can be mistaken for direct observation.

Current mitigation: epistemic metadata distinguishes `OBSERVED`, `REPORTED_BY_HUMAN`, `INFERRED`, `ASSUMED`, `UNCONFIRMED`, `CONTRADICTED`, and temporal states. Narrative reports group statements by epistemic class.

Remaining risk: deterministic classification can miss nuance, and operators can still over-trust a narrative summary.

## Contradiction Ambiguity

Impact: a later contradiction may itself be incomplete, mistaken, or disputed.

Current mitigation: contradiction artifacts preserve links and reasons without deleting the earlier claim.

Remaining risk: the system records contradiction relationships; it does not adjudicate final truth.

## Governance Authority Inflation

Impact: a review, dispute, escalation, or acknowledgment can be mistaken for proof that an interpretation is now settled.

Current mitigation: governance records preserve reviewer, rationale, evidence references, contradictory evidence, escalation state, and dispute state. `btq unresolved-report` surfaces unresolved contradictions, stale assumptions, unreviewed inferences, high-risk unresolved narratives, and disputed operational states.

Remaining risk: human review metadata can still sound authoritative. Acknowledgment means uncertainty was seen, not resolved. Escalation means attention is required, not that truth was confirmed.

## Dispute Persistence Burden

Impact: open disputes can remain visible after business reality has moved on, increasing operator noise.

Current mitigation: disputes are explicit records rather than silent overwrites.

Remaining risk: someone must record later review or resolution. The system preserves disagreement; it does not decide when disagreement is obsolete.

## Vault Divergence

Impact: static site registry may disagree with vault account/location files.

Current mitigation: queue processor validates site IDs against vault before writing site targets.

Remaining risk: event extraction and event-to-queue routing still use `event_pipeline/sites.py`; new valid vault sites can be treated as unknown until registry code changes.

## Human Operational Failures

Impact: wrong config paths, manual edits breaking frontmatter, moving processed files, or editing queue jobs after generation.

Current mitigation: environment verifier, path containment, failed-job quarantine, runbook.

Remaining risk: no signed jobs, no role separation, no operator checklist enforcement.
