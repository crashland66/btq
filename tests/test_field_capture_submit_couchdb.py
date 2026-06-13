from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib import error

from event_pipeline import couchdb_config
from field_capture.auth import TokenStore
from field_capture.server import FieldCaptureHandler, run_server
from tests.test_field_capture_auth import employee_doc, patch_canonical


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 201) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def write_vault_note(path: Path, frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n", encoding="utf-8")


def write_vault_site(vault_root: Path) -> None:
    write_vault_note(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md",
        "type: location\n"
        "site_id: 7050\n"
        "account: Summitsteel\n"
        "location: Summit Wire\n",
    )


def write_vault_person(vault_root: Path, person_id: str = "jordan-avery", name: str = "Jordan Avery", filename: str = "jordan-avery.md") -> None:
    write_vault_note(
        vault_root / "People" / filename,
        "type: employee\n"
        f"name: {name}\n"
        f"person_id: {person_id}\n"
        "job: 7050\n",
    )


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-field-capture-couchdb-test"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, filename, content_type, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"))
        chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def valid_fields() -> dict[str, str]:
    return {
        "job_id": "2026-05-02T00-00-00-04-00__photo-capture-summit-wire",
        "capture_id": "cap-photo-couchdb",
        "site": "Summit Wire",
        "site_id": "7050",
        "qc_category": "Restrooms",
        "note": "Sink checked.",
        "captured_at": "2026-05-02T00:00:00-04:00",
        "exported_at": "2026-05-02T00:01:00-04:00",
        "audio_duration_seconds": "9",
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


def submit_request(server: SimpleNamespace, token: str, body: bytes, content_type: str) -> tuple[int, dict[str, str], bytes]:
    handler = object.__new__(FieldCaptureHandler)
    handler.server = server
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    handler.requestline = "POST /api/submit HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "POST"
    handler.path = "/api/submit"
    handler.close_connection = True
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    handler.headers = headers
    handler.do_POST()
    return parse_handler_response(handler.wfile.getvalue())


def build_test_server(tmp_path: Path, monkeypatch) -> tuple[SimpleNamespace, str, Path, Path]:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_vault_site(vault_root)
    write_vault_person(vault_root)
    write_vault_person(vault_root, person_id="damon-smith", name="Damon Smith", filename="damon-smith.md")
    patch_canonical(
        monkeypatch,
        docs={
            "employee_jordan-avery": employee_doc(),
            "employee_damon-smith": employee_doc(person_id="damon-smith", name="Damon Smith"),
        },
        sites={"7050": "Summit Wire"},
    )
    token_store = TokenStore(runtime_root / "tokens.sqlite3")
    token_store.initialize()
    token = token_store.create_token("jordan-avery", label="Jordan iPhone", site_ids=("7050",))
    server = SimpleNamespace(
        token_store=token_store,
        vault_root=vault_root,
        upload_dir=upload_dir,
        queue_dir=queue_dir,
        max_images=6,
        max_upload_bytes=1024 * 1024,
        request_max_bytes=10 * 1024 * 1024,
        couchdb_config=couchdb_config.CouchDBConfig("http://couchdb.test", "jordan", "secret", 10.0, 10000),
        couchdb_database="btq_field_captures",
    )
    return server, token.token_value, queue_dir, upload_dir


def create_import_token(server: SimpleNamespace) -> str:
    token = server.token_store.create_token(
        "jordan-avery",
        label="Dashboard import",
        role="site_admin",
        token_type="import",
        site_ids=("*",),
    )
    return token.token_value


def test_handle_submit_writes_media_and_puts_couchdb_document_when_configured(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, token, queue_dir, upload_dir = build_test_server(tmp_path, monkeypatch)
    put_calls: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            # Simulate a new document (no existing rev)
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_calls.append(req)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "1-a"})

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    body, content_type = multipart_body(
        valid_fields(),
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo"),
            ("audio", "voice.webm", "audio/webm", b"audio-bytes"),
        ],
    )

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    stored_photo = upload_dir / "2026-05-02" / "cap-photo-couchdb" / "sink.jpg"
    stored_audio = upload_dir / "2026-05-02" / "cap-photo-couchdb" / "voice.webm"
    assert status == 201
    assert stored_photo.read_bytes() == b"\xff\xd8photo"
    assert stored_audio.read_bytes() == b"audio-bytes"
    assert response["couchdb_doc_id"] == "cap-photo-couchdb"
    assert response["capture_id"] == "cap-photo-couchdb"
    assert not list(queue_dir.glob("*.json"))
    req = put_calls[0]
    doc = json.loads(getattr(req, "data").decode("utf-8"))
    assert getattr(req, "full_url") == "http://couchdb.test/btq_field_captures/cap-photo-couchdb"
    assert doc["_id"] == "cap-photo-couchdb"
    assert doc["type"] == "field_capture"
    assert doc["processing_state"] == "pending"
    assert doc["site_id"] == "7050"
    assert doc["person_id"] == "jordan-avery"
    assert doc["person_name"] == "Jordan Avery"
    assert doc["qc_category"] == "Restrooms"
    assert doc["note"] == "Sink checked."
    assert doc["photos"][0]["stored_path"] == str(stored_photo)
    assert doc["audio"][0]["stored_path"] == str(stored_audio)
    assert doc["audio"][0]["duration_seconds"] == "9"


