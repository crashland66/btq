"""Gating tests for prompt 349: render_supply_list() card-grid markup.

349 converted render_supply_list() from a render_table() data-table into a
responsive CARD GRID (one <article> per supply inside a
grid-template-columns:repeat(auto-fill,minmax(260px,1fr)) wrapper). These tests
drive render_supply_list() directly and pin the card markup, the per-card
action link (same text render_row_actions() returns), empty-field omission, the
archived notice + restore action, and the empty-list/empty-count behaviors.
"""
from __future__ import annotations

from ops_dashboard.sections import supplies as supplies_section
from ops_dashboard.sections.supplies import render_row_actions, render_supply_list


def _open_supply(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "supply_id": "sup_7050",
        "_id": "supply_need_sup_7050",
        "item_name": "BrightWash cleaner",
        "status": "open",
        "urgency": "high",
        "site_id": "7050",
        "site_name": "Acme Plant",
        "quantity_needed": "3 cases",
        "requested_by": "Sandy Sandbox",
        "notes": "Running low in the east supply closet.",
        "archived": False,
    }
    base.update(overrides)
    return base


def test_card_grid_markup_present_and_no_table() -> None:
    html_out = render_supply_list([_open_supply()])
    # Responsive card grid wrapper.
    assert "display:grid" in html_out
    assert "minmax(260px,1fr)" in html_out
    # One <article> per supply.
    assert html_out.count("<article") == 1
    # NOT a table any more.
    assert "<table" not in html_out
    assert "data-table" not in html_out


def test_item_name_linked_and_status_pill_and_site_label_present() -> None:
    html_out = render_supply_list([_open_supply()])
    # Item name inside the supply-detail link.
    assert '<a href="/supplies?supply_id=sup_7050">BrightWash cleaner</a>' in html_out
    # Status pill.
    assert '<span class="pill">Open</span>' in html_out
    # Site label.
    assert 'class="site-label"' in html_out
    assert "(7050)" in html_out


def test_action_link_from_render_row_actions_is_in_card() -> None:
    supply = _open_supply()
    actions = render_row_actions(supply)
    # An open supply must have at least one transition action.
    assert actions.strip(), "expected render_row_actions to emit actions for an open supply"
    html_out = render_supply_list([supply])
    # The exact action link(s) render_row_actions returns must appear in the card.
    assert actions in html_out
    # And specifically the human-visible action text.
    assert "Mark ordered" in html_out


def test_empty_optional_fields_omit_lines_no_none_no_empty_labels() -> None:
    supply = _open_supply(
        quantity_needed="",
        requested_by="",
        notes="",
        _id="",
    )
    html_out = render_supply_list([supply])
    # No literal "None" leaks from .get() on missing values.
    assert "None" not in html_out
    # Omitted optional rows must not render their labels at all.
    assert "Quantity:" not in html_out
    assert "Requested by:" not in html_out
    assert "ID:" not in html_out
    # Site is always present even when other meta is omitted.
    assert "Site:" in html_out


def test_archived_supply_shows_notice_and_restore_action() -> None:
    supply = _open_supply(
        supply_id="sup_arch",
        archived=True,
        archived_at="2026-06-11",
        archived_by="Sandy Sandbox",
    )
    html_out = render_supply_list([supply])
    # Archived notice present.
    assert "Archived" in html_out
    # Restore action present (render_row_actions returns the restore form when archived).
    assert 'action="/supplies/restore"' in html_out
    assert ">Restore</button>" in html_out
    # The restore form is the action render_row_actions returns for an archived supply.
    assert render_row_actions(supply) in html_out


def test_empty_list_returns_exact_no_supply_needs_message() -> None:
    assert render_supply_list([]) == "<p>No supply needs found.</p>"
    # Non-dict entries are filtered out, so an all-junk list is also "empty".
    assert render_supply_list(["junk", 123, None]) == "<p>No supply needs found.</p>"


def test_two_supplies_render_two_article_blocks() -> None:
    html_out = render_supply_list(
        [
            _open_supply(supply_id="sup_a", _id="supply_need_sup_a", item_name="Mop heads"),
            _open_supply(supply_id="sup_b", _id="supply_need_sup_b", item_name="Hand soap"),
        ]
    )
    assert html_out.count("<article") == 2
    assert "Mop heads" in html_out
    assert "Hand soap" in html_out
