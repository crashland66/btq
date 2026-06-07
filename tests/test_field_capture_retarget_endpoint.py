from __future__ import annotations

import io
import json
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import couchdb_config
from field_capture.auth import TokenStore
from field_capture.server import FieldCaptureHandler
from tests.test_field_capture_auth import employee_doc, patch_canonical


def write_vault_note(path: Path, frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n", encoding="utf-8")


def write_vault_site(vault_root: Path) -> None:
    write_vault_note(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md",
        "type: location\nsite_id: 7050\naccount: Summitsteel\nlocation: Summit Wire\n",
    )


def write_vault_person(vault_root: Path) -> None:
    write_vault_note(
        vault_root / "People" / "jordan-avery.md",
        "type: employee\nname: Jordan Avery\nperson_id: jordan-avery\njob: 7050\n",
    )


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


def build_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, role: str = "site_admin") -> tuple[SimpleNamespace, str, Path]:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    upload_dir = runtime_root / "uploads"
    write_vault_site(vault_root)
    write_vault_person(vault_root)
    patch_canonical(monkeypatch, docs={"employee_jordan-avery": employee_doc()}, sites={"7050": "Summit Wire"})
    token_store = TokenStore(runtime_root / "tokens.sqlite3")
    token_store.initialize()
    token = token_store.create_token("jordan-avery", label="Jordan iPhone", site_ids=("7050",), role=role)
    server = SimpleNamespace(
        token_store=token_store,
        vault_root=vault_root,
        upload_dir=upload_dir,
        max_images=6,
        max_upload_bytes=1024 * 1024,
        request_max_bytes=10 * 1024 * 1024,
        couchdb_config=couchdb_config.CouchDBConfig("http://couchdb.test", "jordan", "secret", 10.0, 10000),
        couchdb_database="btq_field_captures",
    )
    return server, token.token_value, runtime_root


def request(server: SimpleNamespace, token: str, method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
    handler = object.__new__(FieldCaptureHandler)
    handler.server = server
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.client_address = ("127.0.0.1", 0)
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = method
    handler.path = path
    handler.close_connection = True
    headers = Message()
    headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    handler.headers = headers
    if method == "POST":
        handler.do_POST()
    else:
        handler.do_GET()
    status, _headers, response_body = parse_handler_response(handler.wfile.getvalue())
    return status, json.loads(response_body)


def capture_doc() -> dict[str, object]:
    return {
        "_id": "cap-1",
        "capture_id": "cap-1",
        "target_type": "location",
        "target_id": "7050",
        "site_id": "7050",
        "site": "Summit Wire",
    }


def patch_capture(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object] | None) -> None:
    monkeypatch.setattr("field_capture.server.get_field_capture_document", lambda *_args: doc)


def test_post_retarget_requires_site_admin_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch, role="cleaner")

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050"})

    assert status == HTTPStatus.FORBIDDEN
    assert body["error"] == "role_not_allowed"


def test_post_retarget_returns_202_and_writes_queue_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, capture_doc())

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050"})

    assert status == HTTPStatus.ACCEPTED
    assert body["status"] == "queued"
    jobs = list((runtime / "queue").glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["job_type"] == "retarget_capture"
    assert job["payload"]["capture_id"] == "cap-1"


def test_post_retarget_returns_409_when_stage_acted_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, capture_doc())
    candidate_dir = runtime / "reviews" / "action_candidates" / "field_capture"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "ac-1.json").write_text(json.dumps({"capture_id": "cap-1", "resolution_status": "resolved", "job_type": "log_site_issue"}), encoding="utf-8")

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "location", "new_target_id": "7050"})

    assert status == HTTPStatus.CONFLICT
    assert body["error"] == "stage_not_retargetable"


def test_post_retarget_returns_404_when_capture_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, None)

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "missing", "new_target_type": "location", "new_target_id": "7050"})

    assert status == HTTPStatus.NOT_FOUND
    assert body["error"] == "capture_not_found"


def test_post_retarget_rejects_invalid_target_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "bad", "new_target_id": "7050"})

    assert status == HTTPStatus.BAD_REQUEST
    assert body["error"] == "invalid_payload"


def test_post_retarget_rejects_empty_target_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "location", "new_target_id": ""})

    assert status == HTTPStatus.BAD_REQUEST
    assert body["error"] == "invalid_target_id"


@pytest.mark.parametrize("target_id", ["", "null", "none", "discard"])
def test_post_retarget_rejects_sentinel_target_id_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_id: str) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)

    status, body = request(server, token, "POST", "/api/retarget", {"capture_id": "cap-1", "new_target_type": "location", "new_target_id": target_id})

    assert status == HTTPStatus.BAD_REQUEST
    assert body["error"] == "invalid_target_id"


def test_get_retarget_preview_requires_site_admin_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch, role="cleaner")

    status, body = request(server, token, "GET", "/api/retarget-preview?capture_id=cap-1")

    assert status == HTTPStatus.FORBIDDEN
    assert body["error"] == "role_not_allowed"


def test_get_retarget_preview_returns_current_target_label_and_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, capture_doc())

    status, body = request(server, token, "GET", "/api/retarget-preview?capture_id=cap-1")

    assert status == HTTPStatus.OK
    assert body["current_target"] == {"target_type": "location", "target_id": "7050", "label": "Summit Wire"}
    assert body["stage"] == "processing"
    assert body["retargetable"] is True


def test_get_retarget_preview_lists_pending_candidates_with_auto_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, capture_doc())
    candidate_dir = runtime / "reviews" / "action_candidates" / "field_capture"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "ac-1.json").write_text(json.dumps({"capture_id": "cap-1", "candidate_id": "ac-1", "status": "pending_review", "job_type": "log_site_issue"}), encoding="utf-8")

    status, body = request(server, token, "GET", "/api/retarget-preview?capture_id=cap-1")

    assert status == HTTPStatus.OK
    assert body["downstream"]["candidates"][0]["auto_action_on_retarget"] == "withdraw"
    assert body["warnings"] == ["1 pending candidate will be auto-withdrawn on retarget."]


def test_get_retarget_preview_sets_retargetable_false_when_stage_acted_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, capture_doc())
    candidate_dir = runtime / "reviews" / "action_candidates" / "field_capture"
    candidate_dir.mkdir(parents=True)
    (candidate_dir / "ac-1.json").write_text(json.dumps({"capture_id": "cap-1", "resolution_status": "resolved", "job_type": "log_site_issue"}), encoding="utf-8")

    status, body = request(server, token, "GET", "/api/retarget-preview?capture_id=cap-1")

    assert status == HTTPStatus.OK
    assert body["retargetable"] is False


def test_get_retarget_preview_returns_404_when_capture_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server, token, _runtime = build_server(tmp_path, monkeypatch)
    patch_capture(monkeypatch, None)

    status, body = request(server, token, "GET", "/api/retarget-preview?capture_id=missing")

    assert status == HTTPStatus.NOT_FOUND
    assert body["error"] == "capture_not_found"
