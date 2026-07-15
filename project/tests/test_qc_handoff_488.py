"""Independent verifier coverage for the read-only QC handoff board.

All fixtures are synthetic.  The tests exercise the real grouping and render
boundaries while replacing CouchDB, site-registry, and runtime discovery with
local stubs, so they cannot read or mutate operational data.
"""

from __future__ import annotations

import ast
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from field_capture.display_categories import BUILTIN_FALLBACK_CATEGORIES
from ops_dashboard import app, layout
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos


_FIELD_PHOTOS_SOURCE = Path(field_photos.__file__).resolve()
_QC_SOURCE = _FIELD_PHOTOS_SOURCE
_ADMIN_CSS = _FIELD_PHOTOS_SOURCE.parents[1] / "static" / "admin.css"
_OPERATIONAL_ONLY = {"qc", "baseline", "pre_engagement", "report_an_issue"}


@pytest.fixture(autouse=True)
def _keep_site_labels_synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        field_photos,
        "resolve_site_label",
        lambda site_id, _vault_root=None: str(site_id),
    )


def _ctx(tmp_path: Path, query: dict[str, list[str]] | None = None) -> SectionContext:
    runtime_root = tmp_path / "runtime"
    ctx = SectionContext(
        runtime_root,
        lambda: SimpleNamespace(vault_root=runtime_root / "vault", vault_dir=runtime_root / "vault"),
    )
    ctx.query = query or {}
    ctx.route_path = "/qc-handoff"
    return ctx


def _sidecar(
    asset: str,
    vision_category: str,
    *,
    qc_category: str = "qc",
    agreement: str = "unverifiable",
    capture_id: str = "walk-one",
    site_id: str = "site-one",
    minute: int = 0,
) -> dict[str, object]:
    return {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": capture_id,
        "photo_id": f"photo-{asset}",
        "photo_asset_id": asset,
        "site_id": site_id,
        "qc_category": qc_category,
        "vision_category": vision_category,
        "category_agreement": agreement,
        "area_guess": "synthetic area",
        "description": f"Synthetic verifier photo {asset}",
        "generated_at": f"2026-07-15T12:{minute:02d}:00Z",
        "source_filename": f"{asset}.jpg",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "provenance": {
            "captured_at": f"2026-07-15T12:{minute:02d}:00Z",
            "image_media_url": f"/media/{asset}.jpg",
        },
    }


def _section_assets(grouped: dict[str, object]) -> dict[str, list[str]]:
    sections = grouped["sections"]
    assert isinstance(sections, list)
    return {
        str(section["canonical"]): [str(row["photo_asset_id"]) for row in section["sidecars"]]
        for section in sections
    }


def _needs_assets(grouped: dict[str, object]) -> list[str]:
    needs = grouped["needs_sorting"]
    assert isinstance(needs, list)
    return [str(item["sidecar"]["photo_asset_id"]) for item in needs]


def test_grouping_uses_vision_category_in_canonical_display_order_and_deduplicates() -> None:
    docs = [
        _sidecar("trash", "Trash", minute=8),
        _sidecar("restroom", "Restrooms", qc_category="qc", minute=2),
        # Generic QC is an intake lane; grouping must follow vision_category.
        _sidecar("vision-wins", "Hallways", qc_category="qc", minute=4),
        _sidecar("entry", "Entryways / Lobby / Doorways", minute=1),
        _sidecar("duplicate", "Restrooms", minute=5),
        _sidecar("duplicate", "Trash", minute=6),
    ]

    grouped = field_photos.group_handoff_sidecars(docs)
    assets = _section_assets(grouped)

    expected_order = [
        str(category["canonical"])
        for category in BUILTIN_FALLBACK_CATEGORIES
        if str(category["canonical"]).casefold() not in _OPERATIONAL_ONLY
        and str(category["canonical"]) in assets
    ]
    assert list(assets) == expected_order
    assert assets["Hallways"] == ["vision-wins"]
    assert assets["Restrooms"] == ["restroom", "duplicate"]
    assert "qc" not in assets

    emitted = _needs_assets(grouped) + [asset for values in assets.values() for asset in values]
    assert len(emitted) == len(set(emitted)) == grouped["total"] == 5