def test_handle_submit_import_token_can_override_person_and_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, _normal_token, _queue_dir, _upload_dir = build_test_server(tmp_path, monkeypatch)
    token = create_import_token(server)
    put_calls: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_calls.append(req)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "1-a"})

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    fields = valid_fields() | {"person_id": "damon-smith", "source": "whatsapp_import"}
    body, content_type = multipart_body(
        fields,
        [
            ("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo-1"),
            ("photos", "floor.png", "image/png", b"\x89PNGphoto-2"),
        ],
    )

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    doc = json.loads(getattr(put_calls[0], "data").decode("utf-8"))
    assert status == 201
    assert response["photo_count"] == 2
    assert response["capture_id"] == "cap-photo-couchdb"
    assert doc["_id"] == "cap-photo-couchdb"
    assert doc["person_id"] == "damon-smith"
    assert doc["person_name"] == "Damon Smith"
    assert doc["source"] == "whatsapp_import"
    assert doc["processing_state"] == "pending"
    assert len(doc["photos"]) == 2


def test_handle_submit_import_token_accepts_batch_above_app_max_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # The dashboard batch importer (import token) must accept a WhatsApp-fallback
    # dump larger than the app's small per-capture ceiling (server max_images=6).
    server, _normal_token, _queue_dir, _upload_dir = build_test_server(tmp_path, monkeypatch)
    token = create_import_token(server)
    put_calls: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_calls.append(req)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "1-a"})

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    fields = valid_fields() | {"person_id": "damon-smith", "source": "whatsapp_import"}
    photos = [("photos", f"photo-{i}.jpg", "image/jpeg", b"\xff\xd8photo-%d" % i) for i in range(20)]
    body, content_type = multipart_body(fields, photos)

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 201
    assert response["photo_count"] == 20
    doc = json.loads(getattr(put_calls[0], "data").decode("utf-8"))
    assert len(doc["photos"]) == 20


def test_handle_submit_normal_token_still_capped_at_app_max_images(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Regression guard: the raised ceiling is scoped to the import token; a normal
    # capture token must still be rejected above the app's max_images.
    server, token, _queue_dir, _upload_dir = build_test_server(tmp_path, monkeypatch)

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        from urllib import error as _error
        raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    photos = [("photos", f"photo-{i}.jpg", "image/jpeg", b"\xff\xd8photo-%d" % i) for i in range(7)]
    body, content_type = multipart_body(valid_fields(), photos)

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 400
    assert response["error"] == "too_many_photos"


def test_handle_submit_normal_token_cannot_override_person_or_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, token, _queue_dir, _upload_dir = build_test_server(tmp_path, monkeypatch)
    put_calls: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        put_calls.append(req)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "1-a"})

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    fields = valid_fields() | {"person_id": "damon-smith", "source": "whatsapp_import"}
    body, content_type = multipart_body(fields, [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])

    status, _headers, _response_body = submit_request(server, token, body, content_type)

    doc = json.loads(getattr(put_calls[0], "data").decode("utf-8"))
    assert status == 201
    assert doc["person_id"] == "jordan-avery"
    assert doc["person_name"] == "Jordan Avery"
    assert doc["source"] == "field_capture_app"


