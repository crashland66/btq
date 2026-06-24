from __future__ import annotations

from datetime import datetime
import os
import re
from zoneinfo import ZoneInfo

from config import get_config


def _vault_path_prefixes() -> tuple[str, ...]:
    """Return the set of vault-relative path prefixes used to recognise that a
    payload string is a vault path.

    Generic defaults ship in git; production supplies its real top-level vault
    folders via BTQ_VAULT_PATH_PREFIXES (comma-separated, e.g.
    "OperationalVault/,Accounts/"). The structural folders (People/, Accounts/,
    Journal/, Outbox/) are always included so the matcher works out of the box.
    """
    structural = ["People/", "Accounts/", "Journal/", "Outbox/"]
    raw = os.environ.get("BTQ_VAULT_PATH_PREFIXES", "OperationalVault/,Accounts/")
    extra = [segment.strip() for segment in raw.split(",") if segment.strip()]
    seen: dict[str, None] = {}
    for prefix in [*structural, *extra]:
        normalized = prefix if prefix.endswith("/") else f"{prefix}/"
        seen.setdefault(normalized, None)
    return tuple(seen)


# AUTHORING CONVENTION: site_id is always a string ("7050", never a bare integer).
# Treat all site and job identifiers as opaque strings at every boundary.
# Do not coerce to int. Do not compare numerically.

JOB_APPEND_TO_NOTE = "append_to_note"
JOB_TRIGGER_RECRUITING = "trigger_recruiting"
JOB_CLOSE_RECRUITING = "close_recruiting"
JOB_REMOVE_FROM_SCHEDULE = "remove_from_schedule"
JOB_FLAG_ACCESS_CONSTRAINT = "flag_access_constraint"
JOB_FLAG_RETENTION_RISK = "flag_retention_risk"
JOB_ADD_PERSON = "add_person"
JOB_ASSIGN_EMPLOYEE_SITE = "assign_employee_site"
JOB_SET_EMPLOYEE_ID = "set_employee_id"
JOB_RECORD_SHIFT_REPORT = "record_shift_report"
JOB_SHIFT_REPORT_NOTE = "shift_report_note"
JOB_RECORD_DAY_RECORD = "record_day_record"
JOB_RECORD_UNKNOWN_CAPTURE = "record_unknown_capture"
JOB_RECLASSIFY_UNKNOWN = "reclassify_unknown"
JOB_VISIT_CREATE = "visit_create"
JOB_PARSE_SUPPLY_EMAIL = "parse_supply_email"
JOB_PERSONAL_JOURNAL_ENTRY = "personal_journal_entry"
JOB_PHOTO_CAPTURE = "photo_capture"
JOB_DEEP_ANALYSIS = "deep_analysis"
JOB_PROMOTE_PROSPECT = "promote_prospect"
JOB_RETARGET_CAPTURE = "retarget_capture"
JOB_LOG_SITE_ISSUE = "log_site_issue"
JOB_LOG_SUPPLY_NEED = "log_supply_need"
JOB_LOG_EQUIPMENT_REQUEST = "log_equipment_request"
JOB_LOG_PERSONNEL_EVENT = "log_personnel_event"
JOB_LOG_AVAILABILITY_CONSTRAINT = "log_availability_constraint"
JOB_SET_ENTITY_STATUS = "set_entity_status"
JOB_UPDATE_SITE_EQUIPMENT = "update_site_equipment"
JOB_MARK_SUPPLY_ORDERED = "mark_supply_ordered"
JOB_MARK_SUPPLY_DELIVERED = "mark_supply_delivered"
JOB_MARK_SUPPLY_STOCKED = "mark_supply_stocked"
JOB_MARK_SUPPLY_NO_ACTION_NEEDED = "mark_supply_no_action_needed"
JOB_MARK_EQUIPMENT_APPROVED = "mark_equipment_approved"
JOB_MARK_EQUIPMENT_DENIED = "mark_equipment_denied"
JOB_MARK_EQUIPMENT_ORDERED = "mark_equipment_ordered"
JOB_MARK_EQUIPMENT_PROVIDED = "mark_equipment_provided"
JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED = "mark_equipment_no_action_needed"
JOB_MARK_ISSUE_MONITORING = "mark_issue_monitoring"
JOB_MARK_ISSUE_RESOLVED = "mark_issue_resolved"
JOB_MARK_ISSUE_OPEN = "mark_issue_open"
JOB_MARK_RECORD_ARCHIVED = "mark_record_archived"
JOB_MARK_RECORD_UNARCHIVED = "mark_record_unarchived"
JOB_EDIT_RECORD_FIELDS = "edit_record_fields"
JOB_VOICE_MEMO_NOTE = "voice_memo_note"
# Reserved for future deterministic person-note route. Not produced by any
# current extraction or routing path. Add back to APPEND_DESTINATIONS only
# when the queue processor handles it.
_RESERVED_EMPLOYEE_NOTE = "employee_note"
EVENT_DEFAULT_TIMEZONE = "America/New_York"

