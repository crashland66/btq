# Stage 20 Completion Checkpoint

Stage 20 added read-only maintenance/status visibility for accumulated
field-capture review artifacts.

## Command Added

```bash
./scripts/btq review-maintenance-status --channel field_capture
```

## Supported Options

- `--json`
- `--stale-days N`
- `--include-paths`

## JSON Output Shape

```json
{
  "channel": "field_capture",
  "runtime_root": "...",
  "stale_days": 14,
  "counts": {},
  "findings": [],
  "disk_usage": {},
  "oldest": {},
  "newest": {}
}
```

## Findings Reported

When present, the command reports:

- stale pending candidates
- approved candidates without drafts
- old rejected candidates
- failed candidate artifacts
- approved drafts without staging status
- failed draft artifacts
- staged drafts without queue, processed, failed, or processed-index evidence
- orphaned staging statuses
- queue files pointing to missing drafts
- unreadable processed index

## Meaning Of "No Findings Were Reported"

"No findings were reported" means the maintenance scan found no
stale/unresolved/orphaned review artifacts for the selected runtime paths and
`--stale-days` threshold.

It does not mean artifacts are safe to delete. Stage 20 implements no cleanup,
retention, deletion, archiving, repair, approval, rejection, or restaging
behavior.

## Files Changed

- `project/field_capture/review_maintenance.py`
- `project/btq.py`
- `tests/test_field_capture_audio_semantics.py`
- `project/field_capture/README.md`
- `project/docs/runbook.md`
- `project/docs/architecture/shared_processing_spine.md`

## Validation

Pytest command run:

```bash
./project/.venv/bin/python -m pytest tests/test_processing_core.py tests/test_field_capture_audio_semantics.py tests/test_field_capture_audio_transcription.py tests/test_transcription_pipeline.py tests/test_event_pipeline.py tests/test_queue_spec.py tests/test_queue_processor.py
```

Result:

```text
194 passed
```

Other validation:

- `git diff --check`: passed
- `./scripts/lint-markdown`: passed
- `./scripts/btq review-maintenance-status --help`: passed and showed the new
  command options

## Explicit Safety Confirmations

- Read-only behavior was preserved.
- No deletion was added.
- No archiving was added.
- No repair behavior was added.
- No approval or rejection changes were added.
- No queue staging was added.
- No queue processor invocation was added.
- No vault mutation was added.
- No deploy, database, cloud, or API changes were made.
