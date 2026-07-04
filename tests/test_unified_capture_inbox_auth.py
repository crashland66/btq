from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import field_capture.auth as auth_module
import unified_capture.server as server_module
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


class FakeSiteRegistry:
    def resolve_canonical(self, site_id: str) -> str | None:
        return "Summit Wire" if str(site_id) == "7050" else None

    def list_sites(self) -> list[dict[str, str]]:
        return [{"site_id": "7050", "canonical": "Summit Wire"}]

    def get_display_categories(self, site_id: str) -> list[dict[str, str]]:
        assert str(site_id) == "7050"
        return [{"label": "Restrooms", "canonical": "Restrooms"}]


def parse_handler_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.lower()] = value.strip()
    return status, headers, body


def request(
    server: SimpleNamespace,
    method: str,
    path: str,
    *,
    token: str,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    handler = object.__new__(UnifiedCaptureHandler)
    handler.server = server
    handler.rfile = io.BytesIO(body or b"")
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.close_connection = True
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    handler.headers = headers
    if method == "GET":
        handler.do_GET()
    elif method == "POST":
        handler.do_POST()
    else:
        raise AssertionError(f"unsupported test method: {method}")
    return parse_handler_response(handler.wfile.getvalue())


def build_server(tmp_path: Path, monkeypatch) -> tuple[SimpleNamespace, TokenStore]:
    monkeypatch.setattr(auth_module.CouchDBEntityStore, "from_env", classmethod(lambda cls: FakeEmployeeStore()))
    monkeypatch.setattr(auth_module, "CouchDBSiteRegistry", lambda: FakeSiteRegistry())
    monkeypatch.setattr(server_module, "load_system_defaults", lambda: {})
    monkeypatch.setattr(server_module, "list_job_drafts", lambda *_args, **_kwargs: [draft_doc("draft-1"), draft_doc("draft-2")])
    token_store = TokenStore(tmp_path / "tokens.sqlite3")
    token_store.initialize()
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
    return server, token_store


def draft_doc(draft_id: str) -> dict[str, object]:
    return {
        "_id": draft_id,
        "_rev": "1-test",
        "draft_id": draft_id,
        "job_type": "photo_capture",
        "site_id": "7050",
        "message": "Review field capture",
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Restrooms",
            "note": "Sink checked.",
            "photos": [{"filename": "sink.jpg", "mime_type": "image/jpeg", "stored_path": "/tmp/sink.jpg"}],
            "captured_at": "2026-07-04T09:00:00-04:00",
            "exported_at": "2026-07-04T09:01:00-04:00",
        },
    }


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-unified-capture-read-only-test"
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


def valid_submit_fields() -> dict[str, str]:
    return {
        "job_id": "2026-07-04T09-00-00-04-00__photo-capture-summit-wire",
        "capture_id": "cap-read-only",
        "site": "Summit Wire",
        "site_id": "7050",
        "qc_category": "Restrooms",
        "note": "Sink checked.",
        "captured_at": "2026-07-04T09:00:00-04:00",
        "exported_at": "2026-07-04T09:01:00-04:00",
    }


def test_site_admin_capture_token_can_review_inbox_and_get_count(tmp_path: Path, monkeypatch) -> None:
    server, token_store = build_server(tmp_path, monkeypatch)
    token = token_store.create_token("jordan-avery", role="site_admin", token_type="capture", site_ids=["7050"])

    session_status, _session_headers, session_body = request(server, "GET", "/api/session", token=token.token_value)
    inbox_status, _inbox_headers, inbox_body = request(server, "GET", "/api/inbox", token=token.token_value)
    approve_status, _approve_headers, approve_body = request(
        server,
        "POST",
        "/api/inbox/approve",
        token=token.token_value,
        body=b"{}",
    )

    assert session_status == 200
    session_payload = json.loads(session_body)
    assert session_payload["can_review"] is True
    assert session_payload["inbox_count"] == 2
    assert session_payload["token"]["role"] == "site_admin"
    assert session_payload["token"]["token_type"] == "capture"
    assert inbox_status == 200
    inbox_payload = json.loads(inbox_body)
    assert inbox_payload["count"] == 2
    assert [item["draft_id"] for item in inbox_payload["items"]] == ["draft-1", "draft-2"]
    assert approve_status == 400
    assert json.loads(approve_body) == {"error": "invalid_payload"}


def test_cleaner_token_type_admin_viewer_cannot_review_inbox(tmp_path: Path, monkeypatch) -> None:
    server, token_store = build_server(tmp_path, monkeypatch)
    token = token_store.create_token("jordan-avery", role="cleaner", token_type="admin_viewer", site_ids=["7050"])

    session_status, _session_headers, session_body = request(server, "GET", "/api/session", token=token.token_value)
    assert session_status == 200
    session_payload = json.loads(session_body)
    assert session_payload["can_review"] is False
    assert session_payload["inbox_count"] == 0

    for method, path in (
        ("GET", "/api/inbox"),
        ("POST", "/api/inbox/approve"),
        ("POST", "/api/inbox/reject"),
        ("POST", "/api/inbox/approve-set"),
    ):
        body = b"{}" if method == "POST" else None
        status, _headers, response_body = request(server, method, path, token=token.token_value, body=body)
        assert status == 403
        assert json.loads(response_body) == {"error": "not_authorized"}


def test_read_only_token_cannot_review_inbox(tmp_path: Path, monkeypatch) -> None:
    server, token_store = build_server(tmp_path, monkeypatch)
    token = token_store.create_token("jordan-avery", role="read_only", token_type="admin_viewer", site_ids=["7050"])

    session_status, _session_headers, session_body = request(server, "GET", "/api/session", token=token.token_value)
    assert session_status == 200
    session_payload = json.loads(session_body)
    assert session_payload["can_review"] is False
    assert session_payload["can_submit"] is False
    assert session_payload["inbox_count"] == 0
    assert session_payload["token"]["role"] == "read_only"

    status, _headers, response_body = request(server, "GET", "/api/inbox", token=token.token_value)

    assert status == 403
    assert json.loads(response_body) == {"error": "not_authorized"}


def test_unified_submit_rejects_read_only_token_even_when_can_submit_true(tmp_path: Path, monkeypatch) -> None:
    server, token_store = build_server(tmp_path, monkeypatch)
    token = token_store.create_token("jordan-avery", role="read_only", can_submit=True, site_ids=["7050"])
    body, content_type = multipart_body(
        valid_submit_fields(),
        [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")],
    )

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
    headers["Authorization"] = f"Bearer {token.token_value}"
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    handler.headers = headers

    handler.do_POST()

    status, _headers, response_body = parse_handler_response(handler.wfile.getvalue())
    assert status == 403
    assert json.loads(response_body)["error"] == "submit_not_allowed"
