from __future__ import annotations

import json
from pathlib import Path

from btq import run as btq_run
from queue_processor import health
from queue_processor import metrics
from queue_processor.processed_index import index_path_for


def alert_history_lines(runtime_root: Path) -> list[dict[str, object]]:
    path = health.alert_paths(runtime_root)["history"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_emit_metrics_flag_writes_a_sample(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    prune_calls: list[Path] = []

    def fake_prune(metrics_path: Path, retain_hours: int, now: float | None = None) -> int:
        prune_calls.append(metrics_path)
        return 0

    monkeypatch.setattr(metrics, "prune", fake_prune)

    result = health.run(["--runtime-root", str(runtime_root), "--vault-root", str(vault_root), "--emit-metrics"])

    metrics_path = metrics.metrics_path_for(runtime_root)
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert result == 0
    assert len(lines) == 1
    assert json.loads(lines[0])["queue_depth"] == 0
    assert prune_calls == [metrics_path]


def test_health_default_does_not_write_metrics(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    result = health.run(["--runtime-root", str(runtime_root), "--vault-root", str(vault_root)])

    assert result == 0
    assert not metrics.metrics_path_for(runtime_root).exists()


def test_monitor_non_critical_health_produces_no_alert(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    result = health.run(["--runtime-root", str(runtime_root), "--vault-root", str(vault_root), "--monitor", "--quiet"])

    assert result == health.EXIT_OK
    paths = health.alert_paths(runtime_root)
    assert not paths["history"].exists()
    assert not paths["latest"].exists()


def test_monitor_critical_health_produces_alert_artifact(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "queue" / "job.json").write_text("{}\n", encoding="utf-8")

    result = health.run(
        [
            "--runtime-root",
            str(runtime_root),
            "--vault-root",
            str(vault_root),
            "--queue-backlog-threshold",
            "0",
            "--monitor",
            "--quiet",
        ]
    )

    assert result == health.EXIT_CRITICAL_ALERTED
    [record] = alert_history_lines(runtime_root)
    assert record["status"] == "critical"
    assert record["critical"] == ["queue backlog above threshold (1>0)"]
    latest = json.loads(health.alert_paths(runtime_root)["latest"].read_text(encoding="utf-8"))
    assert latest["fingerprint"] == record["fingerprint"]


def test_monitor_repeated_identical_critical_status_is_throttled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "queue" / "job.json").write_text("{}\n", encoding="utf-8")
    args = [
        "--runtime-root",
        str(runtime_root),
        "--vault-root",
        str(vault_root),
        "--queue-backlog-threshold",
        "0",
        "--monitor",
        "--quiet",
    ]

    assert health.run(args) == health.EXIT_CRITICAL_ALERTED
    assert health.run(args) == health.EXIT_CRITICAL_THROTTLED

    assert len(alert_history_lines(runtime_root)) == 1


def test_monitor_changed_critical_status_alerts_again(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "queue" / "job.json").write_text("{}\n", encoding="utf-8")
    args = [
        "--runtime-root",
        str(runtime_root),
        "--vault-root",
        str(vault_root),
        "--queue-backlog-threshold",
        "0",
        "--monitor",
        "--quiet",
    ]

    assert health.run(args) == health.EXIT_CRITICAL_ALERTED
    index_path = index_path_for(runtime_root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{bad-json\n", encoding="utf-8")
    assert health.run(args) == health.EXIT_CRITICAL_ALERTED

    records = alert_history_lines(runtime_root)
    assert len(records) == 2
    assert records[0]["fingerprint"] != records[1]["fingerprint"]
    assert "corrupted processed index" in records[1]["critical"]


def test_monitor_exit_codes_distinguish_ok_alerted_and_throttled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()

    ok_args = ["--runtime-root", str(runtime_root), "--vault-root", str(vault_root), "--monitor", "--quiet"]
    assert health.run(ok_args) == health.EXIT_OK

    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "queue" / "job.json").write_text("{}\n", encoding="utf-8")
    critical_args = ok_args + ["--queue-backlog-threshold", "0"]
    assert health.run(critical_args) == health.EXIT_CRITICAL_ALERTED
    assert health.run(critical_args) == health.EXIT_CRITICAL_THROTTLED


def test_btq_health_monitor_cli_forwards_scheduled_exit_code(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "queue" / "job.json").write_text("{}\n", encoding="utf-8")

    result = btq_run(
        [
            "health",
            "--runtime-root",
            str(runtime_root),
            "--vault-root",
            str(vault_root),
            "--queue-backlog-threshold",
            "0",
            "--monitor",
            "--quiet",
        ]
    )

    assert result == health.EXIT_CRITICAL_ALERTED
