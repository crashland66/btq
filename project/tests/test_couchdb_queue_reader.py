from __future__ import annotations

import io
import json
import urllib.error

import pytest

from event_pipeline import couchdb_config
from event_pipeline import couchdb_queue_reader as reader


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _config() -> reader.QueueReaderConfig:
    return reader.QueueReaderConfig(
        couchdb=couchdb_config.CouchDBConfig(
            base_url="http://couchdb.test",
            username="jordan",
            password="secret",
            timeout=10.0,
            heartbeat_ms=10000,
        ),
        queue_database="btq_queue",
    )


def _all_docs_payload(docs: list[dict]) -> dict:
    return {"rows": [{"id": doc["_id"], "doc": doc} for doc in docs]}


def test_list_queue_jobs_filters_by_date_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {"_id": "_design/jobs", "btq_state": "pending"},
        {"_id": "2026-06-04T12-00-00Z__old", "created_at": "2026-06-04T12:00:00+00:00", "btq_state": "pending"},
        {"_id": "2026-06-05T09-00-00Z__keep", "created_at": "2026-06-05T09:00:00+00:00", "btq_state": "pending"},
        {"_id": "2026-06-05T10-00-00Z__failed", "created_at": "2026-06-05T10:00:00+00:00", "btq_state": "failed"},
    ]

    monkeypatch.setattr(reader.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse(_all_docs_payload(docs)))

    result = reader.list_queue_jobs(date="2026-06-05", state="pending", config=_config())

    assert [doc["_id"] for doc in result] == ["2026-06-05T09-00-00Z__keep"]


def test_queue_state_counts_and_recent_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {"_id": "2026-06-05T09-00-00Z__pending-one", "job_type": "append_to_note", "btq_state": "pending", "created_at": "2026-06-05T09:00:00+00:00"},
        {"_id": "2026-06-05T10-00-00Z__pending-two", "job_type": "add_person", "btq_state": "pending", "created_at": "2026-06-05T10:00:00+00:00"},
        {"_id": "2026-06-05T11-00-00Z__failed-one", "job_type": "visit_create", "btq_state": "failed", "created_at": "2026-06-05T11:00:00+00:00"},
        {"_id": "2026-06-05T12-00-00Z__complete-one", "job_type": "append_to_note", "btq_state": "complete", "created_at": "2026-06-05T12:00:00+00:00"},
    ]

    monkeypatch.setattr(reader.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse(_all_docs_payload(docs)))

    state = reader.queue_state(states=("failed", "pending"), recent_limit=1, config=_config())

    assert state["counts"] == {"failed": 1, "pending": 2}
    assert state["recent"]["pending"] == [
        {
            "id": "2026-06-05T10-00-00Z__pending-two",
            "job_id": "2026-06-05T10-00-00Z__pending-two",
            "slug": "pending-two",
            "job_type": "add_person",
            "btq_state": "pending",
            "created_at": "2026-06-05T10:00:00+00:00",
            "age_seconds": state["recent"]["pending"][0]["age_seconds"],
        }
    ]
    assert state["recent"]["failed"][0]["job_type"] == "visit_create"


def test_get_job_returns_none_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_404(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError("http://couchdb.test/btq_queue/missing", 404, "not found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(reader.urllib.request, "urlopen", raise_404)

    assert reader.get_job("missing", config=_config()) is None


def test_reader_maps_couchdb_unavailable_to_public_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_url_error(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(reader.urllib.request, "urlopen", raise_url_error)

    with pytest.raises(reader.CouchDBQueueReaderError) as exc:
        reader.list_queue_jobs(config=_config())

    assert exc.value.code == "couchdb_unavailable"
    assert "cannot reach CouchDB" in exc.value.message
