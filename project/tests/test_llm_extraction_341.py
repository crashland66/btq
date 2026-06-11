"""Stub-client gating tests for change 341.

341 made `capture_semantics` job-first against a verbatim live-validated prompt:

* `_build_semantic_prompt` returns a JSON *object* ``{"extracted_actions":[...]}``
  (not a bare array), tells the model to fill ``job_type`` and clean
  ``payload_fields`` hints, and to emit multiple actions for multiple facts.
* `_normalized_action_payload` defaults ``candidate_type="field_capture_follow_up"``
  when the model omits the internal field — without this, every model action is
  dropped (``ValueError: candidate_type is required``) and only the rule fallback
  survives.

These tests drive a STUB model client (no real model — that quality gate already
ran on the Pro) and assert the model path produces valid, job-first
``ExtractedAction``s through ``LocalModelCaptureEngine`` /
``extracted_actions_from_model_payload``.
"""

from __future__ import annotations

import pytest

from processing_core.capture_semantics import (
    CaptureSemanticInput,
    LocalModelCaptureEngine,
    _build_semantic_prompt,
    extracted_actions_from_model_payload,
)
from queue_spec import validate_job


# In the hermetic test environment, the synthetic site_registry.example.json is
# pinned (root conftest.py). Under that fixture registry, "7050" (Summit Wire)
# resolves via resolve_site_id; 1337/602/SANDBOX do NOT. The existing suite uses
# 7050 for the same reason. (On the live box the real registry resolves 1337/602,
# but tests must use a registry-resident id so proposed jobs build.)
RESOLVING_SITE_ID = "7050"


class StubModelClient:
    """Returns a fixed payload from ``generate_json`` (or raises if given an Exception).

    Model-shaped actions intentionally OMIT ``candidate_type`` (the model never
    emits the internal field), so these tests exercise the 341 default.
    """

    provider = "stub"
    model = "job-first-341"

    def __init__(self, response: object) -> None:
        self._response = response

    def generate_json(self, _prompt: str) -> object:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def capture_input(
    text: str,
    *,
    selected_employees: list[dict[str, object]] | None = None,
    site_id: str = RESOLVING_SITE_ID,
    site_label: str = "Summit Wire",
) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-341",
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id=site_id,
        site_label=site_label,
        selected_employees=selected_employees or [],
        submitter_person_id="per_jordan",
        captured_at="2026-06-04T12:00:00-04:00",
    )


def supply_action_without_candidate_type() -> dict[str, object]:
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


def site_issue_action_without_candidate_type() -> dict[str, object]:
    return {
        "action_key": "floor_drain_clog",
        "job_type": "log_site_issue",
        "target_type": "site",
        "target_label": "Summit Wire",
        "summary": "Floor drain is clogged in the restroom.",
        "source_excerpt": "the floor drain is clogged",
        "evidence_terms": ["drain", "clog"],
    }


def retention_action_without_candidate_type() -> dict[str, object]:
    return {
        "action_key": "retention_risk_replace",
        "job_type": "flag_retention_risk",
        "target_type": "employee",
        "target_label": "Marcus Bell",
        "summary": "Operator may need to replace Marcus Bell after repeated no-shows.",
        "source_excerpt": "I may need to replace Marcus Bell",
        "evidence_terms": ["replace"],
    }


def test_candidate_type_less_supply_action_becomes_valid_job() -> None:
    """A candidate_type-less log_supply_need model action survives 341's default
    and yields a valid job with a clean item_name and a RESOLVED site_id."""
    raw = {"extracted_actions": [supply_action_without_candidate_type()]}

    actions = extracted_actions_from_model_payload(
        raw, capture_input("we need a vacuum and a few lint brushes")
    )

    assert len(actions) == 1
    action = actions[0]
    # 341 default fills the omitted internal field instead of dropping the action.
    assert action.candidate_type == "field_capture_follow_up"
    assert action.job_type == "log_supply_need"

    proposed = action.proposed_queue_job
    assert proposed is not None
    assert validate_job(proposed)
    payload = proposed["payload"]
    assert payload["item_name"] == "vacuum, lint brushes"
    # RESOLVING_SITE_ID resolves via resolve_site_id -> real site_id on the job.
    assert payload["site_id"] == RESOLVING_SITE_ID


