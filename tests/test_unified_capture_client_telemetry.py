from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import field_capture.auth as auth_module
from event_pipeline import couchdb_config
from field_capture.auth import TokenStore
from unified_capture.server import UnifiedCaptureHandler


class FakeEmployeeStore:
    def get_optional(self, doc_id: str) -> dict[str, object] | None:
        if doc_id != "employee_jordan-avery":
            return None
        return {
            "_id": "employee_jordan-avery",
            "type": "employee",
            "person_id": "jordan-avery",
            "name": "Jordan Avery",
            "status": "active",
            "site_ids": ["7050"],
        }

    def find_employee_docs_by_person_id(self, person_id: str) -> list[dict[str, object]]:
        doc = self.get_optional(f"employee_{person_id}")
        return [doc] if doc else []


class FakeSiteRegistry:
    def resolve_canonical(self, site_id: str) -> str | None:
        return "Summit Wire" if str(site_id) == "7050" else None

    def list_sites(self) -> list[dict[str, str]]:
        return [{"site_id": "7050", "canonical": "Summit Wire"}]

    def get_display_categories(self, site_id: str) -> list[dict[str, str]]:
        assert str(site_id) == "7050"
        return [{"label": "Restrooms", "canonical": "Restrooms"}]


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-unified-client-test"
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


def valid_fields(capture_id: str) -> dict[str, str]:
    return {
        "job_id": "2026-06-27T10-00-00-04-00__photo-capture-summit-wire",
        "capture_id": capture_id,
        "site": "Summit Wire",
        "site_id": "7050",
        "qc_category": "Restrooms",
        "note": "Sink checked.",
        "captured_at": "2026-06-27T10:00:00-04:00",
        "exported_at": "2026-06-27T10:01:00-04:00",
    }


def build_server(tmp_path: Path, monkeypatch) -> tuple[SimpleNamespace, str]:
    monkeypatch.setattr(auth_module.CouchDBEntityStore, "from_env", classmethod(lambda cls: FakeEmployeeStore()))
    monkeypatch.setattr(auth_module, "CouchDBSiteRegistry", lambda: FakeSiteRegistry())
    token_store = TokenStore(tmp_path / "tokens.sqlite3")
    token_store.initialize()
    token = token_store.create_token("jordan-avery", label="Jordan phone", site_ids=("7050",))
    server = SimpleNamespace(
        token_store=token_store,
        upload_dir=tmp_path / "uploads",
        max_images=6,
        max_upload_bytes=1024 * 1024,
        request_max_bytes=10 * 1024 * 1024,
        couchdb_config=couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000),
        couchdb_database="btq_field_captures",
        site_registry=FakeSiteRegistry(),
        enqueue_capture_media_mirror=lambda _candidates: None,
    )
    return server, token.token_value


def submit_request(
    server: SimpleNamespace,
    token: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    handler = object.__new__(UnifiedCaptureHandler)
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
    for key, value in (extra_headers or {}).items():
        headers[key] = value
    handler.headers = headers
    handler.do_POST()
    raw = handler.wfile.getvalue()
    header_blob, _, response_body = raw.partition(b"\r\n\r\n")
    status = int(header_blob.decode("iso-8859-1").split("\r\n", 1)[0].split()[1])
    return status, response_body


def test_submit_stores_client_block_and_missing_user_agent_is_omitted(tmp_path: Path, monkeypatch) -> None:
    server, token = build_server(tmp_path, monkeypatch)
    put_docs: list[dict[str, object]] = []

    monkeypatch.setattr("unified_capture.server.get_field_capture_document", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "unified_capture.server.put_field_capture_document",
        lambda _config, doc, *, database: put_docs.append(doc) or {"ok": True, "id": doc["_id"], "rev": "1-test"},
    )

    body, content_type = multipart_body(valid_fields("cap-client-ua"), [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])
    chrome_android = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/125.0.0.0 Mobile Safari/537.36"

    status, response_body = submit_request(server, token, body, content_type, {"User-Agent": chrome_android})

    assert status == 201
    assert json.loads(response_body)["capture_id"] == "cap-client-ua"
    assert put_docs[-1]["client"]["user_agent"] == chrome_android
    assert put_docs[-1]["client"]["kind"] == "pwa"
    assert put_docs[-1]["client"]["platform"] == "android"

    body, content_type = multipart_body(valid_fields("cap-client-explicit"), [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])
    status, _response_body = submit_request(
        server,
        token,
        body,
        content_type,
        {
            "User-Agent": chrome_android,
            "X-BTQ-Client": "BTQCapture/1.2.0",
            "X-BTQ-Platform": "ios",
            "X-BTQ-OS-Version": "17.5",
            "X-BTQ-App-Version": "1.2.3",
            "X-BTQ-Device-Model": "iPhone15,3",
        },
    )

    assert status == 201
    assert put_docs[-1]["client"]["kind"] == "native"
    assert put_docs[-1]["client"]["platform"] == "ios"
    assert put_docs[-1]["client"]["os_version"] == "17.5"
    assert put_docs[-1]["client"]["app_version"] == "1.2.3"
    assert put_docs[-1]["client"]["device_model"] == "iPhone15,3"

    body, content_type = multipart_body(valid_fields("cap-client-missing"), [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])
    status, _response_body = submit_request(server, token, body, content_type)

    assert status == 201
    assert "client" not in put_docs[-1]
