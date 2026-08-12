"""Independent behavioral gates for the site-photo-viewer read model (prompt 526).

Authored by the verifier from the 525 design contract (§2.1, §3.3, §3.6–3.7),
not from the implementation. Covers:

1. TokenSiteScope — the explicit-scope viewer gate: role/permission floor,
   empty and wildcard scope rejection, out-of-scope selection rejection,
   single-site auto-selection, multi-site non-selection.
2. Vision join completeness — query_photo_vision_by_capture_ids must
   accumulate every bookmark page; a stalled bookmark or exhausted safety cap
   raises instead of returning a partial mapping.
3. Vision matching — exact photo_id identity beats filename; the legacy
   filename fallback never joins across captures.
4. Invalid media references stay visible as records.
5. SitePhotoPage — exact totals before slicing, deterministic newest-first
   order, all-terms substring search, invalid timestamps grouped under the
   "Date unavailable" bucket and sorted last.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from field_capture import photo_vision_couchdb
from site_photo_viewer.read_model import (
    DATE_UNAVAILABLE,
    INVALID_REFERENCE,
    PAGE_SIZE,
    CapturePhotoProjection,
    SitePhotoCorpus,
    SitePhotoPage,
    TokenSiteScope,
    TokenSiteScopeError,
    VisionByCaptureProjection,
)
from token_store import TokenRecord


UPLOAD_ROOT = Path("/srv/example/uploads")


def _record(role: str = "read_only", can_view_site: bool = True, site_ids: tuple[str, ...] = ("site_a",)) -> TokenRecord:
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


# --------------------------------------------------------------------------- #
# 1. TokenSiteScope gate
# --------------------------------------------------------------------------- #


def test_scope_rejects_cleaner_role() -> None:
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(role="cleaner"))


def test_scope_rejects_without_view_permission() -> None:
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(can_view_site=False))


def test_scope_rejects_empty_site_scope() -> None:
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(site_ids=()))
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(site_ids=("", "  ")))


def test_scope_rejects_wildcard_even_beside_explicit_sites() -> None:
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(site_ids=("*",)))
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(_record(site_ids=("site_a", "*")))


def test_scope_rejects_selection_outside_token() -> None:
    with pytest.raises(TokenSiteScopeError):
        TokenSiteScope.from_token(
            _record(site_ids=("site_a", "site_b")), selected_site_id="site_c"
        )


def test_single_site_token_selects_itself() -> None:
    scope = TokenSiteScope.from_token(_record(site_ids=("site_a",)))
    assert scope.selected_site_id == "site_a"


def test_multi_site_token_does_not_auto_select() -> None:
    scope = TokenSiteScope.from_token(_record(site_ids=("site_a", "site_b")))
    assert scope.selected_site_id is None
    assert scope.allowed_site_ids == ("site_a", "site_b")


def test_multi_site_token_accepts_in_scope_selection() -> None:
    scope = TokenSiteScope.from_token(
        _record(site_ids=("site_a", "site_b")), selected_site_id="site_b"
    )
    assert scope.selected_site_id == "site_b"


def test_site_admin_passes_gate_with_explicit_scope() -> None:
    scope = TokenSiteScope.from_token(_record(role="site_admin"))
    assert scope.role == "site_admin"


# --------------------------------------------------------------------------- #
# 2. Vision join completeness
# --------------------------------------------------------------------------- #


def _vision_doc(capture_id: str, photo_id: str) -> dict[str, object]:
    return {
        "_id": f"vision:{capture_id}:{photo_id}",
        "capture_id": capture_id,
        "photo_id": photo_id,
        "description": "desc",
    }


def test_vision_join_accumulates_every_bookmark_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # One capture id -> page size max(100, 10) = 100. Serve 100 + 100 + 50.
    docs = [_vision_doc("cap_1", f"up_{i}") for i in range(250)]
    pages = [docs[0:100], docs[100:200], docs[200:250]]
    calls: list[object] = []

    def fake_query(config, mango, *, database=None):  # noqa: ANN001
        calls.append(mango.get("bookmark"))
        index = len(calls) - 1
        return {"docs": pages[index], "bookmark": f"bm{index}"}

    monkeypatch.setattr(photo_vision_couchdb, "query_photo_vision", fake_query)
    grouped = photo_vision_couchdb.query_photo_vision_by_capture_ids(
        object(), ["cap_1"], database="btq_photo_vision"
    )
    assert len(grouped["cap_1"]) == 250
    assert calls == [None, "bm0", "bm1"]


def test_vision_join_raises_on_stalled_bookmark(monkeypatch: pytest.MonkeyPatch) -> None:
    full_page = [_vision_doc("cap_1", f"up_{i}") for i in range(100)]

    def fake_query(config, mango, *, database=None):  # noqa: ANN001
        return {"docs": full_page, "bookmark": "same-bookmark"}

    monkeypatch.setattr(photo_vision_couchdb, "query_photo_vision", fake_query)
    with pytest.raises(photo_vision_couchdb.PhotoVisionCouchDBError):
        photo_vision_couchdb.query_photo_vision_by_capture_ids(
            object(), ["cap_1"], database="btq_photo_vision"
        )


def test_vision_join_raises_when_safety_cap_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    full_page = [_vision_doc("cap_1", f"up_{i}") for i in range(100)]
    counter = {"n": 0}

    def fake_query(config, mango, *, database=None):  # noqa: ANN001
        counter["n"] += 1
        return {"docs": full_page, "bookmark": f"bm{counter['n']}"}

    monkeypatch.setattr(photo_vision_couchdb, "query_photo_vision", fake_query)
    with pytest.raises(photo_vision_couchdb.PhotoVisionCouchDBError):
        photo_vision_couchdb.query_photo_vision_by_capture_ids(
            object(), ["cap_1"], database="btq_photo_vision"
        )
    # The cap must have actually bounded the loop.
    assert counter["n"] <= 401


# --------------------------------------------------------------------------- #
# 3 + 4. Projection and matching
# --------------------------------------------------------------------------- #


def _capture(capture_id: str, site_id: str, captured_at: str, photos: list[dict[str, object]]) -> dict[str, object]:
    return {
        "capture_id": capture_id,
        "site_id": site_id,
        "captured_at": captured_at,
        "category": "site",
        "photos": photos,
    }


def test_escaping_stored_path_is_kept_as_invalid_reference() -> None:
    rows = CapturePhotoProjection.from_capture_rows(
        [
            _capture(
                "cap_1",
                "site_a",
                "2026-08-10T12:00:00-04:00",
                [
                    {"filename": "ok.jpg", "upload_id": "2026-08-10/cap_1/ok.jpg"},
                    {"filename": "bad.jpg", "stored_path": "../../etc/passwd"},
                ],
            )
        ],
        site_id="site_a",
        site_label="Site A",
        upload_root=UPLOAD_ROOT,
    )
    assert len(rows) == 2
    states = {row.filename: row.availability_state for row in rows}
    assert states["bad.jpg"] == INVALID_REFERENCE
    assert rows[0].media_key == "2026-08-10/cap_1/ok.jpg"


def test_exact_photo_id_match_beats_filename_and_never_crosses_captures() -> None:
    rows = CapturePhotoProjection.from_capture_rows(
        [
            _capture(
                "cap_1",
                "site_a",
                "2026-08-10T12:00:00-04:00",
                [{"filename": "shared.jpg", "upload_id": "2026-08-10/cap_1/shared.jpg"}],
            ),
            _capture(
                "cap_2",
                "site_a",
                "2026-08-11T12:00:00-04:00",
                [{"filename": "shared.jpg"}],
            ),
        ],
        site_id="site_a",
        site_label="Site A",
        upload_root=UPLOAD_ROOT,
    )
    vision = VisionByCaptureProjection.from_mapping(
        {
            "cap_1": [
                {
                    "_id": "v1",
                    "capture_id": "cap_1",
                    "photo_id": "2026-08-10/cap_1/shared.jpg",
                    "summary": "exact match",
                },
                {
                    "_id": "v1b",
                    "capture_id": "cap_1",
                    "filename": "shared.jpg",
                    "photo_id": "",
                    "summary": "filename-only doc in cap_1",
                },
            ],
            # No vision docs for cap_2: its shared.jpg must stay unmatched even
            # though cap_1 has a doc naming that filename.
        },
        capture_ids=["cap_1", "cap_2"],
    )
    corpus = SitePhotoCorpus.join(rows, vision)
    by_capture = {record.capture_id: record for record in corpus.photos}
    assert by_capture["cap_1"].summary == "exact match"
    assert by_capture["cap_2"].vision_state == "unavailable"


def test_missing_vision_keeps_photo_with_unavailable_state() -> None:
    rows = CapturePhotoProjection.from_capture_rows(
        [
            _capture(
                "cap_1",
                "site_a",
                "2026-08-10T12:00:00-04:00",
                [{"filename": "a.jpg", "upload_id": "2026-08-10/cap_1/a.jpg"}],
            )
        ],
        site_id="site_a",
        site_label="Site A",
        upload_root=UPLOAD_ROOT,
    )
    corpus = SitePhotoCorpus.join(rows, VisionByCaptureProjection.from_mapping({}))
    assert len(corpus.photos) == 1
    assert corpus.photos[0].vision_state == "unavailable"


def test_searchable_text_carries_all_contract_fields_casefolded() -> None:
    rows = CapturePhotoProjection.from_capture_rows(
        [
            _capture(
                "cap_1",
                "site_a",
                "2026-08-10T12:00:00-04:00",
                [{"filename": "a.jpg", "upload_id": "2026-08-10/cap_1/a.jpg"}],
            )
        ],
        site_id="site_a",
        site_label="Cafeteria Building",
        upload_root=UPLOAD_ROOT,
    )
    vision = VisionByCaptureProjection.from_mapping(
        {
            "cap_1": [
                {
                    "_id": "v1",
                    "capture_id": "cap_1",
                    "photo_id": "2026-08-10/cap_1/a.jpg",
                    "description": "Floor MOPPED",
                    "summary": "Clean entryway",
                    "area_guess": "Lobby",
                    "qc_category": "Floors",
                }
            ]
        }
    )
    corpus = SitePhotoCorpus.join(rows, vision)
    text = corpus.photos[0].searchable_text
    for needle in ("floor mopped", "clean entryway", "lobby", "floors", "2026-08-10", "site", "cafeteria building"):
        assert needle in text


# --------------------------------------------------------------------------- #
# 5. SitePhotoPage
# --------------------------------------------------------------------------- #


def _corpus_of(count: int, *, site: str = "site_a") -> SitePhotoCorpus:
    captures = [
        _capture(
            f"cap_{i:04d}",
            site,
            f"2026-07-{(i % 28) + 1:02d}T08:{i % 60:02d}:00-04:00",
            [{"filename": f"p{i}.jpg", "upload_id": f"2026-07-{(i % 28) + 1:02d}/cap_{i:04d}/p{i}.jpg"}],
        )
        for i in range(count)
    ]
    rows = CapturePhotoProjection.from_capture_rows(
        captures, site_id=site, site_label=site, upload_root=UPLOAD_ROOT
    )
    return SitePhotoCorpus.join(rows, VisionByCaptureProjection.from_mapping({}))


def test_exact_total_is_computed_before_slicing() -> None:
    page = SitePhotoPage.from_corpus(_corpus_of(150), page_number=2)
    assert page.total_results == 150
    assert page.page_size == PAGE_SIZE == 60
    assert page.first_position == 61
    assert page.last_position == 120
    assert len(page.photos) == 60


def test_last_page_positions_and_next_url() -> None:
    urls: list[int] = []
    page = SitePhotoPage.from_corpus(
        _corpus_of(150), page_number=3, url_for_page=lambda n: urls.append(n) or f"/p{n}"
    )
    assert page.first_position == 121
    assert page.last_position == 150
    assert page.next_url is None
    assert page.previous_url == "/p2"


def test_order_is_newest_first_and_deterministic() -> None:
    page = SitePhotoPage.from_corpus(_corpus_of(80), page_number=1)
    stamps = [photo.captured_at for photo in page.photos]
    assert stamps == sorted(stamps, reverse=True)


def test_search_requires_every_term() -> None:
    rows = CapturePhotoProjection.from_capture_rows(
        [
            _capture("cap_1", "site_a", "2026-08-10T12:00:00-04:00",
                     [{"filename": "a.jpg", "upload_id": "2026-08-10/cap_1/a.jpg"}]),
            _capture("cap_2", "site_a", "2026-08-11T12:00:00-04:00",
                     [{"filename": "b.jpg", "upload_id": "2026-08-11/cap_2/b.jpg"}]),
        ],
        site_id="site_a",
        site_label="site_a",
        upload_root=UPLOAD_ROOT,
    )
    vision = VisionByCaptureProjection.from_mapping(
        {
            "cap_1": [{"_id": "v1", "capture_id": "cap_1", "photo_id": "2026-08-10/cap_1/a.jpg",
                       "description": "gym floor buffed"}],
            "cap_2": [{"_id": "v2", "capture_id": "cap_2", "photo_id": "2026-08-11/cap_2/b.jpg",
                       "description": "gym windows cleaned"}],
        }
    )
    corpus = SitePhotoCorpus.join(rows, vision)
    both = SitePhotoPage.from_corpus(corpus, query="GYM")
    assert both.total_results == 2
    one = SitePhotoPage.from_corpus(corpus, query="gym floor")
    assert one.total_results == 1
    none = SitePhotoPage.from_corpus(corpus, query="gym pool")
    assert none.total_results == 0
    assert none.photos == ()


def test_invalid_timestamp_groups_under_date_unavailable_and_sorts_last() -> None:
    captures = [
        _capture("cap_ok", "site_a", "2026-08-10T12:00:00-04:00",
                 [{"filename": "ok.jpg", "upload_id": "2026-08-10/cap_ok/ok.jpg"}]),
        _capture("cap_bad", "site_a", "not-a-timestamp",
                 [{"filename": "bad.jpg", "upload_id": "2026-08-09/cap_bad/bad.jpg"}]),
    ]
    rows = CapturePhotoProjection.from_capture_rows(
        captures, site_id="site_a", site_label="site_a", upload_root=UPLOAD_ROOT
    )
    corpus = SitePhotoCorpus.join(rows, VisionByCaptureProjection.from_mapping({}))
    page = SitePhotoPage.from_corpus(corpus)
    assert page.total_results == 2
    assert page.photos[-1].capture_id == "cap_bad"
    labels = [group.label for group in page.groups]
    assert DATE_UNAVAILABLE in labels


def test_page_number_below_one_rejected() -> None:
    with pytest.raises(ValueError):
        SitePhotoPage.from_corpus(_corpus_of(1), page_number=0)
