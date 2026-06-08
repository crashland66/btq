from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_pipeline.couchdb_registry import CouchDBRegistryError
from field_capture import action_candidates
from field_capture import approved_job_drafts as field_approved_job_drafts
from field_capture import draft_staging as field_draft_staging
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.action_candidates import STATUS_APPROVED
import processing_core.capture_semantics as capture_semantics
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    LocalModelCaptureEngine,
    RuleCaptureEngine,
    action_with_valid_proposed_queue_job,
    _build_semantic_prompt,
)
from processing_core.extracted_actions import ExtractedAction, validate_extracted_action
from processing_core.artifacts import write_json_object
from queue_spec import PERSONNEL_EVENT_TYPES, validate_job


class FakeModelClient:
    provider = "fake"
    model = "multi-action"

    def __init__(self, response: object) -> None:
        self.response = response

    def generate_json(self, _prompt: str) -> object:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def capture_input(
    text: str,
    *,
    selected_employees: list[dict[str, object]] | None = None,
    site_id: str = "7050",
    site_label: str = "Summit Wire",
) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap-test",
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id=site_id,
        site_label=site_label,
        selected_employees=selected_employees or [],
        submitter_person_id="per_jordan",
        captured_at="2026-06-04T12:00:00-04:00",
    )


def extracted_action(**overrides: object) -> ExtractedAction:
    values: dict[str, object] = {
        "action_key": "personnel_attendance",
        "candidate_type": "personnel_attendance",
        "target_type": "employee",
        "target_id": "emp-scott",
        "target_label": "Bruce Keller",
        "summary": "Review Bruce Keller attendance.",
        "rationale": "",
        "confidence": "medium",
        "source_excerpt": "Bruce Keller was late.",
        "job_type": "log_personnel_event",
        "payload_fields": None,
        "evidence_terms": (),
        "source_kind": "ops_dashboard_text",
        "proposed_queue_job": None,
        "proposed_queue_job_error": "",
    }
    values.update(overrides)
    return ExtractedAction(**values)


def test_model_multi_action_array_resolves_targets_and_drops_malformed() -> None:
    response = [
        {
            "action_key": "personnel_attendance_no_response",
            "candidate_type": "personnel_attendance",
            "job_type": "log_personnel_event",
            "target_type": "employee",
            "target_label": "Bruce Keller",
            "summary": "Review Bruce Keller attendance/no-response.",
            "source_excerpt": "Bruce Keller said he would be late and did not respond by 21:07.",
            "evidence_terms": ["Bruce Keller", "late", "did not respond"],
        },
        {
            "action_key": "supply_equipment_shrinkage",
            "candidate_type": "supply_equipment_shrinkage",
            "job_type": "",
            "target_type": "site",
            "target_label": "Summit Wire",
            "summary": "Review missing backpack vacuum shrinkage.",
            "source_excerpt": "Summit Wire is missing an unopened backpack vacuum.",
            "evidence_terms": ["Summit Wire", "missing", "backpack vacuum"],
        },
        {"action_key": "malformed"},
    ]
    engine = LocalModelCaptureEngine(FakeModelClient(response))

    result = engine(
        capture_input(
            "Bruce Keller was late. Summit Wire is missing an unopened backpack vacuum.",
            selected_employees=[
                {"id": "emp-damon", "name": "Damon Carver"},
                {"id": "emp-scott", "name": "Bruce Keller"},
            ],
        )
    )

    assert len(result.extracted_actions or []) == 2
    for action in result.extracted_actions or []:
        payload = vars(action).copy()
        payload["evidence_terms"] = list(action.evidence_terms)
        validate_extracted_action(payload)
    personnel = next(action for action in result.extracted_actions or [] if action.target_type == "employee")
    site = next(action for action in result.extracted_actions or [] if action.target_type == "site")
    assert personnel.target_id == "emp-scott"
    assert personnel.target_label == "Bruce Keller"
    assert site.target_id == "7050"
    assert site.target_label == "Summit Wire"


