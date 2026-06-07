from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from event_pipeline import couchdb_worker
from event_pipeline.couchdb_listener import CouchDBListenerError


class FakeListener:
    def __init__(self, docs: list[dict[str, Any]], failures_before_success: int = 0) -> None:
        self.docs = docs
        self.failures_before_success = failures_before_success
        self.mark_processing_calls = 0
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.stopped = False

    def listen(self) -> Any:
        yield from self.docs

    def mark_processing(self, doc: dict[str, Any], state: str = "processing") -> dict[str, Any]:
        self.mark_processing_calls += 1
        if self.mark_processing_calls <= self.failures_before_success:
            raise CouchDBListenerError("transient conflict")
        updated = dict(doc)
        updated["state"] = state
        updated["_rev"] = "2-processing"
        return updated

    def mark_complete(self, doc: dict[str, Any], state: str = "complete") -> None:
        self.completed.append(f"{doc['_id']}:{state}")

    def mark_failed(self, doc: dict[str, Any], reason: str, state: str = "failed") -> None:
        self.failed.append((f"{doc['_id']}:{state}", reason))

    def stop(self) -> None:
        self.stopped = True


def run_worker_with_listener(
    listener: FakeListener,
    processed: list[str],
    *,
    runtime_root: Path,
    once: bool = False,
    json_output: bool = False,
    process_ok: bool = True,
) -> int:
    return couchdb_worker.run_change_worker(
        database="db",
        state_field="state",
        runtime_root=runtime_root,
        stage="test_stage",
        log_path=None,
        default_log_path=lambda root: root / "logs" / "worker.log",
        logger_name="test.couchdb_worker",
        worker_label="CouchDB test",
        missing_url_message="missing",
        config_failed_message="config failed: %s",
        listener_error_message="listener failed: %s",
        tunnel_failed_message="tunnel failed: %s",
        process_doc=lambda doc, _logger: processed.append(str(doc["_id"])) or {"doc_id": doc["_id"], "ok": process_ok, "error": "" if process_ok else "boom"},
        id_for_doc=lambda doc: str(doc["_id"]),
        id_label="doc_id",
        default_failure_reason="failed",
        exhausted_result=lambda doc_id, error: {"doc_id": doc_id, "ok": False, "error": f"mark_processing exhausted: {error}"},
        log_processed=lambda *_args: None,
        once=once,
        json_output=json_output,
        listener=listener,
        logger=logging.getLogger("test.couchdb_worker"),
    )


def test_run_change_worker_once_mode_processes_then_exits(tmp_path: Path) -> None:
    listener = FakeListener([{"_id": "doc1", "_rev": "1-a", "state": "claimed"}])
    processed: list[str] = []

    result = run_worker_with_listener(listener, processed, once=True, runtime_root=tmp_path)

    assert result == 0
    assert processed == ["doc1"]
    assert listener.completed == ["doc1:complete"]
    assert listener.stopped is True


def test_run_change_worker_mark_processing_retries_with_backoff(monkeypatch, capsys, tmp_path: Path) -> None:
    listener = FakeListener([{"_id": "doc1", "_rev": "1-a", "state": "claimed"}], failures_before_success=99)
    processed: list[str] = []
    sleeps: list[float] = []

    monkeypatch.setattr(couchdb_worker, "MARK_PROCESSING_BACKOFF_SECONDS", (0.1, 0.2, 0.4))
    monkeypatch.setattr(couchdb_worker.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = run_worker_with_listener(listener, processed, json_output=True, runtime_root=tmp_path)

    assert result == 0
    assert listener.mark_processing_calls == 3
    assert sleeps == [0.1, 0.2]
    assert processed == []
    assert listener.completed == []
    assert "mark_processing exhausted: transient conflict" in capsys.readouterr().out


def test_worker_records_latency_on_success(monkeypatch, tmp_path: Path) -> None:
    listener = FakeListener([{"_id": "doc1", "_rev": "1-a", "state": "claimed"}])
    processed: list[str] = []
    ticks = iter([10.0, 10.125])
    monkeypatch.setattr(couchdb_worker.time, "monotonic", lambda: next(ticks))

    result = run_worker_with_listener(listener, processed, runtime_root=tmp_path)

    assert result == 0
    lines = (tmp_path / "metrics" / "pipeline_latency.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["stage"] == "test_stage"
    assert payload["duration_ms"] == 125.0


def test_worker_does_not_record_latency_on_failure_or_skip(tmp_path: Path) -> None:
    failed_listener = FakeListener([{"_id": "doc1", "_rev": "1-a", "state": "claimed"}])
    processed: list[str] = []

    result = run_worker_with_listener(failed_listener, processed, runtime_root=tmp_path, process_ok=False)

    assert result == 0
    assert not (tmp_path / "metrics" / "pipeline_latency.jsonl").exists()

    skipped_listener = FakeListener([{"_id": "doc2", "_rev": "1-a", "state": "claimed"}], failures_before_success=99)
    result = run_worker_with_listener(skipped_listener, processed, runtime_root=tmp_path)

    assert result == 0
    assert not (tmp_path / "metrics" / "pipeline_latency.jsonl").exists()
