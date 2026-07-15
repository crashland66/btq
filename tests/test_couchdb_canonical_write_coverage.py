"""CouchDB canonical-write coverage inventory.

Report:
- canonical writers: 42 (41 queue job types plus photo_vision)
- configured CouchDB-path covered: 42
- known uncovered: 0

To close a future gap, add a real configured-path test for the writer, then
move that writer from KNOWN_UNCOVERED to COUCHDB_PATH_TESTS with the collectable
test node id. A configured-path test uses the recording vault store, a dedicated
recording CouchDB store, or an injected/monkeypatched CouchDB put path.
"""

from __future__ import annotations

import ast
from pathlib import Path

from queue_processor.registry import JOB_HANDLERS


CANONICAL_WRITERS: frozenset[str] = frozenset(
    {
        "add_person",
        "append_to_note",
        "assign_employee_site",
        "close_recruiting",
        "edit_record_fields",
        "flag_access_constraint",
        "flag_retention_risk",
        "log_availability_constraint",
        "log_equipment_request",
        "log_personnel_event",
        "log_site_issue",
        "log_supply_need",
        "mark_equipment_approved",
        "mark_equipment_denied",
        "mark_equipment_no_action_needed",
        "mark_equipment_ordered",
        "mark_equipment_provided",
        "mark_issue_monitoring",
        "mark_issue_open",
        "mark_issue_resolved",
        "mark_record_archived",
        "mark_record_unarchived",
        "mark_supply_delivered",
        "mark_supply_no_action_needed",
        "mark_supply_ordered",
        "mark_supply_stocked",
        "parse_supply_email",
        "personal_journal_entry",
        "photo_capture",
        "photo_vision",
        "record_day_record",
        "record_shift_report",
        "record_unknown_capture",
        "remove_from_schedule",
        "set_contact",
        "set_employee_contact",
        "set_employee_id",
        "set_entity_status",
        "set_site_hours",
        "set_site_url",
        "shift_report_note",
        "trigger_recruiting",
        "update_site_equipment",
        "visit_create",
        "voice_memo_note",
    }
)


NON_CANONICAL_JOB_TYPES: frozenset[str] = frozenset(
    {
        "deep_analysis",  # delegates
        "promote_prospect",  # prospect
        "reclassify_unknown",  # classifier
        "retarget_capture",  # capture
    }
)