def test_handle_submit_returns_503_when_couchdb_put_fails(tmp_path: Path, monkeypatch) -> None:
    server, token, queue_dir, upload_dir = build_test_server(tmp_path, monkeypatch)

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        raise error.URLError("connection refused")

    monkeypatch.setattr("event_pipeline.couchdb_capture_writer.request.urlopen", fake_urlopen)
    body, content_type = multipart_body(valid_fields(), [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    stored_photo = upload_dir / "2026-05-02" / "cap-photo-couchdb" / "sink.jpg"
    assert status == 503
    assert response == {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"}
    assert stored_photo.read_bytes() == b"\xff\xd8photo"
    assert not list(queue_dir.glob("*.json"))


def test_run_server_exits_when_btq_couchdb_url_not_set(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    result = run_server(
        "127.0.0.1",
        0,
        tmp_path / "tokens.sqlite3",
        tmp_path / "uploads",
        6,
        1024 * 1024,
        10 * 1024 * 1024,
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "BTQ_COUCHDB_URL must be set" in captured.err
    assert "queue-file fallback was removed in prompt 14" in captured.err
    assert not (tmp_path / "tokens.sqlite3").exists()


def test_handle_submit_returns_idempotent_replay_when_capture_already_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, token, queue_dir, upload_dir = build_test_server(tmp_path, monkeypatch)
    put_calls: list[object] = []

    existing_doc = {
        "_id": "cap-photo-couchdb",
        "_rev": "1-abc",
        "type": "field_capture",
        "capture_id": "cap-photo-couchdb",
        "photos": [{"filename": "sink.jpg"}],
        "audio": [{"filename": "voice.webm"}],
    }

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            return FakeResponse(existing_doc)
        put_calls.append(req)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "2-x"})

    monkeypatch.setattr(
        "event_pipeline.couchdb_capture_writer.request.urlopen",
        fake_urlopen,
    )
    body, content_type = multipart_body(
        valid_fields(),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 200
    assert response["status"] == "submitted"
    assert response["capture_id"] == "cap-photo-couchdb"
    assert response["couchdb_doc_id"] == "cap-photo-couchdb"
    assert response["photo_count"] == 1
    assert response["audio_count"] == 1
    assert response["idempotent_replay"] is True
    # Critical: replay must NOT call PUT or write media to disk.
    assert put_calls == []
    stored_photo = upload_dir / "2026-05-02" / "cap-photo-couchdb" / "sink.jpg"
    assert not stored_photo.exists()
    assert not list(queue_dir.glob("*.json"))


def test_handle_submit_first_post_returns_201_created_no_replay_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, token, _queue, _uploads = build_test_server(tmp_path, monkeypatch)

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        method = getattr(req, "method", "GET")
        if method == "GET":
            from urllib import error as _error
            raise _error.HTTPError(getattr(req, "full_url", ""), 404, "Not Found", hdrs=None, fp=None)
        return FakeResponse({"ok": True, "id": "cap-photo-couchdb", "rev": "1-a"})

    monkeypatch.setattr(
        "event_pipeline.couchdb_capture_writer.request.urlopen",
        fake_urlopen,
    )
    body, content_type = multipart_body(
        valid_fields(),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 201
    assert "idempotent_replay" not in response


def test_handle_submit_returns_503_when_idempotency_lookup_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server, token, _queue, _uploads = build_test_server(tmp_path, monkeypatch)

    def fake_urlopen(req: object, timeout: float) -> FakeResponse:
        # First call is the idempotency GET; raise transport error.
        raise error.URLError("connection refused")

    monkeypatch.setattr(
        "event_pipeline.couchdb_capture_writer.request.urlopen",
        fake_urlopen,
    )
    body, content_type = multipart_body(
        valid_fields(),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

    status, _headers, response_body = submit_request(server, token, body, content_type)

    response = json.loads(response_body)
    assert status == 503
    assert response == {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"}
