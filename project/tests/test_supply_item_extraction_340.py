"""Golden contract for prompt 340: supply ``item_name`` extraction.

Authored by the INDEPENDENT VERIFIER (not the executor). These tests pin the
behavior 340 introduced in ``capture_semantics.py``:

  1. ``supply_item_name_from_text`` / ``clean_supply_item_phrase`` strip vague
     preamble nouns ("a few things / items / stuff / supplies / products") and
     leading articles ("a / an / the / some / a new / new") so the supply
     ``item_name`` is the ACTUAL items, not the lead-in -- while NEVER returning
     "" (a degenerate input falls back to the cleaned source phrase).

  2. The supply hard-signal branch sets ``handled_supply_equipment`` so the
     overlapping empty-``job_type`` ``equipment_review`` follow-up no longer
     double-fires for a supply note (overlap suppression).

The end-to-end cases drive the REAL rule engine through
``run_text_semantic_pipeline`` so a build-to-the-test impl can not slip past:
the asserted ``item_name`` is whatever the production payload actually carries.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import (
    RuleCaptureEngine,
    clean_supply_item_phrase,
    supply_item_name_from_text,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _supply_actions(note: str, *, area: str = "Supply Levels", upload_id: str = "cap-x") -> list[dict]:
    """Run the real text pipeline and return the log_supply_need extracted actions."""
    artifact = run_text_semantic_pipeline(
        note,
        site_id="SANDBOX",
        upload_id=upload_id,
        area=area,
        engine=RuleCaptureEngine(),
    )
    artifact = json.loads(json.dumps(artifact))  # tuple -> list round-trip
    return [
        action
        for action in artifact["extracted_actions"]
        if action.get("action_key") == "log_supply_need"
    ]


def _supply_item_name(note: str, **kwargs) -> str:
    actions = _supply_actions(note, **kwargs)
    assert len(actions) == 1, f"expected exactly one log_supply_need, got {actions}"
    return (actions[0].get("payload_fields") or {}).get("item_name")


# Each row: (note, expected item_name). The expected value is the ACTUAL items,
# with the preamble/article lead-in stripped.
GOLDEN_CASES = [
    ("Need to order a few things. A new vacuum and lint brushes.", "vacuum, lint brushes"),
    ("We're low on paper towels and soap", "paper towels, soap"),
    ("need to restock mop heads", "mop heads"),
    ("out of trash bags", "trash bags"),
    ("a couple of items - gloves and degreaser", "gloves, degreaser"),
]


# --------------------------------------------------------------------------- #
# Unit: supply_item_name_from_text strips the lead-in down to the real items.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("note, expected", GOLDEN_CASES)
def test_supply_item_name_unit(note: str, expected: str) -> None:
    assert supply_item_name_from_text(note) == expected


# --------------------------------------------------------------------------- #
# End-to-end: the log_supply_need payload carries the corrected item_name.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("note, expected", GOLDEN_CASES)
def test_supply_item_name_end_to_end(note: str, expected: str) -> None:
    assert _supply_item_name(note) == expected


# --------------------------------------------------------------------------- #
# A note that already extracted cleanly stays clean.
# --------------------------------------------------------------------------- #
def test_already_clean_item_name_unchanged() -> None:
    assert supply_item_name_from_text("low on nitrile gloves") == "nitrile gloves"
    assert _supply_item_name("low on nitrile gloves") == "nitrile gloves"


# --------------------------------------------------------------------------- #
# Degenerate input never returns "" and never crashes (the guard).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("degenerate", ["order", "stuff", "a few things", "some supplies", "the", "new", ""])
def test_degenerate_input_never_empty_or_crash(degenerate: str) -> None:
    # clean_supply_item_phrase is the inner guard; supply_item_name_from_text wraps it.
    cleaned = clean_supply_item_phrase(degenerate)
    name = supply_item_name_from_text(degenerate)
    assert isinstance(cleaned, str)
    assert isinstance(name, str)
    # Non-empty input must never collapse to "" (the 340 fallback guarantee).
    if degenerate.strip():
        assert name != "", f"{degenerate!r} -> empty item_name"


# --------------------------------------------------------------------------- #
# Overlap suppression: a supply note that also names equipment fires exactly ONE
# log_supply_need and NO equipment_review (the now-suppressed empty-job follow-up).
# --------------------------------------------------------------------------- #
def test_supply_note_does_not_double_fire_equipment_review() -> None:
    artifact = run_text_semantic_pipeline(
        "Need to order a new vacuum.",
        site_id="SANDBOX",
        upload_id="cap-overlap",
        area="Supply Levels",
        engine=RuleCaptureEngine(),
    )
    artifact = json.loads(json.dumps(artifact))
    actions = artifact["extracted_actions"]

    supply = [a for a in actions if a.get("action_key") == "log_supply_need"]
    equipment = [a for a in actions if a.get("action_key") == "equipment_review"]
    equipment_by_job = [a for a in actions if a.get("job_type") == "log_equipment_request"]

    assert len(supply) == 1, f"expected exactly one log_supply_need, got {actions}"
    assert equipment == [], f"equipment_review must be suppressed for a supply note, got {actions}"
    assert equipment_by_job == [], f"no equipment job_type should remain, got {actions}"
    # And the single supply action carries the cleaned item_name.
    assert (supply[0].get("payload_fields") or {}).get("item_name") == "vacuum"
