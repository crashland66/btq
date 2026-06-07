# Operational Model

BTQ is designed around field managers capturing operational facts quickly, usually by voice. The system accepts that raw capture is messy. It preserves source transcripts, records unknowns, and turns only recognized intent into deterministic queue jobs.

## Manager Workflow

The intended daily loop is:

1. Manager records a voice memo after a site visit, call, staffing event, or operational observation.
2. The memo syncs into the configured iCloud voice inbox.
3. The transcription watcher claims it into local runtime storage.
4. Whisper produces a transcript.
5. Rule-based extraction identifies candidate operational events.
6. Queue jobs are staged.
7. The queue watcher applies validated mutations to the vault.
8. Unknown or partial captures land in `Journal/YYYY-MM-DD-unknown.md` for review.
9. Nightly digest synthesis summarizes the day's operational memory.

## Human Review Surfaces

Important review locations:

- `local/audio_processing/` for raw and normalized transcripts.
- `local/events_valid/` and `local/events_failed/` for extraction results.
- `local/queue_jobs/` for generated jobs.
- `<runtime_root>/queue/`, `<runtime_root>/processed/`, and
  `<runtime_root>/failed/` for job state.
- `<runtime_root>/processed_index.jsonl` for append-only processed-job lookup
  records.
- `<runtime_root>/logs/queue_processor_events.jsonl` for structured queue
  lifecycle, lock, dedupe, replay, and index events.
- `<runtime_root>/temp/processor.lock` for the active single-processor lock.
- `<runtime_root>/manifests/<capture_id>.json` for observational lineage
  reconstruction.
- `<runtime_root>/reviews/*.json` for human review, disputes, escalation, and
  acknowledgment provenance.
- `Journal/YYYY-MM-DD-unknown.md` for unresolved capture.
- nightly digest files for cross-source synthesis.

## Runtime Health

Operators can run:

```bash
./scripts/btq health
```

The command reports processor lock status, queue depth, failed-job count, stale claimed audio, unknown captures, processed-index health, old failed jobs, and runtime disk usage. It exits non-zero for stale locks, queue backlog above the configured threshold, and corrupted processed indexes.

For unattended monitoring, run:

```bash
./scripts/btq health --monitor --quiet
```

The monitor mode reuses the same health report and writes throttled critical
alerts to `runtime_root/alerts/health_alerts.jsonl`, with
`runtime_root/alerts/latest_health_alert.json` surfaced on `/health`. The
LaunchAgent installer is `./scripts/install-btq-health-monitor-launch-agent`.

Operators can inspect repair and stale-artifact state with:

```bash
./scripts/btq repair-index
./scripts/btq inspect-runtime
```

`repair-index` is dry-run by default. `--force` can rebuild missing derived index rows from processed queue files, but it does not repair vault content or markers.

Replay is a separate deliberate workflow:

```bash
./scripts/btq replay-plan --failed-only --output replay-plan.json
./scripts/btq replay-dry-run --plan-file replay-plan.json
./scripts/btq replay-execute --plan-file replay-plan.json --approve
```

Replay execution refuses risky candidates unless `--force-dangerous-replay` is supplied. Operators are responsible for inspecting diffs and deciding whether the structural replay is still semantically appropriate.

Semantic reconciliation is available with:

```bash
./scripts/btq reconciliation-report
```

The report summarizes evidence-based drift indicators, unresolved ambiguities, replay risks, mutation confidence categories, and lineage gaps. It is an operator review aid, not an automated decision system.

Epistemic narrative review is available with:

```bash
./scripts/btq narrative-report --include-contradictions
```

Narrative reports group observations, human reports, inferences, assumptions, unresolved ambiguity, and contradiction-linked entries. They are meant to show evolving understanding, not produce final truth.

Epistemic governance review is available with:

```bash
./scripts/btq unresolved-report
```

The unresolved report surfaces unresolved contradictions, stale assumptions, unreviewed inferences, high-risk unresolved narratives, and disputed operational states. It supports `--since`, `--account`, `--high-risk-only`, and `--json`. It does not resolve claims; it makes unresolved interpretation visible.

## Lineage

Each new audio processing run receives a `capture_id`. That id is carried through transcript metadata, valid/failed event JSON, generated queue-job metadata, processed-index records, process logs, structured queue logs, and an observational manifest. The id is an operational lineage handle, not a guarantee that extraction or mutation was semantically correct.

Mutation evidence snapshots extend lineage with lightweight pre/post fingerprints and nearby excerpts for supported handlers. They help answer "what local text changed around this mutation?" They do not answer "is this fact still true?"

## Structured and Unstructured Data

The vault mixes:

- structured frontmatter
- append-only markdown notes
- embedded operational blocks such as `unknown_capture`, `visit`, and `visit_gap`
- generated supply order markdown
- daily digest text

This is intentional but creates a split responsibility: humans can read and edit the vault, while code must tolerate imperfect markdown and partial structure.

## Nightly Synthesis

`nightly_digest_builder.py` reads journals, reports, valid events, failed events, processed/failed jobs, unknown captures, and visit gaps. It separates event logs from derived signals and computes stable hashes after normalizing dynamic metadata. This is a daily operational review layer, not the primary mutation path.

## Future Workflow Evolution

The natural evolution is:

- richer review queues for unknowns and failed jobs
- operator interfaces for epistemic review, disputes, and acknowledgments
- structured indexes over markdown artifacts
- operator dashboards for queue health and unresolved captures
- explicit replay/diff tooling
- eventual datastore migration while preserving human-readable exports