def test_disagreements_unmapped_and_operational_proposals_each_appear_once_in_needs_sorting() -> None:
    docs = [
        _sidecar("mismatch", "Hallways", qc_category="Restrooms", agreement="mismatch"),
        _sidecar("unmapped", "Imaginary Wing"),
        _sidecar("generic-qc", "qc"),
        _sidecar("baseline", "baseline"),
        _sidecar("pre-engagement", "pre_engagement"),
        _sidecar("report-issue", "report_an_issue"),
    ]
    # Repeated source rows must not produce repeated cards.
    grouped = field_photos.group_handoff_sidecars(docs + docs)

    assert _section_assets(grouped) == {}
    assert _needs_assets(grouped) == [
        "mismatch",
        "unmapped",
        "generic-qc",
        "baseline",
        "pre-engagement",
        "report-issue",
    ]
    assert grouped["total"] == 6
    assert len({item["reason"] for item in grouped["needs_sorting"]}) >= 3


def test_board_and_gallery_share_one_clean_section_loader_and_drag_architecture() -> None:
    source = _FIELD_PHOTOS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        matches = [node for node in definitions if node.name == name]
        assert len(matches) == 1
        return matches[0]

    def direct_calls(node: ast.AST) -> set[str]:
        return {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }

    assert "load_filtered_photo_sidecars" in direct_calls(function("render_qc_handoff"))
    assert "load_filtered_photo_sidecars" in direct_calls(function("render"))
    function("load_filtered_photo_sidecars")
    function("render_photo_file_drag_script")
    assert not (_FIELD_PHOTOS_SOURCE.parent / "qc_handoff.py").exists()
    assert "from ops_dashboard.sections" not in source


def test_board_reuses_the_single_file_drag_initializer_and_existing_export_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    drag_marker = "<script data-verifier-drag></script>"
    selection_marker = "<script data-verifier-selection></script>"
    monkeypatch.setattr(field_photos, "render_photo_file_drag_script", lambda: drag_marker)
    monkeypatch.setattr(field_photos, "render_photo_selection_script", lambda: selection_marker)
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})

    rendered = field_photos._render_handoff_board(
        _ctx(tmp_path),
        [_sidecar("restroom", "Restrooms")],
        "walk-one",
        False,
        False,
        BUILTIN_FALLBACK_CATEGORIES,
    )

    assert rendered.count(drag_marker) == 1
    assert rendered.count(selection_marker) == 1
    assert 'method="post" action="/field-photos/export"' in rendered
    assert 'data-download-photo-category="Restrooms"' in rendered
    assert 'aria-label="Download Restrooms section (1 photo)"' in rendered
    assert "data-download-all-photos" in rendered
    assert 'data-photo-group="Restrooms"' in rendered
    assert 'data-selected-photo-count role="status" aria-live="polite" aria-atomic="true"' in rendered
    source = _FIELD_PHOTOS_SOURCE.read_text(encoding="utf-8")
    assert source.count("def render_photo_file_drag_script(") == 1
    assert source.count("window.BTQPhotoFileDrag = {init: init, prepare: prepare};") == 1


def test_render_requires_site_then_one_capture_and_discovery_never_builds_a_mixed_board(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "site-one — Synthetic")])
    calls: list[dict[str, object]] = []

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        return [
            _sidecar("one-a", "Restrooms", capture_id="walk-one", minute=1),
            _sidecar("two-a", "Hallways", capture_id="walk-two", minute=2),
        ], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)

    unscoped = field_photos.render(_ctx(tmp_path, {"capture_id": ["walk-one"]}))
    assert calls == []
    assert "Select a site and walk" in unscoped
    assert "qc-handoff-export-form" not in unscoped

    discovery = field_photos.render(_ctx(tmp_path, {"site_id": ["site-one"]}))
    assert calls[-1]["site_id"] == "site-one"
    assert calls[-1].get("capture_id", "") == ""
    assert "Choose one walk (2 found)" in discovery
    assert "photos from separate walks are never mixed" in discovery
    assert "walk-one" in discovery and "walk-two" in discovery
    assert "qc-handoff-export-form" not in discovery


def test_selected_walk_passes_exact_site_and_capture_to_canonical_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(field_photos, "load_site_handoff_categories", lambda _site_id: BUILTIN_FALLBACK_CATEGORIES)
    observed: dict[str, object] = {}

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        observed.update(filters)
        return [_sidecar("one", "Restrooms")], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(
        _ctx(
            tmp_path,
            {
                "site_id": ["site-one"],
                "capture_id": ["walk-one"],
                "date_from": ["2026-07-01"],
                "date_to": ["2026-07-15"],
            },
        )
    )

    assert observed == {"site_id": "site-one", "capture_id": "walk-one"}
    assert "qc-handoff-export-form" in rendered
    assert 'name="capture_id" value="walk-one"' in rendered
    assert 'name="date_from" value=""' in rendered
    assert 'name="date_to" value=""' in rendered
    assert "<label>Walk ID" in rendered
    assert "Capture ID" not in rendered
    assert "Dates help find a walk. Once selected, the board loads the complete walk." in rendered
    assert "1 photo in this walk" in rendered


