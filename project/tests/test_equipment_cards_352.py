"""Gating tests for prompt 352: render_equipment_list() card-grid markup.

352 converted render_equipment_list() from a render_table() data-table into a
responsive CARD GRID (one <article> per equipment request inside a
grid-template-columns:repeat(auto-fill,minmax(260px,1fr)) wrapper). These tests
drive render_equipment_list() directly and pin the card markup, the per-card
action link (same text render_row_actions() returns), empty-field omission, the
archived notice + restore action, and the empty-list behavior. Mirrors prompt
349's test_supply_cards_349.py for the supply-card analog.
"""
from __future__ import annotations

from ops_dashboard.sections import equipment as equipment_section
from ops_dashboard.sections.equipment import render_equipment_list, render_row_actions


def _open_equipment(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "equipment_id": "eqr_7050",
        "_id": "equipment_request_eqr_7050",
        "equipment_name": "Floor buffer",
        "status": "open",
        "priority": "high",
        "site_id": "7050",
        "site_name": "Acme Plant",
        "requested_by": "Sandy Sandbox",
        "reason": "East wing buffer is out of service.",
        "archived": False,
    }
    base.update(overrides)
    return base


def test_card_grid_markup_present_and_no_table() -> None:
    html_out = render_equipment_list([_open_equipment()])
    # Responsive card grid wrapper.
    assert "display:grid" in html_out
    assert "minmax(260px,1fr)" in html_out
    # One <article> per request.
    assert html_out.count("<article") == 1
    # NOT a table any more.
    assert "<table" not in html_out
    assert "data-table" not in html_out


def test_equipment_name_linked_and_status_pill_and_site_label_present() -> None:
    html_out = render_equipment_list([_open_equipment()])
    # Equipment name inside the equipment-detail link.
    assert '<a href="/equipment?equipment_id=eqr_7050">Floor buffer</a>' in html_out
    # Status pill.
    assert '<span class="pill">Open</span>' in html_out
    # Site label.
    assert 'class="site-label"' in html_out
    assert "(7050)" in html_out


def test_action_link_from_render_row_actions_is_in_card() -> None:
    request = _open_equipment()
    actions = render_row_actions(request)
    # An open equipment request must have at least one transition action.
    assert actions.strip(), "expected render_row_actions to emit actions for an open request"
    html_out = render_equipment_list([request])
    # The exact action link(s) render_row_actions returns must appear in the card.
    assert actions in html_out
    # And specifically the human-visible action text.
    assert "Mark approved" in html_out


def test_empty_optional_fields_omit_lines_no_none_no_empty_labels() -> None:
    request = _open_equipment(
        requested_by="",
        reason="",
        _id="",
    )
    html_out = render_equipment_list([request])
    # No literal "None" leaks from .get() on missing values.
    assert "None" not in html_out
    # Omitted optional rows must not render their labels at all.
    assert "Requested by:" not in html_out
    assert "ID:" not in html_out
    # Site is always present even when other meta is omitted.
    assert "Site:" in html_out


def test_archived_request_shows_notice_and_restore_action() -> None:
    request = _open_equipment(
        equipment_id="eqr_arch",
        archived=True,
        archived_at="2026-06-11",
        archived_by="Sandy Sandbox",
    )
    html_out = render_equipment_list([request])
    # Archived notice present.
    assert "Archived" in html_out
    # Restore action present (render_row_actions returns the restore form when archived).
    assert 'action="/equipment/restore"' in html_out
    assert ">Restore</button>" in html_out
    # The restore form is the action render_row_actions returns for an archived request.
    assert render_row_actions(request) in html_out


def test_empty_list_returns_exact_no_equipment_message() -> None:
    assert render_equipment_list([]) == "<p>No equipment requests found.</p>"
    # Non-dict entries are filtered out, so an all-junk list is also "empty".
    assert render_equipment_list(["junk", 123, None]) == "<p>No equipment requests found.</p>"


def test_two_requests_render_two_article_blocks() -> None:
    html_out = render_equipment_list(
        [
            _open_equipment(equipment_id="eqr_a", _id="equipment_request_eqr_a", equipment_name="Vacuum"),
            _open_equipment(equipment_id="eqr_b", _id="equipment_request_eqr_b", equipment_name="Mop bucket"),
        ]
    )
    assert html_out.count("<article") == 2
    assert "Vacuum" in html_out
    assert "Mop bucket" in html_out