COUCHDB_PATH_TESTS: dict[str, list[str]] = {
    "add_person": [
        "tests/test_queue_processor.py::test_add_person_creates_canonical_person_file",
    ],
    "append_to_note": [
        "tests/test_queue_processor.py::test_append_to_note_writes_canonical_note_doc",
    ],
    "assign_employee_site": [
        "project/tests/test_assign_employee_site_job.py::test_assign_adds_site_and_preserves_other_fields",
    ],
    "close_recruiting": [
        "project/tests/test_queue_processor_couchdb_write.py::test_close_recruiting_filled_updates_site_and_employee_canonical_content",
    ],
    "edit_record_fields": [
        "project/tests/test_queue_processor_couchdb_write.py::test_edit_record_fields_applies_only_allowlisted_fields_and_site_name",
    ],
    "flag_access_constraint": [
        "project/tests/test_queue_processor_couchdb_write.py::test_flag_access_constraint_appends_to_canonical_location_content",
    ],
    "flag_retention_risk": [
        "project/tests/test_queue_processor_couchdb_write.py::test_flag_retention_risk_appends_to_canonical_content",
    ],
    "log_availability_constraint": [
        "project/tests/test_availability_constraint_writer.py::test_c1_valid_job_writes_single_canonical_doc",
    ],
    "log_equipment_request": [
        "project/tests/test_queue_processor_couchdb_write.py::test_log_equipment_request_creates_canonical_doc_when_absent",
    ],
    "log_personnel_event": [
        "project/tests/test_queue_processor_couchdb_write.py::test_log_personnel_event_creates_canonical_doc_when_absent",
    ],
    "log_site_issue": [
        "project/tests/test_queue_processor_couchdb_write.py::test_log_site_issue_creates_canonical_doc_when_absent",
    ],
    "log_supply_need": [
        "project/tests/test_queue_processor_couchdb_write.py::test_log_supply_need_creates_canonical_doc_when_absent",
    ],
    "mark_equipment_approved": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_equipment_valid_transition_uses_canonical_status",
    ],
    "mark_equipment_denied": [
        "tests/test_queue_processor.py::test_mark_equipment_denied_allowed_from_open_or_approved",
    ],
    "mark_equipment_no_action_needed": [
        "tests/test_queue_processor.py::test_mark_equipment_no_action_needed_allowed_from_any_non_terminal",
    ],
    "mark_equipment_ordered": [
        "tests/test_queue_processor.py::test_mark_equipment_ordered_advances_status_from_approved",
    ],
    "mark_equipment_provided": [
        "tests/test_queue_processor.py::test_mark_equipment_provided_advances_status_from_ordered",
    ],
    "mark_issue_monitoring": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_issue_monitoring_advances_status_from_open",
    ],
    "mark_issue_open": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_issue_open_reopens_monitoring_or_resolved",
    ],
    "mark_issue_resolved": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_issue_resolved_valid_transition_uses_canonical_status",
    ],
    "mark_record_archived": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_record_archived_sets_archive_fields",
    ],
    "mark_record_unarchived": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_record_unarchived_clears_archive_fields",
    ],
    "mark_supply_delivered": [
        "tests/test_queue_processor.py::test_mark_supply_delivered_advances_status_from_ordered",
    ],
    "mark_supply_no_action_needed": [
        "tests/test_queue_processor.py::test_mark_supply_no_action_needed_allowed_from_any_non_terminal",
    ],
    "mark_supply_ordered": [
        "project/tests/test_queue_processor_couchdb_write.py::test_mark_supply_valid_transition_uses_canonical_status",
    ],
    "mark_supply_stocked": [
        "tests/test_queue_processor.py::test_mark_supply_stocked_advances_status_from_delivered",
    ],
    "parse_supply_email": [
        "tests/test_queue_processor.py::test_parse_supply_email_creates_canonical_supply_order_with_operator",
    ],
    "personal_journal_entry": [
        "tests/test_queue_processor.py::test_personal_journal_writes_canonical_doc_to_personal_db",
    ],
    "photo_capture": [
        "project/tests/test_queue_processor_couchdb_write.py::test_photo_capture_creates_canonical_journal_doc_with_operator",
    ],
    "photo_vision": [
        "project/tests/test_deep_analysis_canonical_write_415.py::test_configured_couch_put_success_writes_sidecar_and_returns_entry",
    ],
    "record_day_record": [
        "project/tests/test_record_day_record_job.py::test_processing_writes_canonical_day_record_doc",
    ],
    "record_shift_report": [
        "project/tests/test_record_shift_report_job.py::test_processing_writes_canonical_shift_report_doc",
    ],
    "record_unknown_capture": [
        "project/tests/test_record_unknown_capture.py::test_record_unknown_capture_handler_creates_canonical_doc_without_projection",
    ],
    "remove_from_schedule": [
        "project/tests/test_queue_processor_couchdb_write.py::test_remove_from_schedule_appends_to_canonical_content",
    ],
    "set_contact": [
        "project/tests/test_set_contact_job.py::test_site_upsert_adds_and_replaces_by_id_without_touching_customer_fields",
    ],
    "set_employee_id": [
        "project/tests/test_set_employee_id_job.py::test_happy_path",
    ],
    "set_employee_contact": [
        "project/tests/test_set_employee_contact_job.py::test_set_employee_contact_updates_only_contact_and_audit_fields",
    ],
    "set_entity_status": [
        "project/tests/test_queue_processor_couchdb_write.py::test_set_entity_status_site_sets_active_and_removes_legacy_status",
    ],
    "set_site_url": [
        "project/tests/test_set_site_url_job.py::test_missing_urls_reads_as_empty_and_add_appends",
    ],
    "set_site_hours": [
        "project/tests/test_set_site_hours_job.py::test_set_via_job_populates_valid_structure",
    ],
    "shift_report_note": [
        "project/tests/test_shift_report_note_job_412a.py::test_handler_routes_through_apply_canonical_rmw",
    ],
    "trigger_recruiting": [
        "project/tests/test_queue_processor_couchdb_write.py::test_trigger_recruiting_appends_to_canonical_location_content",
    ],
    "update_site_equipment": [
        "tests/test_queue_processor.py::test_update_site_equipment_patches_canonical_content",
    ],
    "visit_create": [
        "project/tests/test_queue_processor_couchdb_write.py::test_visit_create_second_visit_same_day_different_evidence_creates_doc",
    ],
    "voice_memo_note": [
        "project/tests/test_queue_processor_voice_memo_note.py::QueueProcessorVoiceMemoNoteTests::test_voice_memo_note_site_appends_to_canonical_location_content",
    ],
}


KNOWN_UNCOVERED: dict[str, str] = {}


def test_canonical_writer_partition_matches_live_registry() -> None:
    expected = set(JOB_HANDLERS) | {"photo_vision"}

    assert CANONICAL_WRITERS | NON_CANONICAL_JOB_TYPES == expected
    assert CANONICAL_WRITERS.isdisjoint(NON_CANONICAL_JOB_TYPES)


def test_couchdb_path_coverage_partition_is_complete() -> None:
    covered = set(COUCHDB_PATH_TESTS)
    uncovered = set(KNOWN_UNCOVERED)

    assert covered | uncovered == CANONICAL_WRITERS
    assert covered.isdisjoint(uncovered)
    assert all(nodes for nodes in COUCHDB_PATH_TESTS.values())


def test_couchdb_path_test_node_ids_resolve_to_real_tests() -> None:
    for writer, node_ids in COUCHDB_PATH_TESTS.items():
        for node_id in node_ids:
            assert _node_id_exists(node_id), f"{writer}: bogus coverage node id {node_id!r}"


def _node_id_exists(node_id: str) -> bool:
    parts = node_id.split("::")
    if len(parts) not in {2, 3}:
        return False

    repo_root = Path(__file__).resolve().parents[1]
    test_path = repo_root / parts[0]
    if not test_path.is_file():
        return False

    tree = ast.parse(test_path.read_text(encoding="utf-8"), filename=str(test_path))
    if len(parts) == 2:
        return any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[1]
            for node in tree.body
        )

    class_name, method_name = parts[1:]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name
                for item in node.body
            )
    return False