def test_incident_regression_model_target_resolution_does_not_default_to_first_employee() -> None:
    note = (
        "Went to talk with Damon Carver about where we are on the Summit Wire labor budget and "
        "explained why we do not currently have an open position. Bruce Keller said he was at a doctor "
        "appointment and would be late, then did not respond to follow-up messages by 21:07, so we may "
        "need replacement coverage or termination review. Summit Wire is missing an unopened backpack "
        "vacuum, and another vacuum was removed for a cleanup job, so there may be supply or equipment "
        "shrinkage."
    )
    response = [
        {
            "action_key": "personnel_attendance_no_response",
            "candidate_type": "personnel_attendance",
            "job_type": "log_personnel_event",
            "target_type": "employee",
            "target_label": "Bruce Keller",
            "summary": "Review Bruce Keller attendance/no-response.",
            "source_excerpt": "Bruce Keller said he was at a doctor appointment and would be late, then did not respond to follow-up messages by 21:07.",
            "evidence_terms": ["Bruce Keller", "late", "did not respond"],
        },
        {
            "action_key": "supply_equipment_shrinkage",
            "candidate_type": "supply_equipment_shrinkage",
            "job_type": "",
            "target_type": "site",
            "target_label": "Summit Wire",
            "summary": "Review Summit Wire missing backpack vacuum shrinkage.",
            "source_excerpt": "Summit Wire is missing an unopened backpack vacuum, and another vacuum was removed for a cleanup job.",
            "evidence_terms": ["Summit Wire", "missing", "vacuum", "shrinkage"],
        },
    ]
    engine = LocalModelCaptureEngine(FakeModelClient(response))

    result = engine(
        capture_input(
            note,
            selected_employees=[
                {"id": "emp-damon", "name": "Damon Carver"},
                {"id": "emp-scott", "name": "Bruce Keller"},
            ],
        )
    )

    personnel = next(action for action in result.extracted_actions or [] if action.target_type == "employee")
    supply = next(action for action in result.extracted_actions or [] if action.action_key == "supply_equipment_shrinkage")
    assert personnel.target_id == "emp-scott"
    assert personnel.target_label == "Bruce Keller"
    assert personnel.proposed_queue_job is not None
    assert validate_job(personnel.proposed_queue_job)
    personnel_payload = personnel.proposed_queue_job["payload"]
    assert personnel_payload["employee"] == "Bruce Keller"
    assert personnel_payload["event_type"] in PERSONNEL_EVENT_TYPES
    assert personnel_payload["occurred_at"] == "2026-06-04T12:00:00-04:00"
    assert personnel_payload["reported_by"] == "per_jordan"
    assert personnel_payload["summary"] == "Review Bruce Keller attendance/no-response."
    assert supply.target_type == "site"
    assert supply.target_id == "7050"
    assert supply.target_label == "Summit Wire"
    assert supply.proposed_queue_job is None
    assert all(action.target_label != "Damon Carver" for action in result.extracted_actions or [])


def test_model_error_fails_soft_to_rule_engine() -> None:
    engine = LocalModelCaptureEngine(FakeModelClient(RuntimeError("ollama unavailable")))

    result = engine(capture_input("Soap and towels are out at Summit Wire."))

    assert result.issue_type == "supplies"
    assert result.action_candidates == ["Review supply/order follow-up."]
    assert [action.action_key for action in result.extracted_actions or []] == ["supply_review"]


def test_rule_fallback_structured_actions_floor() -> None:
    engine = RuleCaptureEngine()

    cases = [
        ("Soap is low at Summit Wire.", "supply_review"),
        ("The backpack vacuum is missing from Summit Wire.", "supply_equipment_loss"),
        ("Bruce Keller was late and did not respond.", "personnel_attendance"),
        ("Bruce Keller may need replacement coverage or termination review.", "retention_risk"),
        ("Just noting that the lobby looked normal today.", None),
    ]
    for text, expected_key in cases:
        result = engine(capture_input(text, selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}]))
        keys = [action.action_key for action in result.extracted_actions or []]
        if expected_key is None:
            assert keys == []
            assert result.action_candidates
        else:
            assert expected_key in keys
            assert result.action_candidates


