from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import require_directories
from queue_processor.handlers import contacts, misc, people, site_flags_notes, site_hours, site_operational_calendar, site_urls, supplies_equipment, supplies_equipment_transitions, supply_requests, unknowns, visits
from queue_processor.handlers import _shared
from queue_processor.processed_index import ProcessedJobIdLookup, append_record, build_record, load_processed_job_id_lookup, processed_job_id_exists as indexed_processed_job_id_exists
from queue_processor.manifest import record_processed_mutation
from queue_processor.processor_lock import ProcessorLock, ProcessorLockError
from queue_processor.registry import JOB_HANDLERS, JobHandler, _handler_for_job_type
from queue_spec import (
    validate_job,
    JOB_ADD_PERSON, JOB_APPEND_TO_NOTE, JOB_ASSIGN_EMPLOYEE_SITE, JOB_CLOSE_RECRUITING, JOB_FLAG_ACCESS_CONSTRAINT, JOB_FLAG_RETENTION_RISK,
    JOB_CREATE_SUPPLY_REQUEST, JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_PERSONNEL_EVENT, JOB_LOG_SITE_ISSUE, JOB_LOG_SUPPLY_NEED,
    JOB_MARK_EQUIPMENT_APPROVED, JOB_MARK_EQUIPMENT_DENIED, JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED,
    JOB_MARK_EQUIPMENT_ORDERED, JOB_MARK_EQUIPMENT_PROVIDED, JOB_MARK_SUPPLY_DELIVERED,
    JOB_MARK_ISSUE_MONITORING, JOB_MARK_ISSUE_OPEN, JOB_MARK_ISSUE_RESOLVED,
    JOB_MARK_RECORD_ARCHIVED, JOB_MARK_RECORD_UNARCHIVED, JOB_EDIT_RECORD_FIELDS,
    JOB_MARK_SUPPLY_NO_ACTION_NEEDED, JOB_MARK_SUPPLY_ORDERED, JOB_MARK_SUPPLY_STOCKED,
    JOB_DEEP_ANALYSIS, JOB_LOG_AVAILABILITY_CONSTRAINT,
    JOB_PARSE_SUPPLY_EMAIL, JOB_PERSONAL_JOURNAL_ENTRY, JOB_PHOTO_CAPTURE, JOB_PROMOTE_PROSPECT,
    JOB_RECORD_DAY_RECORD, JOB_RECORD_SHIFT_REPORT, JOB_RECORD_UNKNOWN_CAPTURE, JOB_RECLASSIFY_UNKNOWN,
    JOB_REMOVE_FROM_SCHEDULE, JOB_RETARGET_CAPTURE, JOB_SET_EMPLOYEE_CONTACT, JOB_SET_EMPLOYEE_HOME_ADDRESS, JOB_SET_EMPLOYEE_ID, JOB_SHIFT_REPORT_NOTE, JOB_TRIGGER_RECRUITING,
    JOB_SET_CONTACT, JOB_SET_ENTITY_STATUS, JOB_SET_SITE_HOURS, JOB_SET_SITE_OPERATIONAL_CALENDAR, JOB_SET_SITE_URL, JOB_UPDATE_SITE_EQUIPMENT, JOB_VISIT_CREATE, JOB_VOICE_MEMO_NOTE,
)

_NON_QUEUE_JOB_TYPES = frozenset[str]()

DEFAULT_CONFIG = _shared.DEFAULT_CONFIG
DEFAULT_PROJECT_ROOT = _shared.DEFAULT_PROJECT_ROOT
DEFAULT_RUNTIME_ROOT = _shared.DEFAULT_RUNTIME_ROOT
DEFAULT_LOGS_ROOT = _shared.DEFAULT_LOGS_ROOT
QueueProcessorError = _shared.QueueProcessorError
QueueJobError = _shared.QueueJobError
InvalidSiteIdError = _shared.InvalidSiteIdError
QueueJob = _shared.QueueJob
RunContext = _shared.RunContext
validate_date_string = _shared.validate_date_string
validate_payload_keys = _shared.validate_payload_keys
compute_job_id = _shared.compute_job_id
parse_frontmatter = _shared.parse_frontmatter
get_frontmatter_value = _shared.get_frontmatter_value
ensure_within_root = _shared.ensure_within_root
ensure_within_any_root = _shared.ensure_within_any_root
move_job_file = _shared.move_job_file
atomic_write_text = _shared.atomic_write_text
_vault_store = _shared._vault_store
_reset_vault_store = _shared._reset_vault_store
_canonical_vault_upsert = _shared._canonical_vault_upsert
_field_capture_config = _shared._field_capture_config
_field_capture_database = _shared._field_capture_database
_site_id_registered = _shared._site_id_registered
_find_prospect_captures = _shared._find_prospect_captures
get_field_capture_document = _shared.get_field_capture_document
put_field_capture_document = _shared.put_field_capture_document
load_prospect = _shared.load_prospect
write_prospect = _shared.write_prospect


