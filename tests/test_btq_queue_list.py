from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str, relpath: str):
    loader = SourceFileLoader(name, str(REPO_ROOT / relpath))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_btq_queue_list_cli_preserves_json_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    queue_list = _load_script("btq_queue_list_script", "scripts/btq-queue-list")
    docs = [
        {"_id": "2026-06-05T09-00-00Z__first", "created_at": "2026-06-05T09:00:00+00:00", "btq_state": "pending"},
        {"_id": "2026-06-05T10-00-00Z__second", "created_at": "2026-06-05T10:00:00+00:00", "btq_state": "failed"},
    ]

    monkeypatch.setattr(queue_list, "load_reader_config", lambda _path: object())
    monkeypatch.setattr(queue_list, "list_queue_jobs", lambda **_kwargs: docs)

    rc = queue_list.main(["--date", "2026-06-05", "--format", "json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == docs


def test_broker_exposes_only_read_tools() -> None:
    broker = _load_script("btq_cowork_reader_script", "scripts/btq-cowork-reader")

    assert set(broker.BROKER_TOOL_NAMES) == {"btq_queue_state", "btq_list_queue_jobs", "btq_get_queue_job"}
    exported = set(broker.BROKER_TOOL_NAMES) | set(broker.READ_TOOLS)
    forbidden = ("enqueue", "write", "delete", "repair", "retry", "ack", "put", "post")
    assert not any(verb in tool for tool in exported for verb in forbidden)


def test_broker_queue_state_returns_structured_error_when_couchdb_down(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broker = _load_script("btq_cowork_reader_script_error", "scripts/btq-cowork-reader")

    def raise_unavailable(*_args: object, **_kwargs: object) -> object:
        raise broker.CouchDBQueueReaderError("couchdb_unavailable", "cannot reach CouchDB at http://couchdb.test")

    monkeypatch.setattr(broker, "queue_state", raise_unavailable)

    rc = broker.main(["--tool", "queue_state"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "error": {
            "code": "couchdb_unavailable",
            "message": "cannot reach CouchDB at http://couchdb.test",
        },
    }
