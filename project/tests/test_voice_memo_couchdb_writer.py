from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

from voice_memo.couchdb import VoiceMemoCouchDBConfig, VoiceMemoCouchDBError, put_capture_document


def _make_config() -> VoiceMemoCouchDBConfig:
    return VoiceMemoCouchDBConfig(
        base_url="http://couchdb.test",
        username="voiceuser",
        password="secret",
        timeout=10.0,
        database="btq_voice_memos",
    )


class FakeResponse:
    def __init__(self, payload: object, status: int = 201) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self._payload, bytes):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")


# ---------------------------------------------------------------------------
# New document (GET returns 404 → no _rev in PUT body)
# ---------------------------------------------------------------------------

def test_put_capture_document_new_doc_writes_without_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET returns 404; PUT body must not contain _rev."""
    put_reqs: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            raise HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_reqs.append(req)
        return FakeResponse({"ok": True, "id": "vm1", "rev": "1-a"}, status=201)

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)
    result = put_capture_document(_make_config(), {"_id": "vm1", "type": "voice_memo"})

    assert result == {"ok": True, "id": "vm1", "rev": "1-a"}
    assert len(put_reqs) == 1
    body = json.loads(getattr(put_reqs[0], "data").decode("utf-8"))
    assert body == {"_id": "vm1", "type": "voice_memo"}
    assert "_rev" not in body


# ---------------------------------------------------------------------------
# Existing document (GET returns _rev → _rev included in PUT body)
# ---------------------------------------------------------------------------

def test_put_capture_document_existing_doc_includes_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET returns existing doc with _rev; PUT body must include _rev."""
    put_reqs: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            return FakeResponse({"_id": "vm1", "_rev": "5-xyz", "type": "voice_memo"})
        put_reqs.append(req)
        return FakeResponse({"ok": True, "id": "vm1", "rev": "6-new"}, status=201)

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)
    put_capture_document(_make_config(), {"_id": "vm1", "type": "voice_memo"})

    assert len(put_reqs) == 1
    body = json.loads(getattr(put_reqs[0], "data").decode("utf-8"))
    assert body["_rev"] == "5-xyz"


# ---------------------------------------------------------------------------
# 409 on first attempt → retry succeeds
# ---------------------------------------------------------------------------

def test_put_capture_document_retries_once_on_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """First PUT returns 409; writer fetches fresh _rev and retries; second PUT succeeds."""
    call_count = {"get": 0, "put": 0}

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            call_count["get"] += 1
            return FakeResponse({"_id": "vm1", "_rev": f"{call_count['get']}-rev"})
        call_count["put"] += 1
        if call_count["put"] == 1:
            raise HTTPError(getattr(req, "full_url", ""), 409, "Conflict", hdrs=None, fp=None)
        return FakeResponse({"ok": True, "id": "vm1", "rev": "2-new"}, status=201)

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)
    result = put_capture_document(_make_config(), {"_id": "vm1"})

    assert result["ok"] is True
    assert call_count["get"] == 2
    assert call_count["put"] == 2


# ---------------------------------------------------------------------------
# 409 on both attempts → raises
# ---------------------------------------------------------------------------

def test_put_capture_document_raises_after_two_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both PUT attempts return 409; writer must raise VoiceMemoCouchDBError."""
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            return FakeResponse({"_id": "vm1", "_rev": "1-rev"})
        raise HTTPError(getattr(req, "full_url", ""), 409, "Conflict", hdrs=None, fp=None)

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)
    with pytest.raises(VoiceMemoCouchDBError, match="409"):
        put_capture_document(_make_config(), {"_id": "vm1"})
