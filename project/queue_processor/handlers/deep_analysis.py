from __future__ import annotations

from pathlib import Path

from field_capture.deep_analysis import run_deep_analysis
from queue_spec import DEEP_ANALYSIS_PRESET_IDS

from . import _shared
from ._shared import QueueJob, QueueProcessorError, RunContext


def _required_string(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise QueueProcessorError(f"Deep analysis job requires non-empty {field}")
    return value.strip()


def _optional_string(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise QueueProcessorError(f"Deep analysis job field {field} must be a string")
    stripped = value.strip()
    return stripped or None


def process_deep_analysis_job(job_path: Path, job: QueueJob, context: RunContext, processed_dir: Path) -> None:
    payload = job.payload
    capture_id = _required_string(payload, "capture_id")
    photo_asset_id = _required_string(payload, "photo_asset_id")
    actor = _required_string(payload, "actor")
    preset_id = _optional_string(payload, "preset_id")
    custom_prompt = _optional_string(payload, "custom_prompt")

    if (preset_id is None) == (custom_prompt is None):
        raise QueueProcessorError("Deep analysis job must include exactly one of preset_id or custom_prompt")
    if preset_id is not None and preset_id not in DEEP_ANALYSIS_PRESET_IDS:
        raise QueueProcessorError(f"Unknown deep analysis preset_id: {preset_id}")

    processed_destination = processed_dir / job_path.name
    if not context.dry_run and processed_destination.exists():
        raise QueueProcessorError(f"Destination already exists: {processed_destination}")

    print(f"Job {job.job_id}: validated")
    if context.dry_run:
        print(f"Job {job.job_id}: would run deep analysis for photo asset {photo_asset_id}")
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=deep-analysis status=success error=")
        return

    upload_root = context.runtime_root / "uploads"
    run_deep_analysis(
        capture_id=capture_id,
        photo_asset_id=photo_asset_id,
        preset_id=preset_id,
        custom_prompt=custom_prompt,
        actor=actor,
        runtime_root=context.runtime_root,
        upload_root=upload_root,
    )

    moved_path = _shared.move_job_file(job_path, processed_dir)
    print(f"Job {job.job_id}: moved queue file to {moved_path}")
    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=deep-analysis status=success error=")
