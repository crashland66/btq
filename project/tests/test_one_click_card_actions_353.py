"""Gating tests for prompt 353: one-click POST-form card action affordances.

353 changed supplies.render_row_actions() and equipment.render_row_actions()
from emitting confirm-SCREEN links (<a href=".../<route>-confirm?...">) to
emitting DIRECT one-click POST FORMS (mirroring render_restore_form): one
<form method="post" action="{post_route}"> per valid_transitions() entry, each
carrying hidden {supply_id|equipment_id}, hidden actor=default_actor(), hidden
confirm=1, and a <button> labeled with the transition label. Archived items
still render the restore form. These tests drive render_row_actions() directly
on dict fixtures and pin the new POST-form markup, the absence of any -confirm
href, the transition set per status, the archived restore-form behavior, and
the presence of the hidden auth fields.
"""
from __future__ import annotations

from ops_dashboard.sections import equipment as equipment_section
from ops_dashboard.sections import supplies as supplies_section
from ops_dashboard.sections.equipment import render_row_actions as equipment_row_actions
from ops_dashboard.sections.supplies import render_row_actions as supplies_row_actions


def _supply(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "supply_id": "sup_7050",
        "_id": "supply_need_sup_7050",
        "item_name": "BrightWash cleaner",
        "status": "open",
        "archived": False,
    }
    base.update(overrides)
    return base


def _equipment(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "equipment_id": "eqr_7050",
        "_id": "equipment_request_eqr_7050",
        "equipment_name": "Floor buffer",
        "status": "open",
        "archived": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Supplies one-click POST form (no confirm-screen href)
# ---------------------------------------------------------------------------
def test_supplies_open_emits_one_click_post_form_no_confirm_href() -> None:
    out = supplies_row_actions(_supply(status="open"))
    # Direct POST form to the mark-ordered route.
    assert '<form method="post" action="/supplies/mark-ordered"' in out
    # Hidden supply_id carrying the id.
    assert 'name="supply_id"' in out
    assert 'value="sup_7050"' in out
    # Hidden actor field.
    assert 'name="actor"' in out
    # Hidden confirm field with value "1".
    assert 'name="confirm" value="1"' in out
    # A button labeled with the transition label.
    assert ">Mark ordered</button>" in out
    # No confirm-SCREEN link anywhere.
    assert "-confirm" not in out
    assert 'href="/supplies/mark-ordered-confirm' not in out
    # And no anchor link at all in the action affordance.
    assert "<a href=" not in out


# ---------------------------------------------------------------------------
# 2. Equipment one-click POST form (no confirm-screen href)
# ---------------------------------------------------------------------------
def test_equipment_open_emits_one_click_post_form_no_confirm_href() -> None:
    out = equipment_row_actions(_equipment(status="open"))
    # Direct POST form to the mark-approved route.
    assert '<form method="post" action="/equipment/mark-approved"' in out
    # Hidden equipment_id carrying the id.
    assert 'name="equipment_id"' in out
    assert 'value="eqr_7050"' in out
    # Hidden actor field.
    assert 'name="actor"' in out
    # Hidden confirm field with value "1".
    assert 'name="confirm" value="1"' in out
    # A button labeled with the transition label.
    assert ">Mark approved</button>" in out
    # No confirm-SCREEN link anywhere.
    assert "-confirm" not in out
    assert 'href="/equipment/mark-approved-confirm' not in out
    assert "<a href=" not in out


# ---------------------------------------------------------------------------
# 3. Transition SET still matches status
# ---------------------------------------------------------------------------
def test_supplies_ordered_transition_set_matches_status() -> None:
    out = supplies_row_actions(_supply(status="ordered"))
    # ordered -> delivered and -> no-action-needed forms present.
    assert '<form method="post" action="/supplies/mark-delivered"' in out
    assert '<form method="post" action="/supplies/mark-no-action-needed"' in out
    # But NOT a mark-ordered form (the trailing quote guards substring match).
    assert 'action="/supplies/mark-ordered"' not in out


def test_equipment_approved_transition_set_matches_status() -> None:
    # approved -> {mark-ordered, mark-denied, mark-no-action-needed}; NOT mark-approved.
    out = equipment_row_actions(_equipment(status="approved"))
    assert '<form method="post" action="/equipment/mark-ordered"' in out
    assert '<form method="post" action="/equipment/mark-denied"' in out
    assert '<form method="post" action="/equipment/mark-no-action-needed"' in out
    # mark-approved is an open-only transition; must not appear for an approved request.
    assert 'action="/equipment/mark-approved"' not in out


# ---------------------------------------------------------------------------
# 4. Archived -> restore form (unchanged)
# ---------------------------------------------------------------------------
def test_supplies_archived_renders_restore_form_only() -> None:
    out = supplies_row_actions(
        _supply(supply_id="sup_arch", status="open", archived=True)
    )
    assert 'action="/supplies/restore"' in out
    assert ">Restore</button>" in out
    # No transition forms when archived.
    assert 'action="/supplies/mark-ordered"' not in out
    assert 'action="/supplies/mark-no-action-needed"' not in out


def test_equipment_archived_renders_restore_form_only() -> None:
    out = equipment_row_actions(
        _equipment(equipment_id="eqr_arch", status="open", archived=True)
    )
    assert 'action="/equipment/restore"' in out
    assert ">Restore</button>" in out
    # No transition forms when archived.
    assert 'action="/equipment/mark-approved"' not in out
    assert 'action="/equipment/mark-no-action-needed"' not in out


# ---------------------------------------------------------------------------
# 5. All hidden auth fields present on a transition form
# ---------------------------------------------------------------------------
def test_supplies_transition_form_carries_confirm_and_actor() -> None:
    out = supplies_row_actions(_supply(status="open"))
    assert 'name="confirm" value="1"' in out
    assert 'name="actor"' in out


def test_equipment_transition_form_carries_confirm_and_actor() -> None:
    out = equipment_row_actions(_equipment(status="open"))
    assert 'name="confirm" value="1"' in out
    assert 'name="actor"' in out
