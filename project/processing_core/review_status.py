from __future__ import annotations

from pathlib import Path
from typing import Iterable

from processing_core.artifacts import read_json_object, resolve_within_root
from queue_processor.idempotency import compute_job_id
from queue_processor.processed_index import ProcessedIndexError, index_path_for, iter_records


def iter_json_artifacts(root: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    resolved_root = root.expanduser().resolve(strict=False)
    if not resolved_root.exists():
        return
    for path in sorted(candidate for candidate in resolved_root.glob("*.json") if candidate.is_file()):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, resolved_root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def path_exists(path_value: object) -> bool:
    if not isinstance(path_value, str) or not path_value.strip():
        return False
    return Path(path_value).expanduser().exists()


def queue_job_draft_id(payload: dict[str, object]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("draft_id")
    return value if isinstance(value, str) else ""


def queue_job_draft_path(payload: dict[str, object]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    value = metadata.get("draft_artifact_path")
    return value if isinstance(value, str) else ""


def queue_job_computed_id(payload: dict[str, object]) -> str:
    try:
        return compute_job_id(payload)
    except Exception:
        return ""


def queue_job_records(root: Path, state: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path, payload in iter_json_artifacts(root):
        draft_id = queue_job_draft_id(payload)
        if not draft_id:
            continue
        records.append(
            {
                "state": state,
                "path": str(path),
                "draft_id": draft_id,
                "draft_artifact_path": queue_job_draft_path(payload),
                "computed_job_id": queue_job_computed_id(payload),
            }
        )
    return records


def processed_index_records(runtime_root: Path) -> tuple[list[dict[str, object]], str | None]:
    try:
        return iter_records(index_path_for(runtime_root)), None
    except ProcessedIndexError as exc:
        return [], str(exc)


def count_by_status(payloads: Iterable[dict[str, object]], statuses: Iterable[str]) -> dict[str, int]:
    counts = {status: 0 for status in statuses}
    for payload in payloads:
        status = payload.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
    return counts

