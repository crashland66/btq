from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

import btq
from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_reader import CouchDBCaptureReaderError
from field_capture import client_notifications
from field_capture import photo_vision
from field_capture import site_status_export
from field_capture.auth import TokenStore
from field_capture.server import FieldCaptureHandler
from field_capture.site_viewer import (
    SiteImage,
    SiteUploadsNotFound,
    SiteUpload,
    audio_asset_id,
    build_site_payload,
    job_from_capture_doc,
    render_important_items,
    render_site_page,
    render_uploads,
    resolve_media_request,
    upload_as_payload,
    upload_from_job,
    upload_matches_filter,
)
from processing_core.action_candidates import action_candidate_payload, write_action_candidate_review
from processing_core.artifacts import write_json_object
from queue_processor import main as queue_processor_main
from tests.test_field_capture_auth import employee_doc, patch_canonical


def write_capture(
    queue_dir: Path,
    upload_dir: Path,
    *,
    job_id: str,
    site_id: str,
    capture_id: str,
    timestamp: str,
    area: str,
    phase: str = "",
    note: str = "",
    image_count: int = 1,
    audio_count: int = 0,
    submitter_name: str = "",
) -> Path:
    image_paths = []
    for index in range(1, image_count + 1):
        image_path = upload_dir / timestamp[:10] / capture_id / f"{capture_id}-{index}.jpg"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"\xff\xd8image")
        image_paths.append(image_path)
    audio_paths = []
    for index in range(1, audio_count + 1):
        audio_path = upload_dir / timestamp[:10] / capture_id / f"{capture_id}-voice-{index}.webm"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"audio")
        audio_paths.append(audio_path)
    job = {
        "job_id": job_id,
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": capture_id,
            "site_id": site_id,
        }
        | ({"person_name": submitter_name, "person_id": "per_submitter"} if submitter_name else {}),
        "payload": {
            "site": "Summit Wire",
            "qc_category": area,
            "phase": phase,
            "note": note,
            "captured_at": timestamp,
            "exported_at": timestamp,
            "photos": [
                {
                    "filename": image_path.name,
                    "mime_type": "image/jpeg",
                    "stored_path": str(image_path),
                    "upload_id": f"{timestamp[:10]}/{capture_id}/{image_path.name}",
                }
                for image_path in image_paths
            ],
            "audio": [
                {
                    "media_type": "audio",
                    "filename": audio_path.name,
                    "mime_type": "audio/webm",
                    "stored_path": str(audio_path),
                    "upload_id": f"{timestamp[:10]}/{capture_id}/{audio_path.name}",
                    "size_bytes": audio_path.stat().st_size,
                    "duration_seconds": "8",
                }
                for audio_path in audio_paths
            ],
        },
    }
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"{job_id}.json"
    queue_path.write_text(json.dumps(job), encoding="utf-8")
    return image_paths[0]


def capture_docs_from_queue(queue_dir: Path) -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for path in sorted(queue_dir.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if job.get("job_type") != "photo_capture":
            continue
        docs.append(
            {
                "_id": str(metadata.get("capture_id") or job.get("job_id") or path.stem),
                "type": "field_capture",
                "capture_id": str(metadata.get("capture_id") or ""),
                "site_id": str(metadata.get("site_id") or ""),
                "site": str(payload.get("site") or ""),
                "person_id": str(metadata.get("person_id") or ""),
                "person_name": str(metadata.get("person_name") or ""),
                "qc_category": str(payload.get("qc_category") or ""),
                "phase": str(payload.get("phase") or ""),
                "note": str(payload.get("note") or ""),
                "captured_at": str(payload.get("captured_at") or ""),
                "exported_at": str(payload.get("exported_at") or ""),
                "photos": payload.get("photos") if isinstance(payload.get("photos"), list) else [],
                "audio": payload.get("audio") if isinstance(payload.get("audio"), list) else [],
            }
        )
    return docs


def capture_docs_for_site(queue_dir: Path, site_id: str) -> list[dict[str, object]]:
    return [doc for doc in capture_docs_from_queue(queue_dir) if str(doc.get("site_id") or "") == site_id]


def site_id_for_upload_id(queue_dir: Path, upload_id: str) -> str | None:
    for doc in capture_docs_from_queue(queue_dir):
        for key in ("photos", "audio"):
            records = doc.get(key)
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and str(record.get("upload_id") or "") == upload_id:
                    return str(doc.get("site_id") or "") or None
    return None


def target_for_upload_id(queue_dir: Path, upload_id: str) -> dict[str, str] | None:
    for doc in capture_docs_from_queue(queue_dir):
        for key in ("photos", "audio"):
            records = doc.get(key)
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and str(record.get("upload_id") or "") == upload_id:
                    site_id = str(doc.get("site_id") or "").strip()
                    target_type = str(doc.get("target_type") or "location")
                    target_id = str(doc.get("target_id") or site_id).strip()
                    if not target_id:
                        return None
                    return {"target_type": target_type, "target_id": target_id, "site_id": site_id}
    return None


def write_vault_note(path: Path, frontmatter: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n", encoding="utf-8")


def write_vault_site(vault_root: Path, site_id: str, name: str, account: str = "Example") -> None:
    write_vault_note(
        vault_root / "Accounts" / account / "Locations" / f"{site_id} - {name}" / "about.md",
        "type: location\n"
        f"site_id: {site_id}\n"
        f"account: {account}\n"
        f"location: {name}\n",
    )


def write_vault_person(vault_root: Path, *, person_id: str, job: str, additional_jobs: list[str] | None = None) -> None:
    additional_job_lines = ""
    if additional_jobs is not None:
        additional_job_lines = "additional_jobs:\n" + "".join(f"  - {site_id}\n" for site_id in additional_jobs)
    write_vault_note(
        vault_root / "People" / f"{person_id}.md",
        "type: employee\n"
        f"name: {person_id}\n"
        f"person_id: {person_id}\n"
        f"job: {job}\n"
        f"{additional_job_lines}",
    )


def patch_viewer_canonical(
    monkeypatch: pytest.MonkeyPatch,
    people: dict[str, list[str]],
    sites: dict[str, str],
) -> None:
    _store, registry = patch_canonical(
        monkeypatch,
        docs={
            f"employee_{person_id}": employee_doc(person_id=person_id, name=person_id, site_ids=site_ids)
            for person_id, site_ids in people.items()
        },
        sites=sites,
    )
    monkeypatch.setattr("field_capture.server.CouchDBSiteRegistry", lambda: registry)


class RunningFieldCaptureServer:
    def __init__(self, server: SimpleNamespace) -> None:
        self.server = server

    def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        handler = object.__new__(FieldCaptureHandler)
        handler.server = self.server
        handler.rfile = io.BytesIO(body or b"")
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
        elif method == "POST":
            handler.do_POST()
        else:
            raise AssertionError(f"unsupported test method: {method}")
        return parse_handler_response(handler.wfile.getvalue())

    def close(self) -> None:
        return None


def start_field_capture_server(tmp_path: Path) -> tuple[RunningFieldCaptureServer, TokenStore, Path, Path, Path]:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    queue_dir = runtime_root / "queue"
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
        capture_reader=lambda _config, site_id, *, database: capture_docs_for_site(queue_dir, site_id),
        target_lookup_reader=lambda _config, upload_id, *, database: target_for_upload_id(queue_dir, upload_id),
        site_lookup_reader=lambda _config, upload_id, *, database: site_id_for_upload_id(queue_dir, upload_id),
    )
    return RunningFieldCaptureServer(server), token_store, vault_root, queue_dir, upload_dir


def parse_handler_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        lower_key = key.lower()
        stripped = value.strip()
        headers[lower_key] = f"{headers[lower_key]}, {stripped}" if lower_key in headers else stripped
    return status, headers, body


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-field-viewer-test"
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


def valid_submit_fields(site_id: str = "7060") -> dict[str, str]:
    return {
        "job_id": f"2026-05-02T00-00-00-04-00__photo-capture-{site_id}",
        "capture_id": f"cap-photo-submit-{site_id}",
        "site": "Apex Powdered Metals",
        "site_id": site_id,
        "qc_category": "Restrooms",
        "note": "Sink checked.",
        "captured_at": "2026-05-02T00:00:00-04:00",
        "exported_at": "2026-05-02T00:01:00-04:00",
    }


def write_transcript_for_capture(runtime_root: Path, *, capture_id: str, filename: str, stored_path: Path, raw_text: str = "Please fix this.") -> str:
    asset_id = audio_asset_id(capture_id, filename, str(stored_path))
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    write_json_object(
        transcript_dir / f"{asset_id}.json",
        {
            "type": "field_audio_transcript",
            "status": "complete",
            "audio_asset_id": asset_id,
            "upload_id": capture_id,
            "raw_text": raw_text,
        },
    )
    return asset_id


def media_url_for_image(image_path: Path, upload_dir: Path) -> str:
    return "/media/" + image_path.resolve(strict=False).relative_to(upload_dir.resolve(strict=False)).as_posix()


def write_photo_vision_sidecar(runtime_root: Path, image_path: Path, upload_dir: Path, *, description: str, status: str = "completed") -> Path:
    media_url = media_url_for_image(image_path, upload_dir)
    sidecar_dir = runtime_root / "field_capture" / "photo_vision"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = sidecar_dir / f"{image_path.stem}-vision.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "artifact_type": "field_capture_photo_vision",
                "type": "field_capture_photo_vision",
                "status": status,
                "description": description,
                "image_media_url": media_url,
                "provenance": {
                    "image_media_url": media_url,
                    "source_image_path": str(image_path),
                },
            }
        ),
        encoding="utf-8",
    )
    return sidecar_path


