from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import health_pipeline
from tests.test_ops_dashboard import request_text


def install_pipeline_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler_docs: dict[str, object] | None = None,
    session_error: Exception | None = None,
    unloaded_label: str = "",
) -> None:
    cfg = SimpleNamespace(base_url="http://127.0.0.1:5984", auth_header=lambda: {"Authorization": "Basic test"})
    monkeypatch.setattr(health_pipeline.couchdb_config, "from_env", lambda: cfg)

    def fake_session(_config: object) -> dict[str, object]:
        if session_error is not None:
            raise session_error
        return {"ok": True}

    def fake_scheduler(_config: object) -> dict[str, object]:
        return scheduler_docs or {
            "docs": [
                {
                    "doc_id": "vps_to_pro_btq_vault",
                    "source": "http://203.0.113.10:5984/btq_vault/",
                    "target": "http://127.0.0.1:5984/btq_vault/",
                    "state": "running",
                    "info": {"error_count": 0, "last_updated": "2026-05-26T12:00:00Z"},
                },
                {
                    "doc_id": "d68f12729_once",
                    "source": "http://203.0.113.10:5984/btq_once/",
                    "target": "http://127.0.0.1:5984/btq_once/",
                    "state": "completed",
                    "info": {"error_count": 0, "last_updated": "2026-05-26T12:01:00Z"},
                },
            ]
        }

    def fake_launchctl(label: str) -> dict[str, object]:
        if label == unloaded_label:
            return {"label": label, "state": "not loaded", "pid": None, "last_exit_code": None}
        return {"label": label, "state": "running", "pid": 1234, "last_exit_code": 0}

    monkeypatch.setattr(health_pipeline, "_get_session", fake_session)
    monkeypatch.setattr(health_pipeline, "_get_scheduler_docs", fake_scheduler)
    monkeypatch.setattr(health_pipeline, "_launchctl_state", fake_launchctl)


def test_pipeline_status_all_ok_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_pipeline_fixtures(monkeypatch)

    status = health_pipeline.pipeline_status(tmp_path)

    assert set(status) == {"couchdb", "replicators", "watchers", "summary"}
    assert isinstance(status["couchdb"], dict)
    assert isinstance(status["replicators"], list)
    assert isinstance(status["watchers"], list)
    assert isinstance(status["summary"], dict)
    assert status["couchdb"]["reachable"] is True
    assert isinstance(status["couchdb"]["round_trip_ms"], float)
    assert status["summary"]["ok"] is True
    assert status["summary"]["failing"] == []
    assert status["summary"]["replicator_count"] == 2
    assert status["summary"]["running_count"] == 1
    assert status["summary"]["watcher_count"] == len(health_pipeline.WATCHER_LABELS)
    assert status["summary"]["watcher_running_count"] == len(health_pipeline.WATCHER_LABELS)


def test_pipeline_status_replicator_crashing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_pipeline_fixtures(
        monkeypatch,
        scheduler_docs={
            "docs": [
                {
                    "doc_id": "vps_to_pro_btq_sites",
                    "source": "http://203.0.113.10:5984/btq_sites/",
                    "target": "http://127.0.0.1:5984/btq_sites/",
                    "state": "crashing",
                    "info": {"error_count": 8, "last_error": "unauthorized"},
                }
            ]
        },
    )

    status = health_pipeline.pipeline_status(tmp_path)

    assert status["summary"]["ok"] is False
    assert "replicator vps_to_pro_btq_sites: crashing" in status["summary"]["failing"]


def test_pipeline_status_watcher_not_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    label = "com.btq.field-capture-pipeline-watcher"
    install_pipeline_fixtures(monkeypatch, unloaded_label=label)

    status = health_pipeline.pipeline_status(tmp_path)

    assert status["summary"]["ok"] is False
    assert f"watcher {label}: not loaded" in status["summary"]["failing"]
    watcher = next(item for item in status["watchers"] if item["label"] == label)
    assert watcher["state"] == "not loaded"


def test_pipeline_status_couchdb_unreachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_pipeline_fixtures(monkeypatch, session_error=ConnectionError("connection refused"))

    status = health_pipeline.pipeline_status(tmp_path)

    assert status["couchdb"]["reachable"] is False
    assert status["summary"]["ok"] is False
    assert any(item.startswith("couchdb: ") for item in status["summary"]["failing"])


def test_health_pipeline_route_returns_200(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_pipeline_fixtures(monkeypatch)

    status, _content_type, body = request_text("GET", "/health/pipeline", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "Pipeline Health" in body


def test_launchctl_state_returns_outermost_state_not_inner_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: launchctl print emits both the daemon's outer state (e.g.
    'running') and inner sub-block states (e.g. 'active'). Parser must take
    the first/outermost match, not the last."""
    fake_output = (
        "com.btq.example = {\n"
        "\tactive count = 1\n"
        "\tpath = /Users/x/Library/LaunchAgents/com.btq.example.plist\n"
        "\tstate = running\n"
        "\tpid = 4242\n"
        "\tlast exit code = 0\n"
        "\tendpoints = {\n"
        "\t\tstate = active\n"
        "\t}\n"
        "\tspawn type = daemon\n"
        "\tdomain = gui/501 [100040]\n"
        "\t\tstate = active\n"
        "}\n"
    )

    class FakeResult:
        returncode = 0
        stdout = fake_output
        stderr = ""

    monkeypatch.setattr(health_pipeline.subprocess, "run", lambda *a, **k: FakeResult())

    result = health_pipeline._launchctl_state("com.btq.example")

    assert result["state"] == "running", f"expected outermost 'running', got {result['state']!r}"
    assert result["pid"] == 4242
    assert result["last_exit_code"] == 0