def processed_job_id_exists(runtime_root: Path, processed_dir: Path, job_id: str) -> tuple[bool, str]:
    return indexed_processed_job_id_exists(runtime_root, processed_dir, job_id)

process_visit_create_job = visits.process_visit_create_job
process_photo_capture_job = visits.process_photo_capture_job
process_set_contact_job = contacts.process_set_contact_job
process_set_site_hours_job = site_hours.process_set_site_hours_job
process_set_site_operational_calendar_job = site_operational_calendar.process_set_site_operational_calendar_job
process_add_person_job = people.process_add_person_job
process_assign_employee_site_job = people.process_assign_employee_site_job
process_trigger_recruiting_job = people.process_trigger_recruiting_job
process_close_recruiting_job = people.process_close_recruiting_job
process_remove_from_schedule_job = people.process_remove_from_schedule_job
process_log_personnel_event_job = people.process_log_personnel_event_job
process_set_entity_status_job = people.process_set_entity_status_job
process_set_site_url_job = site_urls.process_set_site_url_job
process_log_supply_need_job = supplies_equipment.process_log_supply_need_job
process_create_supply_request_job = supply_requests.process_create_supply_request_job
process_log_equipment_request_job = supplies_equipment.process_log_equipment_request_job
process_update_site_equipment_job = supplies_equipment.process_update_site_equipment_job
_process_mark_supply_job = supplies_equipment_transitions._process_mark_supply_job
_process_mark_equipment_job = supplies_equipment_transitions._process_mark_equipment_job
_process_mark_issue_job = supplies_equipment_transitions._process_mark_issue_job
process_mark_supply_ordered_job = supplies_equipment_transitions.process_mark_supply_ordered_job
process_mark_supply_delivered_job = supplies_equipment_transitions.process_mark_supply_delivered_job
process_mark_supply_stocked_job = supplies_equipment_transitions.process_mark_supply_stocked_job
process_mark_supply_no_action_needed_job = supplies_equipment_transitions.process_mark_supply_no_action_needed_job
process_mark_equipment_approved_job = supplies_equipment_transitions.process_mark_equipment_approved_job
process_mark_equipment_denied_job = supplies_equipment_transitions.process_mark_equipment_denied_job
process_mark_equipment_ordered_job = supplies_equipment_transitions.process_mark_equipment_ordered_job
process_mark_equipment_provided_job = supplies_equipment_transitions.process_mark_equipment_provided_job
process_mark_equipment_no_action_needed_job = supplies_equipment_transitions.process_mark_equipment_no_action_needed_job
process_mark_issue_monitoring_job = supplies_equipment_transitions.process_mark_issue_monitoring_job
process_mark_issue_resolved_job = supplies_equipment_transitions.process_mark_issue_resolved_job
process_mark_issue_open_job = supplies_equipment_transitions.process_mark_issue_open_job
process_mark_record_archived_job = supplies_equipment_transitions.process_mark_record_archived_job
process_mark_record_unarchived_job = supplies_equipment_transitions.process_mark_record_unarchived_job
process_edit_record_fields_job = supplies_equipment_transitions.process_edit_record_fields_job
process_log_site_issue_job = site_flags_notes.process_log_site_issue_job
process_append_to_note_job = site_flags_notes.process_append_to_note_job
process_flag_access_constraint_job = site_flags_notes.process_flag_access_constraint_job
process_flag_retention_risk_job = site_flags_notes.process_flag_retention_risk_job
process_reclassify_unknown_job = unknowns.process_reclassify_unknown_job
process_unknowns = unknowns.process_unknowns
handle_promote_prospect = misc.handle_promote_prospect
handle_retarget_capture = misc.handle_retarget_capture
process_promote_prospect_job = misc.process_promote_prospect_job
process_retarget_capture_job = misc.process_retarget_capture_job
process_voice_memo_note_job = misc.process_voice_memo_note_job
process_personal_journal_entry_job = misc.process_personal_journal_entry_job
process_parse_supply_email_job = misc.process_parse_supply_email_job
# Re-exports for symbols still imported from queue_processor.main by other
# modules/tests (replay, btq, voice-memo + journal tests) after the 181 move.
render_personal_journal_entry = misc.render_personal_journal_entry
upsert_voice_memo_capture_id_frontmatter = misc.upsert_voice_memo_capture_id_frontmatter
voice_memo_capture_id = misc.voice_memo_capture_id
replace_or_insert_subsection = supplies_equipment.replace_or_insert_subsection
validate_person_id = people.validate_person_id


