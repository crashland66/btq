from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_reader import query_captures_by_target
from field_capture.auth import TokenStore
from field_capture.server import FieldCaptureHandler
from field_capture.site_viewer import build_prospect_payload
from tests.test_field_capture_auth import employee_doc, patch_canonical


class RunningFieldCaptureServer:
    def __init__(self, server: SimpleNamespace) -> None:
        self.server = server

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        handler = object.__new__(FieldCaptureHandler)
        handler.server = self.server
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 0)
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = method
        handler.path = path
        handler.close_connection = True
        request_headers = Message()
        for key, value in (headers or {}).items():
            request_headers[key] = value
        handler.headers = request_headers
        if method == "GET":
            handler.do_GET()
        else:
            raise AssertionError(f"unsupported test method: {method}")
        return parse_handler_response(handler.wfile.getvalue())


def parse_handler_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        lower_key = key.lower()
        stripped = value.strip()
        headers[lower_key] = f"{headers[lower_key]}, {stripped}" if lower_key in headers else stripped
    return status, headers, body


def write_vault_note(path: Path, frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n", encoding="utf-8")


def write_vault_site(vault_root: Path, site_id: str, name: str) -> None:
    write_vault_note(
        vault_root / "Accounts" / "Example" / "Locations" / f"{site_id} - {name}" / "about.md",
        "type: location\n"
        f"site_id: {site_id}\n"
        "account: Example\n"
        f"location: {name}\n",
    )


def write_vault_person(vault_root: Path, *, person_id: str, name: str, job: str) -> None:
    write_vault_note(
        vault_root / "People" / f"{person_id}.md",
        "type: employee\n"
        f"name: {name}\n"
        f"person_id: {person_id}\n"
        f"job: {job}\n",
    )


def start_field_capture_server(tmp_path: Path) -> tuple[RunningFieldCaptureServer, TokenStore, Path, Path]:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    upload_dir = runtime_root / "uploads"
    token_store = TokenStore(runtime_root / "tokens.sqlite3")
    token_store.initialize()
    server = SimpleNamespace(
        token_store=token_store,
        vault_root=vault_root,
        upload_dir=upload_dir,
        max_images=6,
        max_upload_bytes=1024 * 1024,
        request_max_bytes=10 * 1024 * 1024,
        couchdb_config=couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000),
        couchdb_database="btq_field_captures",
        capture_reader=lambda _config, _site_id, *, database: [],
        target_lookup_reader=lambda _config, _upload_id, *, database: {
            "target_type": "prospect",
            "target_id": "kmf-birch-1",
            "site_id": "",
        },
        site_lookup_reader=lambda _config, _upload_id, *, database: None,
        prospect_capture_reader=lambda _config, _target_type, _target_id, *, database: [],
    )
    return RunningFieldCaptureServer(server), token_store, vault_root, upload_dir


def prospect_capture_doc(upload_dir: Path, prospect_id: str, capture_id: str = "cap-prospect") -> dict[str, object]:
    image_path = upload_dir / "2026-05-27" / capture_id / f"{capture_id}-1.jpg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"\xff\xd8image")
    return {
        "_id": capture_id,
        "type": "field_capture",
        "capture_id": capture_id,
        "site_id": "",
        "target_type": "prospect",
        "target_id": prospect_id,
        "site": "KMF Birch Ave",
        "person_id": "jordan-avery",
        "person_name": "Avery, Jordan",
        "qc_category": "baseline",
        "phase": "pre_engagement",
        "note": "Front entrance baseline.",
        "captured_at": "2026-05-27T10:00:00-04:00",
        "exported_at": "2026-05-27T10:01:00-04:00",
        "photos": [
            {
                "filename": image_path.name,
                "mime_type": "image/jpeg",
                "stored_path": str(image_path),
                "upload_id": f"2026-05-27/{capture_id}/{image_path.name}",
            }
        ],
        "audio": [],
    }


def test_query_captures_by_target_filters_by_type_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, str, bytes]] = []

    def fake_post(config: object, url: str, body: bytes) -> dict[str, object]:
        calls.append((config, url, body))
        return {
            "docs": [
                {"capture_id": "match", "target_type": "prospect", "target_id": "kmf-birch-1"},
                {"capture_id": "wrong-type", "target_type": "location", "target_id": "kmf-birch-1"},
                {"capture_id": "wrong-id", "target_type": "prospect", "target_id": "other"},
                "not-a-doc",
            ]
        }

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader._request_json_post", fake_post)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "jordan", "secret", 12.0, 10000)

    rows = query_captures_by_target(config, "prospect", "kmf-birch-1", database="btq_field_captures")

    assert rows == [{"capture_id": "match", "target_type": "prospect", "target_id": "kmf-birch-1"}]
    assert calls[0][1] == "http://couchdb.test/btq_field_captures/_find"
    body = json.loads(calls[0][2].decode("utf-8"))
    assert body["selector"]["target_type"] == "prospect"
    assert body["selector"]["target_id"] == "kmf-birch-1"
    # Mango sort was dropped (needs an index); Python-sorted after fetch.
    assert "sort" not in body


