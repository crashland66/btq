from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import queue_processor.main as qp
from queue_spec import ALLOWED_JOB_TYPES


EXPECTED_HANDLER_NAMES = {
    qp.JOB_APPEND_TO_NOTE: "process_append_to_note_job",
    qp.JOB_ADD_PERSON: "process_add_person_job",
    qp.JOB_PERSONAL_JOURNAL_ENTRY: "process_personal_journal_entry_job",
    qp.JOB_FLAG_ACCESS_CONSTRAINT: "process_flag_access_constraint_job",
    qp.JOB_TRIGGER_RECRUITING: "process_trigger_recruiting_job",
    qp.JOB_CLOSE_RECRUITING: "process_close_recruiting_job",
    qp.JOB_PROMOTE_PROSPECT: "process_promote_prospect_job",
    qp.JOB_RETARGET_CAPTURE: "process_retarget_capture_job",
    qp.JOB_REMOVE_FROM_SCHEDULE: "process_remove_from_schedule_job",
    qp.JOB_FLAG_RETENTION_RISK: "process_flag_retention_risk_job",
    qp.JOB_RECLASSIFY_UNKNOWN: "process_reclassify_unknown_job",
    qp.JOB_VISIT_CREATE: "process_visit_create_job",
    qp.JOB_PARSE_SUPPLY_EMAIL: "process_parse_supply_email_job",
    qp.JOB_PHOTO_CAPTURE: "process_photo_capture_job",
    qp.JOB_LOG_SITE_ISSUE: "process_log_site_issue_job",
    qp.JOB_LOG_SUPPLY_NEED: "process_log_supply_need_job",
    qp.JOB_LOG_EQUIPMENT_REQUEST: "process_log_equipment_request_job",
    qp.JOB_LOG_PERSONNEL_EVENT: "process_log_personnel_event_job",
    qp.JOB_SET_ENTITY_STATUS: "process_set_entity_status_job",
    qp.JOB_UPDATE_SITE_EQUIPMENT: "process_update_site_equipment_job",
    qp.JOB_MARK_SUPPLY_ORDERED: "process_mark_supply_ordered_job",
    qp.JOB_MARK_SUPPLY_DELIVERED: "process_mark_supply_delivered_job",
    qp.JOB_MARK_SUPPLY_STOCKED: "process_mark_supply_stocked_job",
    qp.JOB_MARK_SUPPLY_NO_ACTION_NEEDED: "process_mark_supply_no_action_needed_job",
    qp.JOB_MARK_EQUIPMENT_APPROVED: "process_mark_equipment_approved_job",
    qp.JOB_MARK_EQUIPMENT_DENIED: "process_mark_equipment_denied_job",
    qp.JOB_MARK_EQUIPMENT_ORDERED: "process_mark_equipment_ordered_job",
    qp.JOB_MARK_EQUIPMENT_PROVIDED: "process_mark_equipment_provided_job",
    qp.JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED: "process_mark_equipment_no_action_needed_job",
    qp.JOB_MARK_ISSUE_MONITORING: "process_mark_issue_monitoring_job",
    qp.JOB_MARK_ISSUE_RESOLVED: "process_mark_issue_resolved_job",
    qp.JOB_MARK_ISSUE_OPEN: "process_mark_issue_open_job",
    qp.JOB_MARK_RECORD_ARCHIVED: "process_mark_record_archived_job",
    qp.JOB_MARK_RECORD_UNARCHIVED: "process_mark_record_unarchived_job",
    qp.JOB_EDIT_RECORD_FIELDS: "process_edit_record_fields_job",
    qp.JOB_VOICE_MEMO_NOTE: "process_voice_memo_note_job",
}


def context_for(root: Path) -> qp.RunContext:
    runtime_root = root / "runtime"
    runtime_root.mkdir(parents=True)
    return qp.RunContext(
        project_root=root,
        vault_root=root / "vault",
        personal_vault_root=root / "personal",
        runtime_root=runtime_root,
        log_path=runtime_root / "queue.log",
        dry_run=False,
        valid_site_ids=set(),
        site_id_to_opportunities_dir={},
    )


def test_registry_covers_all_allowed_job_types() -> None:
    assert set(qp.JOB_HANDLERS) == set(ALLOWED_JOB_TYPES) - qp._NON_QUEUE_JOB_TYPES


def test_registry_still_covers_all_allowed_job_types() -> None:
    assert set(qp.JOB_HANDLERS) == set(ALLOWED_JOB_TYPES) - qp._NON_QUEUE_JOB_TYPES


def test_handlers_importable_from_handlers_package() -> None:
    homes = {
        "visits": ["process_visit_create_job", "process_photo_capture_job"],
        "people": [
            "process_add_person_job",
            "process_trigger_recruiting_job",
            "process_close_recruiting_job",
            "process_remove_from_schedule_job",
            "process_log_personnel_event_job",
            "process_set_entity_status_job",
        ],
        "supplies_equipment": [
            "process_log_supply_need_job",
            "process_log_equipment_request_job",
            "process_update_site_equipment_job",
        ],
        "supplies_equipment_transitions": [
            "process_mark_supply_ordered_job",
            "process_mark_supply_delivered_job",
            "process_mark_supply_stocked_job",
            "process_mark_supply_no_action_needed_job",
            "process_mark_equipment_approved_job",
            "process_mark_equipment_denied_job",
            "process_mark_equipment_ordered_job",
            "process_mark_equipment_provided_job",
            "process_mark_equipment_no_action_needed_job",
            "process_mark_issue_monitoring_job",
            "process_mark_issue_resolved_job",
            "process_mark_issue_open_job",
            "process_mark_record_archived_job",
            "process_mark_record_unarchived_job",
            "process_edit_record_fields_job",
        ],
        "site_flags_notes": [
            "process_log_site_issue_job",
            "process_flag_access_constraint_job",
            "process_flag_retention_risk_job",
            "process_append_to_note_job",
        ],
        "unknowns": ["process_record_unknown_capture_job", "process_reclassify_unknown_job"],
        "misc": [
            "process_promote_prospect_job",
            "process_retarget_capture_job",
            "process_parse_supply_email_job",
            "process_voice_memo_note_job",
            "process_personal_journal_entry_job",
        ],
    }
    for module_name, handler_names in homes.items():
        module = importlib.import_module(f"queue_processor.handlers.{module_name}")
        for handler_name in handler_names:
            assert callable(getattr(module, handler_name))


