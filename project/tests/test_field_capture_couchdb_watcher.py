from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from event_pipeline.couchdb_listener import CouchDBListenerError
from field_capture import couchdb_watcher


class FakeListener:
    def __init__(self, docs: list[dict[str, Any]], failures_before_success: dict[str, int] | None = None) -> None:
        self.docs = docs
        self.failures_before_success = failures_before_success or {}
        self.mark_processing_calls: dict[str, int] = {}
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def listen(self) -> Any:
        yield from self.docs

    def mark_processing(self, doc: dict[str, Any]) -> dict[str, Any]:
        capture_id = str(doc.get("capture_id") or doc.get("_id"))
        self.mark_processing_calls[capture_id] = self.mark_processing_calls.get(capture_id, 0) + 1
        if self.mark_processing_calls[capture_id] <= self.failures_before_success.get(capture_id, 0):
            raise CouchDBListenerError(f"transient failure for {capture_id}")
        updated = dict(doc)
        updated["processing_state"] = "processing"
        updated["_rev"] = f"{doc.get('_rev')}-processing"
        return updated

    def mark_complete(self, doc: dict[str, Any]) -> None:
        self.completed.append(str(doc.get("capture_id") or doc.get("_id")))

    def mark_failed(self, doc: dict[str, Any], reason: str) -> None:
        self.failed.append((str(doc.get("capture_id") or doc.get("_id")), reason))


def process_ok(doc: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    capture_id = str(doc.get("capture_id") or doc.get("_id"))
    return {"capture_id": capture_id, "ok": True, "counts": {"imported": 1}, "error": ""}


def run_fake_listener(
    monkeypatch,
    listener: FakeListener,
    logger: logging.Logger,
    *,
    json_output: bool = False,
) -> None:
    monkeypatch.setattr(couchdb_watcher, "MARK_PROCESSING_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(couchdb_watcher, "process_one", process_ok)
    couchdb_watcher.run_listener(
        listener=listener,  # type: ignore[arg-type]
        runtime_root=Path("/tmp/runtime"),
        remote_host="btq-vps",
        registry=object(),  # type: ignore[arg-type]
        runner=None,
        dry_run=False,
        json_output=json_output,
        logger=logger,
    )


def test_default_state_field_is_processing_state() -> None:
    assert couchdb_watcher.DEFAULT_STATE_FIELD == "processing_state"


def test_field_capture_watcher_delegates_to_runner(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run_change_worker(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(couchdb_watcher, "run_change_worker", fake_run_change_worker)

    result = couchdb_watcher.run(
        [
            "--runtime-root",
            str(tmp_path),
            "--log-path",
            str(tmp_path / "field.log"),
            "--database",
            "field-db",
            "--state-field",
            "processing_state",
            "--once",
            "--json",
        ]
    )

    assert result == 0
    assert captured["database"] == "field-db"
    assert captured["state_field"] == "processing_state"
    assert captured["stage"] == "field_capture"
    assert captured["state_processing"] == "processing"
    assert captured["state_done"] == "complete"
    assert captured["state_failed"] == "failed"
    assert captured["log_path"] == tmp_path / "field.log"
    assert captured["default_log_path"](tmp_path) == tmp_path / "logs" / "field_capture_couchdb_watch.log"


def test_mark_processing_retries_on_transient_failure_and_succeeds(monkeypatch, caplog) -> None:
    listener = FakeListener(
        [{"_id": "doc1", "_rev": "1-a", "capture_id": "cap1", "processing_state": "claimed"}],
        failures_before_success={"cap1": 2},
    )
    logger = logging.getLogger("test.couchdb_watcher.retry")
    caplog.set_level("WARNING", logger=logger.name)

    run_fake_listener(monkeypatch, listener, logger)

    assert listener.mark_processing_calls["cap1"] == 3
    assert listener.completed == ["cap1"]
    assert "attempt=1" in caplog.text
    assert "attempt=2" in caplog.text


def test_mark_processing_exhausts_retries_and_logs_with_capture_id(monkeypatch, caplog, capsys) -> None:
    listener = FakeListener(
        [{"_id": "doc1", "_rev": "1-a", "capture_id": "cap1", "processing_state": "claimed"}],
        failures_before_success={"cap1": 99},
    )
    logger = logging.getLogger("test.couchdb_watcher.exhaust")
    caplog.set_level("ERROR", logger=logger.name)

    run_fake_listener(monkeypatch, listener, logger, json_output=True)

    assert listener.mark_processing_calls["cap1"] == 3
    assert listener.completed == []
    assert "mark_processing exhausted" in caplog.text
    assert "cap1" in caplog.text
    assert "1-a" in caplog.text
    assert "mark_processing exhausted: transient failure for cap1" in capsys.readouterr().out


def test_mark_processing_failure_does_not_stop_the_watcher_loop(monkeypatch, caplog) -> None:
    listener = FakeListener(
        [
            {"_id": "doc1", "_rev": "1-a", "capture_id": "cap1", "processing_state": "claimed"},
            {"_id": "doc2", "_rev": "1-a", "capture_id": "cap2", "processing_state": "claimed"},
        ],
        failures_before_success={"cap1": 99},
    )
    logger = logging.getLogger("test.couchdb_watcher.continue")
    caplog.set_level("ERROR", logger=logger.name)

    run_fake_listener(monkeypatch, listener, logger)

    assert listener.mark_processing_calls["cap1"] == 3
    assert listener.mark_processing_calls["cap2"] == 1
    assert listener.completed == ["cap2"]
    assert "mark_processing exhausted" in caplog.text