def test_discovery_dates_filter_choices_but_are_not_carried_into_selected_walk_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    observed: list[dict[str, object]] = []

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        observed.append(filters)
        return [_sidecar("one", "Restrooms", capture_id="walk-one")], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(
        _ctx(
            tmp_path,
            {
                "site_id": ["site-one"],
                "date_from": ["2026-07-01"],
                "date_to": ["2026-07-15"],
            },
        )
    )

    assert observed == [
        {"site_id": "site-one", "date_from": "2026-07-01", "date_to": "2026-07-15"}
    ]
    assert 'name="date_from" value="2026-07-01"' in rendered
    assert 'name="date_to" value="2026-07-15"' in rendered
    assert 'href="/qc-handoff?site_id=site-one&amp;capture_id=walk-one"' in rendered
    discovery_link = rendered.split('href="/qc-handoff?', 1)[1].split('"', 1)[0]
    assert "date_from" not in discovery_link and "date_to" not in discovery_link


@pytest.mark.parametrize(
    ("site_categories", "default_categories", "expected"),
    [
        (
            [
                {"label": "Site Second", "canonical": "site_second"},
                {"label": "Site First", "canonical": "site_first"},
            ],
            [{"label": "Default", "canonical": "default"}],
            ["site_second", "site_first"],
        ),
        (
            None,
            [
                {"label": "Default Two", "canonical": "default_two"},
                {"label": "Default One", "canonical": "default_one"},
            ],
            ["default_two", "default_one"],
        ),
        (None, None, [str(row["canonical"]) for row in BUILTIN_FALLBACK_CATEGORIES]),
    ],
)
def test_selected_site_category_authority_is_site_then_default_then_builtin(
    monkeypatch: pytest.MonkeyPatch,
    site_categories: list[dict[str, str]] | None,
    default_categories: list[dict[str, str]] | None,
    expected: list[str],
) -> None:
    class Registry:
        def get_display_categories(self, site_id: str) -> list[dict[str, str]] | None:
            assert site_id == "site-one"
            return site_categories

    monkeypatch.setattr(field_photos, "CouchDBSiteRegistry", Registry)
    monkeypatch.setattr(
        field_photos,
        "load_system_defaults",
        lambda: {"default_display_categories": default_categories},
    )

    loaded = field_photos.load_site_handoff_categories("site-one")
    canonicals = [str(row["canonical"]) for row in loaded]

    assert canonicals == expected


def test_selected_site_custom_sections_control_board_order_and_recognition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom_categories = [
        {"label": "Custom Beta", "canonical": "custom_beta"},
        {"label": "Custom Alpha", "canonical": "custom_alpha"},
        {"label": "QC", "canonical": "qc"},
    ]
    docs = [
        _sidecar("alpha", "custom_alpha", minute=1),
        _sidecar("builtin-only", "Restrooms", minute=2),
        _sidecar("beta", "custom_beta", minute=3),
    ]
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **filters: (docs, False, False)
        if filters == {"site_id": "site-one", "capture_id": "walk-one"}
        else pytest.fail(f"unexpected filters: {filters}"),
    )
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    loaded_sites: list[str] = []

    def categories(site_id: str) -> list[dict[str, str]]:
        loaded_sites.append(site_id)
        return custom_categories

    monkeypatch.setattr(field_photos, "load_site_handoff_categories", categories)
    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "capture_id": ["walk-one"]})
    )

    assert loaded_sites == ["site-one"]
    assert rendered.index(">Custom Beta</h2>") < rendered.index(">Custom Alpha</h2>")
    assert 'data-download-photo-category="custom_beta"' in rendered
    assert 'data-download-photo-category="custom_alpha"' in rendered
    assert 'aria-label="Download Custom Beta section (1 photo)"' in rendered
    assert rendered.count("<strong>Needs sorting:</strong>") == 1
    assert "builtin-only.jpg" in rendered
    assert 'data-download-photo-category="Restrooms"' not in rendered


def test_registry_outage_offers_photo_cache_sites_and_manual_site_id_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [])
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: ([], False, False),
    )
    calls: list[dict[str, object]] = []

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        if not filters:
            return [_sidecar("cached", "Restrooms", site_id="cached-site")], True, True
        return [], True, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(_ctx(tmp_path, {"site_id": ["typed-site"]}))

    assert calls == [{}, {"site_id": "typed-site", "date_from": "", "date_to": ""}]
    assert "The site registry is unavailable" in rendered
    assert "local photo cache" in rendered
    assert f"first {field_photos.PAGE_LIMIT} photos" in rendered
    assert '<input name="site_id" value="typed-site"' in rendered
    assert 'list="qc-handoff-photo-sites"' in rendered
    assert "Enter or choose a site ID" in rendered
    assert 'value="cached-site"' in rendered
    assert "cached-site — from photo data" in rendered
    assert 'value="typed-site"' in rendered
    assert "typed-site — entered site ID" in rendered
    assert "No walks found" in rendered


