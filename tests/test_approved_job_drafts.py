from __future__ import annotations

import json
from pathlib import Path

from field_capture import approved_job_drafts
from processing_core.action_candidates import STATUS_APPROVED, action_candidate_payload, write_action_candidate_review
from queue_spec import JOB_VISIT_CREATE, _is_timezone_aware_iso_datetime, validate_job


def write_named_candidate(runtime_root: Path, summary: str) -> dict[str, object]:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=summary,
        rationale=summary,
        source_text=summary,
        source_context=summary,
        channel_metadata={"channel": "field_capture", "site_id": "7050", "area": "Restrooms", "upload_id": summary.replace(" ", "-")},
        status=STATUS_APPROVED,
    )
    write_action_candidate_review(approved_job_drafts.default_candidate_dir(runtime_root), candidate)
    return candidate


def supply_candidate(summary: str, *, candidate_type: str = "field_capture_follow_up") -> dict[str, object]:
    return action_candidate_payload(
        candidate_type=candidate_type,
        summary=summary,
        rationale=summary,
        source_text=summary,
        source_context=summary,
        channel_metadata={
            "channel": "field_capture",
            "site_id": "7050",
            "area": "Supply levels",
            "upload_id": "cap-supply-test",
            "captured_at": "2026-05-08T14:12:43+00:00",
            "person_name": "Tom Walsh",
        },
        status=STATUS_APPROVED,
    )


def equipment_candidate(summary: str, *, candidate_type: str = "field_capture_follow_up") -> dict[str, object]:
    return action_candidate_payload(
        candidate_type=candidate_type,
        summary=summary,
        rationale=summary,
        source_text=summary,
        source_context=summary,
        channel_metadata={
            "channel": "field_capture",
            "site_id": "7050",
            "area": "Equipment",
            "upload_id": "cap-equipment-test",
            "captured_at": "2026-05-08T14:12:43+00:00",
            "person_name": "Tom Walsh",
        },
        status=STATUS_APPROVED,
    )


def access_candidate(summary: str, *, site_id: str = "7022") -> dict[str, object]:
    return action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Confirm access requirements with the site contact.",
        rationale="Access constraint identified from field text.",
        source_text=summary,
        source_context=summary,
        channel_metadata={
            "channel": "field_capture",
            "site_id": site_id,
            "issue_type": "access",
            "upload_id": "cap-text-access-test",
            "captured_at": "2026-06-02T16:18:09+00:00",
            "person_name": "Jordan Avery",
        },
        status=STATUS_APPROVED,
    )


def seed_three_candidates(runtime_root: Path) -> list[dict[str, object]]:
    return [write_named_candidate(runtime_root, f"Broken drain {index} needs maintenance.") for index in range(3)]


