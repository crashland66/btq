"""Independent gates for the site-photo-viewer auth service (prompt 527).

Authored by the verifier from the 525 design contract (§2.1, §3.2, §3.4, §3.5)
— the new public auth surface. Every test drives the real ``route_response``
routing seam; nothing here binds a socket.

The contract under test:

- no cookie is ever set; the token authorizes every request independently;
- 401/403 bodies are generic and never name out-of-scope sites;
- the media route proves canonical ownership before redirecting, 302s with an
  empty body to an https presigned URL, and fails closed on store errors;
- every response — success, error, and redirect — carries the security
  headers; non-GET/HEAD is 405.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from site_photo_viewer import server as viewer_server
from site_photo_viewer.server import ViewerDependencies, redact_token_text, route_response
from token_store import TokenRecord


UPLOAD_ROOT = Path("/srv/example/uploads")
SIGNED_URL = "https://r2.example.com/bucket/2026-08-10/cap_1/a1.jpg?X-Amz-Signature=demo"


def _record(site_ids: tuple[str, ...] = ("site_a",), role: str = "read_only", can_view_site: bool = True) -> TokenRecord:
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
        can_view_site=can_view_site,
        role=role,
        token_type="capture",
        site_ids=site_ids,
    )


class FakeTokenStore:
    def __init__(self, records: dict[str, TokenRecord]) -> None:
        self.records = records

    def authenticate(self, token_value: str) -> TokenRecord | None:
        return self.records.get(token_value)


class BrokenTokenStore:
    def authenticate(self, token_value: str) -> TokenRecord | None:
        raise OSError("sqlite unavailable")


class FakeStore:
    def __init__(self, present: set[str], url: str = SIGNED_URL) -> None:
        self.present = present
        self.url = url
        self.exists_calls: list[str] = []

    def exists(self, key: str) -> bool:
        self.exists_calls.append(key)
        return key in self.present

    def url_for(self, key: str) -> str:
        return self.url


def _captures_for(site_id: str) -> list[dict[str, object]]:
    by_site = {
        "site_a": [
            {
                "capture_id": "cap_1",
                "site_id": "site_a",
                "captured_at": "2026-08-10T12:00:00-04:00",
                "category": "site",
                "photos": [{"filename": "a1.jpg", "upload_id": "2026-08-10/cap_1/a1.jpg"}],
            }
        ],
        "site_b": [
            {
                "capture_id": "cap_9",
                "site_id": "site_b",
                "captured_at": "2026-08-11T12:00:00-04:00",
                "category": "site",
                "photos": [{"filename": "b1.jpg", "upload_id": "2026-08-11/cap_9/b1.jpg"}],
            }
        ],
    }
    return by_site.get(site_id, [])


def _deps(
    *,
    store: FakeStore | None = None,
    target: dict[str, str] | None | Exception = None,
) -> ViewerDependencies:
    def capture_reader(config, site_id, *, database):  # noqa: ANN001
        return _captures_for(site_id)

    def target_lookup(config, upload_id, *, database):  # noqa: ANN001
        if isinstance(target, Exception):
            raise target
        return target

    return ViewerDependencies(
        config=object(),
        captures_database="btq_field_captures",
        upload_root=UPLOAD_ROOT,
        media_store=store if store is not None else FakeStore(set()),
        capture_reader=capture_reader,
        target_lookup_reader=target_lookup,
        label_resolver={"site_a": "Alpha Building", "site_b": "Beta Campus"}.get,
    )


def _get(path: str, records: dict[str, TokenRecord] | None = None, **kwargs):
    store = FakeTokenStore(records if records is not None else {"good": _record()})
    return route_response("GET", path, store, dependencies=_deps(**kwargs))


# --------------------------------------------------------------------------- #
# Health, method policy, unknown paths
# --------------------------------------------------------------------------- #


def test_health_answers_without_token_and_without_state() -> None:
    status, content_type, body, headers = _get("/api/health")
    assert status == HTTPStatus.OK
    assert b'"app": "site_photo_viewer"' in body
    assert "Set-Cookie" not in headers
    payload = body.decode()
    for forbidden in ("token", "couch", "sqlite", "site_a", "site_b"):
        assert forbidden not in payload


def test_non_get_methods_are_405_with_allow_header() -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
        status, _, _, headers = route_response(
            method, "/?token=good", FakeTokenStore({"good": _record()}), dependencies=_deps()
        )
        assert status == HTTPStatus.METHOD_NOT_ALLOWED
        assert headers.get("Allow") == "GET, HEAD"


def test_unknown_path_is_404() -> None:
    status, _, _, _ = _get("/admin?token=good")
    assert status == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------- #
# Authentication and scope
# --------------------------------------------------------------------------- #


def test_missing_and_invalid_tokens_are_401() -> None:
    assert _get("/")[0] == HTTPStatus.UNAUTHORIZED
    assert _get("/?token=wrong")[0] == HTTPStatus.UNAUTHORIZED


def test_token_store_outage_is_503_not_401() -> None:
    status, _, _, _ = route_response("GET", "/?token=good", BrokenTokenStore(), dependencies=_deps())
    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_cleaner_role_and_wildcard_and_empty_scope_are_403() -> None:
    records = {
        "cleaner": _record(role="cleaner"),
        "wild": _record(site_ids=("*",)),
        "empty": _record(site_ids=()),
        "noview": _record(can_view_site=False),
    }
    for token in records:
        status, _, body, _ = _get(f"/?token={token}", records)
        assert status == HTTPStatus.FORBIDDEN, token
        assert b"site_a" not in body


def test_single_site_token_renders_latest_page_without_cookie() -> None:
    status, content_type, body, headers = _get("/?token=good")
    assert status == HTTPStatus.OK
    assert "Set-Cookie" not in headers
    text = body.decode()
    assert "Alpha Building" in text
    assert 'name="robots"' in text
    # Media links carry the token; the raw token never appears unescaped in
    # a place other than URLs (there is no cookie to carry it).
    assert "token=good" in text


def test_every_response_carries_security_headers() -> None:
    for path, records in (
        ("/?token=good", None),
        ("/?token=wrong", None),
        ("/api/health", None),
        ("/media/%2E%2E?token=good", None),
    ):
        _, _, _, headers = _get(path, records)
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "no-store" in headers["Cache-Control"]
        assert headers["X-Robots-Tag"].startswith("noindex")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "Content-Security-Policy" in headers


def test_single_site_token_may_not_use_site_parameter() -> None:
    assert _get("/?token=good&site=site_a")[0] == HTTPStatus.FORBIDDEN
    assert _get("/?token=good&site=site_b")[0] == HTTPStatus.FORBIDDEN


def test_multi_site_token_gets_picker_never_merged_feed() -> None:
    records = {"multi": _record(site_ids=("site_a", "site_b"))}
    status, _, body, _ = _get("/?token=multi", records)
    assert status == HTTPStatus.OK
    text = body.decode()
    assert "Alpha Building" in text and "Beta Campus" in text
    assert "<img" not in text  # picker only — never photos from any site


def test_multi_site_selection_is_scope_checked() -> None:
    records = {"multi": _record(site_ids=("site_a", "site_b"))}
    ok_status, _, ok_body, _ = _get("/?token=multi&site=site_b", records)
    assert ok_status == HTTPStatus.OK
    assert "Beta Campus" in ok_body.decode()
    bad_status, _, bad_body, _ = _get("/?token=multi&site=site_c", records)
    assert bad_status == HTTPStatus.FORBIDDEN
    assert b"site_c" not in bad_body and b"site_a" not in bad_body


def test_backend_outage_is_503_not_empty_page() -> None:
    def broken_reader(config, site_id, *, database):  # noqa: ANN001
        raise OSError("couchdb down")

    deps = ViewerDependencies(
        config=object(),
        captures_database="btq_field_captures",
        upload_root=UPLOAD_ROOT,
        media_store=FakeStore(set()),
        capture_reader=broken_reader,
        target_lookup_reader=lambda config, upload_id, *, database: None,
        label_resolver=None,
    )
    status, _, body, _ = route_response(
        "GET", "/?token=good", FakeTokenStore({"good": _record()}), dependencies=deps
    )
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert b"No photos" not in body


# --------------------------------------------------------------------------- #
# Media route
# --------------------------------------------------------------------------- #


AUTHORIZED_KEY = "2026-08-10/cap_1/a1.jpg"
FOREIGN_KEY = "2026-08-11/cap_9/b1.jpg"


def _media_get(key: str, *, records=None, store=None, target=None):  # noqa: ANN001
    from urllib.parse import quote

    return _get(
        f"/media/{quote(key, safe='')}?token=good",
        records,
        store=store if store is not None else FakeStore({AUTHORIZED_KEY}),
        target=target,
    )


def test_authorized_media_is_empty_302_to_presigned_https() -> None:
    status, _, body, headers = _media_get(
        AUTHORIZED_KEY, target={"target_type": "location", "site_id": "site_a"}
    )
    assert status == HTTPStatus.FOUND
    assert body == b""
    assert headers["Location"] == SIGNED_URL
    assert "no-store" in headers["Cache-Control"]


def test_media_ownership_falls_back_to_canonical_rows() -> None:
    # Legacy record: the upload-id view knows nothing, canonical rows do.
    status, _, _, headers = _media_get(AUTHORIZED_KEY, target=None)
    assert status == HTTPStatus.FOUND
    assert headers["Location"] == SIGNED_URL


def test_media_for_other_sites_capture_is_forbidden() -> None:
    status, _, body, _ = _media_get(
        FOREIGN_KEY,
        store=FakeStore({FOREIGN_KEY}),
        target={"target_type": "location", "site_id": "site_b"},
    )
    assert status == HTTPStatus.FORBIDDEN
    assert b"site_b" not in body


def test_unknown_media_key_is_404() -> None:
    status, _, _, _ = _media_get("2026-08-10/cap_1/ghost.jpg", target=None)
    assert status == HTTPStatus.NOT_FOUND


def test_traversal_media_key_is_404_and_never_reaches_store() -> None:
    store = FakeStore({AUTHORIZED_KEY})
    for hostile in ("../secrets", "a/../../b", "/etc/passwd", "a//b"):
        from urllib.parse import quote

        status, _, _, _ = _get(
            f"/media/{quote(hostile, safe='')}?token=good",
            None,
            store=store,
            target=None,
        )
        assert status == HTTPStatus.NOT_FOUND, hostile
    assert store.exists_calls == []


def test_absent_r2_object_is_404() -> None:
    status, _, _, _ = _media_get(
        AUTHORIZED_KEY,
        store=FakeStore(set()),
        target={"target_type": "location", "site_id": "site_a"},
    )
    assert status == HTTPStatus.NOT_FOUND


def test_store_failure_is_503() -> None:
    class ExplodingStore(FakeStore):
        def exists(self, key: str) -> bool:
            raise OSError("HEAD failed")

    status, _, _, _ = _media_get(
        AUTHORIZED_KEY,
        store=ExplodingStore(set()),
        target={"target_type": "location", "site_id": "site_a"},
    )
    assert status == HTTPStatus.SERVICE_UNAVAILABLE


def test_non_https_signed_url_is_refused() -> None:
    status, _, _, headers = _media_get(
        AUTHORIZED_KEY,
        store=FakeStore({AUTHORIZED_KEY}, url="http://r2.example.com/plain"),
        target={"target_type": "location", "site_id": "site_a"},
    )
    assert status == HTTPStatus.SERVICE_UNAVAILABLE
    assert "Location" not in headers


def test_audio_target_type_is_not_served_by_photo_endpoint() -> None:
    status, _, _, _ = _media_get(
        AUTHORIZED_KEY,
        target={"target_type": "audio", "site_id": "site_a"},
    )
    assert status == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------- #
# Log redaction
# --------------------------------------------------------------------------- #


def test_token_query_values_are_redacted_in_logs() -> None:
    line = redact_token_text("/media/abc?token=super-secret-value&page=2")
    assert "super-secret-value" not in line
    assert "redacted" in line
    assert "page=2" in line


def test_redaction_leaves_tokenless_urls_alone() -> None:
    assert redact_token_text("/api/health") == "/api/health"
