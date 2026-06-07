from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_writer import (
    CouchDBCaptureWriterError,
    get_field_capture_document,
    put_field_capture_document,
)


def _make_config(base_url: str = "http://couchdb.test", username: str = "jordan", password: str = "secret") -> couchdb_config.CouchDBConfig:
    return couchdb_config.CouchDBConfig(
        base_url=base_url,
        username=username,
        password=password,
        timeout=12.0,
        heartbeat_ms=10000,
    )


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def test_get_field_capture_document_returns_dict_when_exists(monkeypatch) -> None:
    expected = {"_id": "cap-photo-x", "_rev": "3-z", "capture_id": "cap-photo-x"}

    def fake_urlopen(req, timeout):
        return FakeResponse(expected)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "u", "p", 10.0, 10000)
    result = get_field_capture_document(config, "btq_field_captures", "cap-photo-x")
    assert result == expected


def test_get_field_capture_document_returns_none_on_404(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        from urllib import error as _error
        raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "u", "p", 10.0, 10000)
    assert get_field_capture_document(config, "btq_field_captures", "cap-photo-missing") is None


def test_get_field_capture_document_raises_on_500(monkeypatch) -> None:
    def fake_urlopen(req, timeout):
        from urllib import error as _error
        raise _error.HTTPError(getattr(req, "full_url", ""), 500, "boom", hdrs=None, fp=None)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "u", "p", 10.0, 10000)
    with pytest.raises(CouchDBCaptureWriterError):
        get_field_capture_document(config, "btq_field_captures", "cap-photo-x")


# ---------------------------------------------------------------------------
# New document (GET returns 404 → no _rev in PUT body)
# ---------------------------------------------------------------------------

def test_put_field_capture_document_new_doc_writes_without_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET returns 404; PUT body must not contain _rev."""
    put_reqs: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_reqs.append(req)
        return FakeResponse({"ok": True, "id": "cap1", "rev": "1-a"}, status=201)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    result = put_field_capture_document(_make_config(), {"_id": "cap1", "type": "field_capture"}, database="btq_field_captures")

    assert result == {"ok": True, "id": "cap1", "rev": "1-a"}
    assert len(put_reqs) == 1
    body = json.loads(getattr(put_reqs[0], "data").decode("utf-8"))
    assert body == {"_id": "cap1", "type": "field_capture"}
    assert "_rev" not in body


# ---------------------------------------------------------------------------
# Existing document (GET returns _rev → _rev included in PUT body)
# ---------------------------------------------------------------------------

def test_put_field_capture_document_existing_doc_includes_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET returns existing doc with _rev; PUT body must include _rev."""
    put_reqs: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            return FakeResponse({"_id": "cap1", "_rev": "3-abc", "type": "field_capture"})
        put_reqs.append(req)
        return FakeResponse({"ok": True, "id": "cap1", "rev": "4-def"}, status=201)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    put_field_capture_document(_make_config(), {"_id": "cap1", "type": "field_capture"}, database="btq_field_captures")

    assert len(put_reqs) == 1
    body = json.loads(getattr(put_reqs[0], "data").decode("utf-8"))
    assert body["_rev"] == "3-abc"


# ---------------------------------------------------------------------------
# 409 on first attempt → retry succeeds
# ---------------------------------------------------------------------------

def test_put_field_capture_document_retries_once_on_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """First PUT returns 409; writer fetches fresh _rev and retries; second PUT succeeds."""
    call_count = {"get": 0, "put": 0}

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            call_count["get"] += 1
            return FakeResponse({"_id": "cap1", "_rev": f"{call_count['get']}-rev"})
        call_count["put"] += 1
        if call_count["put"] == 1:
            raise error.HTTPError(getattr(req, "full_url", ""), 409, "Conflict", hdrs=None, fp=None)
        return FakeResponse({"ok": True, "id": "cap1", "rev": "2-new"}, status=201)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    result = put_field_capture_document(_make_config(), {"_id": "cap1"}, database="btq_field_captures")

    assert result["ok"] is True
    assert call_count["get"] == 2
    assert call_count["put"] == 2


# ---------------------------------------------------------------------------
# 409 on both attempts → raises
# ---------------------------------------------------------------------------

def test_put_field_capture_document_raises_after_two_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both PUT attempts return 409; writer must raise CouchDBCaptureWriterError."""
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            return FakeResponse({"_id": "cap1", "_rev": "1-rev"})
        raise error.HTTPError(getattr(req, "full_url", ""), 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    with pytest.raises(CouchDBCaptureWriterError, match="409"):
        put_field_capture_document(_make_config(), {"_id": "cap1"}, database="btq_field_captures")


# ---------------------------------------------------------------------------
# Existing tests (updated for CAS: first call is GET, second is PUT)
# ---------------------------------------------------------------------------

def test_put_field_capture_document_writes_doc_with_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    put_reqs: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_reqs.append((req, timeout))
        return FakeResponse({"ok": True, "id": "cap1", "rev": "1-a"}, status=201)

    config = _make_config()
    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)

    response = put_field_capture_document(config, {"_id": "cap1", "type": "field_capture"}, database="btq_field_captures")

    req, timeout = put_reqs[0]
    assert response == {"ok": True, "id": "cap1", "rev": "1-a"}
    assert timeout == 12.0
    assert getattr(req, "full_url") == "http://couchdb.test/btq_field_captures/cap1"
    assert getattr(req, "method") == "PUT"
    assert json.loads(getattr(req, "data").decode("utf-8")) == {"_id": "cap1", "type": "field_capture"}
    assert req.get_header("Authorization", "").startswith("Basic ")


def test_put_field_capture_document_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        raise error.HTTPError(getattr(req, "full_url"), 500, "server error", hdrs=None, fp=None)

    config = _make_config("http://couchdb.test", "", "")
    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)

    with pytest.raises(CouchDBCaptureWriterError, match="HTTP 500"):
        put_field_capture_document(config, {"_id": "cap1"}, database="btq_field_captures")


def test_put_field_capture_document_raises_on_missing_id() -> None:
    config = _make_config("http://couchdb.test", "", "")
    with pytest.raises(CouchDBCaptureWriterError, match="missing _id"):
        put_field_capture_document(config, {"type": "field_capture"}, database="btq_field_captures")


def test_put_field_capture_document_raises_on_invalid_response_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        return FakeResponse(b"{not json", status=201)

    config = _make_config("http://couchdb.test", "", "")
    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)

    with pytest.raises(CouchDBCaptureWriterError, match="invalid JSON"):
        put_field_capture_document(config, {"_id": "cap1"}, database="btq_field_captures")
