from __future__ import annotations

from typing import Callable
from pathlib import Path

from queue_spec import (
    JOB_ADD_PERSON, JOB_APPEND_TO_NOTE, JOB_CLOSE_RECRUITING, JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_FLAG_RETENTION_RISK, JOB_LOG_EQUIPMENT_REQUEST, JOB_LOG_PERSONNEL_EVENT, JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED, JOB_MARK_EQUIPMENT_APPROVED, JOB_MARK_EQUIPMENT_DENIED,
    JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED, JOB_MARK_EQUIPMENT_ORDERED, JOB_MARK_EQUIPMENT_PROVIDED,
    JOB_MARK_ISSUE_MONITORING, JOB_MARK_ISSUE_OPEN, JOB_MARK_ISSUE_RESOLVED,
    JOB_MARK_RECORD_ARCHIVED, JOB_MARK_RECORD_UNARCHIVED, JOB_EDIT_RECORD_FIELDS,
    JOB_MARK_SUPPLY_DELIVERED, JOB_MARK_SUPPLY_NO_ACTION_NEEDED, JOB_MARK_SUPPLY_ORDERED,
    JOB_MARK_SUPPLY_STOCKED, JOB_PARSE_SUPPLY_EMAIL, JOB_PERSONAL_JOURNAL_ENTRY, JOB_PHOTO_CAPTURE,
    JOB_PROMOTE_PROSPECT, JOB_RECORD_DAY_RECORD, JOB_RECORD_SHIFT_REPORT, JOB_RECORD_UNKNOWN_CAPTURE, JOB_RECLASSIFY_UNKNOWN, JOB_REMOVE_FROM_SCHEDULE, JOB_RETARGET_CAPTURE,
    JOB_SET_EMPLOYEE_ID, JOB_SET_ENTITY_STATUS, JOB_TRIGGER_RECRUITING, JOB_UPDATE_SITE_EQUIPMENT, JOB_VISIT_CREATE,
    JOB_VOICE_MEMO_NOTE,
)
from queue_processor.handlers import employee_updates, misc, people, site_flags_notes, supplies_equipment, supplies_equipment_transitions, unknowns, visits
from queue_processor.handlers._shared import QueueJob, QueueProcessorError, RunContext

JobHandler = Callable[[Path, QueueJob, RunContext, Path], None]

JOB_HANDLERS: dict[str, JobHandler] = {
    JOB_APPEND_TO_NOTE: site_flags_notes.process_append_to_note_job,
    JOB_ADD_PERSON: people.process_add_person_job,
    JOB_SET_EMPLOYEE_ID: employee_updates.process_set_employee_id_job,
    JOB_RECORD_SHIFT_REPORT: misc.process_record_shift_report_job,
    JOB_RECORD_DAY_RECORD: misc.process_record_day_record_job,
    JOB_PERSONAL_JOURNAL_ENTRY: misc.process_personal_journal_entry_job,
    JOB_FLAG_ACCESS_CONSTRAINT: site_flags_notes.process_flag_access_constraint_job,
    JOB_TRIGGER_RECRUITING: people.process_trigger_recruiting_job,
    JOB_CLOSE_RECRUITING: people.process_close_recruiting_job,
    JOB_PROMOTE_PROSPECT: misc.process_promote_prospect_job,
    JOB_RETARGET_CAPTURE: misc.process_retarget_capture_job,
    JOB_REMOVE_FROM_SCHEDULE: people.process_remove_from_schedule_job,
    JOB_FLAG_RETENTION_RISK: site_flags_notes.process_flag_retention_risk_job,
    JOB_RECORD_UNKNOWN_CAPTURE: unknowns.process_record_unknown_capture_job,
    JOB_RECLASSIFY_UNKNOWN: unknowns.process_reclassify_unknown_job,
    JOB_VISIT_CREATE: visits.process_visit_create_job,
    JOB_PARSE_SUPPLY_EMAIL: misc.process_parse_supply_email_job,
    JOB_PHOTO_CAPTURE: visits.process_photo_capture_job,
    JOB_LOG_SITE_ISSUE: site_flags_notes.process_log_site_issue_job,
    JOB_LOG_SUPPLY_NEED: supplies_equipment.process_log_supply_need_job,
    JOB_LOG_EQUIPMENT_REQUEST: supplies_equipment.process_log_equipment_request_job,
    JOB_LOG_PERSONNEL_EVENT: people.process_log_personnel_event_job,
    JOB_SET_ENTITY_STATUS: people.process_set_entity_status_job,
    JOB_UPDATE_SITE_EQUIPMENT: supplies_equipment.process_update_site_equipment_job,
    JOB_MARK_SUPPLY_ORDERED: supplies_equipment_transitions.process_mark_supply_ordered_job,
    JOB_MARK_SUPPLY_DELIVERED: supplies_equipment_transitions.process_mark_supply_delivered_job,
    JOB_MARK_SUPPLY_STOCKED: supplies_equipment_transitions.process_mark_supply_stocked_job,
    JOB_MARK_SUPPLY_NO_ACTION_NEEDED: supplies_equipment_transitions.process_mark_supply_no_action_needed_job,
    JOB_MARK_EQUIPMENT_APPROVED: supplies_equipment_transitions.process_mark_equipment_approved_job,
    JOB_MARK_EQUIPMENT_DENIED: supplies_equipment_transitions.process_mark_equipment_denied_job,
    JOB_MARK_EQUIPMENT_ORDERED: supplies_equipment_transitions.process_mark_equipment_ordered_job,
    JOB_MARK_EQUIPMENT_PROVIDED: supplies_equipment_transitions.process_mark_equipment_provided_job,
    JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED: supplies_equipment_transitions.process_mark_equipment_no_action_needed_job,
    JOB_MARK_ISSUE_MONITORING: supplies_equipment_transitions.process_mark_issue_monitoring_job,
    JOB_MARK_ISSUE_RESOLVED: supplies_equipment_transitions.process_mark_issue_resolved_job,
    JOB_MARK_ISSUE_OPEN: supplies_equipment_transitions.process_mark_issue_open_job,
    JOB_MARK_RECORD_ARCHIVED: supplies_equipment_transitions.process_mark_record_archived_job,
    JOB_MARK_RECORD_UNARCHIVED: supplies_equipment_transitions.process_mark_record_unarchived_job,
    JOB_EDIT_RECORD_FIELDS: supplies_equipment_transitions.process_edit_record_fields_job,
    JOB_VOICE_MEMO_NOTE: misc.process_voice_memo_note_job,
}

def _handler_for_job_type(job_type: str) -> JobHandler:
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise QueueProcessorError(f"Unsupported job type object: {job_type}")
    return handler
