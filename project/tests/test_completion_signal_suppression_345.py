"""Completion/routine-done reports must not emit action jobs (prompt 345).

A note describing routine work ALREADY DONE ("trash pulled", "QA trash pull",
"swept and mopped, all good") is an acknowledgement, not an action item, and
must produce zero extracted actions in BOTH the rule path and the model path.
A note that mixes a completion report with a real request/problem ("trash
pulled, but we're out of bags") still surfaces the request action.

Covers ``note_is_completion_report`` (detector truth table), the rule-path
end-to-end suppression via ``run_text_semantic_pipeline`` + ``RuleCaptureEngine``,
the model-path filtering in ``extracted_actions_from_model_payload``, and the
verbatim ``_build_semantic_prompt`` completion-report instruction.
"""

from __future__ import annotations

import json

import pytest

from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    RuleCaptureEngine,
    _build_semantic_prompt,
    extracted_actions_from_model_payload,
    is_supply_area,
    note_is_completion_report,
)

SUPPLY_AREA = "Supply Levels"


def _rule_artifact(text: str, *, area: str = "", upload_id: str = "cap-345") -> dict:
    """Run the typed-note rule pipeline and round-trip through JSON.

    The collector reads artifacts from disk, so the JSON round-trip mirrors
    production (tuples -> lists) the way the existing suppression tests do.
    """
    artifact = run_text_semantic_pipeline(
        text,
        site_id="SANDBOX",
        upload_id=upload_id,
        area=area,
        engine=RuleCaptureEngine(),
    )
    return json.loads(json.dumps(artifact))


def _job_types(artifact: dict) -> list[str]:
    return [str(a.get("job_type") or "") for a in artifact["extracted_actions"]]


# ---------------------------------------------------------------------------
# 1. Detector truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "trash pulled",
        "QA trash pull",
        "Canteena's trash pull",
        "buffer swept and mopped, all good",
        "restrooms done",
    ],
)
def test_detector_true_for_pure_completion_reports(text: str) -> None:
    assert note_is_completion_report(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "we're out of paper towels",
        "trash pulled but we're out of bags",  # mixed -> request wins
        "vacuum is broken",
        "we need mop heads",
        "",  # empty -> False
    ],
)
def test_detector_false_for_requests_problems_and_empty(text: str) -> None:
    assert note_is_completion_report(text) is False


# ---------------------------------------------------------------------------
# 2. Rule path suppression end-to-end
# ---------------------------------------------------------------------------


def test_supply_area_is_recognized() -> None:
    # Guards the test fixture itself: the area we use must be a supply area, or
    # the suppression assertion below would pass for the wrong reason.
    assert is_supply_area(SUPPLY_AREA) is True


def test_rule_path_completion_report_with_supply_area_yields_no_action() -> None:
    artifact = _rule_artifact("trash pulled", area=SUPPLY_AREA)
    # THE load-bearing assertion: a completion report, even with a supply area
    # selected (a hard supply-need signal pre-345), produces zero actions and
    # in particular no log_supply_need job.
    assert artifact["extracted_actions"] == []
    assert "log_supply_need" not in _job_types(artifact)


def test_rule_path_without_supply_area_also_suppressed() -> None:
    artifact = _rule_artifact("buffer swept and mopped, all good")
    assert artifact["extracted_actions"] == []


# ---------------------------------------------------------------------------
# 3. Rule path mixed note keeps the request action
# ---------------------------------------------------------------------------


def test_rule_path_mixed_note_keeps_supply_need() -> None:
    artifact = _rule_artifact(
        "trash pulled, but we're out of trash bags", area=SUPPLY_AREA
    )
    job_types = _job_types(artifact)
    # The completion clause does not suppress the genuine supply request.
    assert "log_supply_need" in job_types, artifact["extracted_actions"]


# ---------------------------------------------------------------------------
# 4. Model path filtering
# ---------------------------------------------------------------------------


def _model_action(*, summary: str, excerpt: str) -> dict:
    return {
        "action_key": "log_supply_need",
        "candidate_type": "field_capture_follow_up",
        "target_type": "site",
        "summary": summary,
        "rationale": "",
        "source_excerpt": excerpt,
    }


def test_model_path_drops_completion_excerpt_action() -> None:
    source = CaptureSemanticInput(
        capture_id="cap-345-model",
        source_kind="ops_dashboard_text",
        source_text="trash pulled",
        site_id="SANDBOX",
    )
    action = _model_action(summary="Trash pulled", excerpt="trash pulled")
    result = extracted_actions_from_model_payload(
        {"extracted_actions": [action]}, source
    )
    assert result == []


def test_model_path_mixed_payload_keeps_only_the_request() -> None:
    # The whole note is NOT a completion report (it contains "out of"), so the
    # per-action excerpt filter is what drops the completion line.
    source = CaptureSemanticInput(
        capture_id="cap-345-model-mixed",
        source_kind="ops_dashboard_text",
        source_text="trash pulled; we are out of trash bags",
        site_id="SANDBOX",
        area=SUPPLY_AREA,
    )
    completion = _model_action(summary="Trash pulled", excerpt="trash pulled")
    supply = _model_action(
        summary="Order more trash bags", excerpt="out of trash bags"
    )
    result = extracted_actions_from_model_payload(
        {"extracted_actions": [completion, supply]}, source
    )
    excerpts = [a.source_excerpt for a in result]
    assert excerpts == ["out of trash bags"], [
        (a.source_excerpt, a.job_type) for a in result
    ]
    assert all("trash pulled" != a.source_excerpt for a in result)


# ---------------------------------------------------------------------------
# 5. Prompt contract
# ---------------------------------------------------------------------------


def test_prompt_contains_completion_report_instruction() -> None:
    source = CaptureSemanticInput(
        capture_id="cap-345-prompt",
        source_kind="ops_dashboard_text",
        source_text="trash pulled",
        site_id="SANDBOX",
    )
    prompt = _build_semantic_prompt(source)
    assert "Completion reports:" in prompt
    assert '{"extracted_actions": []}' in prompt
