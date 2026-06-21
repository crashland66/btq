"""411: /field-photos 'flagged for deeper analysis' filter."""
from __future__ import annotations

from ops_dashboard.sections.field_photos import (
    _build_mango_selector,
    _filter_form,
    _in_memory_filter,
)

_SIDECARS = [
    {"photo_asset_id": "a", "deep_analysis": [{"result": "x"}], "generated_at": "2026-06-01"},
    {"photo_asset_id": "b", "deep_analysis": [], "generated_at": "2026-06-02"},
    {"photo_asset_id": "c", "generated_at": "2026-06-03"},
]


def _ids(rows):
    return sorted(str(r.get("photo_asset_id")) for r in rows)


def test_in_memory_filter_on_keeps_only_nonempty_deep_analysis():
    assert _ids(_in_memory_filter(_SIDECARS, "", "", "", "", "", has_deep_analysis=True)) == ["a"]


def test_in_memory_filter_off_keeps_all():
    assert _ids(_in_memory_filter(_SIDECARS, "", "", "", "", "", has_deep_analysis=False)) == ["a", "b", "c"]


def test_mango_selector_includes_nonempty_list_clause_only_when_on():
    on = _build_mango_selector("", "", "", "", "", has_deep_analysis=True)
    off = _build_mango_selector("", "", "", "", "", has_deep_analysis=False)
    assert "deep_analysis" in str(on)
    assert "deep_analysis" not in str(off)


def test_filter_form_renders_checkbox_with_state():
    checked = _filter_form("", "", "", "", "", True, site_options=[], area_options=[])
    unchecked = _filter_form("", "", "", "", "", False, site_options=[], area_options=[])
    assert 'name="deep_analysis"' in checked and "checked" in checked
    assert 'name="deep_analysis"' in unchecked and "checked" not in unchecked
