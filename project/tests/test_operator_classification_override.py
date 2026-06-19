"""Gating contract for prompt 402: operator classification token overrides model
supply-vs-equipment routing.

Authored by the INDEPENDENT VERIFIER (not the executor).

The bug (live 2026-06-19): a field note ``supply need: squeegee and extension
pole to clean the tall windows`` was extracted by the LLM as a
``log_equipment_request`` (it saw "pole" -> inferred equipment), overriding the
operator's explicit ``supply need:`` declaration. supply = expensed/consumable
(light path); equipment = capitalized (heavy capital-approval). Misrouting a
consumable into capital-approval can stall it.

The fix: a deterministic post-extraction override parses a leading
operator-classification token from the human source text and forces the
supply<->equipment routing accordingly, ONLY when the model action is already
one of ``log_supply_need``/``log_equipment_request``.

These tests drive the REAL model-payload path
(``extracted_actions_from_model_payload``) with a hand-built model action, so a
build-to-the-test impl can not slip past: the asserted job_type + payload is
whatever production actually emits, and the rebuilt ``proposed_queue_job`` must
pass ``validate_job``.
"""
from __future__ import annotations

from processing_core.capture_semantics import (
    CaptureSemanticInput,
    extracted_actions_from_model_payload,
)
from queue_spec import JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_SITE_ISSUE, JOB_LOG_SUPPLY_NEED, validate_job


# --------------------------------------------------------------------------- #
# Harness: drive the real model-payload extraction path. Sandbox identity.
# Non-supply area ("") so the supply-area post-step does not re-derive the
# action -- we assert exactly what the override produced.
# --------------------------------------------------------------------------- #
def _input(note: str) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-402",
        source_kind="ops_dashboard_text",
        source_text=note,
        site_id="SANDBOX",
        site_label="Sandbox Site",
        area="",
    )


def _model_payload(job_type: str, payload_fields: dict, *, excerpt: str) -> dict:
    return {
        "extracted_actions": [
            {
                "action_key": "k",
                "candidate_type": "field_capture_follow_up",
                "target_type": "site",
                "target_label": "Sandbox Site",
                "summary": "summary",
                "source_excerpt": excerpt,
                "job_type": job_type,
                "payload_fields": dict(payload_fields),
            }
        ]
    }


def _single_action(note: str, model_job_type: str, payload_fields: dict):
    actions = extracted_actions_from_model_payload(
        _model_payload(model_job_type, payload_fields, excerpt=note),
        _input(note),
    )
    assert len(actions) == 1, f"expected exactly one action, got {actions}"
    return actions[0]


# --------------------------------------------------------------------------- #
# Criterion 1 (LIVE CASE): "supply need: ..." whose model routing is equipment
# yields log_supply_need with the items in item_name -- NEVER equipment.
# --------------------------------------------------------------------------- #
def test_live_supply_need_overrides_model_equipment_routing() -> None:
    note = "supply need: squeegee and extension pole to clean the tall windows"
    # The model misclassified this as equipment (saw "pole").
    action = _single_action(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "extension pole"})

    assert action.job_type == JOB_LOG_SUPPLY_NEED
    assert action.job_type != JOB_LOG_EQUIPMENT_REQUEST
    # Items land in item_name; the opposite (equipment) field is cleared.
    item_name = (action.payload_fields or {}).get("item_name", "")
    assert "squeegee" in item_name.lower()
    assert "extension pole" in item_name.lower()
    assert "equipment_name" not in (action.payload_fields or {})


# --------------------------------------------------------------------------- #
# Criterion 2: "equipment:" / "equipment need:" forces log_equipment_request
# with the item in equipment_name, even when the model routed it to supply.
# --------------------------------------------------------------------------- #
def test_equipment_token_overrides_model_supply_routing() -> None:
    note = "equipment: backpack vacuum"
    action = _single_action(note, JOB_LOG_SUPPLY_NEED, {"item_name": "backpack vacuum"})

    assert action.job_type == JOB_LOG_EQUIPMENT_REQUEST
    assert "backpack vacuum" in (action.payload_fields or {}).get("equipment_name", "").lower()
    assert "item_name" not in (action.payload_fields or {})


def test_equipment_need_token_overrides_model_supply_routing() -> None:
    note = "equipment need: auto scrubber"
    action = _single_action(note, JOB_LOG_SUPPLY_NEED, {"item_name": "auto scrubber"})

    assert action.job_type == JOB_LOG_EQUIPMENT_REQUEST
    assert "auto scrubber" in (action.payload_fields or {}).get("equipment_name", "").lower()
    assert "item_name" not in (action.payload_fields or {})


# --------------------------------------------------------------------------- #
# Criterion 3: NO classification token -> model job_type is UNCHANGED, both
# directions (no regression on the inference path).
# --------------------------------------------------------------------------- #
def test_no_token_equipment_note_stays_equipment() -> None:
    note = "the vacuum is broken and needs replacing"
    action = _single_action(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "vacuum"})

    assert action.job_type == JOB_LOG_EQUIPMENT_REQUEST
    assert (action.payload_fields or {}).get("equipment_name", "").lower() == "vacuum"


