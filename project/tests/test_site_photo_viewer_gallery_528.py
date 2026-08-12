"""Independent gates for the viewer gallery, search, and pagination (528).

Authored by the verifier from the 525 design contract (§3.6–3.8) plus hostile
probes beyond it (XSS via vision text, parameter abuse). All tests drive the
real ``route_response`` seam with an injected vision reader.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

from site_photo_viewer.read_model import VisionByCaptureProjection
from site_photo_viewer.server import ViewerDependencies, route_response
from token_store import TokenRecord


UPLOAD_ROOT = Path("/srv/example/uploads")
SIGNED_URL = "https://r2.example.com/signed?X-Amz-Signature=demo"


def _record(site_ids: tuple[str, ...] = ("site_a",)) -> TokenRecord:
    return TokenRecord(
        token_id="tok-1",
        token_hash="hash",
        person_id="person_demo",
        created_at="2026-08-01T00:00:00Z",
        expires_at=None,
        revoked=False,
        label="viewer",
        last_used_at=None,
        can_submit=False,
        can_view_site=True,
        role="read_only",
        token_type="capture",
        site_ids=site_ids,
    )


class FakeTokenStore:
    def authenticate(self, token_value: str) -> TokenRecord | None:
        if token_value == "good":
            return _record()
        if token_value == "multi":
            return _record(site_ids=("site_a", "site_b"))
        return None


class FakeStore:
    def __init__(self, present: set[str] | None = None, *, explode: bool = False) -> None:
        self.present = present if present is not None else set()
        self.explode = explode

    def exists(self, key: str) -> bool:
        if self.explode:
            raise OSError("HEAD failed")
        return key in self.present

    def url_for(self, key: str) -> str:
        return SIGNED_URL


KEY_1 = "2026-08-10/cap_1/a1.jpg"
KEY_2 = "2026-08-11/cap_2/b1.jpg"

CAPTURES = [
    {
        "capture_id": "cap_1",
        "site_id": "site_a",
        "captured_at": "2026-08-10T09:30:00-04:00",
        "category": "site",
        "photos": [{"filename": "a1.jpg", "upload_id": KEY_1}],
    },
    {
        "capture_id": "cap_2",
        "site_id": "site_a",
        "captured_at": "2026-08-11T14:00:00-04:00",
        "category": "qc",
        "photos": [
            {"filename": "b1.jpg", "upload_id": KEY_2},
            {"filename": "hostile.jpg", "stored_path": "../../etc/shadow"},
        ],
    },
]

VISION = {
    "cap_1": [
        {
            "_id": "v1",
            "capture_id": "cap_1",
            "photo_id": KEY_1,
            "description": "Gym floor freshly buffed",
            "summary": "Buffed gym floor",
            "area_guess": "Gymnasium",
            "qc_category": "Floors",
        }
    ],
    "cap_2": [
        {
            "_id": "v2",
            "capture_id": "cap_2",
            "photo_id": KEY_2,
            "description": '<script>alert("x")</script> cafeteria tables wiped',
            "summary": "Cafeteria <b>tables</b>",
            "area_guess": "Cafeteria",
            "qc_category": "Surfaces",
        }
    ],
}


def _deps(*, store: FakeStore | None = None, captures=None, vision=None) -> ViewerDependencies:  # noqa: ANN001
    rows = captures if captures is not None else CAPTURES

    def capture_reader(config, site_id, *, database):  # noqa: ANN001
        return [row for row in rows if row.get("site_id") == site_id]

    def vision_reader(config, capture_ids, *, database=None):  # noqa: ANN001
        return VisionByCaptureProjection.from_mapping(
            vision if vision is not None else VISION, capture_ids=list(capture_ids)
        )

    return ViewerDependencies(
        config=object(),
        captures_database="btq_field_captures",
        upload_root=UPLOAD_ROOT,
        media_store=store if store is not None else FakeStore({KEY_1, KEY_2}),
        capture_reader=capture_reader,
        target_lookup_reader=lambda config, upload_id, *, database: None,
        label_resolver={"site_a": "Alpha Building", "site_b": "Beta Campus"}.get,
        vision_database="btq_photo_vision",
        vision_reader=vision_reader,
    )


def _get(path: str, **kwargs):  # noqa: ANN001
    return route_response("GET", path, FakeTokenStore(), dependencies=_deps(**kwargs))


def _page_text(path: str, **kwargs) -> str:  # noqa: ANN001
    status, _, body, _ = _get(path, **kwargs)
    assert status == HTTPStatus.OK
    return body.decode()


# --------------------------------------------------------------------------- #
# Gallery content
# --------------------------------------------------------------------------- #


def test_latest_page_shows_counts_dates_captions_and_details() -> None:
    text = _page_text("/?token=good")
    assert "Showing 1–3 of 3 photos" in text
    assert "2026-08-11" in text and "2026-08-10" in text
    assert "Buffed gym floor" in text
    assert "Photo details" in text
    assert "Gymnasium" in text
    assert "Submitted category: qc" in text


def test_newest_date_group_renders_first() -> None:
    text = _page_text("/?token=good")
    assert text.index("2026-08-11") < text.index("2026-08-10")


def test_vision_text_is_html_escaped() -> None:
    text = _page_text("/?token=good")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "<b>tables</b>" not in text


def test_invalid_reference_renders_placeholder_not_dead_img() -> None:
    text = _page_text("/?token=good")
    assert "Media reference is invalid." in text
    # Exactly the two valid photos produce <img> tags; the invalid one never does.
    assert text.count("<img") == 2


def test_absent_r2_object_renders_durable_storage_placeholder() -> None:
    text = _page_text("/?token=good", store=FakeStore({KEY_1}))
    assert "Media is unavailable in durable storage." in text
    assert text.count("<img") == 1


def test_store_failure_renders_unknown_state_not_outage_page() -> None:
    text = _page_text("/?token=good", store=FakeStore(explode=True))
    assert "Media availability could not be checked." in text
    assert "temporarily unavailable" not in text


def test_vision_absent_photo_still_renders_with_notice() -> None:
    text = _page_text("/?token=good", vision={})
    assert "Vision analysis is not available." in text
    assert text.count("<img") == 2


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def test_search_filters_and_reports_exact_counts() -> None:
    text = _page_text(f"/?token=good&q={quote('gym floor')}")
    assert "Showing 1–1 of 1 photos" in text
    assert "Buffed gym floor" in text
    assert "Cafeteria" not in text


def test_search_no_match_is_distinct_from_no_photos() -> None:
    text = _page_text("/?token=good&q=nonexistentterm")
    assert "No photos match your search." in text
    no_photos = _page_text("/?token=good&q=", captures=[])
    assert "No photos are available." in no_photos


def test_search_terms_are_conjunctive() -> None:
    both_terms = _page_text("/?token=good&q=cafeteria+tables")
    assert "Showing 1–1 of 1 photos" in both_terms
    disjoint = _page_text("/?token=good&q=cafeteria+gym")
    assert "of 0 photos" in disjoint


def test_search_form_preserves_token_and_escapes_query() -> None:
    text = _page_text(f"/?token=good&q={quote('\"><svg onload=x>')}")
    assert 'name="token" value="good"' in text
    assert "<svg" not in text


# --------------------------------------------------------------------------- #
# Pagination and parameter abuse
# --------------------------------------------------------------------------- #


def _many_captures(count: int) -> list[dict[str, object]]:
    return [
        {
            "capture_id": f"cap_{i:04d}",
            "site_id": "site_a",
            "captured_at": f"2026-07-{(i % 28) + 1:02d}T08:00:00-04:00",
            "category": "site",
            "photos": [{"filename": f"p{i}.jpg", "upload_id": f"2026-07-01/cap_{i:04d}/p{i}.jpg"}],
        }
        for i in range(count)
    ]


def test_second_page_slices_and_links_carry_token_and_query() -> None:
    captures = _many_captures(90)
    text = _page_text("/?token=good&page=2", captures=captures, vision={}, store=FakeStore())
    assert "Showing 31–60 of 90 photos" in text
    assert 'rel="prev"' in text
    assert "token=good" in text


def test_beyond_last_page_redirects_to_last_keeping_query() -> None:
    captures = _many_captures(90)
    status, _, body, headers = _get(
        "/?token=good&page=9&q=site", captures=captures, vision={}, store=FakeStore()
    )
    assert status == HTTPStatus.FOUND
    assert body == b""
    location = headers["Location"]
    assert "page=3" in location and "q=site" in location and "token=good" in location


def test_malformed_parameters_are_400() -> None:
    for path in (
        "/?token=good&page=0",
        "/?token=good&page=abc",
        "/?token=good&page=2&page=3",
        "/?token=good&q=a&q=b",
        f"/?token=good&q={quote('x' * 201)}",
    ):
        status, _, _, _ = _get(path)
        assert status == HTTPStatus.BAD_REQUEST, path


def test_outage_is_still_503_with_vision_reader_wired() -> None:
    def broken_vision(config, capture_ids, *, database=None):  # noqa: ANN001
        raise OSError("couch down")

    deps = _deps()
    deps = ViewerDependencies(
        config=deps.config,
        captures_database=deps.captures_database,
        upload_root=deps.upload_root,
        media_store=deps.media_store,
        capture_reader=deps.capture_reader,
        target_lookup_reader=deps.target_lookup_reader,
        label_resolver=deps.label_resolver,
        vision_database=deps.vision_database,
        vision_reader=broken_vision,
    )
    status, _, body, _ = route_response("GET", "/?token=good", FakeTokenStore(), dependencies=deps)
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert b"No photos" not in body


# --------------------------------------------------------------------------- #
# Stylesheet and document shell
# --------------------------------------------------------------------------- #


def test_stylesheet_is_served_and_theme_aware() -> None:
    status, content_type, body, _ = _get("/viewer.css")
    assert status == HTTPStatus.OK
    assert content_type.startswith("text/css")
    css = body.decode()
    assert "prefers-color-scheme" in css
    assert ":root" in css


def test_document_shell_declares_robots_and_color_scheme() -> None:
    text = _page_text("/?token=good")
    assert 'name="robots" content="noindex,nofollow,noarchive"' in text
    assert 'name="color-scheme" content="light dark"' in text
    assert '<link rel="stylesheet" href="/viewer.css">' in text