def test_query_captures_by_target_returns_empty_on_missing_args(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(_config: object, _url: str, _body: bytes) -> dict[str, object]:
        raise AssertionError("empty target args must not call CouchDB")

    monkeypatch.setattr("event_pipeline.couchdb_capture_reader._request_json_post", fail_post)
    config = couchdb_config.CouchDBConfig("http://couchdb.test", "", "", 10.0, 10000)

    assert query_captures_by_target(config, "", "kmf-birch-1", database="btq_field_captures") == []
    assert query_captures_by_target(config, "prospect", "", database="btq_field_captures") == []


def test_build_prospect_payload_includes_captures_and_metadata(tmp_path: Path) -> None:
    upload_dir = tmp_path / "uploads"
    prospect = {
        "prospect_id": "kmf-birch-1",
        "name": "KMF Birch Ave",
        "address": "100 Birch Ave",
        "account": "KMF",
        "status": "active",
    }
    captures = [
        prospect_capture_doc(upload_dir, "kmf-birch-1"),
        prospect_capture_doc(upload_dir, "other", "cap-other"),
    ]

    payload = build_prospect_payload(prospect, captures, upload_dir)

    assert payload["prospect_id"] == "kmf-birch-1"
    assert payload["prospect_name"] == "KMF Birch Ave"
    assert payload["prospect"] == prospect
    assert len(payload["dates"]) == 1
    upload = payload["dates"][0]["uploads"][0]
    assert upload["upload_id"] == "cap-prospect"
    assert upload["area"] == "baseline"
    assert upload["image_count"] == 1
    assert upload["person_name"] == "Avery, Jordan"
    assert upload["images"][0].endswith("?prospect_id=kmf-birch-1")


def test_handle_prospect_view_404_for_missing_prospect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _store, _vault_root, _upload_dir = start_field_capture_server(tmp_path)
    monkeypatch.setattr("field_capture.server.load_prospect", lambda _config, _prospect_id: None)

    status, _headers, body = app.request("GET", "/prospect/missing?format=json")

    assert status == 404
    assert json.loads(body) == {"error": "prospect_not_found"}


def test_handle_prospect_view_renders_html_for_existing_prospect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, upload_dir = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc(name="Avery, Jordan")},
        sites={"7050": "Summit Wire"},
    )
    token = store.create_token("jordan-avery", site_ids=["7050"], role="site_admin")
    prospect = {"prospect_id": "kmf-birch-1", "name": "KMF Birch Ave", "address": "", "account": "KMF", "status": "active"}
    monkeypatch.setattr("field_capture.server.load_prospect", lambda _config, _prospect_id: prospect)
    app.server.prospect_capture_reader = lambda _config, _target_type, _target_id, *, database: [
        prospect_capture_doc(upload_dir, "kmf-birch-1")
    ]

    status, headers, body = app.request("GET", f"/prospect/kmf-birch-1?token={token.token_value}")
    media_status, media_headers, media_body = app.request(
        "GET",
        f"/media/2026-05-27/cap-prospect/cap-prospect-1.jpg?prospect_id=kmf-birch-1&token={token.token_value}",
    )

    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert b"KMF Birch Ave Uploads" in body
    assert b"Front entrance baseline." in body
    assert b"/media/2026-05-27/cap-prospect/cap-prospect-1.jpg?prospect_id=kmf-birch-1" in body
    assert token.token_value.encode("utf-8") not in body
    assert media_status == 200
    assert media_headers["content-type"] == "image/jpeg"
    assert media_body == b"\xff\xd8image"


def test_session_payload_prospect_includes_viewer_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _upload_dir = start_field_capture_server(tmp_path)
    write_vault_site(vault_root, "7050", "Summit Wire")
    write_vault_person(vault_root, person_id="jordan-avery", name="Avery, Jordan", job="7050")
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc(name="Avery, Jordan")},
        sites={"7050": "Summit Wire"},
    )
    token = store.create_token("jordan-avery", site_ids=["7050"], role="site_admin")
    monkeypatch.setattr("field_capture.server.load_system_defaults", lambda: {})
    monkeypatch.setattr(
        "field_capture.server.list_active_prospects",
        lambda _config: [{"prospect_id": "kmf-birch-1", "name": "KMF Birch Ave", "address": "", "account": "KMF"}],
    )

    status, _headers, body = app.request("GET", f"/api/session?token={token.token_value}")

    assert status == 200
    session = json.loads(body)
    assert session["prospects"][0]["viewer_url"] == f"/prospect/kmf-birch-1?token={token.token_value}"