def test_rule_engine_coemits_retention_risk_with_attendance() -> None:
    text = (
        "Bruce Keller said he was at a doctor appointment and would be late, then did not "
        "respond to follow-up messages by 21:07. If he has returned to his old ways, I may "
        "need to replace him. I fired him once already for failing to go to work."
    )
    engine = RuleCaptureEngine()

    result = engine(capture_input(text, selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}]))
    actions = result.extracted_actions or []
    attendance = next(
        action
        for action in actions
        if action.job_type == "log_personnel_event"
        and (action.proposed_queue_job or {}).get("payload", {}).get("event_type") == "attendance"
    )
    retention = next(action for action in actions if action.job_type == "flag_retention_risk")

    assert attendance.target_label == "Bruce Keller"
    assert retention.target_label == "Bruce Keller"
    assert retention.proposed_queue_job is not None
    assert validate_job(retention.proposed_queue_job)
    assert retention.proposed_queue_job["payload"]["employee"] == "Bruce Keller"
    assert "fired him once already" in retention.proposed_queue_job["payload"]["details"]


@pytest.mark.parametrize(
    ("cue", "text"),
    [
        ("let him go", "Bruce Keller was a no-show and I may need to let him go."),
        ("fire", "Bruce Keller was a no-show and I may need to fire him."),
        ("returned to his old ways", "Bruce Keller was a no-show and returned to his old ways."),
        ("may need to replace", "Bruce Keller was a no-show and I may need to replace him."),
    ],
)
def test_rule_engine_retention_cue_variants(cue: str, text: str) -> None:
    engine = RuleCaptureEngine()

    result = engine(capture_input(text, selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}]))
    retention = next(action for action in result.extracted_actions or [] if action.job_type == "flag_retention_risk")

    assert cue in retention.evidence_terms
    assert retention.proposed_queue_job is not None
    assert validate_job(retention.proposed_queue_job)


def test_rule_engine_retention_unresolved_employee_stays_pending() -> None:
    engine = RuleCaptureEngine()

    result = engine(capture_input("Someone was a no-show and I may need to replace them."))
    retention = next(action for action in result.extracted_actions or [] if action.job_type == "flag_retention_risk")

    assert retention.target_type == "employee"
    assert retention.target_label == ""
    assert retention.proposed_queue_job is None


def test_rule_engine_no_false_retention_on_plain_attendance() -> None:
    engine = RuleCaptureEngine()

    result = engine(
        capture_input(
            "Bruce Keller was late and a no-show today. Coverage at Summit Wire is otherwise fine.",
            selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}],
        )
    )

    assert [action.job_type for action in result.extracted_actions or []] == ["log_personnel_event"]
    assert all(action.job_type != "flag_retention_risk" for action in result.extracted_actions or [])


def test_semantic_prompt_instructs_retention_coemission() -> None:
    prompt = _build_semantic_prompt(capture_input("Bruce Keller was a no-show and I may need to replace him."))

    assert (
        "emit BOTH an attendance log_personnel_event action AND a separate flag_retention_risk action "
        "for the same employee; do not merge them"
    ) in prompt


def test_rule_engine_equipment_loss_routes_to_log_site_issue() -> None:
    engine = RuleCaptureEngine()
    result = engine(
        capture_input(
            "There is supply shrinkage at Summit Wire. The unopened backpack vacuum is now gone.",
        )
    )
    actions = result.extracted_actions or []
    issue_actions = [action for action in actions if action.job_type == "log_site_issue"]

    assert len(issue_actions) == 1
    issue = issue_actions[0]
    assert issue.action_key == "supply_equipment_loss"
    assert issue.proposed_queue_job is not None
    assert validate_job(issue.proposed_queue_job)
    payload = issue.proposed_queue_job["payload"]
    assert payload["site_id"] == "7050"
    assert payload["category"] == "supply"
    assert payload["priority"] == "high"
    assert payload["client_notified"] is False
    assert all(action.job_type != "update_site_equipment" for action in actions)


def test_rule_engine_intentional_movement_routes_to_append_to_note() -> None:
    engine = RuleCaptureEngine()
    result = engine(capture_input("I've taken the other with me for use at a cleanup job next week."))
    actions = result.extracted_actions or []
    [movement] = [action for action in actions if action.job_type == "append_to_note"]

    assert movement.action_key == "supply_equipment_movement"
    assert movement.proposed_queue_job is not None
    assert validate_job(movement.proposed_queue_job)
    payload = movement.proposed_queue_job["payload"]
    assert payload["destination"] == "site_note"
    assert payload["path"] == "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
    assert "taken the other with me" in payload["content"]
    assert all(action.job_type != "log_site_issue" for action in actions)
    assert all(action.job_type != "update_site_equipment" for action in actions)


