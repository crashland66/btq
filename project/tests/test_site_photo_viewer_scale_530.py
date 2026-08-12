"""Independent gates for the viewer's production-scale read path (530).

Authored by the verifier from the 530 contract: view-backed vision join
(complete-or-raise), bounded-parallel media availability with per-key
degradation, and the 120 s per-site corpus cache that must key on site —
never the token — and expire.
"""

from __future__ import annotations

import io
import json
import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest

from field_capture import photo_vision_couchdb
from site_photo_viewer import server as viewer_server
from site_photo_viewer.read_model import VisionByCaptureProjection
from site_photo_viewer.server import (
    SitePhotoCorpusCache,
    ViewerDependencies,
    resolve_page_media_availability,
    route_response,
)
from token_store import TokenRecord


UPLOAD_ROOT = Path("/srv/example/uploads")


class _FakeViewResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Config:
    base_url = "http://127.0.0.1:5984"
    timeout = 5

    def auth_header(self) -> dict[str, str]:
        return {}


def _row(capture_id: str, doc_id: str, photo_id: str, **fields: str) -> dict[str, object]:
    value = {
        "photo_id": photo_id,
        "filename": fields.get("filename", ""),
        "source_image_path": fields.get("source_image_path", ""),
        "description": fields.get("description", ""),
        "summary": fields.get("summary", ""),
        "area_guess": fields.get("area_guess", ""),
        "qc_category": fields.get("qc_category", ""),
    }
    return {"id": doc_id, "key": capture_id, "value": value}


# --------------------------------------------------------------------------- #
# View reader
# --------------------------------------------------------------------------- #


def test_view_rows_group_sort_and_carry_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "rows": [
            _row("cap_1", "v2", "zzz.jpg", summary="later"),
            _row("cap_1", "v1", "aaa.jpg", summary="earlier"),
            _row("cap_2", "v3", "b.jpg"),
        ]
    }
    monkeypatch.setattr(
        photo_vision_couchdb.request, "urlopen", lambda req, timeout: _FakeViewResponse(payload)
    )
    grouped = photo_vision_couchdb.query_photo_vision_view_by_capture_ids(
        _Config(), ["cap_1", "cap_2"], database="btq_photo_vision"
    )
    assert [doc["photo_id"] for doc in grouped["cap_1"]] == ["aaa.jpg", "zzz.jpg"]
    assert grouped["cap_1"][0]["_id"] == "v1"
    assert grouped["cap_1"][0]["capture_id"] == "cap_1"
    assert len(grouped["cap_2"]) == 1


def test_view_reader_raises_on_malformed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {"rows": [{"id": "v1", "key": "cap_1", "value": {"photo_id": "a"}}]}  # missing fields
    monkeypatch.setattr(
        photo_vision_couchdb.request, "urlopen", lambda req, timeout: _FakeViewResponse(bad)
    )
    with pytest.raises(photo_vision_couchdb.PhotoVisionCouchDBError):
        photo_vision_couchdb.query_photo_vision_view_by_capture_ids(
            _Config(), ["cap_1"], database="btq_photo_vision"
        )


def test_view_reader_raises_on_unrequested_key(monkeypatch: pytest.MonkeyPatch) -> None:
    stray = {"rows": [_row("cap_other", "v9", "x.jpg")]}
    monkeypatch.setattr(
        photo_vision_couchdb.request, "urlopen", lambda req, timeout: _FakeViewResponse(stray)
    )
    with pytest.raises(photo_vision_couchdb.PhotoVisionCouchDBError):
        photo_vision_couchdb.query_photo_vision_view_by_capture_ids(
            _Config(), ["cap_1"], database="btq_photo_vision"
        )


def test_view_reader_posts_all_keys_in_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def fake_urlopen(req, timeout):  # noqa: ANN001
        seen.append(json.loads(req.data.decode()))
        return _FakeViewResponse({"rows": []})

    monkeypatch.setattr(photo_vision_couchdb.request, "urlopen", fake_urlopen)
    photo_vision_couchdb.query_photo_vision_view_by_capture_ids(
        _Config(), ["b", "a", "a", " "], database="btq_photo_vision"
    )
    assert seen == [{"keys": ["a", "b"]}]


def test_projected_rows_satisfy_match_without_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"rows": [_row("cap_1", "v1", "2026-08-10/cap_1/a1.jpg", summary="hit")]}
    monkeypatch.setattr(
        photo_vision_couchdb.request, "urlopen", lambda req, timeout: _FakeViewResponse(payload)
    )
    projection = VisionByCaptureProjection.fetch_from_view(
        _Config(), ["cap_1"], database="btq_photo_vision"
    )

    class _Photo:
        capture_id = "cap_1"
        upload_id = "2026-08-10/cap_1/a1.jpg"
        media_key = "2026-08-10/cap_1/a1.jpg"
        filename = "a1.jpg"

    matched = projection.match(_Photo())
    assert matched is not None and matched["summary"] == "hit"


# --------------------------------------------------------------------------- #
# Parallel availability
# --------------------------------------------------------------------------- #


