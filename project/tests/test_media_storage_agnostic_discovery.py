"""Gating tests for prompt 392 — storage-agnostic media discovery + serving.

INDEPENDENT VERIFIER. These tests do NOT trust the diff; they pin the observable
contract the prompt promised:

  1. Parity, CouchDB path: a ``btq_field_captures`` doc enumerating photos/audio
     resolves (via the LOCAL store) to the exact pre-392 ``/media/{date}/{cid}/
     {filename}`` URLs, with correct image/audio classification + filename order.
  2. Parity, dual-read fallback: media on disk + NO field_capture doc (or CouchDB
     unavailable / URLError) + LOCAL store still discovers the on-disk media
     identically to the old FS glob; no crash on CouchDB error.
  3. Storage-agnostic + serving: a STUB s3-shaped store (NOT LocalFilesystemStore;
     ``url_for`` -> sentinel presigned URL) makes discovery resolve to the stub's
     presigned URLs, takes NO fs fallback, and ``serve_media_response`` 302-
     redirects with Location == the presigned URL + empty body; local stays 200.
  4. Security: stored_path escaping upload_root is rejected; the presigned URL is
     never written to any log record on the serve path or discovery (keys may).
  5. No flip: media_store default stays "local"; *.json artifact discovery
     untouched (still a filesystem glob).

The CouchDB transport is faked by patching ``common.query_couchdb_find`` + the
config helpers (the same surface the shipped ``couchdb_job_draft_review`` double
patches), so the REAL discovery/key/url logic runs under test. The hermetic
guard in the repo-root conftest already blocks any real :5984 call.
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path
from urllib.error import URLError

import pytest

from event_pipeline import couchdb_config
from media_store import LocalFilesystemStore
from ops_dashboard import app as app_module
from ops_dashboard import common


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
class _StubPresignedStore:
    """An s3-shaped store that is NOT a LocalFilesystemStore.

    ``url_for`` returns a sentinel presigned URL (the thing that must never be
    logged and that discovery/serving must surface verbatim). ``exists`` is True
    so the serve path proceeds. ``read`` must never be called on the non-local
    serve path (302 short-circuits before bytes).
    """

    PRESIGNED = "https://r2.example/PRESIGNED?sig=DEADBEEF"

    def __init__(self) -> None:
        self.url_for_calls: list[str] = []

    def url_for(self, key: str) -> str:
        self.url_for_calls.append(key)
        return self.PRESIGNED

    def exists(self, key: str) -> bool:
        return True

    def read(self, key: str) -> bytes:  # pragma: no cover - must not run on 302 path
        raise AssertionError("non-local serve path must NOT read bytes")


def _config():
    return couchdb_config.CouchDBConfig("http://fake-392", "u", "p", 1.0, 1000)


def _install_couch_docs(monkeypatch, docs, *, raise_exc=None):
    """Patch the field-capture discovery transport to return ``docs``.

    Mirrors the shipped review double: patch the config resolution + DB name +
    the ``query_couchdb_find`` binding that ``common`` imported at module load.
    If ``raise_exc`` is set, the find call raises it (CouchDB-unavailable path).
    """
    monkeypatch.setattr(couchdb_config, "from_env", lambda *a, **k: _config())
    monkeypatch.setattr(
        couchdb_config, "field_captures_database", lambda *a, **k: "btq_field_captures"
    )

    def _fake_find(config, database, selector):
        if raise_exc is not None:
            raise raise_exc
        return {"docs": list(docs)}

    monkeypatch.setattr(common, "query_couchdb_find", _fake_find)


def _write_capture_files(uploads: Path, date: str, capture_id: str, filenames):
    cap_dir = uploads / date / capture_id
    cap_dir.mkdir(parents=True, exist_ok=True)
    for name in filenames:
        (cap_dir / name).write_bytes(b"\xff\xd8\xff\xe0fake" + name.encode())
    return cap_dir


def _capture_doc(date: str, capture_id: str, photos, audio=()):
    return {
        "_id": capture_id,
        "type": "field_capture",
        "capture_id": capture_id,
        "captured_at": f"{date}T08:00:00Z",
        "photos": [
            {"filename": name, "stored_path": f"runtime/uploads/{date}/{capture_id}/{name}"}
            for name in photos
        ],
        "audio": [
            {"filename": name, "stored_path": f"runtime/uploads/{date}/{capture_id}/{name}"}
            for name in audio
        ],
    }


# --------------------------------------------------------------------------- #
# Criterion 1 — parity on the CouchDB path (local store)
# --------------------------------------------------------------------------- #
def test_couchdb_path_resolves_to_local_media_urls(monkeypatch, tmp_path):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-10", "cap-7050"
    # Files need not exist for the CouchDB path, but write them so this is a
    # faithful end-to-end shape.
    _write_capture_files(uploads, date, cid, ["img-002.jpg", "img-001.jpg", "memo.m4a"])
    doc = _capture_doc(date, cid, ["img-002.jpg", "img-001.jpg"], audio=["memo.m4a"])
    _install_couch_docs(monkeypatch, [doc])

    # capture_thumbnails -> images only, sorted by filename, /media/ local URLs.
    thumbs = common.capture_thumbnails(runtime, cid)
    assert thumbs == [
        f"/media/{date}/{cid}/img-001.jpg",
        f"/media/{date}/{cid}/img-002.jpg",
    ]

    # field_capture_media_records -> both media, classified, local URLs.
    records = common.field_capture_media_records(runtime, cid)
    by_name = {r["filename"]: r for r in records}
    assert by_name["img-001.jpg"]["media_type"] == "photo"
    assert by_name["memo.m4a"]["media_type"] == "audio"
    assert by_name["img-001.jpg"]["url"] == f"/media/{date}/{cid}/img-001.jpg"
    assert by_name["memo.m4a"]["url"] == f"/media/{date}/{cid}/memo.m4a"

    # latest_uploads -> dict shape the callers depend on, classification correct.
    uploads_list = common.latest_uploads(uploads, limit=5)
    assert len(uploads_list) == 1
    row = uploads_list[0]
    assert set(row) >= {
        "capture_id",
        "submitter_name",
        "submitter_id",
        "date",
        "path",
        "file_count",
        "audio_count",
        "image_count",
    }
    assert row["capture_id"] == cid
    assert row["date"] == date
    assert row["file_count"] == 3
    assert row["image_count"] == 2
    assert row["audio_count"] == 1


# --------------------------------------------------------------------------- #
# Criterion 2 — dual-read FS fallback (local store), no crash on CouchDB error
# --------------------------------------------------------------------------- #
def test_dual_read_fallback_when_no_couch_doc(monkeypatch, tmp_path):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-10", "cap-fallback"
    _write_capture_files(uploads, date, cid, ["img-001.jpg", "img-002.jpg", "memo.m4a"])
    # CouchDB returns ZERO records.
    _install_couch_docs(monkeypatch, [])

    thumbs = common.capture_thumbnails(runtime, cid)
    assert thumbs == [
        f"/media/{date}/{cid}/img-001.jpg",
        f"/media/{date}/{cid}/img-002.jpg",
    ]

    records = common.field_capture_media_records(runtime, cid)
    kinds = {r["filename"]: r["media_type"] for r in records}
    assert kinds == {"img-001.jpg": "photo", "img-002.jpg": "photo", "memo.m4a": "audio"}

    rows = common.latest_uploads(uploads, limit=5)
    assert len(rows) == 1
    assert rows[0]["image_count"] == 2
    assert rows[0]["audio_count"] == 1


def test_dual_read_fallback_when_couchdb_unavailable_urlerror(monkeypatch, tmp_path):
    """A CouchDB URLError must NOT crash discovery; local fallback still serves."""
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-11", "cap-down"
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])
    _install_couch_docs(monkeypatch, [], raise_exc=URLError("connection refused"))

    thumbs = common.capture_thumbnails(runtime, cid)
    assert thumbs == [f"/media/{date}/{cid}/img-001.jpg"]
    rows = common.latest_uploads(uploads, limit=5)
    assert rows and rows[0]["capture_id"] == cid


def test_swipe_parity_regression_guard_still_passes(monkeypatch, tmp_path):
    """Re-assert the cross-file parity the prompt names: with media on disk and
    NO field_capture doc, the swipe-card thumbnail contract is the old
    /media/{date}/{cid}/{filename} URL (what test_swipe_review exercises)."""
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-10", "cap-test-7050"
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])
    _install_couch_docs(monkeypatch, [])
    assert common.capture_thumbnails(runtime, cid) == [f"/media/{date}/{cid}/img-001.jpg"]


# --------------------------------------------------------------------------- #
# Criterion 3 — storage-agnostic discovery + non-local serving (302 redirect)
# --------------------------------------------------------------------------- #
def test_discovery_resolves_presigned_urls_for_non_local_store(monkeypatch, tmp_path):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-12", "cap-r2"
    # No files on disk: proves the non-local path does NOT depend on the FS.
    doc = _capture_doc(date, cid, ["img-001.jpg"], audio=["memo.m4a"])
    _install_couch_docs(monkeypatch, [doc])

    stub = _StubPresignedStore()
    # Inject the stub store explicitly (the API surface the prompt provides).
    thumbs = common.capture_thumbnails(runtime, cid, media_store=stub)
    assert thumbs == [_StubPresignedStore.PRESIGNED]

    records = common.field_capture_media_records(runtime, cid, media_store=stub)
    assert {r["url"] for r in records} == {_StubPresignedStore.PRESIGNED}
    # Discovery surfaced the canonical keys, not the presigned URL, as the key.
    assert {r["key"] for r in records} == {
        f"{date}/{cid}/img-001.jpg",
        f"{date}/{cid}/memo.m4a",
    }


def test_media_url_for_key_prefers_local_file_over_non_local_store(tmp_path):
    uploads = tmp_path / "uploads"
    date, cid = "2026-07-07", "cap-local-first"
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])

    stub = _StubPresignedStore()
    key = f"{date}/{cid}/img-001.jpg"
    assert common._media_url_for_key(key, uploads, stub) == f"/media/{key}"
    assert stub.url_for_calls == []


def test_media_url_for_key_falls_back_to_non_local_store_when_missing(tmp_path):
    uploads = tmp_path / "uploads"
    stub = _StubPresignedStore()
    key = "2026-07-07/cap-r2-only/img-001.jpg"

    assert common._media_url_for_key(key, uploads, stub) == _StubPresignedStore.PRESIGNED
    assert stub.url_for_calls == [key]


def test_media_url_for_key_empty_and_stat_error_preserve_blank_return(tmp_path, caplog):
    uploads = tmp_path / "uploads"
    stub = _StubPresignedStore()

    assert common._media_url_for_key("", uploads, stub) == ""

    with caplog.at_level(logging.WARNING):
        assert common._media_url_for_key("../secret.jpg", uploads, stub) == ""

    assert stub.url_for_calls == []
    assert any(
        "media url resolution failed for key=../secret.jpg" in rec.getMessage()
        for rec in caplog.records
    )
    assert _StubPresignedStore.PRESIGNED not in "\n".join(
        rec.getMessage() for rec in caplog.records
    )


def test_non_local_store_takes_no_filesystem_fallback(monkeypatch, tmp_path):
    """Files exist on disk, but with a NON-local store and zero CouchDB docs the
    discovery must NOT silently fall back to the on-disk glob (only local does)."""
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-12", "cap-nofallback"
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])
    _install_couch_docs(monkeypatch, [])  # zero docs

    stub = _StubPresignedStore()
    assert common.capture_thumbnails(runtime, cid, media_store=stub) == []
    assert common.field_capture_media_records(runtime, cid, media_store=stub) == []
    assert common.latest_uploads(uploads, limit=5, media_store=stub) == []


def test_serve_media_response_redirects_for_non_local_store(monkeypatch, tmp_path):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-12", "cap-serve"
    # The file must exist on disk so resolve_media_request resolves the path.
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])

    stub = _StubPresignedStore()
    monkeypatch.setattr(app_module, "get_media_store", lambda upload_dir: stub)

    status, ctype, body, headers = app_module.serve_media_response(
        f"{date}/{cid}/img-001.jpg", runtime
    )
    assert status == HTTPStatus.FOUND
    assert headers.get("Location") == _StubPresignedStore.PRESIGNED
    assert body == b""


def test_serve_media_response_local_returns_bytes(monkeypatch, tmp_path):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-12", "cap-local-serve"
    cap_dir = _write_capture_files(uploads, date, cid, ["img-001.jpg"])
    expected = (cap_dir / "img-001.jpg").read_bytes()

    # Default store is local; serve returns 200 + the actual bytes (no Location).
    status, ctype, body, headers = app_module.serve_media_response(
        f"{date}/{cid}/img-001.jpg", runtime
    )
    assert status == HTTPStatus.OK
    assert body == expected
    assert "Location" not in headers


# --------------------------------------------------------------------------- #
# Criterion 4a — security: escaping stored_path rejected
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../../secret",
        "uploads/../../../etc/passwd",
        "/media/../../etc/passwd",
        "",
    ],
)
def test_media_key_rejects_escaping_stored_path(bad, tmp_path):
    upload_root = (tmp_path / "uploads").resolve()
    with pytest.raises(ValueError):
        common.media_key_from_stored_path(bad, upload_root)


def test_media_key_accepts_legitimate_shapes(tmp_path):
    upload_root = (tmp_path / "uploads").resolve()
    expected = "2026-06-10/cap1/img.jpg"
    assert (
        common.media_key_from_stored_path(str(upload_root / expected), upload_root) == expected
    )
    assert (
        common.media_key_from_stored_path(f"runtime/uploads/{expected}", upload_root) == expected
    )
    assert common.media_key_from_stored_path(f"/media/{expected}", upload_root) == expected


# --------------------------------------------------------------------------- #
# Criterion 4b — the presigned URL is never logged (keys may be)
# --------------------------------------------------------------------------- #
def test_presigned_url_never_logged_on_serve_path(monkeypatch, tmp_path, caplog):
    runtime = tmp_path
    uploads = runtime / "uploads"
    date, cid = "2026-06-12", "cap-log-serve"
    _write_capture_files(uploads, date, cid, ["img-001.jpg"])
    stub = _StubPresignedStore()
    monkeypatch.setattr(app_module, "get_media_store", lambda upload_dir: stub)

    with caplog.at_level(logging.DEBUG):
        status, _ctype, _body, headers = app_module.serve_media_response(
            f"{date}/{cid}/img-001.jpg", runtime
        )
    assert status == HTTPStatus.FOUND
    assert headers["Location"] == _StubPresignedStore.PRESIGNED
    # The presigned URL went out on the wire (Location) but never into a log.
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert _StubPresignedStore.PRESIGNED not in blob


def test_presigned_url_never_logged_during_discovery(monkeypatch, tmp_path, caplog):
    runtime = tmp_path
    date, cid = "2026-06-12", "cap-log-disc"
    doc = _capture_doc(date, cid, ["img-001.jpg"])
    _install_couch_docs(monkeypatch, [doc])
    stub = _StubPresignedStore()

    with caplog.at_level(logging.DEBUG):
        records = common.field_capture_media_records(runtime, cid, media_store=stub)
    assert records and records[0]["url"] == _StubPresignedStore.PRESIGNED
    blob = "\n".join(rec.getMessage() for rec in caplog.records)
    assert _StubPresignedStore.PRESIGNED not in blob
    # The KEY is allowed to appear in logs; assert nothing forbids that (sanity:
    # we did not over-constrain by banning the key).
    # (no assertion needed — absence of presigned is the contract.)


# --------------------------------------------------------------------------- #
# Criterion 5 — no flip; *.json artifact discovery untouched (FS glob)
# --------------------------------------------------------------------------- #
def test_media_store_default_is_local():
    from instance_config import DEFAULT_MEDIA_STORE

    assert DEFAULT_MEDIA_STORE == "local"


def test_json_artifact_discovery_is_still_a_filesystem_glob(monkeypatch, tmp_path):
    """*.json sidecar/intake artifacts are NOT media; their discovery must remain
    a plain filesystem glob, unaffected by CouchDB media routing."""
    artifacts = tmp_path / "intake"
    artifacts.mkdir()
    (artifacts / "a.json").write_text("{}")
    (artifacts / "b.json").write_text("{}")
    # Even with CouchDB raising, the json discovery is FS-only and must work.
    _install_couch_docs(monkeypatch, [], raise_exc=URLError("down"))
    found = common.latest_json_artifacts(artifacts, limit=5)
    names = {Path(str(item.get("path") or item.get("name") or "")).name for item in found}
    assert {"a.json", "b.json"} <= names or len(found) == 2