def test_rule_engine_routine_restock_stays_generic_pending() -> None:
    engine = RuleCaptureEngine()
    result = engine(capture_input("We're low on towels, need a restock."))
    actions = result.extracted_actions or []
    [supply] = [action for action in actions if action.action_key == "supply_review"]

    assert supply.job_type == ""
    assert supply.proposed_queue_job is None
    assert all(action.proposed_queue_job is None for action in actions)


def test_rule_engine_equipment_loss_unresolved_site_stays_pending() -> None:
    engine = RuleCaptureEngine()
    result = engine(
        capture_input(
            "The unopened backpack vacuum is now gone.",
            site_id="",
            site_label="Unresolvable Site",
        )
    )
    actions = result.extracted_actions or []
    [loss] = [action for action in actions if action.action_key == "supply_equipment_loss"]

    assert loss.job_type == "log_site_issue"
    assert loss.proposed_queue_job is None


def test_proposed_job_registry_outage_records_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(_site_name: str) -> str | None:
        raise CouchDBRegistryError("registry unavailable")

    monkeypatch.setattr(capture_semantics, "resolve_site_id", unavailable)
    action = extracted_action(
        action_key="supply_equipment_loss",
        candidate_type="supply_equipment_loss",
        target_type="site",
        target_id="7050",
        target_label="Summit Wire",
        summary="Review missing backpack vacuum.",
        source_excerpt="Summit Wire is missing a backpack vacuum.",
        job_type="log_site_issue",
    )

    result = action_with_valid_proposed_queue_job(action, capture_input("Summit Wire is missing a backpack vacuum."))

    assert result.proposed_queue_job is None
    assert result.proposed_queue_job_error == "site_registry_unavailable"


def test_proposed_job_review_only_action_has_no_error() -> None:
    action = extracted_action(
        action_key="supply_review",
        candidate_type="supply_review",
        target_type="site",
        target_id="7050",
        target_label="Summit Wire",
        summary="Review supply follow-up.",
        source_excerpt="Soap is low.",
        job_type="",
    )

    result = action_with_valid_proposed_queue_job(action, capture_input("Soap is low."))

    assert result.proposed_queue_job is None
    assert result.proposed_queue_job_error == ""


def test_proposed_job_builder_bug_records_generic_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_action: ExtractedAction, _source: CaptureSemanticInput) -> dict[str, object] | None:
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(capture_semantics, "proposed_queue_job_for_action", broken)
    action = extracted_action()

    result = action_with_valid_proposed_queue_job(action, capture_input("Bruce Keller was late."))

    assert result.proposed_queue_job is None
    assert result.proposed_queue_job_error == "builder_error:RuntimeError"


def test_serialized_candidate_includes_proposed_job_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, couchdb_review) -> None:
    def unavailable(_site_name: str) -> str | None:
        raise CouchDBRegistryError("registry unavailable")

    monkeypatch.setattr(capture_semantics, "resolve_site_id", unavailable)
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    engine = LocalModelCaptureEngine(
        FakeModelClient(
            [
                {
                    "action_key": "supply_equipment_loss",
                    "candidate_type": "supply_equipment_loss",
                    "target_type": "site",
                    "target_label": "Summit Wire",
                    "summary": "Review missing backpack vacuum.",
                    "source_excerpt": "Summit Wire is missing a backpack vacuum.",
                    "job_type": "log_site_issue",
                },
                {
                    "action_key": "supply_review",
                    "candidate_type": "supply_review",
                    "target_type": "site",
                    "target_label": "Summit Wire",
                    "summary": "Review supply follow-up.",
                    "source_excerpt": "Soap is low.",
                    "job_type": "",
                },
            ]
        )
    )
    artifact = run_text_semantic_pipeline(
        "Summit Wire is missing a backpack vacuum. Soap is low.",
        site_id="7050",
        site_label="Summit Wire",
        upload_id="typed-proposed-error",
        captured_at="2026-06-04T12:00:00-04:00",
        engine=engine,
    )
    write_json_object(semantic_dir / "typed-proposed-error.json", artifact)

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]
    error_candidate = next(candidate for candidate in candidates if candidate["channel_metadata"]["action_key"] == "supply_equipment_loss")
    clean_candidate = next(candidate for candidate in candidates if candidate["channel_metadata"]["action_key"] == "supply_review")

    assert counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert error_candidate["channel_metadata"]["proposed_queue_job_error"] == "site_registry_unavailable"
    assert not error_candidate.get("approval_metadata")  # 308c: empty dict survives CouchDB round-trip; no proposed job
    assert "proposed_queue_job_error" not in clean_candidate["channel_metadata"]