def ensure_local_runtime_root(runtime_root: Path) -> Path:
    resolved_runtime_root = runtime_root.expanduser().resolve()
    pipeline_dir = DEFAULT_CONFIG.pipeline_dir.expanduser()
    if _shared.path_is_within(resolved_runtime_root, pipeline_dir):
        raise QueueProcessorError(f"Runtime root must be local non-iCloud storage, not inside pipeline_dir: {resolved_runtime_root}")
    if _shared.looks_like_icloud_path(resolved_runtime_root):
        raise QueueProcessorError(f"Runtime root must not be inside an iCloud-managed path: {resolved_runtime_root}")
    return resolved_runtime_root

def validate_job_file_name(job_path: Path) -> None:
    if job_path.suffix != ".json":
        raise QueueProcessorError(f"Queue file must end with .json: {job_path.name}")

def load_job_payload(job_path: Path) -> dict:
    validate_job_file_name(job_path)
    try:
        raw_payload = json.loads(job_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise QueueProcessorError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(raw_payload, dict):
        raise QueueProcessorError("Job payload must be a JSON object")
    return raw_payload

def load_job(job_path: Path) -> QueueJob:
    raw_payload = load_job_payload(job_path)
    if not validate_job(raw_payload):
        raise QueueProcessorError("Job does not match queue_spec")
    job_id = _shared.compute_job_id(raw_payload)
    return QueueJob(
        job_id=job_id,
        job_type=str(raw_payload["job_type"]),
        payload=dict(raw_payload["payload"]),
        metadata=dict(raw_payload.get("metadata", {})) if isinstance(raw_payload.get("metadata"), dict) else {},
        intent=dict(raw_payload.get("intent", {})) if isinstance(raw_payload.get("intent"), dict) else {},
        idempotency_key=str(raw_payload["idempotency_key"]).strip() if isinstance(raw_payload.get("idempotency_key"), str) else None,
    )

def append_processed_index_record(job_path: Path, job: QueueJob, context: RunContext, target_path: str, processed_job_ids: ProcessedJobIdLookup | None = None) -> None:
    if context.dry_run:
        return
    record = build_record(computed_job_id=job.job_id, job_type=job.job_type, target_path=target_path, source_queue_file=job_path, capture_id=_shared.capture_id_for_job(job), run_id=context.run_id)
    try:
        append_record(context.runtime_root / "processed_index.jsonl", record)
        if processed_job_ids is not None:
            processed_job_ids.mark_indexed(job.job_id)
        if record.capture_id is not None:
            record_processed_mutation(context.runtime_root, record.capture_id, computed_job_id=job.job_id, job_type=job.job_type, target_path=target_path, source_queue_file=str(job_path), processed_record=record.__dict__)
        _shared.structured_log(context, "processed_index_appended", computed_job_id=job.job_id, job_type=job.job_type, source_queue_file=str(job_path), target_path=target_path, capture_id=_shared.capture_id_for_job(job))
        if record.capture_id is not None:
            _shared.structured_log(context, "manifest_created", capture_id=record.capture_id, computed_job_id=job.job_id, target_path=target_path)
    except Exception as exc:
        _shared.structured_log(context, "processed_index_append_failed", computed_job_id=job.job_id, job_type=job.job_type, source_queue_file=str(job_path), error=str(exc), capture_id=_shared.capture_id_for_job(job))
        _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=processed-index-append status=failure error={exc}")


def _job_has_applied_marker(job: QueueJob, context: RunContext) -> tuple[bool, str | None]:
    try:
        doc_id = _shared._vault_store().job_id_applied_doc_id(job.job_id)
    except Exception:
        return False, None
    return doc_id is not None, doc_id


def _files_have_identical_content(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(64 * 1024)
                right_chunk = right_handle.read(64 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _revision_queue_job_path(job_path: Path, context: RunContext, job: QueueJob) -> Path:
    """Give a revised job a collision-free archive name before processing it."""
    digest = hashlib.sha256(job_path.read_bytes()).hexdigest()[:12]
    revision_path = job_path.with_name(f"{job_path.stem}.{digest}{job_path.suffix}")
    revision_path = context.assert_runtime_write_path(revision_path, "revisioned queue job path")
    if revision_path.exists():
        if _files_have_identical_content(job_path, revision_path):
            job_path.unlink()
            return revision_path
        raise QueueProcessorError(f"Revision queue destination already exists: {revision_path}")
    job_path.replace(revision_path)
    _shared.write_log_line(
        context.log_path,
        f"job_id={job.job_id} action=queue-revision-rename status=success source={job_path} destination={revision_path}",
    )
    _shared.structured_log(
        context,
        "queue_revision_renamed",
        computed_job_id=job.job_id,
        job_type=job.job_type,
        source_queue_file=str(job_path),
        revision_queue_file=str(revision_path),
        capture_id=_shared.capture_id_for_job(job),
    )
    return revision_path


def _archive_job_file(
    job_path: Path,
    destination_dir: Path,
    context: RunContext,
    *,
    job: QueueJob | None,
    already_handled: bool,
    handled_source: str,
    processed_job_ids: ProcessedJobIdLookup | None = None,
) -> Path:
    destination_path = destination_dir / job_path.name
    try:
        moved_path = _shared.move_job_file(job_path, destination_dir)
        if job is not None and destination_dir.name == "processed" and processed_job_ids is not None:
            processed_job_ids.mark_processed_archive(job.job_id)
        return moved_path
    except Exception as exc:
        if not destination_path.exists():
            raise
        computed_job_id = job.job_id if job is not None else job_path.stem
        job_type = job.job_type if job is not None else None
        capture_id = _shared.capture_id_for_job(job) if job is not None else None
        if already_handled:
            source_removed = False
            if not context.dry_run and job_path.exists():
                job_path.unlink()
                source_removed = True
            if job is not None and destination_dir.name == "processed" and processed_job_ids is not None:
                processed_job_ids.mark_processed_archive(job.job_id)
            _shared.write_log_line(
                context.log_path,
                f"job_id={computed_job_id} action=archive-collision status=success reason=already-handled source={handled_source} destination={destination_path}",
            )
            _shared.structured_log(
                context,
                "queue_archive_collision_already_handled",
                computed_job_id=computed_job_id,
                job_type=job_type,
                source_queue_file=str(job_path),
                destination_path=str(destination_path),
                destination_dir=destination_dir.name,
                handled_source=handled_source,
                source_removed=source_removed,
                capture_id=capture_id,
            )
            return destination_path
        _shared.structured_log(
            context,
            "queue_archive_collision_ambiguous",
            computed_job_id=computed_job_id,
            job_type=job_type,
            source_queue_file=str(job_path),
            destination_path=str(destination_path),
            destination_dir=destination_dir.name,
            error=str(exc),
            capture_id=capture_id,
        )
        raise

def _canonical_location_hint(value: object) -> str:
    raw = str(value).strip()
    try:
        site_id = _shared.resolve_site_id(raw)
        if site_id is None and " - " in raw:
            site_id = _shared.resolve_site_id(raw.split(" - ", 1)[0].strip())
    except Exception:
        site_id = None
    return f"location_{site_id or raw}"


def _canonical_employee_hint(value: object) -> str:
    return _shared.canonical_employee_doc_id_from_name(str(value))


def _canonical_account_hint(value: object) -> str:
    raw = str(value).strip()
    if raw.startswith("account_"):
        return raw
    slug = "_".join("".join(ch if ch.isalnum() else " " for ch in raw.lower()).split())
    return f"account_{slug or raw}"


def _append_to_note_target_hint(payload: dict, context: RunContext) -> str:
    raw_path = str(payload["path"])
    path = Path(raw_path)
    if len(path.parts) == 2 and path.parts[0] == "Journal" and path.suffix == ".md":
        return f"note_journal_{path.stem}"
    site_value = payload.get("site_id") or payload.get("site")
    if site_value:
        return _canonical_location_hint(site_value)
    if path.name == "about.md" and "Locations" in path.parts:
        locations_index = path.parts.index("Locations")
        if locations_index + 1 < len(path.parts):
            site_folder = path.parts[locations_index + 1]
            site_id = site_folder.split(" - ", 1)[0].strip()
            if site_id:
                return _canonical_location_hint(site_id)
    try:
        if _shared.is_site_about_path(path):
            return _shared.canonical_location_doc_id_for_projection(path, "")
    except Exception:
        pass
    return raw_path


def target_path_hint(job: QueueJob, context: RunContext) -> str:
    payload = job.payload
    try:
        if job.job_type == JOB_APPEND_TO_NOTE:
            return _append_to_note_target_hint(payload, context)
        if job.job_type == JOB_RECLASSIFY_UNKNOWN:
            return str(payload["path"])
        if job.job_type == JOB_PERSONAL_JOURNAL_ENTRY:
            date = _shared.validate_date_string(str(payload["date"]))
            return f"journal_personal_{date}"
        if job.job_type == JOB_RECORD_SHIFT_REPORT:
            date = _shared.validate_date_string(str(payload["date"]).strip())
            return f"shift_report_journal_{date.replace('-', '_')}_shift_report"
        if job.job_type == JOB_SHIFT_REPORT_NOTE:
            date = _shared.validate_date_string(str(payload["date"]).strip())
            return misc._shift_report_note_doc_id(date, str(payload["content"]), str(payload["capture_id"]).strip())
        if job.job_type == JOB_RECORD_DAY_RECORD:
            date = _shared.validate_date_string(str(payload["date"]).strip())
            return f"day_record_{date.replace('-', '_')}"
        if job.job_type == JOB_VISIT_CREATE:
            return _canonical_location_hint(payload["site"])
        if job.job_type in {JOB_FLAG_ACCESS_CONSTRAINT, JOB_TRIGGER_RECRUITING, JOB_CLOSE_RECRUITING}:
            return _canonical_location_hint(payload["site"])
        if job.job_type in {JOB_REMOVE_FROM_SCHEDULE, JOB_FLAG_RETENTION_RISK}:
            return _canonical_employee_hint(payload["employee"])
        if job.job_type == JOB_ADD_PERSON:
            return _canonical_employee_hint(payload["name"])
        if job.job_type == JOB_ASSIGN_EMPLOYEE_SITE:
            return _canonical_employee_hint(payload["employee_id"])
        if job.job_type == JOB_SET_EMPLOYEE_ID:
            return _canonical_employee_hint(payload["person"])
        if job.job_type == JOB_SET_EMPLOYEE_CONTACT:
            return _canonical_employee_hint(payload["person"])
        if job.job_type == JOB_SET_EMPLOYEE_HOME_ADDRESS:
            return _canonical_employee_hint(payload["person"])
        if job.job_type == JOB_PARSE_SUPPLY_EMAIL:
            return str(misc.resolve_supply_email_path(context, str(payload["html_path"])))
        if job.job_type == JOB_PHOTO_CAPTURE:
            date = visits.photo_capture_date(payload)
            return f"journal_operational_{date}"
        if job.job_type == JOB_DEEP_ANALYSIS:
            return str(payload["photo_asset_id"])
        if job.job_type == JOB_PROMOTE_PROSPECT:
            return f"prospect_{payload.get('prospect_id', '')}->{payload.get('site_id', '')}"
        if job.job_type == JOB_RETARGET_CAPTURE:
            return f"capture_{payload.get('capture_id', '')}->{payload.get('new_target_type', '')}:{payload.get('new_target_id', '')}"
        if job.job_type == JOB_RECORD_UNKNOWN_CAPTURE:
            return str(unknowns.build_unknown_capture_doc_fields(payload)["_id"])
        if job.job_type == JOB_LOG_SITE_ISSUE:
            return _canonical_location_hint(payload["site_id"])
        if job.job_type == JOB_LOG_SUPPLY_NEED:
            return _canonical_location_hint(payload["site_id"])
        if job.job_type == JOB_CREATE_SUPPLY_REQUEST:
            return f"supply_request_{supply_requests.supply_request_id(payload)}"
        if job.job_type == JOB_LOG_EQUIPMENT_REQUEST:
            return _canonical_location_hint(payload["site_id"])
        if job.job_type == JOB_LOG_PERSONNEL_EVENT:
            return people.personnel_event_doc_id(payload)
        if job.job_type == JOB_LOG_AVAILABILITY_CONSTRAINT:
            return people.availability_constraint_doc_id(payload)
        if job.job_type == JOB_SET_ENTITY_STATUS:
            if payload.get("entity_type") == "site":
                return _canonical_location_hint(payload["entity_id"])
            if payload.get("entity_type") == "employee":
                return _canonical_employee_hint(payload["entity_id"])
        if job.job_type == JOB_UPDATE_SITE_EQUIPMENT:
            return _canonical_location_hint(payload.get("site_id") or payload["site"])
        if job.job_type == JOB_SET_SITE_URL:
            return _canonical_location_hint(payload["site_id"])
        if job.job_type in {JOB_SET_SITE_HOURS, JOB_SET_SITE_OPERATIONAL_CALENDAR}:
            return _canonical_location_hint(payload["site_id"])
        if job.job_type == JOB_SET_CONTACT:
            target = payload.get("target")
            if isinstance(target, dict) and target.get("type") == "site":
                return _canonical_location_hint(target.get("id"))
            if isinstance(target, dict) and target.get("type") == "account":
                return _canonical_account_hint(target.get("id"))
        if job.job_type in {JOB_MARK_SUPPLY_ORDERED, JOB_MARK_SUPPLY_DELIVERED, JOB_MARK_SUPPLY_STOCKED, JOB_MARK_SUPPLY_NO_ACTION_NEEDED}:
            return supplies_equipment_transitions._resolve_supply_doc_id(str(payload["supply_id"]))
        if job.job_type in {JOB_MARK_EQUIPMENT_APPROVED, JOB_MARK_EQUIPMENT_DENIED, JOB_MARK_EQUIPMENT_ORDERED, JOB_MARK_EQUIPMENT_PROVIDED, JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED}:
            return supplies_equipment_transitions._resolve_equipment_doc_id(str(payload["equipment_id"]))
        if job.job_type in {JOB_MARK_ISSUE_MONITORING, JOB_MARK_ISSUE_RESOLVED, JOB_MARK_ISSUE_OPEN}:
            return supplies_equipment_transitions._resolve_issue_doc_id(str(payload["issue_id"]))
        if job.job_type in {JOB_MARK_RECORD_ARCHIVED, JOB_MARK_RECORD_UNARCHIVED}:
            record_type = str(payload.get("record_type") or "")
            record_id = str(payload.get("record_id") or "")
            return record_id if record_id.startswith(f"{record_type}_") else f"{record_type}_{record_id}"
        if job.job_type == JOB_EDIT_RECORD_FIELDS:
            record_type = str(payload.get("record_type") or "")
            record_id = str(payload.get("record_id") or "")
            return record_id if record_id.startswith(f"{record_type}_") else f"{record_type}_{record_id}"
        if job.job_type == JOB_VOICE_MEMO_NOTE:
            targets = misc.voice_memo_note_targets(payload, context)
            return ", ".join(str(path) for path, _allow_create in targets)
    except Exception:
        return "unknown"
    return "unknown"

def process_job(job_path: Path, context: RunContext, processed_dir: Path, failed_dir: Path, processed_job_ids: ProcessedJobIdLookup | None = None) -> None:
    job_path = context.assert_runtime_write_path(job_path, "queue job path")
    processed_dir = context.assert_runtime_write_path(processed_dir, "processed queue directory")
    failed_dir = context.assert_runtime_write_path(failed_dir, "failed queue directory")
    job: QueueJob | None = None
    try:
        job = load_job(job_path)
        _shared.structured_log(context, "queue_job_started", computed_job_id=job.job_id, job_type=job.job_type, source_queue_file=str(job_path), capture_id=_shared.capture_id_for_job(job))
        _shared.structured_log(context, "runtime_process_start", computed_job_id=job.job_id, job_type=job.job_type, source_queue_file=str(job_path), capture_id=_shared.capture_id_for_job(job))
        _shared.write_log_line(context.log_path, f"runtime_process_start job_id={job.job_id} job_type={job.job_type} path={job_path}")
        already_processed, dedupe_source = (processed_job_ids.exists(job.job_id) if processed_job_ids is not None else processed_job_id_exists(context.runtime_root, processed_dir, job.job_id))
        _shared.structured_log(context, "dedupe_decision", computed_job_id=job.job_id, job_type=job.job_type, source=dedupe_source, already_processed=already_processed, capture_id=_shared.capture_id_for_job(job))
        if already_processed:
            if job.job_type == JOB_ADD_PERSON and job.idempotency_key is None:
                _shared.structured_log(context, "dedupe_bypassed", computed_job_id=job.job_id, job_type=job.job_type, reason="add-person-without-idempotency-key", source=dedupe_source, capture_id=_shared.capture_id_for_job(job))
            else:
                if context.dry_run:
                    print(f"Job {job.job_id}: job_id already processed — skipping")
                    _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-already-processed")
                    _shared.structured_log(context, "replay_skip", computed_job_id=job.job_id, job_type=job.job_type, reason="job-id-already-processed", source=dedupe_source, capture_id=_shared.capture_id_for_job(job))
                    return
                print(f"Job {job.job_id}: job_id already processed — skipping")
                moved_path = _archive_job_file(job_path, processed_dir, context, job=job, already_handled=True, handled_source=dedupe_source, processed_job_ids=processed_job_ids)
                print(f"Job {job.job_id}: moved queue file to {moved_path}")
                _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-already-processed")
                append_processed_index_record(job_path, job, context, target_path_hint(job, context), processed_job_ids)
                _shared.structured_log(context, "replay_skip", computed_job_id=job.job_id, job_type=job.job_type, reason="job-id-already-processed", source=dedupe_source, moved_to=str(moved_path), capture_id=_shared.capture_id_for_job(job))
                return
        processed_destination = processed_dir / job_path.name
        if not context.dry_run and processed_destination.exists():
            if _files_have_identical_content(job_path, processed_destination):
                print(f"Job {job.job_id}: identical processed archive already present — skipping")
                moved_path = _archive_job_file(
                    job_path,
                    processed_dir,
                    context,
                    job=job,
                    already_handled=True,
                    handled_source="identical-processed-archive",
                    processed_job_ids=processed_job_ids,
                )
                print(f"Job {job.job_id}: moved queue file to {moved_path}")
                _shared.write_log_line(
                    context.log_path,
                    f"job_id={job.job_id} action=skip status=success reason=identical-processed-archive",
                )
                append_processed_index_record(
                    job_path, job, context, target_path_hint(job, context), processed_job_ids
                )
                _shared.structured_log(
                    context,
                    "replay_skip",
                    computed_job_id=job.job_id,
                    job_type=job.job_type,
                    reason="identical-processed-archive",
                    source="identical-processed-archive",
                    moved_to=str(moved_path),
                    capture_id=_shared.capture_id_for_job(job),
                )
                return
            applied, applied_target = _job_has_applied_marker(job, context)
            if applied:
                print(f"Job {job.job_id}: job_id marker already present — skipping")
                moved_path = _archive_job_file(job_path, processed_dir, context, job=job, already_handled=True, handled_source="applied-marker", processed_job_ids=processed_job_ids)
                print(f"Job {job.job_id}: moved queue file to {moved_path}")
                _shared.write_log_line(context.log_path, f"job_id={job.job_id} action=skip status=success reason=job-id-marker-present")
                append_processed_index_record(job_path, job, context, target_path_hint(job, context), processed_job_ids)
                _shared.structured_log(context, "replay_skip", computed_job_id=job.job_id, job_type=job.job_type, reason="job-id-marker-present", source="applied-marker", moved_to=str(moved_path), target_path=applied_target, capture_id=_shared.capture_id_for_job(job))
                return
            job_path = _revision_queue_job_path(job_path, context, job)
            processed_destination = processed_dir / job_path.name
        handler = JOB_HANDLERS.get(job.job_type)
        if handler is None:
            raise QueueProcessorError(f"Unsupported job type object: {job.job_type}")
        handler(job_path, job, context, processed_dir)
        if processed_destination.exists():
            if processed_job_ids is not None:
                processed_job_ids.mark_processed_archive(job.job_id)
            append_processed_index_record(job_path, job, context, target_path_hint(job, context), processed_job_ids)
            for event in ("queue_job_completed", "runtime_process_success"):
                _shared.structured_log(context, event, computed_job_id=job.job_id, job_type=job.job_type, moved_to=str(processed_destination), target_path=target_path_hint(job, context), capture_id=_shared.capture_id_for_job(job))
            _shared.write_log_line(context.log_path, f"runtime_process_success job_id={job.job_id} job_type={job.job_type} path={job_path}")
        return
    except Exception as exc:
        job_id_for_log = job_path.stem
        job_type_for_log = None
        try:
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("job_id"), str) and payload["job_id"].strip():
                job_id_for_log = payload["job_id"]
            if isinstance(payload, dict) and isinstance(payload.get("job_type"), str):
                job_type_for_log = payload["job_type"]
        except Exception:
            pass
        error_message = str(exc)
        print(f"Job {job_id_for_log}: failed")
        print(f"Job {job_id_for_log}: error {error_message}")
        if context.dry_run:
            print(f"Job {job_id_for_log}: would move queue file to {failed_dir / job_path.name}")
            reason = "invalid-site-id" if isinstance(exc, InvalidSiteIdError) else None
            action = "fail" if reason else "dry-run-reject"
            _shared.write_log_line(context.log_path, f"job_id={job_id_for_log} action={action} status=failure " + (f"reason={reason} " if reason else "") + f"error={error_message}")
            _shared.structured_log(context, "queue_job_failed", job_id=job_id_for_log, job_type=job_type_for_log, reason=reason, error=error_message, dry_run=True)
            return
        try:
            already_handled = False
            handled_source = ""
            if job is not None:
                already_handled, handled_source = (processed_job_ids.exists(job.job_id) if processed_job_ids is not None else processed_job_id_exists(context.runtime_root, processed_dir, job.job_id))
                if not already_handled:
                    already_handled, applied_target = _job_has_applied_marker(job, context)
                    if already_handled:
                        handled_source = f"applied-marker:{applied_target}"
            moved_path = _archive_job_file(job_path, failed_dir, context, job=job, already_handled=already_handled, handled_source=handled_source, processed_job_ids=processed_job_ids)
            print(f"Job {job_id_for_log}: moved queue file to {moved_path}")
        except Exception as move_exc:
            error_message = f"{error_message}; failed to move queue file: {move_exc}"
            print(f"Job {job_id_for_log}: error {error_message}")
        reason = "invalid-site-id" if isinstance(exc, InvalidSiteIdError) else None
        action = "fail" if reason else "reject"
        _shared.write_log_line(context.log_path, f"job_id={job_id_for_log} action={action} status=failure " + (f"reason={reason} " if reason else "") + f"error={error_message}")
        _shared.write_log_line(context.log_path, f"runtime_process_failure job_id={job_id_for_log} job_type={job_type_for_log} path={job_path} error={error_message}")
        _shared.structured_log(context, "queue_job_failed", job_id=job_id_for_log, job_type=job_type_for_log, reason=reason, error=error_message)
        _shared.structured_log(context, "runtime_process_failure", job_id=job_id_for_log, job_type=job_type_for_log, source_queue_file=str(job_path), reason=reason, error=error_message)

def process_all(project_root: Path, runtime_root: Path, dry_run: bool, *, skip_unknowns: bool = False) -> dict[str, object]:
    runtime_root = ensure_local_runtime_root(runtime_root)
    require_directories({"project_root": project_root})
    logs_root = _shared.ensure_within_root(runtime_root / "logs" / "queue_processor", runtime_root, "queue processor logs directory")
    queue_dir = _shared.ensure_within_root(runtime_root / "queue", runtime_root, "queue directory")
    processed_dir = _shared.ensure_within_root(runtime_root / "processed", runtime_root, "processed queue directory")
    failed_dir = _shared.ensure_within_root(runtime_root / "failed", runtime_root, "failed queue directory")
    for directory in (queue_dir, processed_dir, failed_dir, logs_root):
        directory.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"queue-run-{run_timestamp}"
    log_path = logs_root / "queue_processor.log"
    _shared.rotate_log_if_large(log_path)
    structured_log_path = _shared.ensure_within_root(runtime_root / "logs" / "queue_processor_events.jsonl", runtime_root, "queue processor structured log path")
    context = RunContext(project_root=project_root, runtime_root=runtime_root, log_path=log_path, dry_run=dry_run, run_id=run_id, structured_log_path=structured_log_path)
    mode_label = "DRY RUN" if dry_run else "REAL RUN"
    print("BTQ queue processor")
    for label, value in (("Mode", mode_label), ("Project root", project_root), ("Runtime root", runtime_root), ("Queue dir", queue_dir), ("Processed dir", processed_dir), ("Failed dir", failed_dir), ("Log file", log_path), ("Skip unknown reclassification", skip_unknowns)):
        print(f"{label}: {value}")
    for line in (f"mode={mode_label}", f"project_root={project_root}", f"runtime_root={runtime_root}", f"run_id={run_id}", f"skip_unknowns={skip_unknowns}"):
        _shared.write_log_line(log_path, line)
    report: dict[str, object] = {"discovered": 0, "processed": 0, "failed": 0, "skipped": 0, "queue_before": 0, "queue_after": 0, "failed_paths": [], "unknown_reclassification_jobs_created": 0, "unknown_reclassification_skipped": bool(skip_unknowns), "dry_run": bool(dry_run), "runtime_root": str(runtime_root), "queue_dir": str(queue_dir), "processed_dir": str(processed_dir), "failed_dir": str(failed_dir), "log_path": str(log_path)}
    lock = ProcessorLock(runtime_root)
    try:
        lock_event = lock.acquire()
    except ProcessorLockError as exc:
        print(f"Queue processor lock refused: {exc}")
        _shared.write_log_line(log_path, f"lock status=refused error={exc}")
        _shared.structured_log(context, "processor_lock_refused", error=str(exc))
        raise QueueProcessorError(str(exc)) from exc
    try:
        if lock_event == "stale_recovered":
            _shared.write_log_line(log_path, "lock status=stale-recovered")
            _shared.structured_log(context, "processor_lock_stale_recovered", lock_path=str(lock.path))
        _shared.write_log_line(log_path, "lock status=acquired")
        _shared.structured_log(context, "processor_lock_acquired", lock_path=str(lock.path))
        queue_files = sorted(path for path in queue_dir.iterdir() if path.is_file())
        report["discovered"] = len(queue_files)
        report["queue_before"] = len(queue_files)
        print(f"Queue files found: {len(queue_files)}")
        _shared.write_log_line(log_path, f"queue_files_found={len(queue_files)}")
        processed_before = {path.name for path in processed_dir.iterdir() if path.is_file()}
        failed_before = {path.name for path in failed_dir.iterdir() if path.is_file()}
        processed_job_ids = load_processed_job_id_lookup(runtime_root, processed_dir)
        for job_path in queue_files:
            print(f"Processing queue file: {job_path}")
            process_job(job_path, context, processed_dir, failed_dir, processed_job_ids)
        processed_after = {path.name for path in processed_dir.iterdir() if path.is_file()}
        failed_after = {path.name for path in failed_dir.iterdir() if path.is_file()}
        new_processed = sorted(processed_after - processed_before)
        new_failed = sorted(failed_after - failed_before)
        report["processed"] = len(new_processed)
        report["failed"] = len(new_failed)
        report["skipped"] = max(0, len(queue_files) - len(new_processed) - len(new_failed))
        report["queue_after"] = len([path for path in queue_dir.iterdir() if path.is_file()])
        report["failed_paths"] = [str(failed_dir / name) for name in new_failed]
        unknown_jobs_created = 0 if skip_unknowns else unknowns.process_unknowns(context)
        report["unknown_reclassification_jobs_created"] = unknown_jobs_created
        print(f"Unknown reclassification jobs created: {unknown_jobs_created}")
        _shared.write_log_line(log_path, f"unknown_reclassification_jobs_created={unknown_jobs_created}")
    finally:
        lock.release()
        _shared.write_log_line(log_path, "lock status=released")
        _shared.structured_log(context, "processor_lock_released", lock_path=str(lock.path))
    return report

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the BTQ queue processor.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-unknowns", action="store_true")
    args = parser.parse_args()
    project_root = _shared.ensure_within_root(Path(args.project_root), DEFAULT_PROJECT_ROOT, "Project root")
    runtime_root = _shared.ensure_within_any_root(Path(args.runtime_root), [DEFAULT_CONFIG.local_runtime_dir, project_root], "Runtime root")
    runtime_root = ensure_local_runtime_root(runtime_root)
    try:
        process_all(project_root=project_root, runtime_root=runtime_root, dry_run=args.dry_run, skip_unknowns=args.skip_unknowns)
    except QueueProcessorError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
