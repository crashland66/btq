from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib import error

import pytest

from token_store import TokenStore
from voice_memo.couchdb import VoiceMemoCouchDBConfig, VoiceMemoCouchDBError, get_voice_memo_document
from voice_memo.server import VoiceMemoHandler


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


def multipart_body(fields: dict[str, str], audio: bytes = b"voice-bytes") -> tuple[bytes, str]:
    boundary = "----voice-memo-upload-test"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(b'Content-Disposition: form-data; name="audio"; filename="memo.webm"\r\n')
    chunks.append(b"Content-Type: audio/webm\r\n\r\n")
    chunks.append(audio)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def valid_fields() -> dict[str, str]:
    return {
        "capture_id": "cap-voice-2026-05-29T09-15-00-04-00",
        "captured_at": "2026-05-29T09:15:00-04:00",
        "duration_seconds": "12",
        "mode": "personal",
        "note": "follow up with the site lead",
    }


def parse_handler_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()
    return status, headers, body


def build_server(tmp_path: Path, *, couchdb_get=None, couchdb_put=None) -> tuple[SimpleNamespace, str]:
    token_store = TokenStore(tmp_path / "tokens.sqlite3")
    token_store.initialize()
    token = token_store.create_token("per_voice01", label="Voice Memo").token_value
    server = SimpleNamespace(
        data_dir=tmp_path / "voice",
        token_store=token_store,
        couchdb_get=couchdb_get,
        couchdb_put=couchdb_put,
        couchdb_find=lambda _database, _selector: {"docs": []},
    )
    return server, token


def upload_request(server: SimpleNamespace, token: str, body: bytes, content_type: str) -> tuple[int, dict[str, str], bytes]:
    handler = object.__new__(VoiceMemoHandler)
    handler.server = server
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    handler.requestline = "POST /api/upload HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/api/upload"
    handler.close_connection = True
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    handler.headers = headers
    handler.do_POST()
    return parse_handler_response(handler.wfile.getvalue())


def test_handle_upload_returns_idempotent_replay_when_capture_exists(tmp_path: Path) -> None:
    put_calls: list[dict] = []
    existing_doc = {
        "_id": "cap-voice-2026-05-29T09-15-00-04-00",
        "capture_id": "cap-voice-2026-05-29T09-15-00-04-00",
        "duration_seconds": 12,
    }
    server, token = build_server(
        tmp_path,
        couchdb_get=lambda _doc_id: existing_doc,
        couchdb_put=lambda doc: put_calls.append(doc) or {"ok": True},
    )
    body, content_type = multipart_body(valid_fields())

    status, _headers, response_body = upload_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 200
    assert response["status"] == "saved"
    assert response["capture_id"] == "cap-voice-2026-05-29T09-15-00-04-00"
    assert response["duration_seconds"] == 12
    assert response["idempotent_replay"] is True
    assert put_calls == []
    assert not list((tmp_path / "voice").rglob("*.webm"))


def test_handle_upload_first_post_returns_201_created_no_replay_flag(tmp_path: Path) -> None:
    put_docs: list[dict] = []
    server, token = build_server(
        tmp_path,
        couchdb_get=lambda _doc_id: None,
        couchdb_put=lambda doc: put_docs.append(doc) or {"ok": True},
    )
    body, content_type = multipart_body(valid_fields())

    status, _headers, response_body = upload_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 201
    assert response["status"] == "saved"
    assert response["capture_id"] == "cap-voice-2026-05-29T09-15-00-04-00"
    assert "idempotent_replay" not in response
    assert put_docs[0]["_id"] == "cap-voice-2026-05-29T09-15-00-04-00"
    assert put_docs[0]["capture_id"] == "cap-voice-2026-05-29T09-15-00-04-00"
    assert (tmp_path / "voice" / "2026" / "05" / "cap-voice-2026-05-29T09-15-00-04-00.webm").exists()


def test_handle_upload_returns_503_when_idempotency_lookup_fails(tmp_path: Path) -> None:
    def fail_get(_doc_id: str) -> dict | None:
        raise VoiceMemoCouchDBError("down")

    put_calls: list[dict] = []
    server, token = build_server(
        tmp_path,
        couchdb_get=fail_get,
        couchdb_put=lambda doc: put_calls.append(doc) or {"ok": True},
    )
    body, content_type = multipart_body(valid_fields())

    status, _headers, response_body = upload_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 503
    assert response == {"error": "couchdb_unavailable", "message": "Voice memo storage is unavailable."}
    assert put_calls == []
    assert not list((tmp_path / "voice").rglob("*.webm"))


def test_get_voice_memo_document_returns_existing_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        assert getattr(req, "full_url") == "http://couchdb.test/btq_voice_memos/cap-voice-1"
        return FakeResponse({"_id": "cap-voice-1", "duration_seconds": 9})

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)

    result = get_voice_memo_document(
        VoiceMemoCouchDBConfig("http://couchdb.test", "voiceuser", "secret", 10.0, "btq_voice_memos"),
        "cap-voice-1",
    )

    assert result == {"_id": "cap-voice-1", "duration_seconds": 9}


def test_get_voice_memo_document_returns_none_for_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        raise error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)

    result = get_voice_memo_document(
        VoiceMemoCouchDBConfig("http://couchdb.test", "", "", 10.0, "btq_voice_memos"),
        "cap-voice-missing",
    )

    assert result is None


def test_get_voice_memo_document_raises_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        raise error.URLError("connection refused")

    monkeypatch.setattr("voice_memo.couchdb.urlopen", fake_urlopen)

    with pytest.raises(VoiceMemoCouchDBError, match="cap-voice-1"):
        get_voice_memo_document(
            VoiceMemoCouchDBConfig("http://couchdb.test", "", "", 10.0, "btq_voice_memos"),
            "cap-voice-1",
        )

