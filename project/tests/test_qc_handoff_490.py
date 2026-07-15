"""Independent verifier coverage for the fixed QC capture-lane boundary.

All data is synthetic and every operational boundary is replaced with a local
stub.  The tests exercise the real read-only render and loader paths without
touching runtime or remote data.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from field_capture.display_categories import QC_CAPTURE_CATEGORY
from ops_dashboard.sections import field_photos
from tests.test_qc_handoff_488 import _ctx, _sidecar


_SOURCE = Path(field_photos.__file__).resolve()


def _mixed_same_day() -> list[dict[str, object]]:
    return [
        _sidecar("qc-restroom", "Restrooms", capture_id="qc-walk-alpha", minute=1),
        _sidecar("qc-hallway", "Hallways", capture_id="qc-walk-beta", minute=2),
        _sidecar(
            "baseline-office",
            "Offices / Classrooms / Exam Rooms",
            qc_category="baseline",
            capture_id="baseline-walk-secret",
            minute=3,
        ),
        _sidecar(
            "issue-trash",
            "Trash",
            qc_category="report_an_issue",
            capture_id="issue-walk-secret",
            minute=4,
        ),
    ]


def test_mixed_same_site_date_discovery_and_board_are_qc_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docs = _mixed_same_day()
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        selected = [row for row in docs if row["site_id"] == filters.get("site_id")]
        if filters.get("qc_category"):
            selected = [row for row in selected if row["qc_category"] == filters["qc_category"]]
        return selected, False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)

    discovery = field_photos.render(_ctx(tmp_path, {"site_id": ["site-one"]}))
    board = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )

    expected = {"site_id": "site-one", "qc_category": QC_CAPTURE_CATEGORY}
    assert calls == [expected, expected]
    assert "2 photos · 2 contributing captures" in discovery
    assert "2 photos on this QC day" in board
    assert "qc-restroom.jpg" in board and "qc-hallway.jpg" in board
    for leaked in (
        "baseline-office",
        "baseline-walk-secret",
        "issue-trash",
        "issue-walk-secret",
        "report_an_issue",
    ):
        assert leaked not in discovery
        assert leaked not in board
    assert 'name="media_key" value="baseline-office.jpg"' not in board
    assert 'data-download-photo-category="Trash"' not in board
    assert board.count("data-photo-file-drag") >= 1
    assert "openLb(" in board
    assert 'method="post" action="/field-photos/export"' in board


def test_couchdb_loader_places_qc_in_selector_before_page_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[dict[str, object]] = []
    docs = [_sidecar(f"qc-{index:03d}", "Restrooms") for index in range(field_photos.PAGE_LIMIT + 1)]
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())

    def query(_config: object, mango: dict[str, object]) -> list[dict[str, object]]:
        observed.append(mango)
        return docs

    monkeypatch.setattr(field_photos, "_query_couchdb", query)

    loaded, fallback, has_more = field_photos.load_filtered_photo_sidecars(
        _ctx(tmp_path), site_id="site-one", qc_category=QC_CAPTURE_CATEGORY
    )

    assert len(observed) == 1
    selector = observed[0]
    assert {"qc_category": QC_CAPTURE_CATEGORY} in selector["selector"]["$and"]
    assert selector["limit"] == field_photos.PAGE_LIMIT + 1
    assert len(loaded) == field_photos.PAGE_LIMIT
    assert fallback is False and has_more is True


def test_disk_fallback_filters_qc_before_page_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    non_qc = [
        _sidecar(f"baseline-{index:03d}", "Restrooms", qc_category="baseline")
        for index in range(field_photos.PAGE_LIMIT + 20)
    ]
    qc = [_sidecar(f"qc-{index}", "Restrooms", minute=index) for index in range(3)]
    for index, row in enumerate(non_qc):
        row["generated_at"] = f"2026-07-16T12:{index % 60:02d}:00Z"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _directory: non_qc + qc)

    loaded, fallback, has_more = field_photos.load_filtered_photo_sidecars(
        _ctx(tmp_path), site_id="site-one", qc_category=QC_CAPTURE_CATEGORY
    )

    assert [row["photo_asset_id"] for row in loaded] == ["qc-2", "qc-1", "qc-0"]
    assert all(row["qc_category"] == QC_CAPTURE_CATEGORY for row in loaded)
    assert fallback is False and has_more is False


def test_degraded_site_suggestions_are_derived_only_from_qc_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    docs = [
        _sidecar("qc-site", "Restrooms", site_id="qc-site"),
        _sidecar("baseline-site", "Restrooms", site_id="baseline-only-site", qc_category="baseline"),
    ]
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [])

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        return (
            [row for row in docs if row["qc_category"] == filters.get("qc_category")],
            True,
            False,
        )

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(_ctx(tmp_path))

    assert calls == [{"qc_category": QC_CAPTURE_CATEGORY}]
    assert 'value="qc-site"' in rendered
    assert "qc-site — from photo data" in rendered
    assert "baseline-only-site" not in rendered


def test_qc_category_query_parameter_cannot_override_fixed_lane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        return [], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(
        _ctx(
            tmp_path,
            {
                "site_id": ["site-one"],
                "qc_date": ["2026-07-15"],
                "qc_category": ["baseline"],
            },
        )
    )

    assert calls == [{"site_id": "site-one", "qc_category": QC_CAPTURE_CATEGORY}]
    assert 'name="qc_category"' not in rendered
    assert "No photos for this QC day" in rendered


def test_ordinary_field_photos_keeps_user_selected_category(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    ctx = _ctx(tmp_path, {"site_id": ["site-one"], "qc_category": ["baseline"]})
    ctx.route_path = "/field-photos"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(field_photos, "_query_processed_asset_ids", lambda _config: set())
    monkeypatch.setattr(field_photos, "_query_terminal_capture_ids", lambda _config: set())
    monkeypatch.setattr(field_photos, "render_filter_form", lambda **_filters: "<form></form>")
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(field_photos, "_pending_photo_records", lambda *_args, **_kwargs: [])

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        return [], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(ctx)

    assert len(calls) == 1
    assert calls[0]["site_id"] == "site-one"
    assert calls[0]["qc_category"] == "baseline"
    assert "Field Photos" in rendered


def test_handoff_scope_is_read_only_and_uses_only_shared_loader_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "render_qc_handoff"
    )
    loader_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_filtered_photo_sidecars"
    ]
    assert len(loader_calls) == 2
    for call in loader_calls:
        qc_keywords = [keyword for keyword in call.keywords if keyword.arg == "qc_category"]
        assert len(qc_keywords) == 1
        assert isinstance(qc_keywords[0].value, ast.Name)
        assert qc_keywords[0].value.id == "QC_CAPTURE_CATEGORY"

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(
        field_photos,
        "load_filtered_photo_sidecars",
        lambda _ctx, **_filters: ([_sidecar("qc-only", "Restrooms")], False, False),
    )
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    assert before == after == []
    assert 'method="post" action="/field-photos/export"' in rendered
    assert source.count("def render_photo_file_drag_script(") == 1
    assert source.count("window.BTQPhotoFileDrag = {init: init, prepare: prepare};") == 1