def test_site_viewer_route_requires_token_and_sets_noindex_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="employee-1200", job="7060")
        write_vault_person(vault_root, person_id="employee-1337", job="7050")
        patch_viewer_canonical(
            monkeypatch,
            {"employee-1200": ["7060"], "employee-1337": ["7050"]},
            {"7060": "Apex Powdered Metals", "7050": "Summit Wire"},
        )
        token_7060 = store.create_token("employee-1200")
        token_7050 = store.create_token("employee-1337")
        write_capture(queue_dir, upload_dir, job_id="cap-1200", site_id="7060", capture_id="cap-1200", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")
        write_capture(queue_dir, upload_dir, job_id="cap-1337", site_id="7050", capture_id="cap-1337", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")

        anonymous_status, anonymous_headers, anonymous_body = app.request("GET", "/site/7060")
        good_status, good_headers, good_body = app.request("GET", f"/site/7060?token={token_7060.token_value}")
        cross_status, _cross_headers, _cross_body = app.request("GET", f"/site/7060?token={token_7050.token_value}")
        reverse_cross_status, _reverse_headers, _reverse_body = app.request("GET", f"/site/7050?token={token_7060.token_value}")
        bad_status, _bad_headers, _bad_body = app.request("GET", "/site/7060?token=bad-token")
        unknown_status, _unknown_headers, _unknown_body = app.request("GET", f"/site/9999?token={token_7060.token_value}")

        assert anonymous_status == 403
        assert b"Access Required" in anonymous_body
        assert anonymous_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert "no-store" in anonymous_headers["cache-control"]
        assert good_status == 200
        assert good_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert "no-store" in good_headers["cache-control"]
        assert b'<meta name="robots" content="noindex,nofollow,noarchive" />' in good_body
        assert token_7060.token_value.encode("utf-8") not in good_body
        assert b"Apex Powdered Metals Uploads" in good_body
        assert cross_status == 403
        assert reverse_cross_status == 403
        assert bad_status == 403
        assert unknown_status == 404
    finally:
        app.close()


def test_site_viewer_json_and_media_are_token_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="employee-1200", job="7060")
        write_vault_person(vault_root, person_id="employee-1337", job="7050")
        patch_viewer_canonical(
            monkeypatch,
            {"employee-1200": ["7060"], "employee-1337": ["7050"]},
            {"7060": "Apex Powdered Metals", "7050": "Summit Wire"},
        )
        token_7060 = store.create_token("employee-1200")
        token_7050 = store.create_token("employee-1337")
        write_capture(queue_dir, upload_dir, job_id="cap-1200", site_id="7060", capture_id="cap-1200", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")

        json_missing_status, json_missing_headers, _json_missing_body = app.request("GET", "/site/7060?format=json")
        json_ok_status, json_ok_headers, json_ok_body = app.request("GET", f"/site/7060?format=json&token={token_7060.token_value}")
        media_path = "/media/2026-05-02/cap-1200/cap-1200-1.jpg"
        media_public_status, media_public_headers, _media_public_body = app.request("GET", media_path)
        media_cross_status, _media_cross_headers, _media_cross_body = app.request("GET", media_path, headers={"Authorization": f"Bearer {token_7050.token_value}"})
        media_ok_status, media_ok_headers, media_ok_body = app.request("GET", media_path, headers={"Authorization": f"Bearer {token_7060.token_value}"})

        assert json_missing_status == 403
        assert json_missing_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert json_ok_status == 200
        assert json_ok_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert json.loads(json_ok_body)["site_id"] == "7060"
        assert media_public_status == 403
        assert media_public_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert media_cross_status == 403
        assert media_ok_status == 200
        assert media_ok_headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert media_ok_body == b"\xff\xd8image"
    finally:
        app.close()


def test_site_viewer_returns_503_when_couchdb_capture_lookup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_person(vault_root, person_id="employee-1200", job="7060")
        patch_viewer_canonical(monkeypatch, {"employee-1200": ["7060"]}, {"7060": "Apex Powdered Metals"})
        token_7060 = store.create_token("employee-1200")
        app.server.capture_reader = lambda _config, site_id, *, database: (_ for _ in ()).throw(CouchDBCaptureReaderError("down"))

        status, headers, body = app.request("GET", f"/site/7060?format=json&token={token_7060.token_value}")

        assert status == 503
        assert headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert json.loads(body) == {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"}
    finally:
        app.close()


def test_media_route_returns_503_when_couchdb_upload_lookup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_person(vault_root, person_id="employee-1200", job="7060")
        patch_viewer_canonical(monkeypatch, {"employee-1200": ["7060"]}, {"7060": "Apex Powdered Metals"})
        token_7060 = store.create_token("employee-1200")
        write_capture(queue_dir, upload_dir, job_id="cap-1200", site_id="7060", capture_id="cap-1200", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: (_ for _ in ()).throw(CouchDBCaptureReaderError("down"))

        status, headers, body = app.request("GET", "/media/2026-05-02/cap-1200/cap-1200-1.jpg", headers={"Authorization": f"Bearer {token_7060.token_value}"})

        assert status == 503
        assert headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert json.loads(body) == {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"}
    finally:
        app.close()


def test_handle_media_serves_prospect_capture_photo_without_query_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="admin-1337", job="7050")
        patch_viewer_canonical(monkeypatch, {"admin-1337": ["7050"]}, {"7050": "Summit Wire"})
        token = store.create_token("admin-1337", role="site_admin")
        media_path = upload_dir / "2026-05-28" / "cap-prospect" / "cap-prospect-1.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"\xff\xd8image")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: {
            "target_type": "prospect",
            "target_id": "kmf-birch-1",
            "site_id": "",
        }

        status, headers, body = app.request(
            "GET",
            "/media/2026-05-28/cap-prospect/cap-prospect-1.jpg",
            headers={"Authorization": f"Bearer {token.token_value}"},
        )

        assert status == 200
        assert headers["content-type"] == "image/jpeg"
        assert body == b"\xff\xd8image"
    finally:
        app.close()


def test_handle_media_ignores_mismatched_prospect_id_query_param(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="admin-1337", job="7050")
        patch_viewer_canonical(monkeypatch, {"admin-1337": ["7050"]}, {"7050": "Summit Wire"})
        token = store.create_token("admin-1337", role="site_admin")
        media_path = upload_dir / "2026-05-28" / "cap-prospect" / "cap-prospect-1.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"\xff\xd8image")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: {
            "target_type": "prospect",
            "target_id": "kmf-birch-1",
            "site_id": "",
        }

        status, _headers, body = app.request(
            "GET",
            "/media/2026-05-28/cap-prospect/cap-prospect-1.jpg?prospect_id=other-prospect",
            headers={"Authorization": f"Bearer {token.token_value}"},
        )

        assert status == 200
        assert body == b"\xff\xd8image"
    finally:
        app.close()


def test_handle_media_returns_403_when_site_token_lacks_prospect_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="cleaner-1337", job="7050")
        patch_viewer_canonical(monkeypatch, {"cleaner-1337": ["7050"]}, {"7050": "Summit Wire"})
        token = store.create_token("cleaner-1337", role="cleaner")
        media_path = upload_dir / "2026-05-28" / "cap-prospect" / "cap-prospect-1.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"\xff\xd8image")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: {
            "target_type": "prospect",
            "target_id": "kmf-birch-1",
            "site_id": "",
        }

        status, headers, body = app.request(
            "GET",
            "/media/2026-05-28/cap-prospect/cap-prospect-1.jpg",
            headers={"Authorization": f"Bearer {token.token_value}"},
        )

        assert status == 403
        assert headers["content-type"] == "text/plain; charset=utf-8"
        assert body == b"access required"
    finally:
        app.close()


