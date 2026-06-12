"""Verifier tests for change 350.

350 makes ``log_equipment_request`` a buildable operator-proposed job (parity
with ``log_supply_need``), so equipment captures become approvable job drafts.

These drive model-shaped payloads through
``extracted_actions_from_model_payload`` -> ``proposed_queue_job_for_action``
and assert the built job validates via ``queue_spec.validate_job``, mirroring
the supply harness in ``test_llm_extraction_341.py``.
"""

from __future__ import annotations

from processing_core import capture_semantics
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    equipment_name_for_action,
    extracted_actions_from_model_payload,
    proposed_queue_job_for_action,
)
from queue_spec import JOB_LOG_EQUIPMENT_REQUEST, validate_job


# "7050" (Summit Wire) resolves under the hermetic synthetic registry pinned by
# the root conftest. 1337/602/SANDBOX do NOT. The supply suite uses 7050 for the
# same reason.
RESOLVING_SITE_ID = "7050"


def capture_input(
    text: str,
    *,
    site_id: str = RESOLVING_SITE_ID,
    site_label: str = "Summit Wire",
) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-350",
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id=site_id,
        site_label=site_label,
        selected_employees=[],
        submitter_person_id="per_jordan",
        captured_at="2026-06-04T12:00:00-04:00",
    )


def equipment_action(
    *,
    payload_fields: dict | None = None,
    target_type: str = "site",
    target_label: str = "Summit Wire",
    summary: str = "Site needs a floor buffer.",
    source_excerpt: str = "we need a floor buffer out here",
) -> dict:
    action: dict = {
        "action_key": "log_equipment_need",
        "job_type": "log_equipment_request",
        "target_type": target_type,
        "target_label": target_label,
        "summary": summary,
        "source_excerpt": source_excerpt,
        "evidence_terms": ["floor buffer"],
    }
    if payload_fields is not None:
        action["payload_fields"] = payload_fields
    return action


def supply_action() -> dict:
    return {
        "action_key": "log_low_supplies",
        "job_type": "log_supply_need",
        "target_type": "site",
        "target_label": "Summit Wire",
        "summary": "Order a vacuum and lint brushes.",
        "source_excerpt": "we need a vacuum and a few lint brushes",
        "payload_fields": {"item_name": "vacuum, lint brushes"},
        "evidence_terms": ["vacuum", "lint brushes"],
    }


def _build(action_dict: dict, source: CaptureSemanticInput):
    actions = extracted_actions_from_model_payload(
        {"extracted_actions": [action_dict]}, source
    )
    assert len(actions) == 1
    action = actions[0]
    return action, proposed_queue_job_for_action(action, source)


# 1. Buildable -------------------------------------------------------------

def test_equipment_request_is_operator_proposed_job_type() -> None:
    assert JOB_LOG_EQUIPMENT_REQUEST in capture_semantics.OPERATOR_PROPOSED_JOB_TYPES


# 2. End-to-end valid job --------------------------------------------------

def test_equipment_action_becomes_valid_job() -> None:
    source = capture_input("we need a floor buffer out here")
    action, proposed = _build(
        equipment_action(payload_fields={"equipment_name": "floor buffer"}), source
    )

    assert action.job_type == "log_equipment_request"
    assert proposed is not None
    assert validate_job(proposed)

    payload = proposed["payload"]
    assert payload["equipment_name"] == "floor buffer"
    assert payload["site_id"] == RESOLVING_SITE_ID
    assert isinstance(payload["site_id"], str) and payload["site_id"]
    assert isinstance(payload["requested_by"], str) and payload["requested_by"]


# 3. reason/priority passthrough + absence -------------------------------

def test_equipment_reason_priority_passthrough() -> None:
    source = capture_input("the old buffer died, high priority")
    _, proposed = _build(
        equipment_action(
            payload_fields={
                "equipment_name": "floor buffer",
                "reason": "the old one died",
                "priority": "high",
            }
        ),
        source,
    )
    assert proposed is not None
    assert validate_job(proposed)
    payload = proposed["payload"]
    assert payload["reason"] == "the old one died"
    assert payload["priority"] == "high"


def test_equipment_without_reason_priority_does_not_fabricate() -> None:
    source = capture_input("we need a floor buffer out here")
    _, proposed = _build(
        equipment_action(payload_fields={"equipment_name": "floor buffer"}), source
    )
    assert proposed is not None
    assert validate_job(proposed)
    payload = proposed["payload"]
    assert "reason" not in payload
    assert "priority" not in payload


# 4. Unresolved site -> no job --------------------------------------------

def test_equipment_unresolved_site_yields_no_job() -> None:
    # Empty site fields + a non-site target whose label does not resolve.
    source = capture_input(
        "we need a floor buffer", site_id="", site_label=""
    )
    action_dict = equipment_action(
        payload_fields={"equipment_name": "floor buffer"},
        target_type="equipment",
        target_label="",
    )
    action, proposed = _build(action_dict, source)
    assert proposed is None  # mirrors supply: no resolvable site -> drop, no crash


# 5. Supply unchanged (regression guard) ----------------------------------

def test_supply_need_still_builds_valid_job() -> None:
    source = capture_input("we need a vacuum and a few lint brushes")
    action, proposed = _build(supply_action(), source)
    assert action.job_type == "log_supply_need"
    assert proposed is not None
    assert validate_job(proposed)
    payload = proposed["payload"]
    assert payload["item_name"] == "vacuum, lint brushes"
    assert payload["site_id"] == RESOLVING_SITE_ID


# 6. equipment_name fallback ----------------------------------------------

def test_equipment_name_fallback_from_summary() -> None:
    source = capture_input("we need a floor buffer out here")
    # No payload_fields equipment_name -> falls back to summary/source_excerpt.
    action_dict = equipment_action(
        payload_fields={},
        summary="Site needs a floor buffer.",
        source_excerpt="we need a floor buffer out here",
    )
    actions = extracted_actions_from_model_payload(
        {"extracted_actions": [action_dict]}, source
    )
    action = actions[0]
    from processing_core.capture_semantics import normalized_payload_fields

    fallback = equipment_name_for_action(
        action, source, normalized_payload_fields(action.payload_fields)
    )
    assert fallback  # non-empty
    assert fallback == "Site needs a floor buffer."

    proposed = proposed_queue_job_for_action(action, source)
    assert proposed is not None
    assert validate_job(proposed)
    assert proposed["payload"]["equipment_name"] == fallback