APPEND_DESTINATIONS = {"missed", "journal_unknown", "site_note", "journal"}
SITE_ISSUE_STATUSES = {"open", "monitoring", "resolved"}
SITE_ISSUE_PRIORITIES = {"low", "normal", "high", "urgent"}
SITE_ISSUE_CATEGORIES = {"maintenance", "supply", "access", "staffing", "quality", "safety", "client_request", "other"}
SUPPLY_NEED_STATUSES = {"open", "ordered", "delivered", "stocked", "no_action_needed"}
SUPPLY_NEED_URGENCIES = {"low", "normal", "high", "critical"}
EQUIPMENT_REQUEST_STATUSES = {"open", "approved", "ordered", "provided", "denied", "no_action_needed"}
EQUIPMENT_REQUEST_PRIORITIES = {"low", "normal", "high", "urgent"}
DEEP_ANALYSIS_PRESET_IDS = (
    "condition_detail",
    "damage_hazard",
    "cleanliness_qc",
    "text_ocr",
    "inventory_count",
    "equipment_id",
    "shift_report",
)
PERSONNEL_EVENT_TYPES = {
    "attendance",
    "performance",
    "accommodation",
    "disciplinary",
    "recognition",
    "incident",
    "other",
}
PERSONNEL_EVENT_SEVERITIES = {
    "info",
    "concern",
    "verbal_warning",
    "written_warning",
    "final_warning",
    "separation",
}
PERSONNEL_EVENT_STATUSES = {"open", "monitoring", "resolved"}
AVAILABILITY_CONSTRAINT_TYPES = {"unavailable_date", "last_working_day"}
ENTITY_STATUS_TYPES = {"site", "employee"}
ENTITY_STATUSES = {"active", "inactive"}
SITE_EQUIPMENT_ITEM_STATUSES = {"operational", "non_functional", "untested"}
ARCHIVABLE_RECORD_TYPES = {"site_issue", "supply_need", "equipment_request", "visit"}
EDIT_RECORD_FIELD_ALLOWLISTS = {
    "site_issue": {"site_id", "title", "summary", "priority", "category", "resolution_trigger"},
    "supply_need": {"site_id", "item_name", "quantity_needed", "urgency", "notes"},
    "equipment_request": {"site_id", "equipment_name", "reason", "priority", "notes"},
}
ENTITY_STATUS_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSPECTION_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECRUITING_CLOSE_OUTCOMES = {"filled", "cancelled", "withdrawn", "superseded"}
ALLOWED_JOB_TYPES = {
    JOB_APPEND_TO_NOTE,
    JOB_TRIGGER_RECRUITING,
    JOB_CLOSE_RECRUITING,
    JOB_REMOVE_FROM_SCHEDULE,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_FLAG_RETENTION_RISK,
    JOB_ADD_PERSON,
    JOB_ASSIGN_EMPLOYEE_SITE,
    JOB_SET_EMPLOYEE_ID,
    JOB_RECORD_SHIFT_REPORT,
    JOB_SHIFT_REPORT_NOTE,
    JOB_RECORD_DAY_RECORD,
    JOB_RECORD_UNKNOWN_CAPTURE,
    JOB_RECLASSIFY_UNKNOWN,
    JOB_VISIT_CREATE,
    JOB_PARSE_SUPPLY_EMAIL,
    JOB_PERSONAL_JOURNAL_ENTRY,
    JOB_PHOTO_CAPTURE,
    JOB_DEEP_ANALYSIS,
    JOB_PROMOTE_PROSPECT,
    JOB_RETARGET_CAPTURE,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_PERSONNEL_EVENT,
    JOB_LOG_AVAILABILITY_CONSTRAINT,
    JOB_SET_ENTITY_STATUS,
    JOB_UPDATE_SITE_EQUIPMENT,
    JOB_MARK_SUPPLY_ORDERED,
    JOB_MARK_SUPPLY_DELIVERED,
    JOB_MARK_SUPPLY_STOCKED,
    JOB_MARK_SUPPLY_NO_ACTION_NEEDED,
    JOB_MARK_EQUIPMENT_APPROVED,
    JOB_MARK_EQUIPMENT_DENIED,
    JOB_MARK_EQUIPMENT_ORDERED,
    JOB_MARK_EQUIPMENT_PROVIDED,
    JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED,
    JOB_MARK_ISSUE_MONITORING,
    JOB_MARK_ISSUE_RESOLVED,
    JOB_MARK_ISSUE_OPEN,
    JOB_MARK_RECORD_ARCHIVED,
    JOB_MARK_RECORD_UNARCHIVED,
    JOB_EDIT_RECORD_FIELDS,
    JOB_VOICE_MEMO_NOTE,
}

# Jobs in this set carry a direct site/site_id payload and their handlers
# require the site to be registered before mutating site-scoped state.
SITE_ROUTABILITY_JOB_TYPES = {
    JOB_TRIGGER_RECRUITING,
    JOB_CLOSE_RECRUITING,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_VISIT_CREATE,
    JOB_PROMOTE_PROSPECT,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_UPDATE_SITE_EQUIPMENT,
}