def test_candidate_type_less_site_issue_action_becomes_valid_job() -> None:
    raw = {"extracted_actions": [site_issue_action_without_candidate_type()]}

    actions = extracted_actions_from_model_payload(
        raw, capture_input("the floor drain is clogged")
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.candidate_type == "field_capture_follow_up"
    assert action.job_type == "log_site_issue"
    assert action.proposed_queue_job is not None
    assert validate_job(action.proposed_queue_job)
    assert action.proposed_queue_job["payload"]["site_id"] == RESOLVING_SITE_ID


def test_candidate_type_less_retention_action_becomes_valid_job() -> None:
    raw = {"extracted_actions": [retention_action_without_candidate_type()]}

    actions = extracted_actions_from_model_payload(
        raw,
        capture_input(
            "I may need to replace Marcus Bell",
            selected_employees=[{"id": "emp-marcus", "name": "Marcus Bell"}],
        ),
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.candidate_type == "field_capture_follow_up"
    assert action.job_type == "flag_retention_risk"
    assert action.proposed_queue_job is not None
    assert validate_job(action.proposed_queue_job)


def test_multi_fact_two_actions_yield_two_extracted_actions() -> None:
    """Multi-fact: the stub returns 2 distinct model actions -> 2 extracted_actions."""
    raw = {
        "extracted_actions": [
            site_issue_action_without_candidate_type(),
            supply_action_without_candidate_type(),
        ]
    }

    actions = extracted_actions_from_model_payload(
        raw, capture_input("the floor drain is clogged and we need a vacuum and lint brushes")
    )

    assert len(actions) == 2
    assert {action.job_type for action in actions} == {"log_site_issue", "log_supply_need"}


def test_engine_uses_model_actions_not_fallback() -> None:
    """A distinctive model-emitted action_key survives end-to-end through
    LocalModelCaptureEngine, proving the model path (not the rule fallback) ran."""
    marker = dict(site_issue_action_without_candidate_type(), action_key="MODEL_MARKER_341")
    engine = LocalModelCaptureEngine(StubModelClient({"extracted_actions": [marker]}))

    result = engine(capture_input("the floor drain is clogged"))

    keys = [action.action_key for action in result.extracted_actions or []]
    assert keys == ["MODEL_MARKER_341"]


def test_engine_falls_back_to_rules_when_stub_raises() -> None:
    """When the stub model client raises, the engine falls back to the rule engine
    (which does NOT emit the model's distinctive marker)."""
    engine = LocalModelCaptureEngine(StubModelClient(RuntimeError("model unavailable")))

    result = engine(capture_input("The unopened backpack vacuum is now gone."))

    actions = result.extracted_actions or []
    keys = [action.action_key for action in actions]
    assert "MODEL_MARKER_341" not in keys
    # Rule fallback routes loss to a log_site_issue action.
    assert any(action.job_type == "log_site_issue" for action in actions)


def test_prompt_contract_object_shape_and_no_bare_array_wording() -> None:
    prompt = _build_semantic_prompt(capture_input("the floor drain is clogged"))

    assert '"extracted_actions"' in prompt
    assert "exactly one JSON array" not in prompt


def test_parser_accepts_both_object_and_bare_list() -> None:
    source = capture_input("the floor drain is clogged")
    action = site_issue_action_without_candidate_type()

    from_object = extracted_actions_from_model_payload({"extracted_actions": [action]}, source)
    from_list = extracted_actions_from_model_payload([action], source)

    assert len(from_object) == 1
    assert len(from_list) == 1
    assert from_object[0].job_type == from_list[0].job_type == "log_site_issue"


def test_candidate_type_default_only_fills_when_absent() -> None:
    """A model action that DOES carry candidate_type keeps it; the 341 default
    only fills the absent case."""
    provided = dict(site_issue_action_without_candidate_type(), candidate_type="my_custom_type")

    actions = extracted_actions_from_model_payload(
        {"extracted_actions": [provided]}, capture_input("the floor drain is clogged")
    )

    assert len(actions) == 1
    assert actions[0].candidate_type == "my_custom_type"
