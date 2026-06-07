from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeChangesResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines
        self.status = 200

    def __enter__(self) -> "FakeChangesResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def readline(self) -> bytes:
        if not self.lines:
            return b""
        return self.lines.pop(0)


def request_payload(req: object) -> dict[str, object]:
    data = getattr(req, "data", None)
    if not data:
        return {}
    return json.loads(data.decode("utf-8"))


def http_error(url: str, code: int) -> error.HTTPError:
    return error.HTTPError(url, code, "error", hdrs=None, fp=None)


def test_listen_yields_claimed_pending_document(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        payload = request_payload(req)
        calls.append((method, url, payload))
        if method == "POST" and "_changes" in url:
            return FakeChangesResponse([b'{"seq":1,"id":"cap1"}\n'])
        if method == "GET" and url.endswith("/cap1"):
            return FakeResponse({"_id": "cap1", "_rev": "1-a", "processing_state": "pending"})
        if method == "PUT" and url.endswith("/cap1"):
            assert payload["processing_state"] == "claimed"
            assert payload["_rev"] == "1-a"
            return FakeResponse({"ok": True, "id": "cap1", "rev": "2-b"})
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")
    iterator = listener.listen()

    claimed = next(iterator)
    listener.stop()

    assert claimed["_id"] == "cap1"
    assert claimed["_rev"] == "2-b"
    assert claimed["processing_state"] == "claimed"
    assert calls[0][0] == "POST"


def test_listen_skips_document_already_claimed_after_change(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        if method == "POST" and "_changes" in url:
            return FakeChangesResponse([b'{"seq":1,"id":"cap1"}\n', b"\n"])
        if method == "GET" and url.endswith("/cap1"):
            listener.stop()
            return FakeResponse({"_id": "cap1", "_rev": "1-a", "processing_state": "claimed"})
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)

    assert list(listener.listen()) == []


def test_listen_skips_document_when_claim_put_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        if method == "POST" and "_changes" in url:
            return FakeChangesResponse([b'{"seq":1,"id":"cap1"}\n', b"\n"])
        if method == "GET" and url.endswith("/cap1"):
            return FakeResponse({"_id": "cap1", "_rev": "1-a", "processing_state": "pending"})
        if method == "PUT" and url.endswith("/cap1"):
            listener.stop()
            raise http_error(url, 409)
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)

    assert list(listener.listen()) == []


def test_listen_reconnects_and_resumes_from_last_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    feed_count = 0

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        nonlocal feed_count
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        calls.append(url)
        if method == "POST" and "_changes" in url:
            feed_count += 1
            if feed_count == 1:
                return FakeChangesResponse([b'{"seq":7,"id":"cap1"}\n'])
            return FakeChangesResponse([b'{"seq":8,"id":"cap2"}\n'])
        if method == "GET" and url.endswith("/cap1"):
            return FakeResponse({"_id": "cap1", "_rev": "1-a", "processing_state": "claimed"})
        if method == "GET" and url.endswith("/cap2"):
            return FakeResponse({"_id": "cap2", "_rev": "1-a", "processing_state": "pending"})
        if method == "PUT" and url.endswith("/cap2"):
            return FakeResponse({"ok": True, "id": "cap2", "rev": "2-b"})
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    monkeypatch.setattr("event_pipeline.couchdb_listener.time.sleep", lambda _seconds: None)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")
    iterator = listener.listen()

    claimed = next(iterator)
    listener.stop()

    assert claimed["_id"] == "cap2"
    assert any("since=7" in url for url in calls if "_changes" in url)


def test_listen_stops_after_stop_called(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        url = getattr(req, "full_url")
        if "_changes" in url:
            listener.stop()
            return FakeChangesResponse([b'{"seq":1,"id":"cap1"}\n'])
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)

    assert list(listener.listen()) == []


