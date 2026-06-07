from __future__ import annotations

import importlib.util
import json
import logging
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline.couchdb_queue_reader import CouchDBQueueReaderError
from queue_processor import cowork_read_watcher as watcher


REPO_ROOT = Path(__file__).resolve().parent.parent


def _dirs(tmp_path: Path) -> dict[str, Path]:
    drop_root = tmp_path / "cowork_read"
    dirs = {
        "requests": drop_root / "requests",
        "responses": drop_root / "responses",
        "processed": drop_root / "processed",
        "failed": drop_root / "failed",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    return dirs


def _logger() -> logging.Logger:
    return logging.getLogger("test_cowork_read_watcher")


def _write_request(dirs: dict[str, Path], request_id: str, tool: str, args: dict | None = None) -> Path:
    path = dirs["requests"] / f"{request_id}.json"
    path.write_text(
        json.dumps({"request_id": request_id, "tool": tool, "args": args or {}, "created_at": "2026-06-05T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return path


def _load_helper(name: str = "btq_cowork_read_script"):
    loader = SourceFileLoader(name, str(REPO_ROOT / "scripts" / "btq-cowork-read"))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_process_request_queue_state_writes_response_and_moves_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = _dirs(tmp_path)
    source = _write_request(dirs, "req-state", "queue_state", {"states": ["pending"], "recent_limit": 2})
    fake_config = object()
    calls: list[dict] = []

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", lambda: fake_config)

    def fake_queue_state(**kwargs):
        calls.append(kwargs)
        return {"counts": {"pending": 1}, "recent": {"pending": [{"job_id": "job-1"}]}}

    monkeypatch.setattr(watcher.couchdb_queue_reader, "queue_state", fake_queue_state)

    watcher.process_request(source, dirs, _logger())

    assert calls == [{"states": ("pending",), "recent_limit": 2, "config": fake_config}]
    assert not source.exists()
    assert (dirs["processed"] / "req-state.json").exists()
    response = json.loads((dirs["responses"] / "req-state.json").read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert response["request_id"] == "req-state"
    assert response["tool"] == "queue_state"
    assert response["result"]["counts"] == {"pending": 1}


def test_process_request_list_queue_jobs_writes_response_and_moves_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = _dirs(tmp_path)
    source = _write_request(
        dirs,
        "req-list",
        "list_queue_jobs",
        {"date": "2026-06-05", "since": "2026-06-01", "state": "pending", "all_dates": False, "limit": 5},
    )
    fake_config = object()
    calls: list[dict] = []

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", lambda: fake_config)

    def fake_list_queue_jobs(**kwargs):
        calls.append(kwargs)
        return [{"_id": "job-1", "btq_state": "pending"}]

    monkeypatch.setattr(watcher.couchdb_queue_reader, "list_queue_jobs", fake_list_queue_jobs)

    watcher.process_request(source, dirs, _logger())

    assert calls == [{
        "date": "2026-06-05",
        "since": "2026-06-01",
        "state": "pending",
        "all_dates": False,
        "limit": 5,
        "config": fake_config,
    }]
    assert (dirs["processed"] / "req-list.json").exists()
    response = json.loads((dirs["responses"] / "req-list.json").read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert response["tool"] == "list_queue_jobs"
    assert response["result"] == [{"_id": "job-1", "btq_state": "pending"}]


def test_process_request_get_job_writes_response_and_moves_processed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = _dirs(tmp_path)
    source = _write_request(dirs, "req-get", "get_job", {"job_id": "job-123"})
    fake_config = object()
    calls: list[dict] = []

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", lambda: fake_config)

    def fake_get_job(job_id: str, **kwargs):
        calls.append({"job_id": job_id, **kwargs})
        return {"_id": job_id, "job_type": "append_to_note"}

    monkeypatch.setattr(watcher.couchdb_queue_reader, "get_job", fake_get_job)

    watcher.process_request(source, dirs, _logger())

    assert calls == [{"job_id": "job-123", "config": fake_config}]
    assert (dirs["processed"] / "req-get.json").exists()
    response = json.loads((dirs["responses"] / "req-get.json").read_text(encoding="utf-8"))
    assert response["ok"] is True
    assert response["tool"] == "get_job"
    assert response["result"] == {"_id": "job-123", "job_type": "append_to_note"}


@pytest.mark.parametrize(
    ("filename", "content", "expected_code"),
    [
        ("bad-json.json", "{not json", "invalid_json"),
        ("bad-tool.json", json.dumps({"request_id": "bad-tool", "tool": "enqueue", "args": {}}), "invalid_tool"),
        ("bad-args.json", json.dumps({"request_id": "bad-args", "tool": "queue_state", "args": []}), "invalid_request"),
    ],
)
def test_bad_requests_move_failed_and_write_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content: str,
    expected_code: str,
) -> None:
    dirs = _dirs(tmp_path)
    source = dirs["requests"] / filename
    source.write_text(content, encoding="utf-8")

    def fail_load_config():
        raise AssertionError("bad requests must not load CouchDB reader config")

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", fail_load_config)

    watcher.process_request(source, dirs, _logger())

    assert not source.exists()
    assert (dirs["failed"] / filename).exists()
    response = json.loads((dirs["responses"] / f"{Path(filename).stem}.json").read_text(encoding="utf-8"))
    assert response["ok"] is False
    assert response["error"]["code"] == expected_code
    assert response["error"]["message"]


def test_transient_reader_error_leaves_request_for_retry_and_writes_no_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = _dirs(tmp_path)
    source = _write_request(dirs, "retry-me", "queue_state")

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", lambda: object())

    def raise_unavailable(**_kwargs):
        raise CouchDBQueueReaderError("couchdb_unavailable", "cannot reach CouchDB")

    monkeypatch.setattr(watcher.couchdb_queue_reader, "queue_state", raise_unavailable)

    watcher.process_request(source, dirs, _logger())

    assert source.exists()
    assert not any(dirs["responses"].iterdir())
    assert not any(dirs["processed"].iterdir())
    assert not any(dirs["failed"].iterdir())


def test_non_transient_reader_error_moves_failed_and_writes_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = _dirs(tmp_path)
    source = _write_request(dirs, "config-error", "queue_state")

    def raise_config_error():
        raise CouchDBQueueReaderError("config_error", "missing CouchDB credentials")

    monkeypatch.setattr(watcher.couchdb_queue_reader, "load_reader_config", raise_config_error)

    watcher.process_request(source, dirs, _logger())

    assert not source.exists()
    assert (dirs["failed"] / "config-error.json").exists()
    response = json.loads((dirs["responses"] / "config-error.json").read_text(encoding="utf-8"))
    assert response["ok"] is False
    assert response["error"] == {"code": "config_error", "message": "missing CouchDB credentials"}


def test_watcher_read_only_invariant() -> None:
    source = (REPO_ROOT / "project" / "queue_processor" / "cowork_read_watcher.py").read_text(encoding="utf-8")
    forbidden = ("btq-enqueue", "cowork_drop_watcher", "process_file", "subprocess", "_enqueue")
    assert not any(term in source for term in forbidden)
    assert "couchdb_queue_reader" in source


def test_helper_writes_request_and_returns_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _load_helper("btq_cowork_read_success")
    pipeline_dir = tmp_path / "pipeline"
    sleeps = 0

    def fake_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        requests = list((pipeline_dir / "cowork_read" / "requests").glob("*.json"))
        if requests:
            request = json.loads(requests[0].read_text(encoding="utf-8"))
            response_dir = pipeline_dir / "cowork_read" / "responses"
            response_dir.mkdir(parents=True, exist_ok=True)
            (response_dir / f"{request['request_id']}.json").write_text(
                json.dumps({"request_id": request["request_id"], "ok": True, "tool": request["tool"], "result": {"counts": {}}}),
                encoding="utf-8",
            )

    monkeypatch.setattr(helper.time, "sleep", fake_sleep)

    response = helper.submit_and_wait(
        "queue_state",
        {"recent_limit": 1},
        timeout=1,
        pipeline_dir=pipeline_dir,
        request_id="helper-success",
    )

    assert sleeps >= 1
    request = json.loads((pipeline_dir / "cowork_read" / "requests" / "helper-success.json").read_text(encoding="utf-8"))
    assert request["tool"] == "queue_state"
    assert request["args"] == {"recent_limit": 1}
    assert response["ok"] is True
    assert response["result"] == {"counts": {}}


def test_helper_returns_structured_timeout_when_no_response(tmp_path: Path) -> None:
    helper = _load_helper("btq_cowork_read_timeout")

    response = helper.submit_and_wait(
        "queue_state",
        {},
        timeout=0,
        pipeline_dir=tmp_path / "pipeline",
        request_id="helper-timeout",
    )

    assert response["ok"] is False
    assert response["request_id"] == "helper-timeout"
    assert response["error"]["code"] == "reader_timeout"
