"""Independent verifier tests for the /field-photos/export SafetyCulture bridge.

Focus: path-traversal safety (the gating security requirement) and that the
export is strictly read-only. These intentionally drive the real
``route_response_with_headers`` entry point and use only synthetic fixtures.
"""

from __future__ import annotations

import zipfile
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

import pytest

import ops_dashboard.app as ops_app
from ops_dashboard.sections import field_photos


class _RealishStore:
    """Reads straight off disk under upload_root, recording every read.

    Deliberately does NOT re-validate keys, so the ONLY thing standing between
    a client-supplied key and an arbitrary file read is the handler's call to
    ``resolve_media_request``. If that guard is bypassed, the traversal tests
    below will surface the leaked bytes.
    """

    def __init__(self, upload_root: Path) -> None:
        self.upload_root = upload_root
        self.reads: list[str] = []
        self.writes: list[str] = []

    def write(self, key: str, data: bytes) -> None:  # pragma: no cover - guarded
        self.writes.append(key)
        raise AssertionError("export must not write to the media store")

    def read(self, key: str) -> bytes:
        self.reads.append(key)
        return (self.upload_root / key).read_bytes()

    def exists(self, key: str) -> bool:
        return (self.upload_root / key).is_file()

    def url_for(self, key: str) -> str:
        return f"/media/{key}"


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    upload_root = (runtime_root / "uploads").resolve()
    upload_root.mkdir(parents=True)
    # A legitimate in-store photo.
    photo_key = "2026-06-25/cap-walk/photo-1.jpg"
    (upload_root / "2026-06-25" / "cap-walk").mkdir(parents=True)
    (upload_root / photo_key).write_bytes(b"jpeg-bytes")

    # A secret OUTSIDE the store. If any traversal key escapes, this leaks.
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP-SECRET-OUT-OF-STORE")

    store = _RealishStore(upload_root)
    monkeypatch.setattr(field_photos, "get_media_store", lambda _upload_dir: store)
    return runtime_root, store, photo_key, secret


def _post(runtime_root: Path, params):
    return ops_app.route_response_with_headers(
        "POST",
        "/field-photos/export",
        runtime_root,
        urlencode(params, doseq=True).encode(),
    )


# Keys that contain `..` segments and MUST raise UnsafeMediaPath -> 400.
TRAVERSAL_REJECTED_KEYS = [
    "../../etc/passwd",
    "../secret.txt",
    "../../secret.txt",
    "2026-06-25/cap-walk/../../../secret.txt",
    "..%2F..%2Fsecret.txt",  # URL-encoded traversal
    "%2e%2e%2f%2e%2e%2fsecret.txt",  # fully encoded ../../
    "/media/../../secret.txt",
]

# Absolute paths are neutralized by lstrip('/') -> re-rooted INSIDE the store,
# so they cannot escape; they resolve to a (typically non-existent) in-store
# key. The security property here is "no out-of-store read", NOT the 400 code.
ABSOLUTE_NEUTRALIZED_KEYS = [
    "/etc/passwd",
    "/Users/secret.txt",
]


@pytest.mark.parametrize("evil_key", TRAVERSAL_REJECTED_KEYS)
def test_traversal_key_rejected_before_any_read(world, evil_key):
    runtime_root, store, _photo_key, secret = world
    status, _ct, body, _headers = _post(runtime_root, {"media_key": evil_key})

    # Rejected with 400 unsafe_media_key, and NOTHING was read from the store.
    assert status == HTTPStatus.BAD_REQUEST, (evil_key, status, body)
    assert b"unsafe_media_key" in body, (evil_key, body)
    assert store.reads == [], (evil_key, store.reads)
    assert store.writes == []
    # The out-of-store secret never appears in the response.
    assert secret.read_bytes() not in body