def test_mark_processing_puts_processing_state_and_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        sent.append(request_payload(req))
        return FakeResponse({"ok": True, "id": "cap1", "rev": "2-b"})

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    updated = listener.mark_processing({"_id": "cap1", "_rev": "1-a", "processing_state": "claimed"})

    assert sent[0]["processing_state"] == "processing"
    assert sent[0]["_rev"] == "1-a"
    assert updated["_rev"] == "2-b"


def test_mark_complete_puts_complete_state(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        sent.append(request_payload(req))
        return FakeResponse({"ok": True, "id": "cap1", "rev": "2-b"})

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    listener.mark_complete({"_id": "cap1", "_rev": "1-a", "processing_state": "processing"})

    assert sent[0]["processing_state"] == "complete"


def test_mark_failed_puts_failed_state_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        sent.append(request_payload(req))
        return FakeResponse({"ok": True, "id": "cap1", "rev": "2-b"})

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    listener.mark_failed({"_id": "cap1", "_rev": "1-a", "processing_state": "processing"}, "boom")

    assert sent[0]["processing_state"] == "failed"
    assert sent[0]["error_reason"] == "boom"


def test_mark_processing_raises_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        raise http_error(getattr(req, "full_url"), 409)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    with pytest.raises(CouchDBListenerError):
        listener.mark_processing({"_id": "cap1", "_rev": "1-a", "processing_state": "claimed"})


def test_listen_raises_after_reconnect_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: object, timeout: float = 60.0) -> object:
        raise error.URLError("connection refused")

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    monkeypatch.setattr("event_pipeline.couchdb_listener.time.sleep", lambda _seconds: None)
    listener = CouchDBChangesListener(
        database="btq_captures",
        state_field="processing_state",
        base_url="http://couchdb.test",
        max_reconnect_attempts=2,
    )

    with pytest.raises(CouchDBListenerError):
        list(listener.listen())


def test_listener_max_reconnect_default_is_sixty() -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    assert listener.max_reconnect_attempts == 60


def test_listener_reconnect_backoff_caps_at_thirty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    monkeypatch.setattr("event_pipeline.couchdb_listener.time.sleep", delays.append)
    attempts = 0
    for _ in range(8):
        attempts = listener._next_reconnect_attempt(attempts, error.URLError("connection refused"))

    assert max(delays) == 30


def test_listener_reconnect_backoff_uses_initial_exponential_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    monkeypatch.setattr("event_pipeline.couchdb_listener.time.sleep", delays.append)
    attempts = 0
    for _ in range(5):
        attempts = listener._next_reconnect_attempt(attempts, error.URLError("connection refused"))

    assert delays == [1, 2, 4, 8, 16]


def test_listener_reconnect_allows_sixty_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    monkeypatch.setattr("event_pipeline.couchdb_listener.time.sleep", lambda _seconds: None)
    attempts = 0
    for _ in range(60):
        attempts = listener._next_reconnect_attempt(attempts, error.URLError("connection refused"))

    assert attempts == 60


def test_listen_skips_empty_heartbeat_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    listener = CouchDBChangesListener(database="btq_captures", state_field="processing_state", base_url="http://couchdb.test")

    def fake_urlopen(req: object, timeout: float = 60.0) -> object:
        method = getattr(req, "method")
        url = getattr(req, "full_url")
        if method == "POST" and "_changes" in url:
            return FakeChangesResponse([b"\n", b'{"seq":1,"id":"cap1"}\n'])
        if method == "GET" and url.endswith("/cap1"):
            return FakeResponse({"_id": "cap1", "_rev": "1-a", "processing_state": "pending"})
        if method == "PUT" and url.endswith("/cap1"):
            return FakeResponse({"ok": True, "id": "cap1", "rev": "2-b"})
        raise AssertionError(url)

    monkeypatch.setattr("event_pipeline.couchdb_listener.request.urlopen", fake_urlopen)
    iterator = listener.listen()

    claimed = next(iterator)
    listener.stop()

    assert claimed["_id"] == "cap1"