def test_no_token_supply_note_stays_supply() -> None:
    note = "we are low on paper towels"
    action = _single_action(note, JOB_LOG_SUPPLY_NEED, {"item_name": "paper towels"})

    assert action.job_type == JOB_LOG_SUPPLY_NEED
    assert (action.payload_fields or {}).get("item_name", "").lower() == "paper towels"


# --------------------------------------------------------------------------- #
# Criterion 4: the override NEVER reclassifies log_site_issue (or other
# non-material job types) -- even when the excerpt literally LEADS with a token.
# This proves the job_type gate (not merely the regex) protects other types.
# --------------------------------------------------------------------------- #
def test_site_issue_with_leading_token_is_not_reclassified() -> None:
    note = "supply need: report a water leak by the loading dock"
    action = _single_action(note, JOB_LOG_SITE_ISSUE, {"summary": "water leak"})

    assert action.job_type == JOB_LOG_SITE_ISSUE
    assert "item_name" not in (action.payload_fields or {})
    assert "equipment_name" not in (action.payload_fields or {})


def test_site_issue_with_supply_word_in_excerpt_is_not_reclassified() -> None:
    note = "supply closet door lock is broken"
    action = _single_action(note, JOB_LOG_SITE_ISSUE, {"summary": "broken lock"})

    assert action.job_type == JOB_LOG_SITE_ISSUE


# --------------------------------------------------------------------------- #
# Criterion 5: the forced job_type produces a queue_spec-VALID proposed job,
# and provenance/source text is preserved.
# --------------------------------------------------------------------------- #
def test_forced_supply_payload_is_queue_spec_valid_and_preserves_provenance() -> None:
    note = "supply need: squeegee and extension pole to clean the tall windows"
    action = _single_action(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "extension pole"})

    assert action.source_excerpt == note  # source text preserved on the action
    proposed = action.proposed_queue_job
    assert isinstance(proposed, dict), "override must leave a rebuilt proposed_queue_job"
    assert proposed["job_type"] == JOB_LOG_SUPPLY_NEED
    assert validate_job(proposed) is True
    payload = proposed["payload"]
    assert "extension pole" in str(payload.get("item_name", "")).lower()
    # provenance: the originating capture id flows into the rebuilt job.
    assert "cap-402" in payload.get("related_capture_ids", [])


def test_forced_equipment_payload_is_queue_spec_valid() -> None:
    note = "equipment: backpack vacuum"
    action = _single_action(note, JOB_LOG_SUPPLY_NEED, {"item_name": "backpack vacuum"})

    proposed = action.proposed_queue_job
    assert isinstance(proposed, dict)
    assert proposed["job_type"] == JOB_LOG_EQUIPMENT_REQUEST
    assert validate_job(proposed) is True
    assert "backpack vacuum" in str(proposed["payload"].get("equipment_name", "")).lower()


# --------------------------------------------------------------------------- #
# Criterion 6: token parsing is case/spacing/punctuation tolerant; a bare
# mention of "supply"/"equipment" MID-SENTENCE (not a leading declaration) must
# NOT trigger a forced override (false-positive guard).
# --------------------------------------------------------------------------- #
def test_token_parse_is_case_and_spacing_tolerant() -> None:
    variants = [
        ("Supply Need: gloves", JOB_LOG_SUPPLY_NEED, "gloves"),
        ("supply need : trash bags", JOB_LOG_SUPPLY_NEED, "trash bags"),
        ("SUPPLY: degreaser", JOB_LOG_SUPPLY_NEED, "degreaser"),
        ("  supply:   mop heads", JOB_LOG_SUPPLY_NEED, "mop heads"),
        ("Equipment Need: floor buffer", JOB_LOG_EQUIPMENT_REQUEST, "floor buffer"),
    ]
    for note, expected_job, expected_item in variants:
        # Model routed to the OPPOSITE so a flip is observable.
        model_job = JOB_LOG_EQUIPMENT_REQUEST if expected_job == JOB_LOG_SUPPLY_NEED else JOB_LOG_SUPPLY_NEED
        model_fields = {"equipment_name": "x"} if model_job == JOB_LOG_EQUIPMENT_REQUEST else {"item_name": "x"}
        action = _single_action(note, model_job, model_fields)
        assert action.job_type == expected_job, f"{note!r} did not force {expected_job}"
        field = "item_name" if expected_job == JOB_LOG_SUPPLY_NEED else "equipment_name"
        assert expected_item in (action.payload_fields or {}).get(field, "").lower()


def test_mid_sentence_supply_mention_does_not_force_override() -> None:
    note = "the cleaner asked about the supply order yesterday"
    # Model routed to equipment; with no LEADING token, it must STAY equipment.
    action = _single_action(note, JOB_LOG_EQUIPMENT_REQUEST, {"equipment_name": "mop"})
    assert action.job_type == JOB_LOG_EQUIPMENT_REQUEST


def test_mid_sentence_equipment_mention_does_not_force_override() -> None:
    note = "we are low on equipment cleaning wipes"
    # Model routed to supply; "equipment" mid-phrase is NOT a leading token -> stays supply.
    action = _single_action(note, JOB_LOG_SUPPLY_NEED, {"item_name": "wipes"})
    assert action.job_type == JOB_LOG_SUPPLY_NEED