@pytest.mark.parametrize("evil_key", ABSOLUTE_NEUTRALIZED_KEYS)
def test_absolute_path_neutralized_never_escapes_store(world, evil_key):
    """Absolute keys re-root inside the store; the real OS file is never read."""
    runtime_root, store, _photo_key, secret = world
    status, _ct, body, _headers = _post(runtime_root, {"media_key": evil_key})

    # Never a 200 (would mean an out-of-store file was served).
    assert status != HTTPStatus.OK, (evil_key, status)
    # The handler may report 404 (re-rooted, in-store, non-existent) — that is
    # safe. The hard requirement: no read escaped the store and no real secret
    # bytes leaked.
    assert secret.read_bytes() not in body
    # Any read the store DID see must be a relative, in-store key (never the
    # absolute OS path).
    for read_key in store.reads:
        assert not read_key.startswith("/"), (evil_key, read_key)
        resolved = (store.upload_root / read_key).resolve()
        assert str(resolved).startswith(str(store.upload_root)), (evil_key, resolved)
    assert store.writes == []


def test_traversal_mixed_with_valid_key_still_rejected(world):
    """A valid key alongside a malicious one must abort the whole export."""
    runtime_root, store, photo_key, secret = world
    status, _ct, body, _headers = _post(
        runtime_root, {"media_key": [photo_key, "../../secret.txt"]}
    )
    assert status == HTTPStatus.BAD_REQUEST
    assert b"unsafe_media_key" in body
    # No zip produced, nothing read (even the legit key is not served).
    assert store.reads == []
    assert secret.read_bytes() not in body


def test_valid_export_serves_only_in_store_bytes(world):
    runtime_root, store, photo_key, _secret = world
    status, content_type, body, headers = _post(
        runtime_root, {"capture_id": "cap-walk", "media_key": photo_key}
    )
    assert status == HTTPStatus.OK
    assert content_type == "application/zip"
    assert headers.get("Cache-Control") == "no-store"
    with zipfile.ZipFile(BytesIO(body), "r") as archive:
        names = archive.namelist()
        assert len(names) == 1
        assert archive.read(names[0]) == b"jpeg-bytes"
    assert store.reads == [photo_key]
    assert store.writes == []


def test_media_prefix_is_stripped_for_valid_key(world):
    """Keys posted with the /media/ prefix resolve to the same in-store file."""
    runtime_root, store, photo_key, _secret = world
    status, _ct, body, _headers = _post(
        runtime_root, {"media_key": f"/media/{photo_key}"}
    )
    assert status == HTTPStatus.OK
    assert store.reads == [photo_key]


def test_empty_selection_is_graceful_not_500(world):
    runtime_root, store, _photo_key, _secret = world
    for params in ({}, {"media_key": ""}, {"media_key": ["", "  "]}):
        status, _ct, body, _headers = _post(runtime_root, params)
        assert status == HTTPStatus.BAD_REQUEST, (params, status)
        assert b"no_photos_selected" in body, (params, body)
        assert store.reads == []


def test_missing_in_store_key_is_404_not_traversal(world):
    """A safe-but-absent key is 404 media_not_found, distinct from traversal."""
    runtime_root, store, _photo_key, _secret = world
    status, _ct, body, _headers = _post(
        runtime_root, {"media_key": "2026-06-25/cap-walk/does-not-exist.jpg"}
    )
    assert status == HTTPStatus.NOT_FOUND
    assert b"media_not_found" in body
    assert store.writes == []


def test_zip_entry_names_unique_for_duplicate_hints(world):
    """Two selections with colliding filename hints get de-duplicated names."""
    runtime_root, store, photo_key, _secret = world
    # add a second real photo
    second = "2026-06-25/cap-walk/photo-2.jpg"
    (store.upload_root / second).write_bytes(b"jpeg-two")
    params = {
        "media_key": [photo_key, second],
        "filename_hint": [
            f"{photo_key}\tsame.jpg",
            f"{second}\tsame.jpg",
        ],
    }
    status, _ct, body, _headers = _post(runtime_root, params)
    assert status == HTTPStatus.OK
    with zipfile.ZipFile(BytesIO(body), "r") as archive:
        names = archive.namelist()
    assert len(names) == len(set(names)) == 2