JOB_SCHEMAS = {
    JOB_APPEND_TO_NOTE: ["path", "content", "destination"],
    JOB_TRIGGER_RECRUITING: ["site", "priority", "details"],
    JOB_CLOSE_RECRUITING: ["site", "outcome"],
    JOB_REMOVE_FROM_SCHEDULE: ["employee", "site"],
    JOB_FLAG_ACCESS_CONSTRAINT: ["site", "details"],
    JOB_FLAG_RETENTION_RISK: ["employee", "site", "details"],
    JOB_ADD_PERSON: ["name", "role"],
    JOB_ASSIGN_EMPLOYEE_SITE: ["employee_id", "site_id", "actor"],
    JOB_SET_EMPLOYEE_ID: ["person", "employee_id"],
    JOB_RECORD_SHIFT_REPORT: ["date", "content"],
    JOB_SHIFT_REPORT_NOTE: ["date", "content", "actor", "capture_id", "photo_asset_id"],
    JOB_RECORD_DAY_RECORD: ["date", "content"],
    JOB_RECORD_UNKNOWN_CAPTURE: ["path", "content", "timestamp", "audio_file"],
    JOB_RECLASSIFY_UNKNOWN: ["path"],
    JOB_VISIT_CREATE: ["site", "confidence", "source", "evidence"],
    JOB_PARSE_SUPPLY_EMAIL: ["html_path", "subject", "source_email_date"],
    JOB_PERSONAL_JOURNAL_ENTRY: ["date", "timestamp", "audio_file", "body", "raw_transcript_path"],
    JOB_PHOTO_CAPTURE: ["site", "qc_category", "note", "photos", "captured_at", "exported_at"],
    JOB_DEEP_ANALYSIS: ["capture_id", "photo_asset_id", "actor"],
    JOB_PROMOTE_PROSPECT: ["prospect_id", "site_id", "actor"],
    JOB_RETARGET_CAPTURE: ["capture_id", "new_target_type", "new_target_id", "actor"],
    JOB_LOG_SITE_ISSUE: ["site_id", "title", "reported_by", "client_notified", "resolution_trigger"],
    JOB_LOG_SUPPLY_NEED: ["site_id", "item_name", "requested_by"],
    JOB_LOG_EQUIPMENT_REQUEST: ["site_id", "equipment_name", "requested_by"],
    JOB_LOG_PERSONNEL_EVENT: ["employee", "event_type", "summary", "occurred_at", "reported_by"],
    JOB_LOG_AVAILABILITY_CONSTRAINT: ["employee", "constraint_type", "date", "reported_by", "source_text"],
    JOB_SET_ENTITY_STATUS: ["entity_type", "entity_id", "status", "reason", "source"],
    JOB_UPDATE_SITE_EQUIPMENT: ["equipment", "inspection_date", "inspected_by"],
    JOB_MARK_SUPPLY_ORDERED: ["supply_id", "actor"],
    JOB_MARK_SUPPLY_DELIVERED: ["supply_id", "actor"],
    JOB_MARK_SUPPLY_STOCKED: ["supply_id", "actor"],
    JOB_MARK_SUPPLY_NO_ACTION_NEEDED: ["supply_id", "actor"],
    JOB_MARK_EQUIPMENT_APPROVED: ["equipment_id", "actor"],
    JOB_MARK_EQUIPMENT_DENIED: ["equipment_id", "actor"],
    JOB_MARK_EQUIPMENT_ORDERED: ["equipment_id", "actor"],
    JOB_MARK_EQUIPMENT_PROVIDED: ["equipment_id", "actor"],
    JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED: ["equipment_id", "actor"],
    JOB_MARK_ISSUE_MONITORING: ["issue_id", "actor"],
    JOB_MARK_ISSUE_RESOLVED: ["issue_id", "actor"],
    JOB_MARK_ISSUE_OPEN: ["issue_id", "actor"],
    JOB_MARK_RECORD_ARCHIVED: ["record_type", "record_id", "actor"],
    JOB_MARK_RECORD_UNARCHIVED: ["record_type", "record_id", "actor"],
    JOB_EDIT_RECORD_FIELDS: ["record_type", "record_id", "fields", "actor"],
    JOB_VOICE_MEMO_NOTE: ["capture_id", "timestamp", "audio_file", "raw_transcript_path", "transcript_text"],
}

ALLOWED_TOP_LEVEL_FIELDS = {"job_id", "job_type", "payload", "metadata", "intent", "idempotency_key"}
ADD_PERSON_ALLOWED_PAYLOAD_FIELDS = {
    "name",
    "first",
    "last",
    "employee_id",
    "role",
    "employment_type",
    "status",
    "job",
    "additional_jobs",
    "assignments",
    "contact",
    "metadata",
}
ADD_PERSON_ASSIGNMENT_FIELDS = {"job", "account", "location", "shift"}
ADD_PERSON_CONTACT_FIELDS = {"phone", "email"}
ADD_PERSON_METADATA_FIELDS = {"source"}
ASSIGN_EMPLOYEE_SITE_ALLOWED_PAYLOAD_FIELDS = {
    "employee_id",
    "site_id",
    "actor",
    "action",
    "source",
}
SET_EMPLOYEE_ID_ALLOWED_PAYLOAD_FIELDS = {
    "person",
    "employee_id",
    "employment_type",
    "hire_date",
    "status",
    "source",
    "metadata",
}
RECORD_SHIFT_REPORT_ALLOWED_PAYLOAD_FIELDS = {"date", "content", "prepared_by", "source"}
SHIFT_REPORT_NOTE_ALLOWED_PAYLOAD_FIELDS = {
    "date",
    "content",
    "actor",
    "capture_id",
    "photo_asset_id",
    "site_id",
    "prompt_id",
    "prompt_label",
}
RECORD_DAY_RECORD_ALLOWED_PAYLOAD_FIELDS = {"date", "content", "source"}
LOG_SITE_ISSUE_ALLOWED_PAYLOAD_FIELDS = {
    "site_id",
    "title",
    "summary",
    "observations",
    "category",
    "priority",
    "status",
    "observed_at",
    "reported_by",
    "source",
    "notes",
    "client_notified",
    "client_informed",
    "client_informed_at",
    "client_informed_by",
    "client_informed_method",
    "client_informed_note",
    "client_notified_at",
    "client_notified_by",
    "client_notified_method",
    "client_notified_note",
    "related_capture_ids",
    "related_candidate_ids",
    "related_media",
    "source_artifacts",
    "resolution_trigger",
    "resolved_at",
    "resolution_summary",
    "issue_id",
}
LOG_SUPPLY_NEED_ALLOWED_PAYLOAD_FIELDS = {
    "site_id",
    "item_name",
    "quantity_needed",
    "urgency",
    "requested_by",
    "observed_at",
    "source",
    "notes",
    "related_capture_ids",
    "related_candidate_ids",
    "related_media",
    "source_artifacts",
    "status",
    "supply_id",
    "ordered_at",
    "ordered_by",
    "ordered_note",
    "delivered_at",
    "delivered_by",
    "delivered_note",
    "stocked_at",
    "stocked_by",
    "stocked_note",
}
LOG_EQUIPMENT_REQUEST_ALLOWED_PAYLOAD_FIELDS = {
    "site_id",
    "equipment_name",
    "reason",
    "priority",
    "requested_by",
    "observed_at",
    "source",
    "notes",
    "related_capture_ids",
    "related_candidate_ids",
    "related_media",
    "source_artifacts",
    "status",
    "equipment_id",
    "approved_at",
    "approved_by",
    "approval_note",
    "denied_at",
    "denied_by",
    "denial_note",
    "ordered_at",
    "ordered_by",
    "ordered_note",
    "provided_at",
    "provided_by",
    "provided_note",
}
LOG_PERSONNEL_EVENT_ALLOWED_PAYLOAD_FIELDS = {
    "employee",
    "event_type",
    "summary",
    "occurred_at",
    "reported_by",
    "severity",
    "status",
    "related_site",
    "source",
    "notes",
    "client_notified",
    "client_notified_at",
    "client_notified_by",
    "client_notified_method",
    "client_notified_note",
    "resolution_trigger",
    "resolved_at",
    "resolution_summary",
    "related_capture_ids",
    "related_candidate_ids",
    "related_media",
    "source_artifacts",
    "event_id",
}
LOG_AVAILABILITY_CONSTRAINT_ALLOWED_PAYLOAD_FIELDS = {
    "employee",
    "constraint_type",
    "date",
    "reported_by",
    "source_text",
    "related_site",
    "reported_at",
    "event_id",
}
SET_ENTITY_STATUS_ALLOWED_PAYLOAD_FIELDS = {
    "entity_type",
    "entity_id",
    "status",
    "reason",
    "source",
    "observed_at",
    "details",
}
UPDATE_SITE_EQUIPMENT_ALLOWED_PAYLOAD_FIELDS = {
    "site",
    "site_id",
    "equipment",
    "inspection_date",
    "inspected_by",
    "section_notes",
}
MARK_SUPPLY_ALLOWED_PAYLOAD_FIELDS = {
    "supply_id",
    "actor",
    "note",
    "occurred_at",
}
MARK_EQUIPMENT_ALLOWED_PAYLOAD_FIELDS = {
    "equipment_id",
    "actor",
    "note",
    "occurred_at",
}
MARK_ISSUE_ALLOWED_PAYLOAD_FIELDS = {
    "issue_id",
    "actor",
    "note",
    "occurred_at",
}
MARK_RECORD_ALLOWED_PAYLOAD_FIELDS = {
    "record_type",
    "record_id",
    "actor",
    "note",
}
EDIT_RECORD_ALLOWED_PAYLOAD_FIELDS = {
    "record_type",
    "record_id",
    "fields",
    "actor",
}
PATH_FIELD_TOKENS = {"path", "file", "directory", "dir", "folder"}
VAULT_PATH_PREFIXES = _vault_path_prefixes()