class _Page:
    """Minimal stand-in exposing the SitePhotoPage seam the resolver uses."""


def _real_page(keys: list[str | None]):
    from site_photo_viewer.read_model import (
        CapturePhotoProjection,
        SitePhotoCorpus,
        SitePhotoPage,
        VisionByCaptureProjection as VBP,
    )

    photos = []
    for i, key in enumerate(keys):
        photo = {"filename": f"p{i}.jpg"}
        if key is None:
            photo["stored_path"] = "../../escape"
        else:
            photo["upload_id"] = key
        photos.append(photo)
    captures = [
        {
            "capture_id": "cap_1",
            "site_id": "site_a",
            "captured_at": "2026-08-10T12:00:00-04:00",
            "category": "site",
            "photos": photos,
        }
    ]
    rows = CapturePhotoProjection.from_capture_rows(
        captures, site_id="site_a", site_label="site_a", upload_root=UPLOAD_ROOT
    )
    corpus = SitePhotoCorpus.join(rows, VBP.from_mapping({}))
    return SitePhotoPage.from_corpus(corpus)


def test_parallel_resolver_mixes_states_and_degrades_per_key() -> None:
    calls: list[str] = []
    lock = threading.Lock()

    def exists(key: str) -> bool:
        with lock:
            calls.append(key)
        if key.endswith("boom.jpg"):
            raise OSError("HEAD failed")
        return key.endswith("ok.jpg")

    page = _real_page(["d/ok.jpg", "d/gone.jpg", "d/boom.jpg", None])
    resolved_page, resolved = resolve_page_media_availability(page, exists, {})
    states = {photo.filename: photo.availability_state for photo in resolved_page.photos}
    assert states["p0.jpg"] == "available"
    assert states["p1.jpg"] == "unavailable"
    assert states["p2.jpg"] == "check_failed"
    assert states["p3.jpg"] == "invalid_reference"
    assert resolved == {"d/ok.jpg": True, "d/gone.jpg": False}
    assert sorted(calls) == ["d/boom.jpg", "d/gone.jpg", "d/ok.jpg"]


def test_cached_availability_skips_repeat_heads() -> None:
    calls: list[str] = []

    def exists(key: str) -> bool:
        calls.append(key)
        return True

    page = _real_page(["d/ok.jpg", "d/two.jpg"])
    _, resolved = resolve_page_media_availability(page, exists, {"d/ok.jpg": True})
    assert calls == ["d/two.jpg"]
    assert resolved == {"d/two.jpg": True}


# --------------------------------------------------------------------------- #
# Corpus cache
# --------------------------------------------------------------------------- #


def test_cache_expires_and_never_keys_on_token(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = SitePhotoCorpusCache()
    corpus = _real_page(["d/a.jpg"])  # any object works for identity checks
    cache.store("db", "site_a", corpus)
    assert cache.get("db", "site_a") is not None
    for key in cache._entries:  # structural: keys are (database, site_id) only
        assert key == ("db", "site_a")

    real_monotonic = time.monotonic
    monkeypatch.setattr(
        viewer_server.time, "monotonic", lambda: real_monotonic() + viewer_server.CORPUS_CACHE_TTL_SECONDS + 1
    )
    assert cache.get("db", "site_a") is None


def test_cache_serves_second_request_without_backend_reads() -> None:
    reads = {"captures": 0, "vision": 0}

    def capture_reader(config, site_id, *, database):  # noqa: ANN001
        reads["captures"] += 1
        return [
            {
                "capture_id": "cap_1",
                "site_id": "site_a",
                "captured_at": "2026-08-10T12:00:00-04:00",
                "category": "site",
                "photos": [{"filename": "a.jpg", "upload_id": "d/a.jpg"}],
            }
        ]

    def vision_reader(config, capture_ids, *, database=None):  # noqa: ANN001
        reads["vision"] += 1
        return VisionByCaptureProjection.from_mapping({})

    class Store:
        def exists(self, key: str) -> bool:
            return True

        def url_for(self, key: str) -> str:
            return "https://r2.example.com/x"

    class Tokens:
        def authenticate(self, value: str) -> TokenRecord | None:
            if value != "good":
                return None
            return TokenRecord(
                token_id="t",
                token_hash="h",
                person_id="p",
                created_at="2026-08-01T00:00:00Z",
                expires_at=None,
                revoked=False,
                label="viewer",
                last_used_at=None,
                can_submit=False,
                can_view_site=True,
                role="read_only",
                site_ids=("site_a",),
            )

    deps = ViewerDependencies(
        config=object(),
        captures_database="db",
        upload_root=UPLOAD_ROOT,
        media_store=Store(),
        capture_reader=capture_reader,
        target_lookup_reader=lambda config, upload_id, *, database: None,
        label_resolver=None,
        vision_database="vdb",
        vision_reader=vision_reader,
    )
    for _ in range(3):
        status, _, _, _ = route_response("GET", "/?token=good", Tokens(), dependencies=deps)
        assert status == HTTPStatus.OK
    assert reads == {"captures": 1, "vision": 1}