def test_handle_media_returns_404_when_upload_id_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="admin-1337", job="7050")
        patch_viewer_canonical(monkeypatch, {"admin-1337": ["7050"]}, {"7050": "Summit Wire"})
        token = store.create_token("admin-1337", role="site_admin")
        media_path = upload_dir / "2026-05-28" / "cap-prospect" / "cap-prospect-1.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"\xff\xd8image")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: None

        status, _headers, _body = app.request(
            "GET",
            "/media/2026-05-28/cap-prospect/cap-prospect-1.jpg",
            headers={"Authorization": f"Bearer {token.token_value}"},
        )

        assert status == 404
    finally:
        app.close()


def test_handle_media_returns_503_when_couchdb_lookup_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, _queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="admin-1337", job="7050")
        patch_viewer_canonical(monkeypatch, {"admin-1337": ["7050"]}, {"7050": "Summit Wire"})
        token = store.create_token("admin-1337", role="site_admin")
        media_path = upload_dir / "2026-05-28" / "cap-prospect" / "cap-prospect-1.jpg"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"\xff\xd8image")
        app.server.target_lookup_reader = lambda _config, upload_id, *, database: (_ for _ in ()).throw(CouchDBCaptureReaderError("down"))

        status, headers, body = app.request(
            "GET",
            "/media/2026-05-28/cap-prospect/cap-prospect-1.jpg",
            headers={"Authorization": f"Bearer {token.token_value}"},
        )

        assert status == 503
        assert headers["x-robots-tag"] == "noindex,nofollow,noarchive"
        assert json.loads(body) == {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"}
    finally:
        app.close()


def test_universal_token_can_view_multiple_sites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_site(vault_root, "7050", "Summit Wire")
        write_vault_person(vault_root, person_id="admin", job="7050")
        patch_viewer_canonical(
            monkeypatch,
            {"admin": ["7050"]},
            {"7060": "Apex Powdered Metals", "7050": "Summit Wire"},
        )
        admin_token = store.create_token("admin", token_type="admin_viewer", site_ids=["*"])
        write_capture(queue_dir, upload_dir, job_id="cap-1200", site_id="7060", capture_id="cap-1200", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")
        write_capture(queue_dir, upload_dir, job_id="cap-1337", site_id="7050", capture_id="cap-1337", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")

        site_7060_status, _site_7060_headers, site_7060_body = app.request("GET", f"/site/7060?token={admin_token.token_value}")
        site_7050_status, _site_7050_headers, site_7050_body = app.request("GET", f"/site/7050?token={admin_token.token_value}")

        assert site_7060_status == 200
        assert b"Apex Powdered Metals Uploads" in site_7060_body
        assert site_7050_status == 200
        assert b"Summit Wire Uploads" in site_7050_body
        assert admin_token.token_value.encode("utf-8") not in site_7060_body
        assert admin_token.token_value.encode("utf-8") not in site_7050_body
    finally:
        app.close()


def test_api_session_returns_tokenized_viewer_urls_without_enabling_viewer_only_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, vault_root, queue_dir, upload_dir = start_field_capture_server(tmp_path)
    try:
        write_vault_site(vault_root, "7060", "Apex Powdered Metals")
        write_vault_person(vault_root, person_id="viewer-1200", job="7060")
        patch_viewer_canonical(monkeypatch, {"viewer-1200": ["7060"]}, {"7060": "Apex Powdered Metals"})
        viewer_token = store.create_token(
            "viewer-1200",
            can_submit=False,
            can_view_site=True,
            token_type="client_viewer",
            site_ids=["7060"],
        )
        write_capture(queue_dir, upload_dir, job_id="cap-1200", site_id="7060", capture_id="cap-1200", timestamp="2026-05-02T10:00:00-04:00", area="Restrooms")

        session_status, _session_headers, session_body = app.request("GET", f"/api/session?token={viewer_token.token_value}")
        view_status, _view_headers, view_body = app.request("GET", f"/site/7060?token={viewer_token.token_value}")
        body, content_type = multipart_body(valid_submit_fields("7060"), [("photos", "sink.jpg", "image/jpeg", b"\xff\xd8photo")])
        submit_status, _submit_headers, submit_body = app.request(
            "POST",
            "/api/submit",
            headers={"Authorization": f"Bearer {viewer_token.token_value}", "Content-Type": content_type, "Content-Length": str(len(body))},
            body=body,
        )

        assert session_status == 200
        session = json.loads(session_body)
        assert session["token"]["can_submit"] is False
        assert session["token"]["can_view_site"] is True
        assert session["token"]["token_type"] == "client_viewer"
        assert session["sites"][0]["viewer_url"] == f"/site/7060?token={viewer_token.token_value}"
        assert view_status == 200
        assert viewer_token.token_value.encode("utf-8") not in view_body
        assert submit_status == 403
        assert json.loads(submit_body)["error"] == "submit_not_allowed"
    finally:
        app.close()


def write_site_issue(vault_root: Path, *, site_id: str, issue_id: str, title: str, status: str = "open") -> Path:
    account = "Summitsteel" if site_id == "7050" else "Contworks"
    site_dir = "7050 - Summit Wire" if site_id == "7050" else "7060 - Continental Metalworks"
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Issues" / f"{issue_id}__issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: site_issue
issue_id: {issue_id}
site_id: "{site_id}"
site: {site_dir.split(" - ", 1)[-1]}
account: {account}
title: {title}
status: {status}
priority: high
category: maintenance
client_notified: true
client_notified_at: 2026-05-08T15:28:09+00:00
client_notified_by: Jordan
client_notified_method: email
reported_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
created_at: 2026-05-08T17:43:43+00:00
resolution_trigger: Maintenance confirms the issue is corrected.
related_capture_ids:
  - cap-issue
related_candidate_ids:
  - ac_issue
---
# {title}

## Summary
Structured issue summary for display.
""",
        encoding="utf-8",
    )
    return path


def write_approved_candidate(
    runtime_root: Path,
    *,
    capture_id: str,
    site_id: str = "7050",
    summary: str = "Fix the leaking sink.",
    rationale: str = "Maintenance should repair the leak.",
    review_rationale: str = "Approved for maintenance follow-up.",
    audio_asset_id_value: str = "",
) -> dict[str, object]:
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=summary,
        rationale=rationale,
        confidence="unknown",
        source_text=rationale,
        source_context=rationale,
        provenance={
            "audio_asset_id": audio_asset_id_value,
            "source_transcript_path": str(runtime_root / "field_capture" / "audio_transcripts" / f"{audio_asset_id_value}.json") if audio_asset_id_value else "",
            "semantic_artifact_path": str(runtime_root / "field_capture" / "audio_semantics" / "semantic.json"),
        },
        channel_metadata={
            "channel": "field_capture",
            "site_id": site_id,
            "upload_id": capture_id,
            "area": "Restrooms",
        },
        status="approved",
    )
    candidate["reviewer"] = "Jordan"
    candidate["reviewed_at"] = "2026-05-08T14:20:00+00:00"
    candidate["review_rationale"] = review_rationale
    write_action_candidate_review(candidate_dir, candidate)
    return candidate


def test_site_viewer_groups_uploads_by_date_newest_first(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="older",
        site_id="7050",
        capture_id="cap-old",
        timestamp="2026-05-01T09:00:00-04:00",
        area="Restrooms",
    )
    write_capture(
        queue_dir,
        upload_dir,
        job_id="newer",
        site_id="7050",
        capture_id="cap-new",
        timestamp="2026-05-02T10:00:00-04:00",
        area="Entryways",
        phase="Final walkthrough",
        image_count=2,
        audio_count=1,
    )
    write_capture(
        queue_dir,
        upload_dir,
        job_id="other-site",
        site_id="7060",
        capture_id="cap-other",
        timestamp="2026-05-03T10:00:00-04:00",
        area="Offices",
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)

    assert payload["site_id"] == "7050"
    assert payload["dates"][0]["date"] == "2026-05-02"
    assert payload["dates"][0]["summary"]["total_uploads"] == 1
    assert payload["dates"][0]["summary"]["total_images"] == 2
    assert payload["dates"][0]["summary"]["total_audio"] == 1
    assert payload["dates"][0]["summary"]["areas_present"] == ["Entryways"]
    assert payload["dates"][0]["summary"]["phases_present"] == ["after"]
    assert payload["dates"][0]["summary"]["latest_upload_time"] == "10:00 AM"
    assert "Restrooms" in payload["dates"][0]["summary"]["missing_areas"]
    assert payload["dates"][0]["uploads"][0]["upload_id"] == "cap-new"
    assert payload["dates"][0]["uploads"][0]["area"] == "Entryways"
    assert payload["dates"][0]["uploads"][0]["phase"] == "after"
    assert payload["dates"][0]["uploads"][0]["display_time"] == "10:00 AM"
    assert payload["dates"][0]["uploads"][0]["images"] == [
        "/media/2026-05-02/cap-new/cap-new-1.jpg",
        "/media/2026-05-02/cap-new/cap-new-2.jpg",
    ]
    assert payload["dates"][0]["uploads"][0]["audio"] == [
        {
            "url": "/media/2026-05-02/cap-new/cap-new-voice-1.webm",
            "filename": "cap-new-voice-1.webm",
            "mime_type": "audio/webm",
            "size_bytes": 5,
            "duration_seconds": "8",
            "transcript": None,
            "semantic": None,
        }
    ]
    assert payload["dates"][1]["date"] == "2026-05-01"


def test_export_field_capture_site_status_creates_safe_review_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    image_path = write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
        note="Please check the sink.",
        audio_count=1,
    )
    write_photo_vision_sidecar(runtime_root, image_path, upload_dir, description="A sink area is visible with a maintenance concern near the fixture.")
    audio_path = upload_dir / "2026-05-08" / "cap-reviewed" / "cap-reviewed-voice-1.webm"
    asset_id = write_transcript_for_capture(runtime_root, capture_id="cap-reviewed", filename="cap-reviewed-voice-1.webm", stored_path=audio_path)
    candidate = write_approved_candidate(runtime_root, capture_id="cap-reviewed", audio_asset_id_value=asset_id)
    output_path = runtime_root / "field_capture" / "site_viewer_exports" / "site_7050.json"

    exit_code = btq.run(["export-field-capture-site-status", "--site-id", "7050", "--runtime-root", str(runtime_root), "--json"])
    stdout = capsys.readouterr().out
    exported = json.loads(output_path.read_text(encoding="utf-8"))
    printed = json.loads(stdout)

    assert exit_code == 0
    assert printed["output_path"] == str(output_path)
    assert exported["type"] == "field_capture_site_viewer_status"
    assert exported["site_id"] == "7050"
    assert exported["counts"]["reviewed_items"] == 1
    [item] = exported["reviewed_items"]
    assert item["capture_id"] == "cap-reviewed"
    assert item["candidate_id"] == candidate["candidate_id"]
    assert item["status"] == "approved"
    assert item["review_type"] == "maintenance_issue"
    assert item["summary"] == "Fix the leaking sink."
    assert item["display_title"] == "Approved for maintenance follow-up."
    assert item["display_body"] == "Maintenance should repair the leak."
    assert item["source_context"] == "Maintenance should repair the leak."
    assert item["transcript_excerpt"] == "Please fix this."
    assert item["text_note"] == "Please check the sink."
    assert item["review_rationale"] == "Approved for maintenance follow-up."
    assert item["reviewer"] == "Jordan"
    assert item["has_audio"] is True
    assert item["has_text_note"] is True
    assert item["has_voice_transcript"] is True
    assert item["media"][0]["media_url"].startswith("/media/2026-05-08/cap-reviewed/")
    assert item["media"][0]["visual_context"] == "A sink area is visible with a maintenance concern near the fixture."
    assert item["media"][0]["alt_text"] == "A sink area is visible with a maintenance concern near the fixture."
    assert "field_capture_token" not in stdout
    assert "bearer" not in stdout.lower()


def test_site_status_export_includes_requested_site_issues_only(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    write_site_issue(vault_root, site_id="7050", issue_id="iss_summit", title="Restroom drain backup")
    write_site_issue(vault_root, site_id="7060", issue_id="iss_continental", title="Wall damage")

    payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        vault_root=vault_root,
        include_issues=True,
        generated_at="2026-05-08T15:00:00+00:00",
    )

    assert payload["site_issues"]["counts"]["total"] == 1
    [issue] = payload["site_issues"]["issues"]
    assert issue["issue_id"] == "iss_summit"
    assert issue["title"] == "Restroom drain backup"
    assert issue["client_notified"] is True
    assert issue["related_capture_ids"] == ["cap-issue"]
    assert "iss_continental" not in json.dumps(payload)
    assert "vault_issue_path" not in json.dumps(payload)


def test_site_viewer_renders_open_issues_from_export_without_issue_export_requirement(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    export_path = site_status_export.default_export_path("7050", runtime_root)
    site_status_export.write_site_status_export(
        export_path,
        {
            "type": "field_capture_site_viewer_status",
            "site_id": "7050",
            "generated_at": "2026-05-08T15:00:00+00:00",
            "reviewed_items": [],
            "site_issues": {
                "issues": [
                    {
                        "issue_id": "iss_summit",
                        "site_id": "7050",
                        "title": "Restroom drain backup",
                        "status": "open",
                        "priority": "high",
                        "category": "maintenance",
                        "client_notified": True,
                        "client_notified_method": "email",
                        "reported_by": "Tom Walsh",
                        "observed_at": "2026-05-08T14:12:43+00:00",
                        "summary": "Structured issue summary for display.",
                        "resolution_trigger": "Maintenance confirms the issue is corrected.",
                        "related_capture_ids": ["cap-issue"],
                        "related_candidate_ids": ["ac_issue"],
                    }
                ],
                "counts": {"total": 1, "current": 1},
            },
            "counts": {"reviewed_items": 0},
        },
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir, known_site_ids=["7050"], site_status_path=export_path)
    html = render_site_page("7050", payload)

    assert "Open Site Issues" in html
    assert "Restroom drain backup" in html
    assert "Client Informed" in html
    assert "Client informed by email" in html
    assert "Structured issue summary for display." in html
    assert "cap-issue" in html
    assert "Raw Capture Stream" in html


def test_mark_client_informed_cli_writes_sidecar_and_preserves_history(tmp_path: Path, capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_approved_candidate(runtime_root, capture_id="cap-reviewed")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate_id = json.loads(candidate_path.read_text(encoding="utf-8"))["candidate_id"]

    first_exit = btq.run(
        [
            "mark-client-informed",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            candidate_id,
            "--method",
            "email",
            "--by",
            "Jordan",
            "--note",
            "Emailed client with photo/context.",
            "--json",
        ]
    )
    first_stdout = capsys.readouterr().out
    first = json.loads(first_stdout)
    second_exit = btq.run(
        [
            "mark-client-informed",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            candidate_id,
            "--method",
            "phone",
            "--by",
            "Jordan",
            "--note",
            "Called client with update.",
            "--json",
        ]
    )
    second = json.loads(capsys.readouterr().out)
    sidecar_path = runtime_root / "reviews" / "client_notifications" / "field_capture" / f"{candidate_id}.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert first_exit == 0
    assert second_exit == 0
    assert first["client_informed"] is True
    assert sidecar["type"] == "field_capture_client_notification"
    assert sidecar["candidate_id"] == candidate_id
    assert sidecar["capture_id"] == "cap-reviewed"
    assert sidecar["site_id"] == "7050"
    assert sidecar["client_informed_method"] == "phone"
    assert sidecar["client_informed_by"] == "Jordan"
    assert sidecar["client_informed_note"] == "Called client with update."
    assert second["notification_history"][0]["client_informed_method"] == "email"
    assert sidecar["notification_history"][0]["client_informed_note"] == "Emailed client with photo/context."
    assert sidecar["created_at"]
    assert sidecar["updated_at"]


def test_mark_client_informed_cli_fails_closed_for_invalid_candidates(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    approved = write_approved_candidate(runtime_root, capture_id="cap-approved")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    doc_id = f"action_candidate_{approved['candidate_id']}"

    # missing candidate -> fail closed (NameError on this path was a real 308c bug,
    # now fixed: client_notifications imports CandidateReviewError).
    with pytest.raises(SystemExit):
        btq.run(["mark-client-informed", "--runtime-root", str(runtime_root), "--candidate-id", "missing", "--method", "email", "--by", "Jordan"])

    # 308c: the approved-status guard reads the CANONICAL CouchDB doc, not the
    # filesystem artifact. Mutate the canonical store to exercise fail-closed.
    couchdb_review.docs[doc_id]["status"] = "pending_review"
    with pytest.raises(SystemExit):
        btq.run(["mark-client-informed", "--runtime-root", str(runtime_root), "--candidate-id", approved["candidate_id"], "--method", "email", "--by", "Jordan"])

    couchdb_review.docs[doc_id]["status"] = "rejected"
    with pytest.raises(SystemExit):
        btq.run(["mark-client-informed", "--runtime-root", str(runtime_root), "--candidate-id", approved["candidate_id"], "--method", "email", "--by", "Jordan"])

    # (The pre-308c "duplicate filesystem artifact" fail-closed case no longer
    # exists: CouchDB keys candidates by a unique _id, so a duplicate is
    # structurally impossible on the canonical read path.)
    assert not (runtime_root / "reviews" / "client_notifications").exists()


def test_mark_client_informed_sidecar_suppresses_raw_tokens(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate = write_approved_candidate(runtime_root, capture_id="cap-reviewed")

    payload = client_notifications.mark_client_informed(
        candidate_id=str(candidate["candidate_id"]),
        method="email",
        informed_by="Jordan",
        note="Bearer secret field_capture_token fct_secret /Users/jordan/private",
        runtime_root=runtime_root,
    )
    serialized = json.dumps(payload)

    assert payload["client_informed_note"] == ""
    assert "Bearer" not in serialized
    assert "field_capture_token" not in serialized
    assert "fct_secret" not in serialized
    assert "/Users/jordan" not in serialized


def test_site_status_export_is_deterministic_with_fixed_timestamp(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Supply Levels",
        note="Need towels.",
    )
    write_approved_candidate(
        runtime_root,
        capture_id="cap-reviewed",
        summary="Restock paper towels.",
        rationale="Supply request for towels.",
        review_rationale="Approved supply request.",
    )

    first = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T15:00:00+00:00",
    )
    second = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T15:00:00+00:00",
    )

    assert first == second
    assert first["reviewed_items"][0]["review_type"] == "supply_request"
    assert first["reviewed_items"][0]["priority"] > 100


def test_export_field_capture_site_status_does_not_invoke_queue_or_mutate_vault(tmp_path: Path, monkeypatch, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
    )
    write_approved_candidate(runtime_root, capture_id="cap-reviewed")
    called = {"queue": False}

    def fail_if_queue_runs(*_args: object, **_kwargs: object) -> None:
        called["queue"] = True
        raise AssertionError("queue processor must not run during site status export")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_runs)

    payload = site_status_export.build_site_status_export(site_id="7050", runtime_root=runtime_root, generated_at="2026-05-08T15:00:00+00:00")

    assert payload["counts"]["reviewed_items"] == 1
    assert called["queue"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()


def test_site_viewer_filter_logic_detects_issue_uploads(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="issue",
        site_id="7050",
        capture_id="cap-issue",
        timestamp="2026-05-02T19:42:00-04:00",
        area="Restrooms",
        note="Sink needs attention.",
    )
    job = json.loads((queue_dir / "issue.json").read_text(encoding="utf-8"))
    upload = upload_from_job(job, upload_dir)

    assert upload is not None
    assert upload.phase == "issue"
    assert upload_matches_filter(upload, area="Restrooms")
    assert upload_matches_filter(upload, phase="issue")
    assert upload_matches_filter(upload, issue_only=True)
    assert not upload_matches_filter(upload, area="Entryways")


def test_upload_from_job_populates_submitter_fields(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="submitter",
        site_id="7050",
        capture_id="cap-submitter",
        timestamp="2026-05-12T18:00:00-04:00",
        area="Entryways",
        submitter_name="Jordan Avery",
    )
    job = json.loads((queue_dir / "submitter.json").read_text(encoding="utf-8"))

    upload = upload_from_job(job, upload_dir)

    assert upload is not None
    assert upload.person_id == "per_submitter"
    assert upload.person_name == "Jordan Avery"


def test_upload_from_job_handles_missing_submitter_metadata(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="anonymous",
        site_id="7050",
        capture_id="cap-anonymous",
        timestamp="2026-05-12T18:05:00-04:00",
        area="Entryways",
    )
    job = json.loads((queue_dir / "anonymous.json").read_text(encoding="utf-8"))

    upload = upload_from_job(job, upload_dir)

    assert upload is not None
    assert upload.person_id == ""
    assert upload.person_name == ""


def test_upload_as_payload_includes_person_name_and_first_name() -> None:
    upload = SiteUpload(
        upload_id="cap-submitter",
        timestamp="2026-05-12T18:00:00-04:00",
        display_time="6:00 PM",
        area="Entryways",
        phase="before",
        text_note="",
        images=[
            SiteImage(
                url="/media/2026-05-12/cap-submitter/cap-submitter-1.jpg",
                stored_path="/tmp/cap-submitter-1.jpg",
            )
        ],
        audio=[],
        person_id="per_jordan",
        person_name="Jordan Avery",
    )

    payload = upload_as_payload(upload)

    assert payload["person_id"] == "per_jordan"
    assert payload["person_name"] == "Jordan Avery"
    assert payload["submitter_first_name"] == "Jordan"


def test_upload_as_payload_empty_submitter_renders_empty_first_name() -> None:
    upload = SiteUpload(
        upload_id="cap-no-submitter",
        timestamp="2026-05-12T18:00:00-04:00",
        display_time="6:00 PM",
        area="Entryways",
        phase="before",
        text_note="",
        images=[],
        audio=[],
    )

    payload = upload_as_payload(upload)

    assert payload["person_name"] == ""
    assert payload["submitter_first_name"] == ""


def test_render_uploads_includes_submitter_pill_when_first_name_present() -> None:
    html = render_uploads(
        [
            {
                "upload_id": "cap-submitter",
                "display_time": "6:00 PM",
                "area": "Entryways",
                "phase": "before",
                "images": [],
                "audio": [],
                "image_count": 0,
                "audio_count": 0,
                "submitter_first_name": "Jordan",
            }
        ]
    )

    assert 'class="pill pill-submitter"' in html
    assert ">Jordan<" in html


def test_render_uploads_omits_submitter_pill_when_first_name_missing() -> None:
    html = render_uploads(
        [
            {
                "upload_id": "cap-no-submitter",
                "display_time": "6:00 PM",
                "area": "Entryways",
                "phase": "before",
                "images": [],
                "audio": [],
                "image_count": 0,
                "audio_count": 0,
                "submitter_first_name": "",
            }
        ]
    )

    assert "pill-submitter" not in html


def test_render_important_items_uses_submitter_first_name_from_upload() -> None:
    html = render_important_items(
        [
            {
                "kind": "reviewed",
                "review": {
                    "capture_id": "cap-important-submitter",
                    "candidate_id": "ac_important_submitter",
                    "display_title": "Important reviewed item",
                    "submitter": "per_EXAMPLE0000000000000000000",
                },
                "upload": {
                    "upload_id": "cap-important-submitter",
                    "submitter_first_name": "Jordan",
                    "images": [],
                },
            }
        ]
    )

    assert "Submitter Jordan" in html
    assert "per_EXAMPLE0000000000000000000" not in html


def test_render_important_items_omits_submitter_line_when_no_first_name() -> None:
    html = render_important_items(
        [
            {
                "kind": "reviewed",
                "review": {
                    "capture_id": "cap-important-no-name",
                    "candidate_id": "ac_important_no_name",
                    "display_title": "Important reviewed item",
                    "submitter": "carver-damon",
                },
                "upload": {
                    "upload_id": "cap-important-no-name",
                    "submitter_first_name": "",
                    "images": [],
                },
            }
        ]
    )

    assert "Submitter carver-damon" not in html
    assert "Submitter " not in html


def test_render_important_items_omits_submitter_when_first_name_is_only_whitespace() -> None:
    html = render_important_items(
        [
            {
                "kind": "reviewed",
                "review": {
                    "capture_id": "cap-important-whitespace",
                    "candidate_id": "ac_important_whitespace",
                    "display_title": "Important reviewed item",
                    "submitter": "carver-damon",
                },
                "upload": {
                    "upload_id": "cap-important-whitespace",
                    "submitter_first_name": "   ",
                    "images": [],
                },
            }
        ]
    )

    assert "Submitter " not in html


def test_job_from_capture_doc_propagates_to_rendered_html(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="couchdb-shape",
        site_id="7050",
        capture_id="cap-couchdb-shape",
        timestamp="2026-05-12T18:10:00-04:00",
        area="Entryways",
        submitter_name="Jordan Avery",
    )
    doc = capture_docs_for_site(queue_dir, "7050")[0]

    job = job_from_capture_doc(doc)
    upload = upload_from_job(job, upload_dir)
    assert upload is not None
    payload = upload_as_payload(upload)
    html = render_uploads([payload])

    assert "Jordan" in html
    assert 'class="pill pill-submitter"' in html


def test_site_viewer_renders_audio_player(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="audio",
        site_id="7050",
        capture_id="cap-audio",
        timestamp="2026-05-02T10:00:00-04:00",
        area="Entryways",
        audio_count=1,
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert '<audio controls preload="metadata">' in html
    assert "Voice note: cap-audio-voice-1.webm (8 sec)" in html
    assert '<span class="pill">1 images</span>' in html
    assert '<span class="pill">1 audio</span>' in html
    assert 'src="/media/2026-05-02/cap-audio/cap-audio-voice-1.webm"' in html
    assert 'type="audio/webm"' in html


def test_site_viewer_renders_reviewed_important_items_from_export(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="raw-newer",
        site_id="7050",
        capture_id="cap-raw-newer",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
        note="Newer context item.",
    )
    write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed-older",
        site_id="7050",
        capture_id="cap-reviewed-older",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
        note="Sink needs repair.",
        audio_count=1,
        submitter_name="Alice Example",
    )
    write_approved_candidate(runtime_root, capture_id="cap-reviewed-older")
    export_payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T15:00:00+00:00",
    )
    site_status_export.write_site_status_export(site_status_export.default_export_path("7050", runtime_root), export_payload)

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert "Reviewed / Important Items" in html
    assert "Internal-only reviewed and contextual capture signals" in html
    assert "Raw Capture Stream" in html
    assert "/media/2026-05-08/cap-reviewed-older/cap-reviewed-older-1.jpg" in html
    assert "Reviewed" in html
    assert "Voice note" in html
    assert "Text note" in html
    assert "Maintenance Issue" in html
    assert "Approved for maintenance follow-up." in html
    assert "Maintenance should repair the leak." in html
    assert "Submitter Alice" in html
    assert html.index("Approved for maintenance follow-up.") < html.index("Candidate ac_")
    assert html.index("Reviewed / Important Items") < html.index("cap-raw-newer")
    assert payload["important_items"][0]["review"]["capture_id"] == "cap-reviewed-older"
    assert payload["important_items"][0]["kind"] == "reviewed"
    assert len(payload["important_items"]) == 1


def test_site_viewer_export_and_card_show_client_informed(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
    )
    candidate = write_approved_candidate(runtime_root, capture_id="cap-reviewed")
    client_notifications.mark_client_informed(
        candidate_id=str(candidate["candidate_id"]),
        method="email",
        informed_by="Jordan",
        note="Emailed client with photo/context.",
        runtime_root=runtime_root,
        informed_at="2026-05-08T16:00:00+00:00",
    )
    export_payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T16:05:00+00:00",
    )
    site_status_export.write_site_status_export(site_status_export.default_export_path("7050", runtime_root), export_payload)

    [item] = export_payload["reviewed_items"]
    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert item["client_informed"] is True
    assert item["client_informed_method"] == "email"
    assert item["client_informed_by"] == "Jordan"
    assert item["client_informed_note"] == "Emailed client with photo/context."
    assert export_payload["counts"]["client_informed"] == 1
    assert "Client Informed" in html
    assert "Client informed by email" in html
    assert "Client informed at 2026-05-08T16:00:00+00:00" in html


def test_site_viewer_card_shows_not_yet_informed_for_maintenance_issue(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
    )
    write_approved_candidate(runtime_root, capture_id="cap-reviewed")
    export_payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T16:05:00+00:00",
    )
    site_status_export.write_site_status_export(site_status_export.default_export_path("7050", runtime_root), export_payload)

    html = render_site_page("7050", build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir))

    assert export_payload["reviewed_items"][0]["client_informed"] is False
    assert "Client not yet informed" in html


def test_reviewed_item_image_uses_photo_vision_alt_and_visible_context(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    image_path = write_capture(
        queue_dir,
        upload_dir,
        job_id="reviewed",
        site_id="7050",
        capture_id="cap-reviewed",
        timestamp="2026-05-08T10:00:00-04:00",
        area="Restrooms",
    )
    write_photo_vision_sidecar(runtime_root, image_path, upload_dir, description="A restroom sink and mirror are visible with supplies nearby.")
    write_approved_candidate(runtime_root, capture_id="cap-reviewed")
    export_payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T15:00:00+00:00",
    )
    site_status_export.write_site_status_export(site_status_export.default_export_path("7050", runtime_root), export_payload)

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert 'alt="A restroom sink and mirror are visible with supplies nearby."' in html
    assert "<strong>Visual context:</strong> A restroom sink and mirror are visible with supplies nearby." in html
    assert "Candidate ac_" in html
    assert "/media/2026-05-08/cap-reviewed/cap-reviewed-1.jpg" in html


def test_raw_capture_image_does_not_read_mac_side_photo_vision_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    image_path = write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-raw",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )
    write_photo_vision_sidecar(runtime_root, image_path, upload_dir, description="A lobby doorway and adjacent floor area are visible.")

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert 'alt="Field capture image for cap-raw"' in html
    assert "A lobby doorway and adjacent floor area are visible." not in html
    assert "Visual context:" not in html
    assert "cap-raw" in html


def test_image_without_photo_vision_uses_fallback_alt_and_no_visible_context(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-no-vision",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert 'alt="Field capture image for cap-no-vision"' in html
    assert "Visual context:" not in html


def test_visual_context_and_alt_text_do_not_expose_tokens_paths_or_internal_auth(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    image_path = write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-sensitive",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )
    write_photo_vision_sidecar(
        runtime_root,
        image_path,
        upload_dir,
        description="Bearer secret field_capture_token fct_secret /Users/jordan/private/source_image_path",
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert 'alt="Field capture image for cap-sensitive"' in html
    assert "Visual context:" not in html
    assert "Bearer" not in html
    assert "field_capture_token" not in html
    assert "fct_secret" not in html
    assert "/Users/jordan" not in html
    authored_labels = html.lower()
    assert "ai judgment" not in authored_labels
    assert "score" not in authored_labels
    assert "rating" not in authored_labels
    assert "pass/fail" not in authored_labels
    assert "best photo" not in authored_labels


def test_site_viewer_visual_context_does_not_call_ollama_or_mutate_queue_or_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    image_path = write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-no-model",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )
    sidecar_path = write_photo_vision_sidecar(runtime_root, image_path, upload_dir, description="A hallway floor area is visible.")
    before_sidecar = sidecar_path.read_text(encoding="utf-8")
    called = {"ollama": False, "processor": False}

    def fail_ollama(*_args: object, **_kwargs: object) -> None:
        called["ollama"] = True
        raise AssertionError("viewer must not construct Ollama clients")

    def fail_processor(*_args: object, **_kwargs: object) -> None:
        called["processor"] = True
        raise AssertionError("viewer must not run photo vision processing")

    monkeypatch.setattr(photo_vision, "OllamaVisionClient", fail_ollama)
    monkeypatch.setattr(photo_vision, "process_photo_assets", fail_processor)

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert "A hallway floor area is visible." not in html
    assert 'alt="Field capture image for cap-no-model"' in html
    assert called == {"ollama": False, "processor": False}
    assert sidecar_path.read_text(encoding="utf-8") == before_sidecar
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()


def test_site_viewer_still_renders_normal_captures_without_export(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-raw",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert "cap-raw" in html
    assert "Reviewed / Important Items" in html
    assert "No reviewed or important items yet." in html
    assert "Raw Capture Stream" in html
    assert html.index("Reviewed / Important Items") < html.index("Raw Capture Stream") < html.index("cap-raw")


def test_site_viewer_empty_reviewed_export_keeps_consistent_structure(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="raw",
        site_id="7050",
        capture_id="cap-raw",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
    )
    export_path = site_status_export.default_export_path("7050", runtime_root)
    site_status_export.write_site_status_export(
        export_path,
        {
            "type": "field_capture_site_viewer_status",
            "site_id": "7050",
            "generated_at": "2026-05-08T15:00:00+00:00",
            "reviewed_items": [],
            "counts": {"reviewed_items": 0},
        },
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert "Reviewed / Important Items" in html
    assert "No reviewed or important items yet." in html
    assert "Raw Capture Stream" in html
    assert "cap-raw" in html
    assert '<article class="important-card"' not in html


def test_site_viewer_marks_context_and_renders_importance_filter(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="context",
        site_id="7050",
        capture_id="cap-context",
        timestamp="2026-05-08T12:00:00-04:00",
        area="Entryways",
        note="Front door needs attention.",
        audio_count=1,
    )

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir)
    html = render_site_page("7050", payload)

    assert '<select id="importanceFilter">' in html
    assert '<option value="reviewed">Reviewed</option>' in html
    assert '<option value="context">With context</option>' in html
    assert '<option value="maintenance">Maintenance / Issues</option>' in html
    assert '<option value="requests">Supplies / Requests</option>' in html
    assert 'data-context="true"' in html
    assert "Voice note" in html
    assert "Text note" in html
    assert "Reviewed / Important Items" in html
    assert "No reviewed or important items yet." in html
    assert "Raw Capture Stream" in html


def test_reviewed_item_without_joined_media_falls_back_cleanly(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    write_approved_candidate(
        runtime_root,
        capture_id="cap-missing-media",
        summary="Review the field audio note and decide whether follow-up is needed.",
        rationale="The staff member asked for extra coverage near the office.",
        review_rationale="Staff request",
    )
    export_payload = site_status_export.build_site_status_export(
        site_id="7050",
        runtime_root=runtime_root,
        generated_at="2026-05-08T15:00:00+00:00",
    )
    site_status_export.write_site_status_export(site_status_export.default_export_path("7050", runtime_root), export_payload)
    # Known site keeps the page renderable even though this reviewed item has no raw media match.
    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir, known_site_ids=["7050"], site_status_path=site_status_export.default_export_path("7050", runtime_root))
    html = render_site_page("7050", payload)

    assert "Reviewed item - media not found" in html
    assert "cap-missing-media" in html
    assert "Raw Capture Stream" in html
    assert "No captures submitted yet." in html
    assert "Staff request" in html
    assert "The staff member asked for extra coverage near the office." in html


def test_known_site_with_zero_uploads_returns_empty_payload(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"

    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir, known_site_ids=["7050"], site_name="Summit Wire")

    assert payload == {
        "site_id": "7050",
        "site_name": "Summit Wire",
        "dates": [],
        "review_export": {},
        "important_items": [],
        "site_issues": {},
    }


def test_known_site_with_zero_uploads_renders_friendly_empty_state(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    payload = build_site_payload("7050", capture_docs_for_site(queue_dir, "7050"), upload_dir, known_site_ids=["7050"], site_name="Summit Wire")

    html = render_site_page("7050", payload)

    assert "<h1>Summit Wire Uploads</h1>" in html
    assert "Reviewed / Important Items" in html
    assert "No reviewed or important items yet." in html
    assert "Raw Capture Stream" in html
    assert "No captures submitted yet." in html
    assert "Submitted photos and voice notes will appear here during the shift." in html


def test_media_request_must_stay_under_upload_root(tmp_path: Path) -> None:
    upload_dir = tmp_path / "uploads"
    image_path = upload_dir / "2026-05-02" / "cap-one" / "photo.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")

    assert resolve_media_request("2026-05-02/cap-one/photo.jpg", upload_dir) == image_path.resolve()

    with pytest.raises(Exception):
        resolve_media_request("../outside.jpg", upload_dir)


def test_missing_site_returns_not_found_result(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue"
    upload_dir = tmp_path / "uploads"
    write_capture(
        queue_dir,
        upload_dir,
        job_id="only-site",
        site_id="7050",
        capture_id="cap-only",
        timestamp="2026-05-02T10:00:00-04:00",
        area="Entryways",
    )

    with pytest.raises(SiteUploadsNotFound):
        build_site_payload("9999", capture_docs_for_site(queue_dir, "9999"), upload_dir)


def test_unknown_site_with_zero_uploads_still_returns_not_found(tmp_path: Path) -> None:
    with pytest.raises(SiteUploadsNotFound):
        build_site_payload("9999", [], tmp_path / "uploads", known_site_ids=["7050"])
