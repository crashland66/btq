from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from queue_processor import metrics


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _iso(hours_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _iso_from(now: datetime, hours_ago: int = 0) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()


def test_sample_metrics_counts_queue_in_flight_and_failed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    _write_json(runtime_root / "queue" / "one.json", {})
    _write_json(runtime_root / "queue" / "two.json", {})
    _write_json(runtime_root / "claimed" / "audio" / "claimed.json", {})
    _write_json(runtime_root / "failed" / "failed.json", {})

    sample = metrics.sample_metrics(runtime_root)

    assert sample["queue_depth"] == 2
    assert sample["in_flight"] == 1
    assert sample["failed"] == 1
    assert "latency" in sample


def test_error_rate_uses_rolling_window(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    index_path = runtime_root / "processed_index.jsonl"
    index_path.parent.mkdir(parents=True)
    records = [
        {"computed_job_id": "in-1", "timestamp": _iso(1)},
        {"computed_job_id": "in-2", "timestamp": _iso(2)},
        {"computed_job_id": "out-1", "timestamp": _iso(30)},
    ]
    index_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    _write_json(runtime_root / "failed" / "inside.json", {"timestamp": _iso(3)})
    _write_json(runtime_root / "failed" / "outside.json", {"timestamp": _iso(40)})

    sample = metrics.sample_metrics(runtime_root)

    assert sample["completed_window"] == 2
    assert sample["failed_window"] == 1
    assert sample["error_rate"] == 1 / 3


def test_error_rate_zero_when_no_activity(tmp_path: Path) -> None:
    sample = metrics.sample_metrics(tmp_path / "runtime")

    assert sample["completed_window"] == 0
    assert sample["failed_window"] == 0
    assert sample["error_rate"] == 0.0


def test_append_sample_is_append_only_and_durable(tmp_path: Path) -> None:
    metrics_path = tmp_path / "runtime" / "metrics" / "pipeline_metrics.jsonl"
    first = {"ts": _iso(), "queue_depth": 1}
    second = {"ts": _iso(), "queue_depth": 2}

    metrics.append_sample(metrics_path, first)
    metrics.append_sample(metrics_path, second)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["queue_depth"] == 1
    assert json.loads(lines[1])["queue_depth"] == 2


def test_record_latency_appends_durable_line(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    metrics.record_latency(runtime_root, "queue", 123.4, now=1_800_000_000)
    metrics.record_latency(runtime_root, "queue", 567.8, now=1_800_000_001)

    lines = metrics.latency_path_for(runtime_root).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["stage"] == "queue"
    assert first["duration_ms"] == 123.4
    assert second["duration_ms"] == 567.8


def test_record_latency_never_raises_on_write_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(metrics, "_append_json_line", fail_write)

    metrics.record_latency(tmp_path / "runtime", "queue", 1.0)


def test_latency_summary_percentiles_and_grouping(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    latency_path = metrics.latency_path_for(runtime_root)
    now = datetime(2026, 5, 31, 12, tzinfo=timezone.utc)
    records = [
        {"ts": _iso_from(now, 1), "stage": "queue", "duration_ms": 100},
        {"ts": _iso_from(now, 2), "stage": "queue", "duration_ms": 200},
        {"ts": _iso_from(now, 3), "stage": "queue", "duration_ms": 400},
        {"ts": _iso_from(now, 4), "stage": "field_capture", "duration_ms": 50},
        {"ts": _iso_from(now, 5), "stage": "field_capture", "duration_ms": 150},
        {"ts": _iso_from(now, 30), "stage": "queue", "duration_ms": 999},
    ]
    latency_path.parent.mkdir(parents=True)
    latency_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    summary = metrics.latency_summary(runtime_root, window_hours=24, now=now.timestamp())

    assert summary["queue"]["count"] == 3
    assert summary["queue"]["p50_ms"] == 200
    assert summary["queue"]["p95_ms"] == 380
    assert summary["queue"]["max_ms"] == 400
    assert summary["field_capture"]["count"] == 2
    assert summary["field_capture"]["p50_ms"] == 100
    assert summary["field_capture"]["p95_ms"] == 145
    assert summary["field_capture"]["max_ms"] == 150


def test_prune_drops_samples_older_than_retention(tmp_path: Path) -> None:
    metrics_path = tmp_path / "runtime" / "metrics" / "pipeline_metrics.jsonl"
    samples = [
        {"ts": _iso(200), "queue_depth": 1},
        {"ts": _iso(2), "queue_depth": 2},
        {"ts": _iso(1), "queue_depth": 3},
    ]
    metrics_path.parent.mkdir(parents=True)
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")

    pruned = metrics.prune(metrics_path, retain_hours=168)

    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert pruned == 1
    assert [json.loads(line)["queue_depth"] for line in lines] == [2, 3]


def test_prune_bounds_latency_log(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    metrics_path = metrics.metrics_path_for(runtime_root)
    latency_path = metrics.latency_path_for(runtime_root)
    now = datetime(2026, 5, 31, 12, tzinfo=timezone.utc)
    samples = [{"ts": _iso_from(now, 1), "queue_depth": 1}]
    records = [
        {"ts": _iso_from(now, 200), "stage": "queue", "duration_ms": 1},
        {"ts": _iso_from(now, 2), "stage": "queue", "duration_ms": 2},
        {"ts": _iso_from(now, 1), "stage": "field_capture", "duration_ms": 3},
    ]
    metrics_path.parent.mkdir(parents=True)
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")
    latency_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    pruned = metrics.prune(metrics_path, retain_hours=168, now=now.timestamp())

    lines = latency_path.read_text(encoding="utf-8").splitlines()
    assert pruned == 1
    assert [json.loads(line)["duration_ms"] for line in lines] == [2, 3]


def test_sample_metrics_includes_latency_summary(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    metrics.record_latency(runtime_root, "queue", 25, now=1_800_000_000)

    sample = metrics.sample_metrics(runtime_root, now=1_800_000_001)

    assert set(sample) >= {
        "ts",
        "queue_depth",
        "in_flight",
        "failed",
        "completed_window",
        "failed_window",
        "error_rate",
        "latency",
    }
    assert sample["latency"] == {"queue": {"p50_ms": 25.0, "p95_ms": 25.0, "max_ms": 25.0, "count": 1}}


def test_load_recent_filters_by_window(tmp_path: Path) -> None:
    metrics_path = tmp_path / "runtime" / "metrics" / "pipeline_metrics.jsonl"
    samples = [
        {"ts": _iso(25), "queue_depth": 1},
        {"ts": _iso(3), "queue_depth": 2},
        {"ts": _iso(1), "queue_depth": 3},
    ]
    metrics_path.parent.mkdir(parents=True)
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")

    recent = metrics.load_recent(metrics_path, window_hours=24)

    assert [sample["queue_depth"] for sample in recent] == [2, 3]
