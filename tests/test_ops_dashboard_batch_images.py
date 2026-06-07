from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import capture_ingest
from ops_dashboard import app
from ops_dashboard.sections import batch_images


def multipart_body(fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----btq-batch-images-test"
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


class FakeContext:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime_root = tmp_path
        self.audit_calls: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", b"", {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_calls.append((route, payload, result))


class FakeRegistry:
    def resolve_canonical(self, site_id: str) -> str | None:
        return "Summit Wire" if site_id == "7050" else None


def test_parse_batch_multipart_extracts_all_images() -> None:
    body, content_type = multipart_body(
        {"site_id": "7050", "person_id": "damon-smith"},
        [
            ("images", "one.jpg", "image/jpeg", b"one"),
            ("images", "two.webp", "image/webp", b"two"),
        ],
    )

    fields, images = batch_images.parse_batch_multipart(body, content_type)

    assert fields["site_id"] == "7050"
    assert fields["person_id"] == "damon-smith"
    assert [image.filename for image in images] == ["one.jpg", "two.webp"]
    assert [image.content for image in images] == [b"one", b"two"]


def test_build_submit_multipart_uses_photos_parts() -> None:
    body, content_type = batch_images.build_submit_multipart(
        {"capture_id": "cap-import-test", "person_id": "damon-smith", "source": "whatsapp_import"},
        [
            batch_images.UploadedImage("one.jpg", "image/jpeg", b"one"),
            batch_images.UploadedImage("two.png", "image/png", b"two"),
        ],
    )

    fields, uploads = capture_ingest.parse_multipart(body, content_type)

    assert fields["capture_id"] == "cap-import-test"
    assert fields["person_id"] == "damon-smith"
    assert fields["source"] == "whatsapp_import"
    assert [upload.field_name for upload in uploads] == ["photos", "photos"]
    assert [upload.content for upload in uploads] == [b"one", b"two"]


def test_handle_batch_upload_post_builds_one_submit_call(monkeypatch, tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    submit_calls: list[tuple[dict[str, str], list[batch_images.UploadedImage]]] = []
    monkeypatch.setattr(batch_images, "CouchDBSiteRegistry", FakeRegistry)
    monkeypatch.setattr(batch_images, "utc_now_iso", lambda: "2026-06-05T12:00:00Z")
    monkeypatch.setattr(batch_images, "make_capture_id", lambda site_id, exported_at: "cap-import-1337-fixed")

    def fake_submit(fields: dict[str, str], images: list[batch_images.UploadedImage]) -> dict[str, object]:
        submit_calls.append((dict(fields), list(images)))
        return {"status_code": 201, "capture_id": fields["capture_id"]}

    monkeypatch.setattr(batch_images, "submit_to_field_capture", fake_submit)
    body, content_type = multipart_body(
        {
            "site_id": "7050",
            "person_id": "damon-smith",
            "qc_category": "Restrooms",
            "captured_at": "2026-06-04T09:30",
            "note": "WhatsApp fallback",
        },
        [
            ("images", "one.jpg", "image/jpeg", b"one"),
            ("images", "two.jpg", "image/jpeg", b"two"),
        ],
    )

    status, _ctype, _body, headers = batch_images.handle_batch_upload_post(ctx, body, content_type)

    assert status == 303
    assert headers["Location"].startswith("/batch-images?")
    fields, images = submit_calls[0]
    assert fields["capture_id"] == "cap-import-1337-fixed"
    assert fields["site"] == "Summit Wire"
    assert fields["site_id"] == "7050"
    assert fields["target_id"] == "7050"
    assert fields["person_id"] == "damon-smith"
    assert fields["source"] == "whatsapp_import"
    assert fields["captured_at"] == "2026-06-04T09:30:00Z"
    assert fields["exported_at"] == "2026-06-05T12:00:00Z"
    assert len(images) == 2
    assert ctx.audit_calls[-1][2] == "success"


def test_handle_batch_upload_post_forwards_existing_capture_id(monkeypatch, tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    submit_calls: list[dict[str, str]] = []
    monkeypatch.setattr(batch_images, "CouchDBSiteRegistry", FakeRegistry)
    monkeypatch.setattr(batch_images, "utc_now_iso", lambda: "2026-06-05T12:00:00Z")

    def fake_submit(fields: dict[str, str], images: list[batch_images.UploadedImage]) -> dict[str, object]:
        submit_calls.append(dict(fields))
        return {"status_code": 200, "idempotent_replay": True, "capture_id": fields["capture_id"]}

    monkeypatch.setattr(batch_images, "submit_to_field_capture", fake_submit)
    body, content_type = multipart_body(
        {
            "capture_id": "cap-import-1337-existing",
            "site_id": "7050",
            "person_id": "damon-smith",
            "qc_category": "Restrooms",
        },
        [("images", "one.jpg", "image/jpeg", b"one")],
    )

    status, _ctype, _body, headers = batch_images.handle_batch_upload_post(ctx, body, content_type)

    assert status == 303
    assert parse_qs(urlsplit(headers["Location"]).query)["message"] == ["imported 1 photo(s) as cap-import-1337-existing"]
    assert submit_calls[0]["capture_id"] == "cap-import-1337-existing"


def test_handle_batch_upload_post_rejects_empty_image_set(monkeypatch, tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    monkeypatch.setattr(batch_images, "CouchDBSiteRegistry", FakeRegistry)
    body, content_type = multipart_body(
        {"site_id": "7050", "person_id": "damon-smith", "qc_category": "Restrooms"},
        [],
    )

    status, _ctype, _body, headers = batch_images.handle_batch_upload_post(ctx, body, content_type)

    query = parse_qs(urlsplit(headers["Location"]).query)
    assert status == 303
    assert query["error"] == ["Add at least one image."]


def test_handle_batch_upload_post_rejects_heic(monkeypatch, tmp_path: Path) -> None:
    ctx = FakeContext(tmp_path)
    monkeypatch.setattr(batch_images, "CouchDBSiteRegistry", FakeRegistry)
    body, content_type = multipart_body(
        {"site_id": "7050", "person_id": "damon-smith", "qc_category": "Restrooms"},
        [("images", "one.heic", "image/heic", b"heic")],
    )

    status, _ctype, _body, headers = batch_images.handle_batch_upload_post(ctx, body, content_type)

    query = parse_qs(urlsplit(headers["Location"]).query)
    assert status == 303
    assert query["error"] == ["Unsupported image type: image/heic"]


def test_batch_images_route_is_registered(tmp_path: Path) -> None:
    assert app.SECTION_ROUTES["/batch-images"] is batch_images
    status, _ctype, _body, _headers = app.route_response_with_headers(
        "POST",
        "/batch-images/upload",
        tmp_path,
        body=b"",
        content_type="application/octet-stream",
    )
    assert status == 303