# Job types whose payload["path"] is a vault-relative path joined with
# context.vault_root at execution time. External producers (remote
# Claude/Codex prompts, cowork-authored JSON dropped into the outbox) have at
# times emitted these with a leading "<vault_name>/" prefix, which doubles up
# when joined with the vault root and produces a path that doesn't exist.
# normalize_vault_relative_path() is applied at load time for these types.
VAULT_RELATIVE_PATH_JOB_TYPES = frozenset({"append_to_note", "record_unknown_capture", "reclassify_unknown"})


def normalize_vault_relative_path(path: object) -> str:
    """Return a vault-relative path with leading slashes and vault-name
    segments stripped.

    Remote payload producers have at times emitted paths like
    "OperationalVault/Accounts/Hillcrest/.../about.md" when they should emit
    "Accounts/Hillcrest/.../about.md" — the vault root directory is already at
    .../OperationalVault, so the doubled prefix produces
    .../OperationalVault/OperationalVault/Accounts/... which
    doesn't exist. Strip the vault-name prefix here so downstream consumers
    always see a clean vault-relative path. The vault name is read from
    get_config() rather than hardcoded so a vault rename doesn't silently
    re-break paths. Config is imported lazily so this module stays
    importable without config side effects.
    """
    if not isinstance(path, str):
        return ""
    cleaned = path.strip()
    while cleaned.startswith(("./", "/")):
        cleaned = cleaned[2:] if cleaned.startswith("./") else cleaned[1:]
    try:
        vault_name = get_config().vault_dir.name
    except Exception:
        vault_name = ""
    if vault_name:
        prefix = f"{vault_name}/"
        while cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_valid_employee_id(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, int):
        return value >= 0
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and stripped.isdigit()


