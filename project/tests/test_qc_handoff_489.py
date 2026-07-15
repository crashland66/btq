"""Independent verifier coverage for the full-day QC handoff contract.

Fixtures are synthetic and all integration boundaries are replaced with local
stubs.  These tests do not read or mutate operational data.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from field_capture.display_categories import BUILTIN_FALLBACK_CATEGORIES
from ops_dashboard.sections import field_photos
from tests.test_qc_handoff_488 import _ctx, _needs_assets, _section_assets, _sidecar


_SOURCE = Path(field_photos.__file__).resolve()
_CSS = _SOURCE.parents[1] / "static" / "admin.css"


def _with_times(
    sidecar: dict[str, object],
    *,
    provenance_captured_at: str | None = None,
    captured_at: str | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    result = dict(sidecar)
    provenance = dict(result.get("provenance") or {})
    if provenance_captured_at is None:
        provenance.pop("captured_at", None)
    else:
        provenance["captured_at"] = provenance_captured_at
    result["provenance"] = provenance
    if captured_at is None:
        result.pop("captured_at", None)
    else:
        result["captured_at"] = captured_at
    if generated_at is None:
        result.pop("generated_at", None)
    else:
        result["generated_at"] = generated_at
    return result


@pytest.mark.parametrize(
    ("sidecar", "expected"),
    [
        (
            _with_times(
                _sidecar("provenance", "Restrooms"),
                provenance_captured_at="2026-07-15T23:58:00-04:00",
                captured_at="2026-07-16T00:02:00-04:00",
                generated_at="2026-07-16T05:00:00Z",
            ),
            "2026-07-15",
        ),
        (
            _with_times(
                _sidecar("captured", "Restrooms"),
                captured_at="2026-07-14T23:59:00+09:00",
                generated_at="2026-07-15T14:00:00Z",
            ),
            "2026-07-14",
        ),
        (
            _with_times(
                _sidecar("generated", "Restrooms"),
                generated_at="2026-07-13T01:00:00Z",
            ),
            "2026-07-13",
        ),
    ],
)
def test_local_date_uses_evidence_precedence_and_preserves_encoded_calendar_day(
    sidecar: dict[str, object], expected: str
) -> None:
    assert field_photos._handoff_local_capture_date(sidecar) == expected


def test_one_site_and_local_date_aggregates_every_loaded_capture_without_midnight_misbucketing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _with_times(
        _sidecar("first", "Restrooms", capture_id="capture-alpha"),
        provenance_captured_at="2026-07-15T23:55:00-04:00",
        generated_at="2026-07-16T04:20:00Z",
    )
    second = _with_times(
        _sidecar("second", "Hallways", capture_id="capture-beta"),
        captured_at="2026-07-15T08:00:00-04:00",
        generated_at="2026-07-16T01:00:00Z",
    )
    duplicate = dict(first)
    other_day = _with_times(
        _sidecar("other-day", "Trash", capture_id="capture-gamma"),
        provenance_captured_at="2026-07-16T00:01:00-04:00",
        generated_at="2026-07-16T04:30:00Z",
    )
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("site-one", "Synthetic")])
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(
        field_photos,
        "load_site_handoff_categories",
        lambda site_id: BUILTIN_FALLBACK_CATEGORIES if site_id == "site-one" else pytest.fail(site_id),
    )

    def load(_ctx: object, **filters: object) -> tuple[list[dict[str, object]], bool, bool]:
        calls.append(filters)
        return [first, second, duplicate, other_day], False, False

    monkeypatch.setattr(field_photos, "load_filtered_photo_sidecars", load)
    rendered = field_photos.render(
        _ctx(tmp_path, {"site_id": ["site-one"], "qc_date": ["2026-07-15"]})
    )

    assert calls == [
        {"site_id": "site-one", "qc_category": field_photos.QC_CAPTURE_CATEGORY}
    ]
    assert "2 photos on this QC day" in rendered
    assert rendered.count('name="media_key" value="first.jpg"') == 1
    assert rendered.count('name="media_key" value="second.jpg"') == 1
    assert "other-day.jpg" not in rendered
    assert "capture-alpha" not in rendered
    assert "capture-beta" not in rendered
    assert 'name="capture_id" value="site-one-2026-07-15"' in rendered


def test_date_discovery_has_one_row_per_date_deduped_photo_and_capture_counts_without_ids() -> None:
    docs = [
        _sidecar("one", "Restrooms", capture_id="secret-capture-alpha", minute=1),
        _sidecar("two", "Hallways", capture_id="secret-capture-alpha", minute=2),
        _sidecar("three", "Trash", capture_id="secret-capture-beta", minute=3),
        _sidecar("three", "Trash", capture_id="secret-capture-beta", minute=4),
        _with_times(
            _sidecar("previous", "Restrooms", capture_id="secret-capture-old"),
            provenance_captured_at="2026-07-14T22:00:00-04:00",
        ),
    ]

    summaries = field_photos._handoff_date_discovery(docs)
    assert [(row["date"], row["count"], len(row["capture_ids"])) for row in summaries] == [
        ("2026-07-15", 3, 2),
        ("2026-07-14", 1, 1),
    ]

    rendered = field_photos._render_handoff_discovery(
        summaries, site_id="site-one", fallback=False, has_more=False
    )
    assert rendered.count('class="qc-handoff-date-choice"') == 2
    assert rendered.count("Open 2026-07-15") == 1
    assert "3 photos · 2 contributing captures" in rendered
    assert "1 photo · 1 contributing capture" in rendered
    assert "secret-capture" not in rendered
    assert "capture_id=" not in rendered


def test_category_precedence_uses_recognized_vision_then_non_operational_operator_fallback() -> None:
    categories = [
        {"label": "Hallways", "canonical": "Hallways"},
        {"label": "Restrooms", "canonical": "Restrooms"},
        {"label": "QC", "canonical": "qc"},
        {"label": "Baseline", "canonical": "baseline"},
        {"label": "Pre-engagement", "canonical": "pre_engagement"},
        {"label": "Report an issue", "canonical": "report_an_issue"},
    ]
    docs = [
        _sidecar("vision-mismatch", "Hallways", qc_category="Restrooms", agreement="mismatch"),
        _sidecar("vision-recognized", "Hallways", qc_category="Restrooms"),
        _sidecar("operator-unmapped-vision", "Unknown Wing", qc_category="Restrooms"),
        _sidecar("operator-no-vision", "", qc_category="Restrooms"),
        _sidecar("operator-operational-vision", "qc", qc_category="Restrooms"),
        _sidecar("neither-generic", "qc", qc_category="baseline"),
        _sidecar("neither-unmapped", "Unknown Wing", qc_category="report_an_issue"),
    ]

    grouped = field_photos.group_handoff_sidecars(docs + docs, categories)
    assert _section_assets(grouped) == {
        "Hallways": ["vision-mismatch", "vision-recognized"],
        "Restrooms": [
            "operator-unmapped-vision",
            "operator-no-vision",
            "operator-operational-vision",
        ],
    }
    assert _needs_assets(grouped) == ["neither-generic", "neither-unmapped"]
    assert not ({"qc", "baseline", "pre_engagement", "report_an_issue"} & _section_assets(grouped).keys())
    emitted = _needs_assets(grouped) + [
        asset for assets in _section_assets(grouped).values() for asset in assets
    ]
    assert len(emitted) == len(set(emitted)) == grouped["total"] == 7


def test_compact_cards_keep_photo_lightbox_drag_download_selection_and_hide_operational_metadata() -> None:
    sidecar = _sidecar(
        "compact",
        "Restrooms",
        qc_category="sensitive-qc-category",
        capture_id="secret-capture-id",
    )
    sidecar.update(
        {
            "area_guess": "secret-area",
            "description": "secret-description",
            "possible_conditions": ["secret-condition"],
            "possible_issues": ["secret-issue"],
            "deep_analysis": [{"prompt_text": "secret-analysis"}],
        }
    )
    card = field_photos.render_photo_card(
        sidecar,
        {"secret-capture-id": {"submitter_name": "Secret Submitter"}},
        Path("/synthetic/vault"),
        selection_group="Restrooms",
        handoff_presentation=True,
    )

    for hidden in (
        "secret-analysis",
        "secret-area",
        "sensitive-qc-category",
        "secret-description",
        "secret-condition",
        "secret-issue",
        "Secret Submitter",
        "secret-capture-id",
        "Vision agrees",
    ):
        assert hidden not in card
    assert 'class="field-photo-image-link"' in card
    assert "openLb(" in card
    assert 'object-fit:contain' in card
    assert 'data-photo-file-drag' in card
    assert 'data-photo-drag-filename="Restrooms-001-compact.jpg"' in card
    assert 'input type="checkbox" name="media_key" value="compact.jpg"' in card
    assert 'data-photo-group="Restrooms"' in card
    assert 'data-filename-hint="Restrooms-001-compact.jpg"' in card


def test_handoff_reuses_shared_loader_card_media_export_selection_and_drag_once() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    functions = {node.name: node for node in definitions}

    def calls(function_name: str, called_name: str) -> int:
        return sum(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == called_name
            for node in ast.walk(functions[function_name])
        )

    for name in (
        "load_filtered_photo_sidecars",
        "render_photo_card",
        "handle_export_post",
        "render_photo_selection_script",
        "render_photo_file_drag_script",
    ):
        assert sum(node.name == name for node in definitions) == 1
    assert calls("render_qc_handoff", "load_filtered_photo_sidecars") >= 1
    assert calls("_render_handoff_card_shell", "render_photo_card") == 1
    assert calls("_render_card", "safe_media_url") == 1
    assert calls("_render_handoff_board", "render_photo_selection_script") == 1
    assert calls("_render_handoff_board", "render_photo_file_drag_script") == 1
    assert not (_SOURCE.parent / "qc_handoff.py").exists()


def test_archive_label_responsive_grid_and_truthful_empty_degraded_truncated_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    board = field_photos._render_handoff_board(
        _ctx(tmp_path),
        [_sidecar("one", "Restrooms", capture_id="walk-secret")],
        "site-one",
        "2026-07-15",
        True,
        True,
        BUILTIN_FALLBACK_CATEGORIES,
    )
    assert "local photo cache" in board
    assert "may be incomplete" in board
    assert "Downloads include only the photos shown below" in board
    assert "Download visible photos" in board
    assert 'name="capture_id" value="site-one-2026-07-15"' in board
    assert "full walk" not in board.casefold()

    truncated_empty = field_photos._render_handoff_board(
        _ctx(tmp_path), [], "site-one", "2026-07-15", False, True, BUILTIN_FALLBACK_CATEGORIES
    )
    assert "cannot be confirmed empty" in truncated_empty
    assert "More site photos exist beyond" in truncated_empty

    no_dates = field_photos._render_handoff_discovery(
        [], site_id="site-one", fallback=True, has_more=True
    )
    assert "No QC dates found" in no_dates
    assert "usable evidence capture date" in no_dates
    assert "counts may be incomplete" in no_dates

    css = _CSS.read_text(encoding="utf-8").split("/* ---- QC SafetyCulture handoff", 1)[1]
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    medium = css.split("@media (max-width: 1040px)", 1)[1].split("@media (max-width: 760px)", 1)[0]
    small = css.split("@media (max-width: 760px)", 1)[1]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in medium
    assert ".qc-handoff-grid" in small and "grid-template-columns: 1fr" in small


def test_handoff_rejects_unsafe_external_media_without_leaking_metadata() -> None:
    sidecar = _sidecar("unsafe", "Restrooms", capture_id="secret-capture")
    provenance = dict(sidecar["provenance"])
    provenance["image_media_url"] = "https://example.invalid/private.jpg"
    sidecar["provenance"] = provenance

    card = field_photos.render_photo_card(
        sidecar,
        {},
        Path("/synthetic/vault"),
        selection_group="Restrooms",
        handoff_presentation=True,
    )
    assert "example.invalid" not in card
    assert "secret-capture" not in card
    assert "No image" in card
    assert 'name="media_key"' not in card
    assert "data-photo-file-drag" not in card
