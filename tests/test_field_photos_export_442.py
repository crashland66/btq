from __future__ import annotations

import zipfile
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import ops_dashboard.app as ops_app
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos


def _ctx(runtime_root: Path) -> SectionContext:
    vault = runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


def test_field_photos_selector_includes_capture_id_with_existing_filters() -> None:
    mango = field_photos._build_mango_selector(
        "restroom",
        "7050",
        "restroom",
        "2026-06-01",
        "2026-06-25",
        capture_id="cap-qc-walk",
    )

    clauses = mango["selector"]["$and"]
    assert {"doc_type": "photo_vision_sidecar"} in clauses
    assert {"capture_id": "cap-qc-walk"} in clauses
    assert {"site_id": "7050"} in clauses
    assert {"area_guess": "restroom"} in clauses
    assert {"search_text": {"$regex": "restroom"}} in clauses
    assert {"generated_at": {"$gte": "2026-06-01", "$lte": "2026-06-25Z"}} in clauses


def test_field_photos_render_has_selection_checkbox_description_and_export(tmp_path: Path, monkeypatch) -> None:
    doc = {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": "cap-qc-walk",
        "photo_id": "photo-1",
        "photo_asset_id": "fcp-1",
        "site_id": "7050",
        "generated_at": "2026-06-25T12:00:00Z",
        "description": "Floor machine and restroom threshold are visible.",
        "area_guess": "restroom",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "provenance": {"image_media_url": "/media/2026-06-25/cap-qc-walk/photo-1.jpg"},
    }
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(field_photos, "_query_couchdb", lambda _cfg, _mango: [doc])
    monkeypatch.setattr(field_photos, "_query_processed_asset_ids", lambda _cfg: {"fcp-1"})
    monkeypatch.setattr(field_photos, "_query_terminal_capture_ids", lambda _cfg: set())
    monkeypatch.setattr(field_photos, "_load_site_options", lambda: [])
    monkeypatch.setattr(field_photos, "_load_area_options", lambda _cfg: ["restroom"])
    ctx = _ctx(tmp_path / "runtime")
    ctx.query = {"capture_id": ["cap-qc-walk"]}

    html = field_photos.render(ctx)

    assert 'name="capture_id" value="cap-qc-walk"' in html
    assert 'type="checkbox" name="media_key"' in html
    assert 'value="2026-06-25/cap-qc-walk/photo-1.jpg"' in html
    assert 'aria-label="Select photo:' in html
    assert "Floor machine and restroom threshold are visible." in html
    assert "Download selected as JPEGs" in html


class _RecordingStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.reads: list[str] = []
        self.writes: list[str] = []

    def write(self, key: str, data: bytes) -> None:
        self.writes.append(key)
        raise AssertionError("export must not write to the media store")

    def read(self, key: str) -> bytes:
        self.reads.append(key)
        return self.payloads[key]

    def exists(self, key: str) -> bool:
        return key in self.payloads

    def url_for(self, key: str) -> str:
        return f"/media/{key}"


def test_field_photos_export_returns_zip_of_selected_jpegs(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "2026-06-25/cap-qc-walk/photo-1.jpg": b"jpeg-one",
        "2026-06-25/cap-qc-walk/photo-2.jpg": b"jpeg-two",
    }
    store = _RecordingStore(payloads)
    monkeypatch.setattr(field_photos, "get_media_store", lambda _upload_dir: store)

    body = urlencode(
        {
            "capture_id": "cap-qc-walk",
            "media_key": list(payloads),
            "filename_hint": [
                "2026-06-25/cap-qc-walk/photo-1.jpg\tcap-qc-walk-restroom-001-photo-1.jpg",
                "2026-06-25/cap-qc-walk/photo-2.jpg\tcap-qc-walk-restroom-002-photo-2.jpg",
            ],
        },
        doseq=True,
    ).encode()

    status, content_type, body_bytes, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-photos/export",
        tmp_path / "runtime",
        body,
    )

    assert status == HTTPStatus.OK
    assert content_type == "application/zip"
    assert headers["Content-Disposition"] == 'attachment; filename="qc-photos-cap-qc-walk.zip"'
    with zipfile.ZipFile(BytesIO(body_bytes), "r") as archive:
        assert archive.namelist() == [
            "cap-qc-walk-restroom-001-photo-1.jpg",
            "cap-qc-walk-restroom-002-photo-2.jpg",
        ]
        assert archive.read("cap-qc-walk-restroom-001-photo-1.jpg") == b"jpeg-one"
        assert archive.read("cap-qc-walk-restroom-002-photo-2.jpg") == b"jpeg-two"
    assert store.reads == list(payloads)
    assert store.writes == []


def test_field_photos_export_rejects_traversal_before_store_read(tmp_path: Path, monkeypatch) -> None:
    store = _RecordingStore({})
    monkeypatch.setattr(field_photos, "get_media_store", lambda _upload_dir: store)

    status, _content_type, body, _headers = ops_app.route_response_with_headers(
        "POST",
        "/field-photos/export",
        tmp_path / "runtime",
        urlencode({"media_key": "../../etc/passwd"}).encode(),
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert b"unsafe_media_key" in body
    assert store.reads == []
    assert store.writes == []


def test_field_photos_export_is_read_only_and_bypasses_mutating_post_dispatch(tmp_path: Path, monkeypatch) -> None:
    key = "2026-06-25/cap-qc-walk/photo-1.jpg"
    store = _RecordingStore({key: b"jpeg-one"})
    monkeypatch.setattr(field_photos, "get_media_store", lambda _upload_dir: store)

    def fail_dispatch(*_args, **_kwargs):
        raise AssertionError("export should not use the mutating POST route dispatcher")

    monkeypatch.setattr(ops_app, "dispatch_post_route", fail_dispatch)

    status, _content_type, _body, _headers = ops_app.route_response_with_headers(
        "POST",
        "/field-photos/export",
        tmp_path / "runtime",
        urlencode({"media_key": key}).encode(),
    )

    assert status == HTTPStatus.OK
    assert store.writes == []