def test_successful_proposed_job_has_no_error() -> None:
    action = extracted_action(
        action_key="supply_equipment_loss",
        candidate_type="supply_equipment_loss",
        target_type="site",
        target_id="7050",
        target_label="Summit Wire",
        summary="Review missing backpack vacuum.",
        source_excerpt="Summit Wire is missing a backpack vacuum.",
        job_type="log_site_issue",
    )

    result = action_with_valid_proposed_queue_job(action, capture_input("Summit Wire is missing a backpack vacuum."))

    assert result.proposed_queue_job is not None
    assert validate_job(result.proposed_queue_job)
    assert result.proposed_queue_job_error == ""


def test_rule_engine_loss_never_targets_update_site_equipment() -> None:
    engine = RuleCaptureEngine()
    spans = [
        "There is supply shrinkage at Summit Wire. The unopened backpack vacuum is now gone.",
        "I've taken the other with me for use at a cleanup job next week.",
    ]

    for span in spans:
        result = engine(capture_input(span))
        assert all(action.job_type != "update_site_equipment" for action in result.extracted_actions or [])


def test_semantic_prompt_routes_equipment_loss() -> None:
    prompt = _build_semantic_prompt(capture_input("The backpack vacuum is missing."))

    assert "append_to_note" in prompt
    assert "missing, lost, gone, stolen, theft-related" in prompt
    assert "emit log_site_issue with category supply" in prompt
    assert "intentionally moved, removed off-site, taken with the operator, or brought to another job, emit append_to_note" in prompt
    assert "Do NOT emit update_site_equipment for attribute-less loss or movement" in prompt


def test_text_artifact_collects_multiple_structured_candidates(tmp_path: Path, couchdb_review) -> None:
    semantic_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    engine = LocalModelCaptureEngine(
        FakeModelClient(
            [
                {
                    "action_key": "personnel_attendance_no_response",
                    "candidate_type": "personnel_attendance",
                    "job_type": "log_personnel_event",
                    "target_type": "employee",
                    "target_label": "Bruce Keller",
                    "summary": "Review Bruce Keller attendance/no-response.",
                    "source_excerpt": "Bruce Keller was late and did not respond.",
                    "evidence_terms": ["Bruce Keller", "late"],
                },
                {
                    "action_key": "supply_equipment_shrinkage",
                    "candidate_type": "supply_equipment_shrinkage",
                    "target_type": "site",
                    "target_label": "Summit Wire",
                    "summary": "Review missing backpack vacuum.",
                    "source_excerpt": "Summit Wire is missing a backpack vacuum.",
                    "evidence_terms": ["Summit Wire", "backpack vacuum"],
                },
            ]
        )
    )
    artifact = run_text_semantic_pipeline(
        "Bruce Keller was late and did not respond. Summit Wire is missing a backpack vacuum.",
        site_id="7050",
        site_label="Summit Wire",
        upload_id="typed-multi",
        selected_employees=[{"id": "emp-damon", "name": "Damon Carver"}, {"id": "emp-scott", "name": "Bruce Keller"}],
        engine=engine,
    )
    write_json_object(semantic_dir / "typed-multi.json", artifact)

    first = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    second = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]

    assert first == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert second == {"discovered": 2, "skipped": 2, "completed": 0, "failed": 0}
    assert {candidate["channel_metadata"]["target_label"] for candidate in candidates} == {"Bruce Keller", "Summit Wire"}


