from __future__ import annotations

from field_capture.approval_routes import (
    APPROVAL_ROUTE_ACCESS_CONSTRAINT,
    APPROVAL_ROUTE_NO_ACTION,
    APPROVAL_ROUTE_OPEN_ISSUE,
    APPROVAL_ROUTE_SUPPLY_NEED,
    DEFAULT_APPROVAL_ROUTE_BY_JOB_TYPE,
    build_approval_route_job,
    is_valid_approval_route,
    normalize_approval_route,
)
from queue_spec import JOB_FLAG_ACCESS_CONSTRAINT, JOB_LOG_SITE_ISSUE, validate_job


def access_draft() -> dict[str, object]:
    return {
        "type": "job_draft",
        "draft_id": "draft-access-1",
        "source_capture_id": "cap-access-1",
        "job_type": "flag_access_constraint",
        "payload": {
            "site": "7060",
            "details": "No access, crew locked out at the front gate.",
            "related_candidate_ids": ["cand-access-1"],
            "source_artifacts": ["runtime/evidence/access.json"],
        },
        "site_id": "7060",
        "message": "No access, crew locked out at the front gate.",
        "submitter_name": "Casey Worker",
        "created_at": "2026-07-02T13:30:00+00:00",
    }


def test_route_model_normalizes_empty_as_no_override() -> None:
    assert normalize_approval_route(None) is None
    assert normalize_approval_route("") is None
    assert is_valid_approval_route("open_issue") is True
    assert is_valid_approval_route("bogus") is False
    assert DEFAULT_APPROVAL_ROUTE_BY_JOB_TYPE[JOB_LOG_SITE_ISSUE] == APPROVAL_ROUTE_OPEN_ISSUE


def test_build_open_issue_from_access_origin_forces_access_payload_and_preserves_provenance() -> None:
    job_type, payload, error = build_approval_route_job(
        access_draft(),
        APPROVAL_ROUTE_OPEN_ISSUE,
        reviewer="per_reviewer",
    )

    assert error == ""
    assert job_type == "log_site_issue"
    assert validate_job({"job_type": job_type, "payload": payload}) is True
    assert payload["site_id"] == "7060"
    assert payload["category"] == "access"
    assert payload["priority"] == "high"
    assert payload["reported_by"] == "Casey Worker"
    assert payload["client_notified"] is False
    assert payload["resolution_trigger"] == "access issue resolved and service can proceed normally"
    assert payload["related_capture_ids"] == ["cap-access-1"]
    assert payload["related_candidate_ids"] == ["cand-access-1"]
    assert payload["source_artifacts"] == ["runtime/evidence/access.json"]


def test_build_access_constraint_uses_site_and_details_shape() -> None:
    job_type, payload, error = build_approval_route_job(
        access_draft(),
        APPROVAL_ROUTE_ACCESS_CONSTRAINT,
        reviewer="per_reviewer",
    )

    assert error == ""
    assert job_type == JOB_FLAG_ACCESS_CONSTRAINT
    assert payload == {"site": "7060", "details": "No access, crew locked out at the front gate."}
    assert validate_job({"job_type": job_type, "payload": payload}) is True


def test_build_supply_need_uses_original_item_and_reviewer_fallback() -> None:
    doc = {
        "type": "job_draft",
        "draft_id": "draft-supply-1",
        "source_capture_id": "cap-supply-1",
        "job_type": "log_site_issue",
        "payload": {"site_id": "7060", "item_name": "paper towels", "related_capture_ids": ["cap-original"]},
        "message": "Please send paper towels today.",
        "created_at": "2026-07-02T13:30:00+00:00",
    }

    job_type, payload, error = build_approval_route_job(doc, APPROVAL_ROUTE_SUPPLY_NEED, reviewer="per_reviewer")

    assert error == ""
    assert job_type == "log_supply_need"
    assert payload["item_name"] == "paper towels"
    assert payload["requested_by"] == "per_reviewer"
    assert payload["urgency"] == "high"
    assert payload["related_capture_ids"] == ["cap-supply-1", "cap-original"]
    assert validate_job({"job_type": job_type, "payload": payload}) is True


def test_no_action_builds_no_queue_job() -> None:
    assert build_approval_route_job(access_draft(), APPROVAL_ROUTE_NO_ACTION, reviewer="per_reviewer") == ("", {}, "")
