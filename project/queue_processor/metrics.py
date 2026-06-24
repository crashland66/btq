from __future__ import annotations

# Read-derived observability only; this module writes only bounded metrics sidecars.

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from processing_core.artifacts import append_json_line
from queue_processor.health import count_json_files
from queue_processor.processed_index import index_path_for, iter_records
from queue_processor.timeparse import parse_timestamp as _parse_timestamp


DEFAULT_ERROR_WINDOW_HOURS = 24
DEFAULT_RETAIN_HOURS = 168
METRICS_FILENAME = "pipeline_metrics.jsonl"
LATENCY_FILENAME = "pipeline_latency.jsonl"

logger = logging.getLogger(__name__)


def metrics_path_for(runtime_root: Path) -> Path:
    return runtime_root / "metrics" / METRICS_FILENAME


def latency_path_for(runtime_root: Path) -> Path:
    return runtime_root / "metrics" / LATENCY_FILENAME


def _utc_now(now: float | None = None) -> datetime:
    return datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)


def _json_timestamp(path: Path) -> datetime | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("failed_at", "timestamp", "ts"):
        parsed = _parse_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _failed_entry_timestamp(path: Path) -> datetime | None:
    parsed = _json_timestamp(path)
    if parsed is not None:
        return parsed
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _in_window(timestamp: datetime | None, cutoff: datetime) -> bool:
    return timestamp is not None and timestamp >= cutoff


def _count_completed_window(runtime_root: Path, cutoff: datetime) -> int:
    count = 0
    for record in iter_records(index_path_for(runtime_root)):
        if _in_window(_parse_timestamp(record.get("timestamp")), cutoff):
            count += 1
    return count


def _count_failed_window(runtime_root: Path, cutoff: datetime) -> int:
    failed_dir = runtime_root / "failed"
    if not failed_dir.exists():
        return 0
    count = 0
    for path in failed_dir.rglob("*.json"):
        if path.is_file() and _in_window(_failed_entry_timestamp(path), cutoff):
            count += 1
    return count


def _count_json_files_recursive(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(count_json_files(directory) for directory in (path, *[item for item in path.rglob("*") if item.is_dir()]))


def sample_metrics(runtime_root: Path, now: float | None = None, window_hours: int = DEFAULT_ERROR_WINDOW_HOURS) -> dict[str, object]:
    current = _utc_now(now)
    cutoff = current - timedelta(hours=window_hours)
    completed_window = _count_completed_window(runtime_root, cutoff)
    failed_window = _count_failed_window(runtime_root, cutoff)
    total_window = completed_window + failed_window
    return {
        "ts": current.isoformat(),
        "queue_depth": count_json_files(runtime_root / "queue"),
        "in_flight": _count_json_files_recursive(runtime_root / "claimed"),
        "failed": count_json_files(runtime_root / "failed"),
        "completed_window": completed_window,
        "failed_window": failed_window,
        "error_rate": failed_window / max(1, total_window),
        "latency": latency_summary(runtime_root, window_hours, now),
    }


def _append_json_line(path: Path, payload: dict) -> None:
    append_json_line(path, payload)


def append_sample(metrics_path: Path, sample: dict) -> None:
    _append_json_line(metrics_path, sample)


def record_latency(runtime_root: Path, stage: str, duration_ms: float, now: float | None = None) -> None:
    try:
        payload = {
            "ts": _utc_now(now).isoformat(),
            "stage": str(stage),
            "duration_ms": float(duration_ms),
        }
        _append_json_line(latency_path_for(runtime_root), payload)
    except Exception as exc:  # noqa: BLE001 - best-effort observability only.
        logger.debug("pipeline latency record skipped stage=%s error=%s", stage, exc)


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    return records


def _iter_sample_lines(metrics_path: Path) -> list[dict[str, Any]]:
    return _iter_jsonl(metrics_path)


def _percentile(values: list[float], percent: float) -> float:
    """Return a linearly interpolated percentile from sorted numeric values."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def latency_summary(
    runtime_root: Path,
    window_hours: int = DEFAULT_ERROR_WINDOW_HOURS,
    now: float | None = None,
) -> dict[str, dict[str, float | int]]:
    cutoff = _utc_now(now) - timedelta(hours=window_hours)
    grouped: dict[str, list[float]] = {}
    for record in _iter_jsonl(latency_path_for(runtime_root)):
        if not _in_window(_parse_timestamp(record.get("ts")), cutoff):
            continue
        stage = record.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            continue
        try:
            duration_ms = float(record.get("duration_ms"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault(stage, []).append(duration_ms)
    return {
        stage: {
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "max_ms": max(values),
            "count": len(values),
        }
        for stage, values in sorted(grouped.items())
    }


def load_recent(metrics_path: Path, window_hours: int) -> list[dict[str, Any]]:
    cutoff = _utc_now() - timedelta(hours=window_hours)
    return [sample for sample in _iter_sample_lines(metrics_path) if _in_window(_parse_timestamp(sample.get("ts")), cutoff)]


def _prune_path(path: Path, retain_hours: int = DEFAULT_RETAIN_HOURS, now: float | None = None) -> int:
    samples = _iter_jsonl(path)
    if not samples:
        return 0
    cutoff = _utc_now(now) - timedelta(hours=retain_hours)
    retained = [sample for sample in samples if _in_window(_parse_timestamp(sample.get("ts")), cutoff)]
    pruned = len(samples) - len(retained)
    if pruned == 0:
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for sample in retained:
                handle.write(json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return pruned


def prune(metrics_path: Path, retain_hours: int = DEFAULT_RETAIN_HOURS, now: float | None = None) -> int:
    pruned = _prune_path(metrics_path, retain_hours, now)
    if metrics_path.name == METRICS_FILENAME:
        pruned += _prune_path(metrics_path.with_name(LATENCY_FILENAME), retain_hours, now)
    return pruned
