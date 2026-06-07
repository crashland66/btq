import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "project"
sys.path.insert(0, str(PROJECT_ROOT))

from queue_processor import status  # noqa: E402


def test_queue_status_counts_runtime_layers(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "intake" / "outbox").mkdir(parents=True)
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "processing").mkdir(parents=True)
    (runtime_root / "failed").mkdir(parents=True)
    (runtime_root / "quarantine").mkdir(parents=True)
    (runtime_root / "processed").mkdir(parents=True)

    (runtime_root / "intake" / "outbox" / "intake.json").write_text("{}\n", encoding="utf-8")
    (runtime_root / "queue" / "queued.json").write_text("{}\n", encoding="utf-8")
    (runtime_root / "processing" / "active.json").write_text("{}\n", encoding="utf-8")
    (runtime_root / "failed" / "failed.json").write_text("{}\n", encoding="utf-8")
    (runtime_root / "quarantine" / "held.json").write_text("{}\n", encoding="utf-8")
    (runtime_root / "processed" / "done.json").write_text("{}\n", encoding="utf-8")

    result = status.build_status(runtime_root)

    assert result.local_intake_count == 1
    assert result.durable_queue_count == 1
    assert result.processing_count == 1
    assert result.failed_count == 1
    assert result.quarantine_count == 1
    assert result.last_successful_process_timestamp is not None


def test_queue_status_runs_against_empty_runtime_returns_zero(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    result = status.run(["--runtime-root", str(runtime_root)])

    assert result == 0


def test_queue_status_runs_against_populated_runtime_returns_zero(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    for dirname in ("queue", "processing", "failed"):
        (runtime_root / dirname).mkdir(parents=True, exist_ok=True)
        (runtime_root / dirname / f"{dirname}.json").write_text("{}\n", encoding="utf-8")

    result = status.run(["--runtime-root", str(runtime_root)])

    assert result == 0