def test_create_approved_job_drafts_candidate_ids_filter_processes_only_named(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidates = seed_three_candidates(runtime_root)

    counts = approved_job_drafts.create_approved_job_drafts(
        approved_job_drafts.default_candidate_dir(runtime_root),
        approved_job_drafts.default_draft_dir(runtime_root),
        runtime_root=runtime_root,
        candidate_ids={str(candidates[0]["candidate_id"])},
    )

    drafts = sorted(approved_job_drafts.default_draft_dir(runtime_root).glob("*.json"))
    assert counts == {"discovered": 3, "skipped": 2, "completed": 1, "failed": 0}
    assert len(drafts) == 1
    assert json.loads(drafts[0].read_text(encoding="utf-8"))["candidate_id"] == candidates[0]["candidate_id"]


def test_create_approved_job_drafts_candidate_ids_filter_skips_others(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidates = seed_three_candidates(runtime_root)

    approved_job_drafts.create_approved_job_drafts(
        approved_job_drafts.default_candidate_dir(runtime_root),
        approved_job_drafts.default_draft_dir(runtime_root),
        runtime_root=runtime_root,
        candidate_ids={str(candidates[1]["candidate_id"])},
    )

    written = [json.loads(path.read_text(encoding="utf-8"))["candidate_id"] for path in approved_job_drafts.default_draft_dir(runtime_root).glob("*.json")]
    assert written == [candidates[1]["candidate_id"]]
    assert candidates[0]["candidate_id"] not in written
    assert candidates[2]["candidate_id"] not in written


def test_approved_job_drafts_report_candidate_ids_filter_dry_run(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidates = seed_three_candidates(runtime_root)

    report = approved_job_drafts.approved_job_drafts_report(
        approved_job_drafts.default_candidate_dir(runtime_root),
        approved_job_drafts.default_draft_dir(runtime_root),
        runtime_root=runtime_root,
        dry_run=True,
        candidate_ids={str(candidates[2]["candidate_id"])},
    )

    assert report["dry_run"] is True
    assert report["counts"] == {"discovered": 3, "skipped": 2, "completed": 1, "failed": 0}
    assert [item["candidate_id"] for item in report["results"]] == [candidates[2]["candidate_id"]]
    assert not approved_job_drafts.default_draft_dir(runtime_root).exists()


def test_is_supply_need_candidate_detects_we_need_paper_towels() -> None:
    candidate = supply_candidate("We need paper towels at Summit Wire.")
    assert approved_job_drafts.is_supply_need_candidate(candidate) is True


def test_is_supply_need_candidate_detects_out_of_toilet_paper() -> None:
    candidate = supply_candidate("We're out of toilet paper here.")
    assert approved_job_drafts.is_supply_need_candidate(candidate) is True


def test_is_supply_need_candidate_detects_low_on_mop_heads() -> None:
    candidate = supply_candidate("Running low on mop heads in the restroom closet.")
    assert approved_job_drafts.is_supply_need_candidate(candidate) is True


def test_is_supply_need_candidate_returns_false_for_maintenance_issue() -> None:
    candidate = supply_candidate("Broken soap dispenser needs repair.")
    assert approved_job_drafts.is_supply_need_candidate(candidate) is False


def test_is_supply_need_candidate_returns_false_for_general_observation() -> None:
    candidate = supply_candidate("Restroom looked clean after the evening shift.")
    assert approved_job_drafts.is_supply_need_candidate(candidate) is False


def test_default_field_capture_supply_payload_returns_valid_payload(tmp_path: Path) -> None:
    candidate = supply_candidate("We need BrightWash cleaner at Summit Wire today.")
    payload = approved_job_drafts.default_field_capture_supply_payload(candidate, runtime_root=tmp_path)

    assert payload is not None
    assert payload["site_id"] == "7050"
    assert payload["item_name"] == "BrightWash cleaner"
    assert payload["requested_by"] == "Tom Walsh"
    assert payload["urgency"] == "high"
    assert payload["observed_at"] == "2026-05-08T14:12:43+00:00"
    assert payload["source"] == "We need BrightWash cleaner at Summit Wire today."
    assert payload["related_capture_ids"] == ["cap-supply-test"]
    assert payload["related_candidate_ids"] == [candidate["candidate_id"]]


def test_default_field_capture_supply_payload_extracts_item_name(tmp_path: Path) -> None:
    candidate = supply_candidate("We need paper towels at Summit Wire.")
    payload = approved_job_drafts.default_field_capture_supply_payload(candidate, runtime_root=tmp_path)

    assert payload is not None
    assert payload["item_name"] == "paper towels"


def test_default_field_capture_supply_payload_returns_none_when_not_supply_need(tmp_path: Path) -> None:
    candidate = supply_candidate("Restroom looked clean after the evening shift.")
    assert approved_job_drafts.default_field_capture_supply_payload(candidate, runtime_root=tmp_path) is None


def test_visit_payload_includes_visited_by_when_submitter_person_id_present() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Field audio completion note.",
        rationale="Field audio completion note.",
        source_text="All done looks good.",
        source_context="Completion audio note.",
        channel_metadata={
            "channel": "field_capture",
            "site_id": "7050",
            "submitter_person_id": "per_test005",
            "visit_proposed": True,
        },
        status=STATUS_APPROVED,
    )

    payload = approved_job_drafts.default_field_capture_visit_payload(candidate)

    assert payload is not None
    assert payload["visited_by"] == "per_test005"


def test_visit_payload_omits_visited_by_when_submitter_person_id_empty() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Field audio completion note.",
        rationale="Field audio completion note.",
        source_text="All done looks good.",
        source_context="Completion audio note.",
        channel_metadata={
            "channel": "field_capture",
            "site_id": "7050",
            "submitter_person_id": "",
            "visit_proposed": True,
        },
        status=STATUS_APPROVED,
    )

    payload = approved_job_drafts.default_field_capture_visit_payload(candidate)

    assert payload is not None
    assert "visited_by" not in payload


def test_visit_payload_includes_occurred_at_from_captured_at() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Synthetic completion note.",
        rationale="Synthetic completion note.",
        source_text="Synthetic completion note.",
        source_context="Synthetic completion note.",
        channel_metadata={
            "channel": "field_capture",
            "site_id": "site_synthetic_001",
            "captured_at": "2026-06-23T21:30:32-04:00",
            "visit_proposed": True,
        },
        status=STATUS_APPROVED,
    )

    payload = approved_job_drafts.default_field_capture_visit_payload(candidate)

    assert payload is not None
    assert payload["occurred_at"] == "2026-06-23T21:30:32-04:00"
    assert _is_timezone_aware_iso_datetime(payload["occurred_at"]) is True
    assert validate_job({"job_type": JOB_VISIT_CREATE, "payload": payload}) is True


def test_visit_payload_omits_occurred_at_when_capture_time_unknown() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Synthetic completion note.",
        rationale="Synthetic completion note.",
        source_text="Synthetic completion note.",
        source_context="Synthetic completion note.",
        channel_metadata={
            "channel": "field_capture",
            "site_id": "site_synthetic_001",
            "visit_proposed": True,
        },
        status=STATUS_APPROVED,
    )
    candidate.pop("created_at", None)

    payload = approved_job_drafts.default_field_capture_visit_payload(candidate)

    assert payload is not None
    assert "occurred_at" not in payload
    assert validate_job({"job_type": JOB_VISIT_CREATE, "payload": payload}) is True