def _is_valid_idempotency_key(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(stripped) > 160:
        return False
    return all(character.isalnum() or character in {"-", "_", ".", ":"} for character in stripped)


def _is_timezone_aware_iso_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def normalize_occurred_at(value: object, *, default_timezone: str = EVENT_DEFAULT_TIMEZONE) -> str | None:
    """Coerce a capture/event timestamp into a valid visit_create occurred_at."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(default_timezone))

    normalized = parsed.isoformat()
    if not _is_timezone_aware_iso_datetime(normalized):
        return None
    return normalized


def _looks_like_payload_path(key: str, value: object) -> bool:
    normalized_key = key.strip().lower()
    if normalized_key in PATH_FIELD_TOKENS or normalized_key.endswith("_path") or normalized_key.endswith("path"):
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if any(stripped.startswith(prefix) for prefix in VAULT_PATH_PREFIXES):
        return True
    return stripped.endswith(".md") or "/People/" in stripped or "/Accounts/" in stripped or "/Journal/" in stripped


def _contains_payload_path(value: object, parent_key: str = "") -> bool:
    if isinstance(value, dict):
        return any(_looks_like_payload_path(str(key), nested) or _contains_payload_path(nested, str(key)) for key, nested in value.items())
    if isinstance(value, list):
        return any(_contains_payload_path(item, parent_key) for item in value)
    return _looks_like_payload_path(parent_key, value)


def _is_onboarding_append(payload: dict) -> bool:
    content = payload.get("content")
    if not isinstance(content, str):
        return False
    destination = payload.get("destination")
    if destination == "journal_unknown" and "type: unknown_capture" in content:
        return False
    if destination == "site_note" and "type: site_audio_memo" in content:
        return False
    normalized = " ".join(content.lower().split())
    # Strong markers indicate onboarding even on their own — these phrases
    # are almost never used outside a hiring context.
    strong_onboarding_markers = (
        "new hire",
        "hired as",
        "has been hired",
        "new employee",
        "onboard",              # also covers onboarded / onboarding
        "new hire orientation",
        "employee orientation",
    )
    if any(marker in normalized for marker in strong_onboarding_markers):
        return True
    # Weak markers (eHub, employee id, base pay) appear routinely in HR
    # onboarding documents. "start date" is intentionally excluded — it appears
    # in legitimate operational entries recording placement outcomes (interview →
    # hired, planned start date noted) and firing on it caused false positives.
    # The eHub/employee-id signals are sufficient to catch real onboarding docs.
    weak_onboarding_markers = (
        "employee id",
        "ehub",
        "base pay",
    )
    hiring_context_markers = (
        "hire",          # also matches hired/hiring/hires
        "joining",
        "first day",
        "background check",
        "new staff",
        "added to roster",
        "added to the roster",
    )
    if (
        any(marker in normalized for marker in weak_onboarding_markers)
        and any(marker in normalized for marker in hiring_context_markers)
    ):
        return True
    # Explicit person-creation phrasing mis-filed as a journal append, e.g.
    # "add a new cleaner", "create an employee record". The creation verb and
    # the person noun must be adjacent — co-occurrence anywhere in a long entry
    # produced false positives ("create a public posting" sharing a note with
    # an unrelated "cleaner backfill").
    return bool(
        re.search(
            r"\b(?:add|create|register)\s+(?:a |an |the |new )*(?:employee|person|cleaner|worker)\b",
            normalized,
        )
    )


def _validate_add_person_payload(payload: dict) -> bool:
    payload_keys = set(payload)
    if payload_keys - ADD_PERSON_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("name")) or not _is_non_empty_string(payload.get("role")):
        return False
    if not _is_valid_employee_id(payload.get("employee_id")):
        return False
    if _contains_payload_path(payload):
        return False

    for field in ("first", "last", "employment_type", "status"):
        value = payload.get(field)
        if value is not None and not _is_non_empty_string(value):
            return False

    job = payload.get("job")
    if job is not None and not _is_non_empty_string(job):
        return False

    additional_jobs = payload.get("additional_jobs")
    if additional_jobs is not None:
        if not isinstance(additional_jobs, list):
            return False
        for value in additional_jobs:
            if not _is_non_empty_string(value):
                return False

    assignments = payload.get("assignments")
    if assignments is not None:
        if not isinstance(assignments, list):
            return False
        for assignment in assignments:
            if not isinstance(assignment, dict):
                return False
            if set(assignment) - ADD_PERSON_ASSIGNMENT_FIELDS:
                return False
            for value in assignment.values():
                if value is not None and not _is_non_empty_string(value):
                    return False

    contact = payload.get("contact")
    if contact is not None:
        if not isinstance(contact, dict) or set(contact) - ADD_PERSON_CONTACT_FIELDS:
            return False
        for value in contact.values():
            if value is not None and not isinstance(value, str):
                return False

    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - ADD_PERSON_METADATA_FIELDS:
            return False
        source = metadata.get("source")
        if source is not None and not _is_non_empty_string(source):
            return False

    return True


def _validate_assign_employee_site_payload(payload: dict) -> bool:
    if set(payload) - ASSIGN_EMPLOYEE_SITE_ALLOWED_PAYLOAD_FIELDS:
        return False
    for field in ("employee_id", "site_id", "actor"):
        if not _is_non_empty_string(payload.get(field)):
            return False
    action = payload.get("action")
    if action is not None and action not in {"assign", "unassign"}:
        return False
    source = payload.get("source")
    if source is not None and not _is_non_empty_string(source):
        return False
    return True


def _validate_set_employee_id_payload(payload: dict) -> bool:
    if set(payload) - SET_EMPLOYEE_ID_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("person")):
        return False
    employee_id = payload.get("employee_id")
    if employee_id is None:
        return False
    if isinstance(employee_id, str) and not employee_id.strip():
        return False
    if not _is_valid_employee_id(employee_id):
        return False
    for field in ("employment_type", "hire_date", "status", "source"):
        value = payload.get(field)
        if value is not None and not _is_non_empty_string(value):
            return False
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - ADD_PERSON_METADATA_FIELDS:
            return False
        source = metadata.get("source")
        if source is not None and not _is_non_empty_string(source):
            return False
    return True


def _validate_record_shift_report_payload(payload: dict) -> bool:
    if set(payload) - RECORD_SHIFT_REPORT_ALLOWED_PAYLOAD_FIELDS:
        return False
    date = payload.get("date")
    if not _is_non_empty_string(date) or INSPECTION_DATE_RE.match(str(date).strip()) is None:
        return False
    if not _is_non_empty_string(payload.get("content")):
        return False
    return True


def _validate_shift_report_note_payload(payload: dict) -> bool:
    if set(payload) - SHIFT_REPORT_NOTE_ALLOWED_PAYLOAD_FIELDS:
        return False
    date = payload.get("date")
    if not _is_non_empty_string(date) or INSPECTION_DATE_RE.match(str(date).strip()) is None:
        return False
    for field in ("content", "actor", "capture_id", "photo_asset_id"):
        if not _is_non_empty_string(payload.get(field)):
            return False
    for field in ("site_id", "prompt_id", "prompt_label"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True


def _validate_record_day_record_payload(payload: dict) -> bool:
    if set(payload) - RECORD_DAY_RECORD_ALLOWED_PAYLOAD_FIELDS:
        return False
    date = payload.get("date")
    if not _is_non_empty_string(date) or INSPECTION_DATE_RE.match(str(date).strip()) is None:
        return False
    if not _is_non_empty_string(payload.get("content")):
        return False
    return True


def _is_string_list(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in value)


def _is_non_empty_string_list(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, list):
        return False
    return all(_is_non_empty_string(item) for item in value)


def _validate_log_site_issue_payload(payload: dict) -> bool:
    if set(payload) - LOG_SITE_ISSUE_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("site_id")) or not _is_non_empty_string(payload.get("title")):
        return False
    if not _is_non_empty_string(payload.get("reported_by")):
        return False
    if not _is_non_empty_string(payload.get("resolution_trigger")):
        return False
    if not isinstance(payload.get("client_notified"), bool):
        return False
    summary = payload.get("summary")
    observations = payload.get("observations")
    if not _is_non_empty_string(summary) and not (isinstance(observations, list) and any(_is_non_empty_string(item) for item in observations)):
        return False
    status = payload.get("status", "open")
    if status is not None and status not in SITE_ISSUE_STATUSES:
        return False
    priority = payload.get("priority", "normal")
    if priority is not None and priority not in SITE_ISSUE_PRIORITIES:
        return False
    category = payload.get("category", "other")
    if category is not None and category not in SITE_ISSUE_CATEGORIES:
        return False
    if payload.get("client_informed") is not None and not isinstance(payload.get("client_informed"), bool):
        return False
    for field in ("observed_at", "source", "notes", "client_informed_at", "client_informed_by", "client_informed_method", "client_informed_note", "client_notified_at", "client_notified_by", "client_notified_method", "client_notified_note", "resolved_at", "resolution_summary", "issue_id"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return all(_is_string_list(payload.get(field)) for field in ("observations", "related_capture_ids", "related_candidate_ids", "related_media", "source_artifacts"))


def _validate_log_supply_need_payload(payload: dict) -> bool:
    if set(payload) - LOG_SUPPLY_NEED_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("site_id")):
        return False
    if not _is_non_empty_string(payload.get("item_name")):
        return False
    if not _is_non_empty_string(payload.get("requested_by")):
        return False
    urgency = payload.get("urgency", "normal")
    if urgency is not None and urgency not in SUPPLY_NEED_URGENCIES:
        return False
    status = payload.get("status", "open")
    if status is not None and status not in SUPPLY_NEED_STATUSES:
        return False
    for field in (
        "quantity_needed",
        "observed_at",
        "source",
        "notes",
        "supply_id",
        "ordered_at",
        "ordered_by",
        "ordered_note",
        "delivered_at",
        "delivered_by",
        "delivered_note",
        "stocked_at",
        "stocked_by",
        "stocked_note",
    ):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return all(_is_non_empty_string_list(payload.get(field)) for field in ("related_capture_ids", "related_candidate_ids", "related_media", "source_artifacts"))


def _validate_log_equipment_request_payload(payload: dict) -> bool:
    if set(payload) - LOG_EQUIPMENT_REQUEST_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("site_id")):
        return False
    if not _is_non_empty_string(payload.get("equipment_name")):
        return False
    if not _is_non_empty_string(payload.get("requested_by")):
        return False
    priority = payload.get("priority", "normal")
    if priority is not None and priority not in EQUIPMENT_REQUEST_PRIORITIES:
        return False
    status = payload.get("status", "open")
    if status is not None and status not in EQUIPMENT_REQUEST_STATUSES:
        return False
    for field in (
        "reason",
        "observed_at",
        "source",
        "notes",
        "equipment_id",
        "approved_at",
        "approved_by",
        "approval_note",
        "denied_at",
        "denied_by",
        "denial_note",
        "ordered_at",
        "ordered_by",
        "ordered_note",
        "provided_at",
        "provided_by",
        "provided_note",
    ):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return all(_is_non_empty_string_list(payload.get(field)) for field in ("related_capture_ids", "related_candidate_ids", "related_media", "source_artifacts"))


def _validate_log_personnel_event_payload(payload: dict) -> bool:
    if set(payload) - LOG_PERSONNEL_EVENT_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("employee")):
        return False
    if not _is_non_empty_string(payload.get("summary")):
        return False
    if not _is_non_empty_string(payload.get("reported_by")):
        return False
    occurred_at = payload.get("occurred_at")
    if not isinstance(occurred_at, str) or not occurred_at.strip():
        return False
    event_type = payload.get("event_type")
    if event_type not in PERSONNEL_EVENT_TYPES:
        return False
    severity = payload.get("severity")
    if severity is not None and severity not in PERSONNEL_EVENT_SEVERITIES:
        return False
    status = payload.get("status", "open")
    if status is not None and status not in PERSONNEL_EVENT_STATUSES:
        return False
    if payload.get("client_notified") is not None and not isinstance(payload.get("client_notified"), bool):
        return False
    for field in (
        "related_site",
        "source",
        "notes",
        "client_notified_at",
        "client_notified_by",
        "client_notified_method",
        "client_notified_note",
        "resolution_trigger",
        "resolved_at",
        "resolution_summary",
        "event_id",
    ):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return all(_is_non_empty_string_list(payload.get(field)) for field in ("related_capture_ids", "related_candidate_ids", "related_media", "source_artifacts"))


def _validate_log_availability_constraint_payload(payload: dict) -> bool:
    if set(payload) - LOG_AVAILABILITY_CONSTRAINT_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("employee")):
        return False
    if payload.get("constraint_type") not in AVAILABILITY_CONSTRAINT_TYPES:
        return False
    date = payload.get("date")
    if not isinstance(date, str) or ENTITY_STATUS_DATE_RE.match(date) is None:
        return False
    if not _is_non_empty_string(payload.get("reported_by")):
        return False
    if not _is_non_empty_string(payload.get("source_text")):
        return False
    for field in ("related_site", "reported_at", "event_id"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            return False
    return True


def _validate_update_site_equipment_payload(payload: dict) -> bool:
    if set(payload) - UPDATE_SITE_EQUIPMENT_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("site")) and not _is_non_empty_string(payload.get("site_id")):
        return False
    inspection_date = payload.get("inspection_date")
    if not isinstance(inspection_date, str) or INSPECTION_DATE_RE.match(inspection_date) is None:
        return False
    if not _is_non_empty_string(payload.get("inspected_by")):
        return False
    equipment = payload.get("equipment")
    if not isinstance(equipment, list) or not equipment:
        return False
    allowed_item_fields = {"description", "brand", "color", "status", "notes"}
    for item in equipment:
        if not isinstance(item, dict):
            return False
        if set(item) - allowed_item_fields:
            return False
        for field in ("description", "brand", "color"):
            if not _is_non_empty_string(item.get(field)):
                return False
        if item.get("status") not in SITE_EQUIPMENT_ITEM_STATUSES:
            return False
        if "notes" in item and not _is_non_empty_string(item.get("notes")):
            return False
    if "section_notes" in payload and not _is_non_empty_string(payload.get("section_notes")):
        return False
    return True


def _validate_set_entity_status_payload(payload: dict) -> bool:
    if set(payload) - SET_ENTITY_STATUS_ALLOWED_PAYLOAD_FIELDS:
        return False
    if payload.get("entity_type") not in ENTITY_STATUS_TYPES:
        return False
    if payload.get("status") not in ENTITY_STATUSES:
        return False
    for field in ("entity_id", "reason", "source"):
        if not _is_non_empty_string(payload.get(field)):
            return False
    observed_at = payload.get("observed_at")
    if observed_at is not None and (not isinstance(observed_at, str) or ENTITY_STATUS_DATE_RE.match(observed_at) is None):
        return False
    details = payload.get("details")
    if details is not None and not isinstance(details, str):
        return False
    return True


def _validate_mark_supply_payload(payload: dict) -> bool:
    if set(payload) - MARK_SUPPLY_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("supply_id")):
        return False
    if not _is_non_empty_string(payload.get("actor")):
        return False
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False
    occurred_at = payload.get("occurred_at")
    if occurred_at is not None and not isinstance(occurred_at, str):
        return False
    return True


def _validate_mark_equipment_payload(payload: dict) -> bool:
    if set(payload) - MARK_EQUIPMENT_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("equipment_id")):
        return False
    if not _is_non_empty_string(payload.get("actor")):
        return False
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False
    occurred_at = payload.get("occurred_at")
    if occurred_at is not None and not isinstance(occurred_at, str):
        return False
    return True


def _validate_mark_issue_payload(payload: dict) -> bool:
    if set(payload) - MARK_ISSUE_ALLOWED_PAYLOAD_FIELDS:
        return False
    if not _is_non_empty_string(payload.get("issue_id")):
        return False
    if not _is_non_empty_string(payload.get("actor")):
        return False
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False
    occurred_at = payload.get("occurred_at")
    if occurred_at is not None and not isinstance(occurred_at, str):
        return False
    return True


def _validate_mark_record_payload(payload: dict) -> bool:
    if set(payload) - MARK_RECORD_ALLOWED_PAYLOAD_FIELDS:
        return False
    if payload.get("record_type") not in ARCHIVABLE_RECORD_TYPES:
        return False
    if not _is_non_empty_string(payload.get("record_id")):
        return False
    if not _is_non_empty_string(payload.get("actor")):
        return False
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        return False
    return True


def _validate_edit_record_fields_payload(payload: dict) -> bool:
    if set(payload) - EDIT_RECORD_ALLOWED_PAYLOAD_FIELDS:
        return False
    record_type = payload.get("record_type")
    if record_type not in EDIT_RECORD_FIELD_ALLOWLISTS:
        return False
    if not _is_non_empty_string(payload.get("record_id")):
        return False
    if not _is_non_empty_string(payload.get("actor")):
        return False
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        return False
    if set(fields) - EDIT_RECORD_FIELD_ALLOWLISTS[str(record_type)]:
        return False
    for value in fields.values():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            return False
    return True


def _validate_deep_analysis_payload(payload: dict) -> bool:
    for field in ("capture_id", "photo_asset_id", "actor"):
        if not _is_non_empty_string(payload.get(field)):
            return False

    preset_id = payload.get("preset_id")
    custom_prompt = payload.get("custom_prompt")
    has_preset = _is_non_empty_string(preset_id)
    has_custom = _is_non_empty_string(custom_prompt)
    if has_preset == has_custom:
        return False
    if has_preset and str(preset_id).strip() not in DEEP_ANALYSIS_PRESET_IDS:
        return False
    if preset_id is not None and not isinstance(preset_id, str):
        return False
    if custom_prompt is not None and not isinstance(custom_prompt, str):
        return False
    return True


def validate_job(job: dict) -> bool:
    if not isinstance(job, dict):
        return False
    if set(job) - ALLOWED_TOP_LEVEL_FIELDS:
        return False

    job_type = job.get("job_type")
    payload = job.get("payload")
    job_id = job.get("job_id")
    idempotency_key = job.get("idempotency_key")
    if job_id is not None and not _is_non_empty_string(job_id):
        return False
    if not _is_valid_idempotency_key(idempotency_key):
        return False
    if not isinstance(job_type, str) or job_type not in ALLOWED_JOB_TYPES:
        return False
    if not isinstance(payload, dict):
        return False

    required_fields = JOB_SCHEMAS[job_type]
    if not all(field in payload for field in required_fields):
        return False

    if job_type == JOB_APPEND_TO_NOTE:
        destination = payload.get("destination")
        if destination not in APPEND_DESTINATIONS:
            return False
        if _is_onboarding_append(payload):
            return False
    if job_type == JOB_CLOSE_RECRUITING:
        if payload.get("outcome") not in RECRUITING_CLOSE_OUTCOMES:
            return False
        if payload.get("outcome") == "filled" and not _is_non_empty_string(payload.get("filled_by")):
            return False
        date = payload.get("date")
        if date is not None and (not isinstance(date, str) or INSPECTION_DATE_RE.match(date) is None):
            return False
        for field in ("notes", "recruiting_trigger_id"):
            value = payload.get(field)
            if value is not None and not isinstance(value, str):
                return False
    if job_type == JOB_ADD_PERSON:
        if not _validate_add_person_payload(payload):
            return False
    if job_type == JOB_ASSIGN_EMPLOYEE_SITE:
        if not _validate_assign_employee_site_payload(payload):
            return False
    if job_type == JOB_SET_EMPLOYEE_ID:
        if not _validate_set_employee_id_payload(payload):
            return False
    if job_type == JOB_RECORD_SHIFT_REPORT:
        if not _validate_record_shift_report_payload(payload):
            return False
    if job_type == JOB_SHIFT_REPORT_NOTE:
        if not _validate_shift_report_note_payload(payload):
            return False
    if job_type == JOB_RECORD_DAY_RECORD:
        if not _validate_record_day_record_payload(payload):
            return False
    if job_type == JOB_VISIT_CREATE:
        confidence = payload.get("confidence")
        if confidence not in {"high", "medium"}:
            return False
        for field in ("site", "source", "evidence"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
        visit_type = payload.get("visit_type")
        if visit_type is not None and (not isinstance(visit_type, str) or not visit_type.strip()):
            return False
        visited_by = payload.get("visited_by")
        if visited_by is not None and (not isinstance(visited_by, str) or not visited_by.strip()):
            return False
        occurred_at = payload.get("occurred_at")
        if occurred_at is not None and not _is_timezone_aware_iso_datetime(occurred_at):
            return False
    if job_type == JOB_PARSE_SUPPLY_EMAIL:
        for field in ("html_path", "subject", "source_email_date"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
    if job_type == JOB_PERSONAL_JOURNAL_ENTRY:
        for field in ("date", "timestamp", "audio_file", "body", "raw_transcript_path"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
    if job_type == JOB_PHOTO_CAPTURE:
        for field in ("site", "qc_category", "note", "captured_at", "exported_at"):
            value = payload.get(field)
            if not isinstance(value, str):
                return False
        if not payload["site"].strip() or not payload["qc_category"].strip():
            return False
        photos = payload.get("photos")
        if not isinstance(photos, list):
            return False
        if "audio" in payload:
            audio = payload.get("audio")
            if not isinstance(audio, list) or len(audio) > 1:
                return False
        else:
            audio = []
        note = payload.get("note")
        if not photos and not audio and (not isinstance(note, str) or not note.strip()):
            return False
        for photo in photos:
            if not isinstance(photo, dict):
                return False
            for field in ("filename", "mime_type"):
                value = photo.get(field)
                if not isinstance(value, str) or not value.strip():
                    return False
            data_url = photo.get("data_url")
            stored_path = photo.get("stored_path")
            if not _is_non_empty_string(data_url) and not _is_non_empty_string(stored_path):
                return False
        for audio_record in audio:
            if not isinstance(audio_record, dict):
                return False
            for field in ("filename", "mime_type"):
                value = audio_record.get(field)
                if not isinstance(value, str) or not value.strip():
                    return False
            data_url = audio_record.get("data_url")
            stored_path = audio_record.get("stored_path")
            if not _is_non_empty_string(data_url) and not _is_non_empty_string(stored_path):
                return False
    if job_type == JOB_DEEP_ANALYSIS:
        if not _validate_deep_analysis_payload(payload):
            return False
    if job_type == JOB_LOG_SITE_ISSUE:
        if not _validate_log_site_issue_payload(payload):
            return False
    if job_type == JOB_LOG_SUPPLY_NEED:
        if not _validate_log_supply_need_payload(payload):
            return False
    if job_type == JOB_LOG_EQUIPMENT_REQUEST:
        if not _validate_log_equipment_request_payload(payload):
            return False
    if job_type == JOB_LOG_PERSONNEL_EVENT:
        if not _validate_log_personnel_event_payload(payload):
            return False
    if job_type == JOB_LOG_AVAILABILITY_CONSTRAINT:
        if not _validate_log_availability_constraint_payload(payload):
            return False
    if job_type == JOB_SET_ENTITY_STATUS:
        if not _validate_set_entity_status_payload(payload):
            return False
    if job_type == JOB_UPDATE_SITE_EQUIPMENT:
        if not _validate_update_site_equipment_payload(payload):
            return False
    if job_type in {
        JOB_MARK_SUPPLY_ORDERED,
        JOB_MARK_SUPPLY_DELIVERED,
        JOB_MARK_SUPPLY_STOCKED,
        JOB_MARK_SUPPLY_NO_ACTION_NEEDED,
    }:
        if not _validate_mark_supply_payload(payload):
            return False
    if job_type in {
        JOB_MARK_EQUIPMENT_APPROVED,
        JOB_MARK_EQUIPMENT_DENIED,
        JOB_MARK_EQUIPMENT_ORDERED,
        JOB_MARK_EQUIPMENT_PROVIDED,
        JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED,
    }:
        if not _validate_mark_equipment_payload(payload):
            return False
    if job_type in {
        JOB_MARK_ISSUE_MONITORING,
        JOB_MARK_ISSUE_RESOLVED,
        JOB_MARK_ISSUE_OPEN,
    }:
        if not _validate_mark_issue_payload(payload):
            return False
    if job_type in {JOB_MARK_RECORD_ARCHIVED, JOB_MARK_RECORD_UNARCHIVED}:
        if not _validate_mark_record_payload(payload):
            return False
    if job_type == JOB_EDIT_RECORD_FIELDS:
        if not _validate_edit_record_fields_payload(payload):
            return False
    if job_type == JOB_VOICE_MEMO_NOTE:
        for field in ("capture_id", "timestamp", "audio_file", "raw_transcript_path", "transcript_text"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return False
        for field in ("routing_flag", "site_id", "site", "note", "geolocation"):
            value = payload.get(field)
            if value is not None and not isinstance(value, (str, dict)):
                return False
        employees = payload.get("employees")
        if employees is not None and not isinstance(employees, list):
            return False

    return True