def operator_action(job_type: str) -> dict[str, object]:
    base: dict[str, object] = {
        "action_key": job_type,
        "candidate_type": job_type,
        "job_type": job_type,
        "target_type": "employee",
        "target_label": "Bruce Keller",
        "summary": f"Review {job_type}.",
        "source_excerpt": f"Review {job_type} for Bruce Keller at Summit Wire.",
        "evidence_terms": [job_type, "Bruce Keller"],
    }
    if job_type == "log_personnel_event":
        base.update(
            {
                "action_key": "personnel_attendance",
                "candidate_type": "personnel_attendance",
                "summary": "Review Bruce Keller attendance.",
                "source_excerpt": "Bruce Keller was late and did not respond.",
                "evidence_terms": ["Bruce Keller", "late", "did not respond"],
            }
        )
    elif job_type == "flag_retention_risk":
        base.update(
            {
                "action_key": "retention_risk",
                "candidate_type": "retention_risk",
                "summary": "Review Bruce Keller retention risk.",
                "source_excerpt": "Bruce Keller may quit and needs replacement coverage.",
                "evidence_terms": ["Bruce Keller", "quit", "replacement coverage"],
            }
        )
    elif job_type == "trigger_recruiting":
        base.update(
            {
                "action_key": "staffing_risk",
                "candidate_type": "staffing_risk",
                "target_type": "site",
                "target_label": "Summit Wire",
                "summary": "Review Summit Wire recruiting need.",
                "source_excerpt": "Summit Wire is short staffed and needs urgent cleaner coverage.",
                "evidence_terms": ["Summit Wire", "short staffed", "urgent"],
            }
        )
    elif job_type == "remove_from_schedule":
        base.update(
            {
                "action_key": "remove_from_schedule",
                "candidate_type": "employee_schedule",
                "summary": "Review removing Bruce Keller from the Summit Wire schedule.",
                "source_excerpt": "Remove Bruce Keller from the Summit Wire schedule.",
                "evidence_terms": ["Bruce Keller", "remove", "schedule"],
            }
        )
    return base


def stage_single_structured_action(tmp_path: Path, raw_action: dict[str, object]) -> dict[str, object]:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    engine = LocalModelCaptureEngine(FakeModelClient([raw_action]))
    artifact = run_text_semantic_pipeline(
        str(raw_action["source_excerpt"]),
        site_id="7050",
        site_label="Summit Wire",
        upload_id="typed-operator-action",
        person_id="operator",
        captured_at="2026-06-04T12:00:00-04:00",
        selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}],
        engine=engine,
    )
    write_json_object(semantic_dir / "typed-operator-action.json", artifact)

    assert action_candidates.collect_action_candidates(semantic_dir, candidate_dir) == {
        "discovered": 1,
        "skipped": 0,
        "completed": 1,
        "failed": 0,
    }
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    proposed = candidate["approval_metadata"]["proposed_queue_job"]
    assert validate_job(proposed)

    candidate["status"] = STATUS_APPROVED
    candidate["reviewer"] = "test"
    candidate["approved_at"] = "2026-06-04T12:05:00-04:00"
    write_json_object(candidate_path, candidate)

    assert field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root) == {
        "discovered": 1,
        "skipped": 0,
        "completed": 1,
        "failed": 0,
    }
    assert field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir) == {
        "discovered": 1,
        "skipped": 0,
        "completed": 1,
        "failed": 0,
    }
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    queue_job = json.loads(queue_path.read_text(encoding="utf-8"))
    assert validate_job(queue_job)
    assert queue_job["job_type"] == proposed["job_type"]
    assert queue_job["payload"] == proposed["payload"]
    return {"candidate": candidate, "queue_job": queue_job}


def test_operator_structured_actions_stage_valid_queue_jobs_end_to_end(tmp_path: Path, couchdb_review) -> None:
    for job_type in ("log_personnel_event", "flag_retention_risk", "trigger_recruiting", "remove_from_schedule"):
        result = stage_single_structured_action(tmp_path / job_type, operator_action(job_type))
        assert result["queue_job"]["job_type"] == job_type