def test_empty_no_selection_fallback_truncation_and_multi_walk_states_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    empty_board = field_photos._render_handoff_board(
        _ctx(tmp_path), [], "walk-one", False, False, BUILTIN_FALLBACK_CATEGORIES
    )
    assert "No photos for this walk" in empty_board
    assert "Check the site and walk ID" in empty_board

    discovery = field_photos._render_handoff_discovery(
        [
            {"capture_id": "walk-one", "count": 2, "captured_at": "2026-07-15T12:00:00Z"},
            {"capture_id": "walk-two", "count": 1, "captured_at": "2026-07-15T13:00:00Z"},
        ],
        site_id="site-one",
        fallback=True,
        has_more=True,
    )
    assert "CouchDB is unavailable" in discovery
    assert f"first {field_photos.PAGE_LIMIT} photos" in discovery
    assert "Choose one walk (2 found)" in discovery

    board = field_photos._render_handoff_board(
        _ctx(tmp_path),
        [_sidecar("one", "Restrooms")],
        "walk-one",
        True,
        True,
        BUILTIN_FALLBACK_CATEGORIES,
    )
    assert "local photo cache" in board
    assert f"more than {field_photos.PAGE_LIMIT} photos" in board
    assert "0 selected" in board
    assert 'data-export-selected-photos disabled' in board


def test_disk_fallback_reports_when_canonical_page_limit_truncates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docs = [
        _sidecar(f"asset-{index:03d}", "Restrooms", minute=index % 60)
        for index in range(field_photos.PAGE_LIMIT + 1)
    ]
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _directory: docs)

    loaded, fallback, has_more = field_photos.load_filtered_photo_sidecars(
        _ctx(tmp_path), site_id="site-one", capture_id="walk-one"
    )

    assert len(loaded) == field_photos.PAGE_LIMIT
    assert fallback is False  # local-only configuration is not a CouchDB outage
    assert has_more is True, "disk fallback truncated a walk without exposing the truncation state"


def test_handoff_cards_are_large_responsive_and_uncropped() -> None:
    card = field_photos.render_photo_card(
        _sidecar("one", "Restrooms"),
        {},
        Path("/synthetic/vault"),
        handoff_presentation=True,
    )
    css = _ADMIN_CSS.read_text(encoding="utf-8")
    handoff_css = css.split("/* ---- QC SafetyCulture handoff", 1)[1]

    assert 'class="field-photo-image field-photo-image--handoff"' in card
    assert "aspect-ratio:4/3;object-fit:contain" in card
    assert "field-photo-card--handoff" in card
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in handoff_css
    assert "gap: 24px" in handoff_css
    responsive = handoff_css.split("@media (max-width: 760px)", 1)[1]
    assert ".qc-handoff-grid" in responsive
    assert "grid-template-columns: 1fr" in responsive


def test_navigation_get_route_and_read_only_method_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [])
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: ([], False, False),
    )

    assert app.SECTION_ROUTES["/qc-handoff"] is field_photos
    assert ("qc_handoff", "QC Handoff", "/qc-handoff", "Q") in layout.NAV_ITEMS

    status, content_type, body, headers = app.route_response_with_headers(
        "GET", "/qc-handoff", tmp_path / "runtime"
    )
    rendered = body.decode("utf-8")
    assert status == HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert headers == {}
    assert '<a href="/qc-handoff" title="QC Handoff" aria-current="page">' in rendered

    status, content_type, body, headers = app.route_response_with_headers(
        "POST", "/qc-handoff", tmp_path / "runtime", b"site_id=site-one"
    )
    assert status == HTTPStatus.METHOD_NOT_ALLOWED
    assert content_type == "application/json; charset=utf-8"
    assert b'"read_only"' in body
    assert headers == {}


def test_rendering_board_has_no_filesystem_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(field_photos, "load_site_handoff_categories", lambda _site_id: BUILTIN_FALLBACK_CATEGORIES)
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: ([_sidecar("one", "Restrooms")], False, False),
    )
    runtime_root = tmp_path / "runtime"
    before = list(tmp_path.rglob("*"))

    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "capture_id": ["walk-one"]})
    )

    assert "QC Handoff" in rendered
    assert list(tmp_path.rglob("*")) == before
    assert not runtime_root.exists()