def test_no_star_import_from_shared() -> None:
    handlers_root = Path(qp.__file__).resolve().parent / "handlers"
    for path in handlers_root.glob("*.py"):
        if path.name == "_shared.py":
            continue
        assert "from ._shared import *" not in path.read_text(encoding="utf-8")


def test_handler_modules_do_not_reference_markdown_write_gate() -> None:
    handlers_root = Path(qp.__file__).resolve().parent / "handlers"
    for path in handlers_root.glob("*.py"):
        if path.name == "_shared.py":
            continue
        assert "_markdown_write_enabled" not in path.read_text(encoding="utf-8"), path.name


def test_handler_modules_within_size_budget() -> None:
    # Regrowth guard: no handler module — shared or domain — may exceed 700 lines.
    # (The point is preventing a return of the ~3000-line god-module; _shared holds
    # only genuinely cross-cutting helpers, each used by >=2 domains.)
    handlers_root = Path(qp.__file__).resolve().parent / "handlers"
    for path in handlers_root.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 700, path.name


def process_job_source_from_main() -> str:
    main_path = Path(qp.__file__).resolve()
    main_source = main_path.read_text(encoding="utf-8")
    main_tree = ast.parse(main_source)
    process_job_node = next(
        node
        for node in main_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "process_job"
    )
    source_lines = main_source.splitlines()
    return "\n".join(
        source_lines[process_job_node.lineno - 1:process_job_node.end_lineno]
    )


def test_process_job_has_no_ifelif_dispatch() -> None:
    process_job_source = process_job_source_from_main()
    forbidden_dispatch_lines = (
        "elif job.job_type ==",
        "elif job_type ==",
        "if job_type ==",
    )
    allowed_job_type_if = "if job.job_type == JOB_ADD_PERSON and job.idempotency_key is None:"

    for line in process_job_source.splitlines():
        stripped_line = line.strip()
        assert not any(
            stripped_line.startswith(pattern)
            for pattern in forbidden_dispatch_lines
        ), stripped_line
        if stripped_line.startswith("if job.job_type =="):
            assert stripped_line == allowed_job_type_if


def test_single_dispatch_path() -> None:
    process_job_source = process_job_source_from_main()
    process_job_tree = ast.parse(process_job_source)
    job_type_mapping_lookups: list[str] = []
    forbidden_handler_helpers: list[ast.Call] = []

    def is_job_job_type(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "job_type"
            and isinstance(node.value, ast.Name)
            and node.value.id == "job"
        )

    for node in ast.walk(process_job_tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and len(node.args) == 1
                and is_job_job_type(node.args[0])
            ):
                job_type_mapping_lookups.append(f"{node.func.value.id}.get")
            if isinstance(node.func, ast.Name) and node.func.id == "_handler_for_job_type":
                forbidden_handler_helpers.append(node)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and is_job_job_type(node.slice)
        ):
            job_type_mapping_lookups.append(f"{node.value.id}[]")

    assert job_type_mapping_lookups == ["JOB_HANDLERS.get"]
    assert forbidden_handler_helpers == []
    assert 'QueueProcessorError(f"Unsupported job type object: {job.job_type}")' in process_job_source


@pytest.mark.parametrize("job_type", sorted(EXPECTED_HANDLER_NAMES))
def test_registry_dispatch_matches_legacy_for_each_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
) -> None:
    assert qp.JOB_HANDLERS[job_type] is getattr(qp, EXPECTED_HANDLER_NAMES[job_type])

    context = context_for(tmp_path)
    queue_file = context.runtime_root / f"{job_type}.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"
    sentinel_calls = []
    loaded_job = qp.QueueJob(
        job_id=f"{job_type}-job",
        job_type=job_type,
        payload={},
        metadata={},
        intent={},
    )

    def sentinel(
        job_path: Path,
        job: qp.QueueJob,
        run_context: qp.RunContext,
        target_dir: Path,
    ) -> None:
        sentinel_calls.append((job_path, job, run_context, target_dir))

    monkeypatch.setattr(qp, "load_job", lambda path: loaded_job)
    monkeypatch.setattr(
        qp,
        "processed_job_id_exists",
        lambda runtime_root, processed_root, job_id: (False, "none"),
    )
    monkeypatch.setitem(qp.JOB_HANDLERS, job_type, sentinel)

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert sentinel_calls == [
        (queue_file.resolve(), loaded_job, context, processed_dir.resolve())
    ]


def test_unsupported_job_type_raises_queueprocessorerror() -> None:
    job_type = "unknown_job_type"

    with pytest.raises(
        qp.QueueProcessorError,
        match=f"Unsupported job type object: {job_type}",
    ):
        qp._handler_for_job_type(job_type)