def test_structured_action_builder_fails_soft_without_required_target(tmp_path: Path, couchdb_review) -> None:
    semantic_dir = tmp_path / "runtime" / "field_capture" / "semantics"
    candidate_dir = tmp_path / "runtime" / "reviews" / "action_candidates" / "field_capture"
    engine = LocalModelCaptureEngine(
        FakeModelClient(
            [
                {
                    "action_key": "personnel_attendance",
                    "candidate_type": "personnel_attendance",
                    "job_type": "log_personnel_event",
                    "target_type": "employee",
                    "target_label": "",
                    "summary": "Review unresolved attendance.",
                    "source_excerpt": "Someone was late.",
                    "evidence_terms": ["late"],
                }
            ]
        )
    )
    artifact = run_text_semantic_pipeline(
        "Someone was late.",
        site_id="7050",
        site_label="Summit Wire",
        upload_id="typed-unresolved",
        captured_at="2026-06-04T12:00:00-04:00",
        engine=engine,
    )
    write_json_object(semantic_dir / "typed-unresolved.json", artifact)

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert candidate["status"] == "pending_review"
    assert not candidate.get("approval_metadata")  # 308c: empty dict survives CouchDB round-trip; no proposed job


def test_model_payload_fields_are_honored_for_valid_personnel_payload() -> None:
    explicit_payload = {
        "employee": "Bruce Keller",
        "event_type": "recognition",
        "summary": "Bruce Keller received a client compliment.",
        "occurred_at": "2026-06-04T10:00:00-04:00",
        "reported_by": "operator",
    }
    engine = LocalModelCaptureEngine(
        FakeModelClient(
            [
                {
                    "action_key": "recognition:bruce-keller",
                    "candidate_type": "personnel_recognition",
                    "job_type": "log_personnel_event",
                    "target_type": "employee",
                    "target_label": "Bruce Keller",
                    "summary": "Different model summary.",
                    "payload_fields": explicit_payload,
                    "source_excerpt": "Bruce got a client compliment.",
                    "evidence_terms": ["Bruce", "compliment"],
                }
            ]
        )
    )

    result = engine(capture_input("Bruce got a client compliment.", selected_employees=[{"id": "emp-scott", "name": "Bruce Keller"}]))
    [action] = result.extracted_actions or []

    assert action.proposed_queue_job == {"job_type": "log_personnel_event", "payload": explicit_payload}


# --- Prompt 296: operator-selected supply QC category forces structured log_supply_need ---


def supply_capture_input(
    text: str,
    *,
    area: str = "Supply Levels",
    site_id: str = "7050",
    site_label: str = "Summit Wire",
    selected_employees: list[dict[str, object]] | None = None,
) -> CaptureSemanticInput:
    """capture_input with the operator-selected QC area set (defaults to a supply category)."""
    return CaptureSemanticInput(
        capture_id="cap-test",
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id=site_id,
        site_label=site_label,
        selected_employees=selected_employees or [],
        submitter_person_id="per_jordan",
        captured_at="2026-06-04T12:00:00-04:00",
        area=area,
    )


def _supply_need_actions(result: CaptureSemanticInput) -> list[ExtractedAction]:
    return [action for action in (result.extracted_actions or []) if action.job_type == "log_supply_need"]


def test_rule_engine_supply_category_forces_structured_log_supply_need() -> None:
    engine = RuleCaptureEngine()
    result = engine(supply_capture_input("We need mop heads and neutralizer."))

    [supply] = _supply_need_actions(result)
    assert supply.job_type == "log_supply_need"
    # item_name is the named supply items, not a bare "follow-up" placeholder.
    item_name = supply.proposed_queue_job["payload"]["item_name"]
    assert "mop heads" in item_name.lower()
    assert "neutralizer" in item_name.lower()
    assert "follow-up" not in item_name.lower()

    assert supply.proposed_queue_job is not None
    assert validate_job(supply.proposed_queue_job)
    payload = supply.proposed_queue_job["payload"]
    assert payload["site_id"] == "7050"
    assert payload["requested_by"] == "per_jordan"


def test_rule_engine_supply_category_one_need_all_items() -> None:
    """All named items collapse into ONE structured supply need, not one per item."""
    engine = RuleCaptureEngine()
    result = engine(supply_capture_input("We're low on mop heads and neutralizer and paper towels."))

    supply_needs = _supply_need_actions(result)
    assert len(supply_needs) == 1
    item_name = supply_needs[0].proposed_queue_job["payload"]["item_name"].lower()
    assert "mop heads" in item_name
    assert "neutralizer" in item_name
    assert "paper towels" in item_name