def test_proposed_queue_job_routes_supply_need_candidate_to_log_supply_need(tmp_path: Path) -> None:
    candidate = supply_candidate("We need toilet paper at Summit Wire.")
    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate, runtime_root=tmp_path)

    assert error == ""
    assert job_type == "log_supply_need"
    assert isinstance(payload, dict)
    assert payload["item_name"] == "toilet paper"


def test_proposed_queue_job_prefers_maintenance_issue_over_supply_need(tmp_path: Path) -> None:
    candidate = supply_candidate("Broken toilet paper dispenser needs repair at Summit Wire.")
    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate, runtime_root=tmp_path)

    assert error == ""
    assert job_type == "log_site_issue"
    assert isinstance(payload, dict)
    assert payload["category"] == "maintenance"


def test_proposed_queue_job_routes_access_candidate_to_flag_access_constraint(tmp_path: Path) -> None:
    candidate = access_candidate("Access Notes: Inside Door code: 0419 Outside Lockbox: 12345 Alarm codes are posted on the alarm panel.")
    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate, runtime_root=tmp_path)

    assert error == ""
    assert job_type == "flag_access_constraint"
    assert isinstance(payload, dict)
    assert payload == {
        "site": "7022",
        "details": "Access Notes: Inside Door code: 0419 Outside Lockbox: 12345 Alarm codes are posted on the alarm panel.",
        "date": "2026-06-02",
    }


def test_is_equipment_request_candidate_detects_vacuum_broken() -> None:
    candidate = equipment_candidate("The vacuum is broken, we need a replacement at Summit Wire.")
    assert approved_job_drafts.is_equipment_request_candidate(candidate) is True


def test_is_equipment_request_candidate_detects_we_need_extension_pole() -> None:
    candidate = equipment_candidate("We need extension pole at Continental.")
    assert approved_job_drafts.is_equipment_request_candidate(candidate) is True


def test_is_equipment_request_candidate_detects_replace_mop_bucket() -> None:
    candidate = equipment_candidate("Replace mop bucket for the restroom crew.")
    assert approved_job_drafts.is_equipment_request_candidate(candidate) is True


def test_is_equipment_request_candidate_returns_false_for_supply_need() -> None:
    candidate = equipment_candidate("We need paper towels at Summit Wire.")
    assert approved_job_drafts.is_equipment_request_candidate(candidate) is False


def test_is_equipment_request_candidate_returns_false_for_maintenance_issue() -> None:
    candidate = equipment_candidate("Broken soap dispenser needs repair.")
    assert approved_job_drafts.is_equipment_request_candidate(candidate) is False


def test_default_field_capture_equipment_payload_returns_valid_payload(tmp_path: Path) -> None:
    candidate = equipment_candidate("The vacuum is broken, we need a replacement at Summit Wire ASAP.")
    payload = approved_job_drafts.default_field_capture_equipment_payload(candidate, runtime_root=tmp_path)

    assert payload is not None
    assert payload["site_id"] == "7050"
    assert payload["equipment_name"] == "vacuum"
    assert payload["requested_by"] == "Tom Walsh"
    assert payload["priority"] == "urgent"
    assert payload["observed_at"] == "2026-05-08T14:12:43+00:00"
    assert payload["source"] == "field_capture"
    assert payload["related_capture_ids"] == ["cap-equipment-test"]
    assert payload["related_candidate_ids"] == [candidate["candidate_id"]]
    assert "vacuum is broken" in payload["reason"]


def test_default_field_capture_equipment_payload_extracts_equipment_name(tmp_path: Path) -> None:
    candidate = equipment_candidate("We need extension pole at Summit Wire.")
    payload = approved_job_drafts.default_field_capture_equipment_payload(candidate, runtime_root=tmp_path)

    assert payload is not None
    assert payload["equipment_name"] == "extension pole"


def test_default_field_capture_equipment_payload_returns_none_when_not_equipment_request(tmp_path: Path) -> None:
    candidate = equipment_candidate("Restroom looked clean after the evening shift.")
    assert approved_job_drafts.default_field_capture_equipment_payload(candidate, runtime_root=tmp_path) is None


def test_proposed_queue_job_routes_equipment_request_candidate_to_log_equipment_request(tmp_path: Path) -> None:
    candidate = equipment_candidate("We need extension pole at Summit Wire.")
    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate, runtime_root=tmp_path)

    assert error == ""
    assert job_type == "log_equipment_request"
    assert isinstance(payload, dict)
    assert payload["equipment_name"] == "extension pole"


def test_proposed_queue_job_prefers_supply_need_over_equipment_request(tmp_path: Path) -> None:
    candidate = equipment_candidate("We need dispenser refills at Summit Wire.")
    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate, runtime_root=tmp_path)

    assert error == ""
    assert job_type == "log_supply_need"
    assert isinstance(payload, dict)
    assert payload["item_name"] == "dispenser refills"
