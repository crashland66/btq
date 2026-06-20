"""Gating contract for prompt 404: a supply/equipment job requires an EXPRESSED need.

Authored by the INDEPENDENT VERIFIER (not the executor). Composes on top of 401
(multi-item split) + 402 (operator-classification override) + 403 (contentless
guard).

The bug (live 2026-06-19): a field note whose entire text is "Safety supply
room" (a location LABEL -- a PPE room for the plant's staff, not a B&T supply)
produced a ``log_supply_need`` candidate. Root cause: ``supply_category_actions``
manufactured a supply action whenever the AREA was supply-classified, regardless
of whether any need was expressed.

The fix (``processing_core/capture_semantics.py``): a deterministic
``need_signal_present(text)`` (reusing the need vocab; word-boundary aware so it
does NOT match "supply" inside "supply room"). A supply/equipment job is
suppressed when the human source text has NO need signal -- applied in
``supply_category_actions`` (the area-forced path) AND to a model-emitted
``log_supply_need`` / ``log_equipment_request`` with no need signal.

THE CRITICAL OVER-SUPPRESSION GATE: a real need that merely NAMES a room must
still fire. The guard discriminates on ABSENCE of a need signal, never on
room/label words.

These tests drive the REAL composed path end to end:

    hand-built model payload
      -> extracted_actions_from_model_payload   (402 override + 404 guard +
                                                  area-forced supply_category)
      -> serialize to a semantic artifact dict
      -> structured_payloads_from_semantic       (401/403 split)

so a build-to-the-test impl can not slip past: the asserted candidates are
whatever production actually emits. The helper is also unit-tested in isolation.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from field_capture.action_candidates import structured_payloads_from_semantic
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    extracted_actions_from_model_payload,
    need_signal_present,
)
from queue_spec import JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_SUPPLY_NEED


# --------------------------------------------------------------------------- #
# Harness -- sandbox identity. ``area`` is parameterizable: a supply-classified
# area ("Supply Room") drives the area-forced post-step (the live-bug path); a
# non-supply area ("") isolates the model-emitted guard path.
# --------------------------------------------------------------------------- #
def _input(note: str, area: str = "") -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-404",
        source_kind="ops_dashboard_text",
        source_text=note,
        site_id="SANDBOX",
        site_label="Sandbox Site",
        area=area,
    )


def _model_payload(actions: list[dict]) -> dict:
    return {"extracted_actions": actions}


def _supply_action(item: str, *, excerpt: str, job_type: str = JOB_LOG_SUPPLY_NEED) -> dict:
    field = "item_name" if job_type == JOB_LOG_SUPPLY_NEED else "equipment_name"
    return {
        "action_key": "k",
        "candidate_type": "field_capture_follow_up",
        "target_type": "site",
        "target_label": "Sandbox Site",
        "summary": f"Supply request: {item}.",
        "source_excerpt": excerpt,
        "job_type": job_type,
        "payload_fields": {field: item},
    }


def _site_issue_action(excerpt: str) -> dict:
    return {
        "action_key": "k",
        "candidate_type": "field_capture_follow_up",
        "target_type": "site",
        "target_label": "Sandbox Site",
        "summary": "Site issue.",
        "source_excerpt": excerpt,
        "job_type": "log_site_issue",
        "payload_fields": {"issue_summary": excerpt},
    }


def _candidates(note: str, model_actions: list[dict], *, area: str = "") -> list[dict]:
    """Full composed path: model payload -> 402/404 -> artifact -> 401/403 split."""
    actions = extracted_actions_from_model_payload(
        _model_payload(model_actions),
        _input(note, area=area),
    )
    artifact = {
        "capture_id": "cap-404",
        "upload_id": "cap-404",
        "extracted_actions": [asdict(a) for a in actions],
    }
    artifact = json.loads(json.dumps(artifact))  # tuple -> list, exactly like on-disk
    return structured_payloads_from_semantic(Path("cap-404.json"), artifact)


def _job_type(candidate: dict) -> str:
    return (candidate.get("channel_metadata") or {}).get("job_type") or ""


def _supply_equipment_candidates(candidates: list[dict]) -> list[dict]:
    return [
        c
        for c in candidates
        if _job_type(c) in {JOB_LOG_SUPPLY_NEED, JOB_LOG_EQUIPMENT_REQUEST}
    ]


def _items(candidate: dict) -> str:
    pf = (candidate.get("channel_metadata") or {}).get("payload_fields") or {}
    return (pf.get("item_name") or pf.get("equipment_name") or "").strip()


# =========================================================================== #
# CRITERION 1 -- THE LIVE CASE. "Safety supply room" in a supply-classified area
# -> ZERO supply/equipment candidates. This is the gating test the mutation
# check flips RED.
# =========================================================================== #
def test_live_safety_supply_room_area_forced_emits_zero_supply_candidates() -> None:
    # The exact live regression. Supply-classified AREA, but the note is a pure
    # location LABEL with no expressed need. The model emits nothing; the
    # area-forced post-step previously manufactured a spurious log_supply_need.
    cands = _candidates("Safety supply room", [], area="Supply Room")
    assert _supply_equipment_candidates(cands) == [], (
        "label-only note in a supply area leaked a supply/equipment candidate: "
        f"{[_items(c) for c in _supply_equipment_candidates(cands)]}"
    )


def test_live_safety_supply_room_even_if_model_misemits_supply() -> None:
    # Belt-and-suspenders: even if the model itself misroutes "Safety supply
    # room" to a log_supply_need, the no-need guard suppresses it.
    cands = _candidates(
        "Safety supply room",
        [_supply_action("safety supplies", excerpt="Safety supply room")],
        area="Supply Room",
    )
    assert _supply_equipment_candidates(cands) == [], (
        f"misemitted supply candidate survived: {[_items(c) for c in cands]}"
    )


# =========================================================================== #
# CRITERION 2 -- THE CRITICAL OVER-SUPPRESSION GATE.
# Real needs that mention rooms MUST still fire. Each produces ONE supply_need.
# A real need is never dropped just because it names a room.
# =========================================================================== #
REAL_NEEDS_NAMING_ROOMS = [
    "the supply closet is empty",
    "need towels in the supply room",
    "out of liners in the janitor's room",
    "need 2 cases of floor cleaner",
    "running low on trash bags",
    "order more gloves",
]


@pytest.mark.parametrize("note", REAL_NEEDS_NAMING_ROOMS)
def test_real_need_in_supply_area_still_fires(note: str) -> None:
    # Area-forced path: a genuine need in a supply-classified area still yields a
    # supply candidate.
    cands = _candidates(note, [], area="Supply Room")
    se = _supply_equipment_candidates(cands)
    assert len(se) == 1, f"real need wrongly suppressed (area path) {note!r}: {[_items(c) for c in cands]}"
    assert _job_type(se[0]) == JOB_LOG_SUPPLY_NEED


@pytest.mark.parametrize("note", REAL_NEEDS_NAMING_ROOMS)
def test_real_need_model_emitted_still_fires(note: str) -> None:
    # Model-emitted path (non-supply area): a genuine need is not stripped by the
    # 404 guard.
    cands = _candidates(note, [_supply_action("towels", excerpt=note)], area="")
    se = _supply_equipment_candidates(cands)
    assert len(se) >= 1, f"real need wrongly suppressed (model path) {note!r}: {[_items(c) for c in cands]}"


# =========================================================================== #
# CRITERION 3 -- label-only variants (no need term, no quantity) -> no job.
# =========================================================================== #
LABEL_ONLY = ["supply closet", "equipment room", "janitor supply room", "PPE room"]


@pytest.mark.parametrize("note", LABEL_ONLY)
def test_label_only_area_forced_no_job(note: str) -> None:
    cands = _candidates(note, [], area="Supply Room")
    assert _supply_equipment_candidates(cands) == [], (
        f"label-only note manufactured a job (area path) {note!r}: {[_items(c) for c in cands]}"
    )


@pytest.mark.parametrize("note", LABEL_ONLY)
def test_label_only_model_emitted_suppressed(note: str) -> None:
    cands = _candidates(note, [_supply_action("supplies", excerpt=note)], area="")
    assert _supply_equipment_candidates(cands) == [], (
        f"label-only note survived as model job {note!r}: {[_items(c) for c in cands]}"
    )


# =========================================================================== #
# CRITERION 4 -- word-boundary correctness. The guard keys on NEED terms, never
# on the substrings supply/room/equipment.
# =========================================================================== #
def test_guard_does_not_match_supply_inside_supply_room() -> None:
    # "supply room" alone is not a need (the substring "supply" must not satisfy
    # the guard); "low on X" is.
    assert need_signal_present("supply room") is False
    assert need_signal_present("equipment room") is False
    assert need_signal_present("safety supply room") is False
    assert need_signal_present("low on trash bags") is True


@pytest.mark.parametrize(
    "note, expected",
    [
        ("supply room", False),
        ("the supply closet is empty", True),
        ("need towels in the supply room", True),
        ("out of liners in the janitor's room", True),
        ("need 2 cases of floor cleaner", True),
        ("running low on trash bags", True),
        ("order more gloves", True),
        ("PPE room", False),
        ("janitor supply room", False),
        ("equipment room", False),
        # quantity-only need (no need verb) still fires.
        ("2 cases of liners", True),
        ("a case of cups", True),
    ],
)
def test_need_signal_present_helper_battery(note: str, expected: bool) -> None:
    assert need_signal_present(note) is expected, f"{note!r} -> {need_signal_present(note)} (expected {expected})"


# =========================================================================== #
# CRITERION 5 -- NO regression on unrelated routes.
# =========================================================================== #
def test_site_issue_not_suppressed_in_supply_area() -> None:
    # A log_site_issue in a supply area must pass through untouched -- the guard
    # is scoped to supply/equipment jobs only.
    note = "broken light fixture in the supply room"
    cands = _candidates(note, [_site_issue_action(note)], area="Supply Room")
    assert any(_job_type(c) == "log_site_issue" for c in cands), [_job_type(c) for c in cands]


def test_real_need_with_quantity_only_fires_via_composed_path() -> None:
    # A need expressed only as a QUANTITY ("2 cases of liners"), no need verb.
    cands = _candidates(
        "2 cases of liners",
        [_supply_action("liners", excerpt="2 cases of liners")],
        area="",
    )
    se = _supply_equipment_candidates(cands)
    assert len(se) >= 1, f"quantity-only need wrongly suppressed: {[_items(c) for c in cands]}"


def test_equipment_request_label_only_suppressed() -> None:
    # Equipment side of the guard: a label-only equipment note is suppressed.
    cands = _candidates(
        "equipment room",
        [_supply_action("machine", excerpt="equipment room", job_type=JOB_LOG_EQUIPMENT_REQUEST)],
        area="",
    )
    assert _supply_equipment_candidates(cands) == [], [_items(c) for c in cands]


def test_equipment_request_real_need_fires() -> None:
    cands = _candidates(
        "need a new vacuum for the equipment room",
        [_supply_action("vacuum", excerpt="need a new vacuum", job_type=JOB_LOG_EQUIPMENT_REQUEST)],
        area="",
    )
    se = _supply_equipment_candidates(cands)
    assert len(se) >= 1, f"real equipment need suppressed: {[_items(c) for c in cands]}"


# =========================================================================== #
# Helper unit battery -- need vs no-need, in isolation. Word-boundary aware.
# =========================================================================== #
@pytest.mark.parametrize(
    "note",
    [
        # Label/location only -- no need expressed.
        "Safety supply room", "supply room", "supply closet", "equipment room",
        "janitor supply room", "PPE room", "safety supply room",
        "", "   ",
    ],
)
def test_helper_flags_no_need(note: str) -> None:
    assert need_signal_present(note) is False, f"{note!r} wrongly read as a need signal"


@pytest.mark.parametrize(
    "note",
    [
        "the supply closet is empty",
        "need towels in the supply room",
        "out of liners in the janitor's room",
        "need 2 cases of floor cleaner",
        "running low on trash bags",
        "order more gloves",
        "restock paper",
        "reorder gloves",
        "we need soap",
        "soap is out",
        "no more liners",
        "almost out of degreaser",
        "bring more rags next visit",
        "2 cases of liners",
        # operator declaration is always a need.
        "supply need: paper towels",
        "equipment: backpack vacuum",
    ],
)
def test_helper_detects_need(note: str) -> None:
    assert need_signal_present(note) is True, f"{note!r} wrongly read as no-need"