def test_model_low_confidence_supply_category_still_emits_structured_need() -> None:
    """Category wins even when the LLM returns a generic, low-confidence supply result."""
    response = [
        {
            "action_key": "supply_review",
            "candidate_type": "supply_review",
            "job_type": "",
            "target_type": "site",
            "target_label": "Summit Wire",
            "summary": "Maybe a supply follow-up.",
            "source_excerpt": "We need mop heads and neutralizer.",
            "confidence": "low",
            "evidence_terms": ["supply"],
        }
    ]
    engine = LocalModelCaptureEngine(FakeModelClient(response))
    result = engine(supply_capture_input("We need mop heads and neutralizer."))

    [supply] = _supply_need_actions(result)
    assert supply.job_type == "log_supply_need"
    assert supply.proposed_queue_job is not None
    assert validate_job(supply.proposed_queue_job)
    item_name = supply.proposed_queue_job["payload"]["item_name"].lower()
    assert "mop heads" in item_name
    assert "neutralizer" in item_name


def test_model_empty_result_supply_category_still_emits_structured_need() -> None:
    """Even if the model returns no actions, the supply category synthesizes the need."""
    engine = LocalModelCaptureEngine(FakeModelClient([]))
    result = engine(supply_capture_input("We need mop heads and neutralizer."))

    [supply] = _supply_need_actions(result)
    assert supply.job_type == "log_supply_need"
    assert supply.proposed_queue_job is not None
    assert validate_job(supply.proposed_queue_job)


def test_supply_category_proposed_job_passes_queue_spec_validation() -> None:
    """The produced proposed_queue_job satisfies queue_spec for log_supply_need."""
    import queue_spec

    engine = RuleCaptureEngine()
    result = engine(supply_capture_input("We need mop heads and neutralizer."))
    [supply] = _supply_need_actions(result)
    proposed = supply.proposed_queue_job

    assert proposed["job_type"] == "log_supply_need"
    payload = proposed["payload"]
    # queue_spec's required fields for log_supply_need.
    assert payload["site_id"]
    assert payload["item_name"]
    assert payload["requested_by"]
    assert queue_spec._validate_log_supply_need_payload(payload) is True
    assert validate_job(proposed) is True


# --- Prompt 267 preserved: category-less supply mention stays generic supply_review ---


def test_rule_engine_categoryless_supplies_stays_generic_supply_review() -> None:
    """Ambient restock WITHOUT the supply QC area stays the generic, jobless supply_review."""
    engine = RuleCaptureEngine()
    result = engine(capture_input("We're low on mop heads and neutralizer, need a restock."))

    assert result.issue_type == "supplies"
    [supply] = [action for action in result.extracted_actions or [] if action.action_key == "supply_review"]
    assert supply.job_type == ""
    assert supply.proposed_queue_job is None
    # No structured supply need was emitted without the category.
    assert _supply_need_actions(result) == []


def test_model_categoryless_supplies_stays_generic_supply_review() -> None:
    """Model path: a generic supply result without the supply area is not promoted."""
    response = [
        {
            "action_key": "supply_review",
            "candidate_type": "supply_review",
            "job_type": "",
            "target_type": "site",
            "target_label": "Summit Wire",
            "summary": "Supply follow-up.",
            "source_excerpt": "We're low on towels, need a restock.",
            "confidence": "low",
            "evidence_terms": ["supply"],
        }
    ]
    engine = LocalModelCaptureEngine(FakeModelClient(response))
    result = engine(capture_input("We're low on towels, need a restock."))

    assert _supply_need_actions(result) == []
    [supply] = [action for action in result.extracted_actions or [] if action.job_type == ""]
    assert supply.proposed_queue_job is None


# --- Loss/theft still routes to log_site_issue, even under a supply category ---


def test_loss_under_supply_category_still_routes_to_log_site_issue() -> None:
    """A specific-item loss is an issue, not a need: stays log_site_issue even with supply area selected."""
    engine = RuleCaptureEngine()
    result = engine(supply_capture_input("The backpack vacuum is gone."))

    actions = result.extracted_actions or []
    issue_actions = [action for action in actions if action.job_type == "log_site_issue"]
    assert len(issue_actions) == 1
    assert issue_actions[0].action_key == "supply_equipment_loss"
    # Loss must NOT be reclassified as a supply need.
    assert _supply_need_actions(result) == []
    assert issue_actions[0].proposed_queue_job is not None
    assert validate_job(issue_actions[0].proposed_queue_job)
