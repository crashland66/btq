"""Independent gates for O(1) media ownership (531).

Authored by the verifier from the 531 contract after the live incident:
60 per-page media requests must not each re-fetch the site capture set.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

from site_photo_viewer.read_model import VisionByCaptureProjection
from site_photo_viewer.server import ViewerDependencies, route_response
from token_store import TokenRecord


UPLOAD_ROOT = Path("/srv/example/uploads")
KEY = "2026-08-10/cap_1/a1.jpg"
FOREIGN_KEY = "2026-08-11/cap_9/b1.jpg"


def _record(site_ids: tuple[str, ...] = ("site_a",)) -> TokenRecord:
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
        site_ids=site_ids,
    )


class Tokens:
    def __init__(self, records: dict[str, TokenRecord] | None = None) -> None:
        self.records = records or {"good": _record()}

    def authenticate(self, value: str) -> TokenRecord | None:
        return self.records.get(value)


class Store:
    def exists(self, key: str) -> bool:
        return True

    def url_for(self, key: str) -> str:
        return "https://r2.example.com/signed"


def _make_deps(counters: dict[str, int], *, target=None) -> ViewerDependencies:  # noqa: ANN001
    def capture_reader(config, site_id, *, database):  # noqa: ANN001
        counters["captures"] += 1
        if site_id != "site_a":
            return []
        return [
            {
                "capture_id": "cap_1",
                "site_id": "site_a",
                "captured_at": "2026-08-10T12:00:00-04:00",
                "category": "site",
                "photos": [{"filename": "a1.jpg", "upload_id": KEY}],
            }
        ]

    def vision_reader(config, capture_ids, *, database=None):  # noqa: ANN001
        counters["vision"] += 1
        return VisionByCaptureProjection.from_mapping({})

    def target_lookup(config, upload_id, *, database):  # noqa: ANN001
        counters["target"] += 1
        return target

    return ViewerDependencies(
        config=object(),
        captures_database="db",
        upload_root=UPLOAD_ROOT,
        media_store=Store(),
        capture_reader=capture_reader,
        target_lookup_reader=target_lookup,
        label_resolver=None,
        vision_database="vdb",
        vision_reader=vision_reader,
    )


def _media(path_key: str, deps: ViewerDependencies, records=None):  # noqa: ANN001
    return route_response(
        "GET",
        f"/media/{quote(path_key, safe='')}?token=good",
        Tokens(records),
        dependencies=deps,
    )


def test_page_then_sixty_media_requests_fetch_captures_once() -> None:
    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters)
    status, _, _, _ = route_response("GET", "/?token=good", Tokens(), dependencies=deps)
    assert status == HTTPStatus.OK
    for _ in range(60):
        status, _, _, headers = _media(KEY, deps)
        assert status == HTTPStatus.FOUND
        assert headers["Location"].startswith("https://")
    assert counters["captures"] == 1
    assert counters["vision"] == 1
    assert counters["target"] == 0  # cache membership answered every request


def test_cold_media_request_builds_corpus_once_then_reuses_it() -> None:
    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters, target={"target_type": "location", "site_id": "site_a"})
    for _ in range(10):
        status, _, _, _ = _media(KEY, deps)
        assert status == HTTPStatus.FOUND
    assert counters["captures"] == 1
    assert counters["target"] == 1  # only the first, cold request consulted the view


def test_cold_legacy_record_uses_canonical_fallback_once() -> None:
    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters, target=None)
    status, _, _, _ = _media(KEY, deps)
    assert status == HTTPStatus.FOUND
    status, _, _, _ = _media(KEY, deps)
    assert status == HTTPStatus.FOUND
    assert counters["captures"] == 1


def test_cross_site_and_audio_and_unknown_semantics_survive() -> None:
    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters, target={"target_type": "location", "site_id": "site_b"})
    status, _, body, _ = _media(FOREIGN_KEY, deps)
    assert status == HTTPStatus.FORBIDDEN
    assert b"site_b" not in body

    counters2 = {"captures": 0, "vision": 0, "target": 0}
    deps2 = _make_deps(counters2, target={"target_type": "audio", "site_id": "site_a"})
    status, _, _, _ = _media("2026-08-10/cap_1/voice.m4a", deps2)
    assert status == HTTPStatus.NOT_FOUND

    counters3 = {"captures": 0, "vision": 0, "target": 0}
    deps3 = _make_deps(counters3, target=None)
    status, _, _, _ = _media("2026-01-01/ghost/x.jpg", deps3)
    assert status == HTTPStatus.NOT_FOUND


def test_backend_outage_on_cold_media_is_503() -> None:
    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters, target={"target_type": "location", "site_id": "site_a"})

    def broken_reader(config, site_id, *, database):  # noqa: ANN001
        raise OSError("couch down")

    deps = ViewerDependencies(
        config=deps.config,
        captures_database=deps.captures_database,
        upload_root=deps.upload_root,
        media_store=deps.media_store,
        capture_reader=broken_reader,
        target_lookup_reader=deps.target_lookup_reader,
        label_resolver=None,
        vision_database=deps.vision_database,
        vision_reader=deps.vision_reader,
    )
    status, _, _, _ = _media(KEY, deps)
    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_server_survives_a_thirty_image_burst_behind_a_proxy() -> None:
    """Backlog and keep-alive contract from the 2026-08-12 502 incident."""
    from site_photo_viewer.server import SitePhotoViewerHandler, SitePhotoViewerServer

    assert SitePhotoViewerServer.request_queue_size >= 32
    assert SitePhotoViewerHandler.protocol_version == "HTTP/1.1"


def test_lightbox_enhancement_is_wired_with_csp_and_fallback() -> None:
    """532: same-origin script, script-src in CSP, href fallback intact."""
    from http import HTTPStatus as HS

    from site_photo_viewer.server import SECURITY_HEADERS, route_response

    assert "script-src 'self'" in SECURITY_HEADERS["Content-Security-Policy"]

    counters = {"captures": 0, "vision": 0, "target": 0}
    deps = _make_deps(counters)
    status, content_type, body, _ = route_response(
        "GET", "/viewer.js?token=ignored", Tokens(), dependencies=deps
    )
    assert status == HS.OK
    assert "javascript" in content_type
    assert b"preventDefault" in body
    assert b"Escape" in body

    status, _, page, _ = route_response("GET", "/?token=good", Tokens(), dependencies=deps)
    assert status == HS.OK
    text = page.decode()
    assert '<script src="/viewer.js" defer></script>' in text
    assert 'class="media-frame" href="/media/' in text  # no-JS fallback preserved
