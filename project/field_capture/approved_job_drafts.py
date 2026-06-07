from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from btq_vault.projector import ProjectorError, query_view
from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.site_registry_data import load_brand_keywords
from field_capture import client_notifications
from field_capture.action_candidates import normalize_vault_relative_path
from event_pipeline.sites import SITES
from processing_core.action_candidates import STATUS_APPROVED
from processing_core.approved_job_drafts import (
    DRAFT_STATUS_APPROVED,
    DRAFT_STATUS_FAILED,
    approved_job_draft_path,
    approved_job_draft_payload,
    write_approved_job_draft,
)
from processing_core.artifacts import read_json_object, resolve_within_root
from processing_core.draft_staging import queue_job_from_draft, staging_result_path
from processing_core.review_status import processed_index_records, queue_job_records
from processing_core.results import result_counts
from queue_processor.idempotency import compute_job_id
from queue_spec import (
    JOB_APPEND_TO_NOTE,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    JOB_SET_ENTITY_STATUS,
    JOB_VISIT_CREATE,
    validate_job,
)


DRAFT_PLAN_WOULD_CREATE = "would_create"
DRAFT_PLAN_SKIPPED = "skipped"
DRAFT_PLAN_FAILED = "failed"
DRAFT_LIST_STATUSES = ("approved_draft", "failed_draft")
FIELD_CAPTURE_CANDIDATE_TYPE = "field_capture_follow_up"
GENERIC_CANDIDATE_LABELS = {
    "capture after photo once corrected.",
    "review the field audio note and decide whether follow-up is needed.",
    "review supply/order follow-up.",
}
GENERIC_ISSUE_LABELS = {
    "client maintenance issue",
    "maintenance issue",
    "site maintenance issue",
}
MAINTENANCE_TERMS = (
    "backed up",
    "baseboard separation",
    "broken",
    "damage",
    "damaged",
    "drain",
    "hole",
    "inoperable",
    "leak",
    "maintenance",
    "repair",
    "stall",
    "water on floor",
)
MAINTENANCE_CONTEXT_TERMS = (
    "follow-up needed",
    "maintenance follow-up",
    "need fixed",
    "needs fixed",
    "needs maintenance",
    "needs repair",
)
CLEANING_ONLY_TERMS = (
    "dust",
    "dusty",
    "power washed",
    "scrubbed",
    "sweep",
    "stain",
    "mop floor",
    "trash",
    "wipe",
)
SUPPLY_NEED_ACTION_PATTERN = re.compile(
    r"\b(?:need|needs|out of|running low on|running out of|low on|empty|no more|we(?:'re| are) out|ran out of|stock|restock|order)\b",
    re.IGNORECASE,
)
SUPPLY_ITEM_TERMS = (
    "bathroom cleaner",
    "cleaner",
    "dispenser refill",
    "dispenser refills",
    "hand soap",
    "mop head",
    "mop heads",
    "paper towel",
    "paper towels",
    "sanitizer",
    "soap",
    "toilet paper",
    "trash bag",
    "trash bags",
    "trash liner",
    "trash liners",
    "wipes",
) + tuple(load_brand_keywords("supply"))
SUPPLY_ITEM_EXTRACT_PATTERN = re.compile(
    r"(?:we(?:'re| are)?\s+)?(?:need|needs|out of|running low on|running out of|low on|empty|no more|ran out of|stock|restock|order)\s+(?P<item>.+)",
    re.IGNORECASE,
)
EQUIPMENT_REQUEST_ACTION_PATTERN = re.compile(
    r"\b(?:need|needs|broken|doesn't work|does not work|won't start|will not start|not working|missing|replace|replacement|we don't have|we do not have|would help if we had|could use a)\b",
    re.IGNORECASE,
)
EQUIPMENT_ITEM_TERMS = (
    "broom",
    "cleaning cart",
    "dispenser housing",
    "dustpan",
    "extension pole",
    "floor machine",
    "ladder",
    "mop bucket",
    "scrubber",
    "sign holder",
    "trash can",
    "vacuum",
) + tuple(load_brand_keywords("equipment"))
EQUIPMENT_ITEM_EXTRACT_PATTERN = re.compile(
    r"(?:we(?:'re| are| do(?:n't| not))?\s+)?(?:need|needs|broken|doesn't work|does not work|won't start|will not start|not working|missing|replace|replacement|would help if we had|could use a)\s+(?P<item>.+)",
    re.IGNORECASE,
)


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_candidate_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "reviews" / "action_candidates" / "field_capture"


def default_draft_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "reviews" / "approved_job_drafts" / "field_capture"


def iter_candidate_artifacts(candidate_dir: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    root = candidate_dir.expanduser().resolve(strict=False)
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def iter_draft_artifacts(draft_dir: Path) -> Iterable[tuple[Path, dict[str, object]]]:
    root = draft_dir.expanduser().resolve(strict=False)
    for path in sorted(root.glob("*.json")):
        resolved = path.resolve(strict=False)
        try:
            resolve_within_root(resolved, root)
        except ValueError:
            continue
        payload = read_json_object(resolved)
        if payload is not None:
            yield resolved, payload


def normalized_text(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def proposed_queue_job(candidate: dict[str, object], *, runtime_root: Path | None = None) -> tuple[object, object, str]:
    approval_metadata = candidate.get("approval_metadata")
    if isinstance(approval_metadata, dict):
        proposed = approval_metadata.get("proposed_queue_job")
        if isinstance(proposed, dict):
            return proposed.get("job_type"), proposed.get("payload"), ""

    channel_metadata = candidate.get("channel_metadata")
    if isinstance(channel_metadata, dict):
        status_job = voice_memo_status_candidate_job(candidate, channel_metadata)
        if status_job is not None:
            return status_job
        # Re-normalize here so old candidate artifacts (written before
        # normalize_vault_relative_path landed in semantic_channel_metadata)
        # don't emit jobs with a doubled vault-name prefix that the queue processor
        # cannot resolve.
        note_path = normalize_vault_relative_path(channel_metadata.get("proposed_note_path"))
        if note_path:
            return (
                JOB_APPEND_TO_NOTE,
                {
                    "path": note_path,
                    "destination": "site_note",
                    "content": draft_note_content(candidate),
                },
                "",
            )

    if candidate.get("candidate_type") == FIELD_CAPTURE_CANDIDATE_TYPE:
        job_type, payload, error = default_field_capture_job(candidate, runtime_root=runtime_root)
        return job_type, payload, error

    return "", {}, ""


def first_non_empty_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def voice_memo_employee_status_entity_id(channel_metadata: dict) -> str:
    target = first_non_empty_text(channel_metadata.get("target_id"))
    employee_names = channel_metadata.get("employee_names")
    employee_slugs = channel_metadata.get("employee_slugs")
    names = [str(value or "").strip() for value in employee_names] if isinstance(employee_names, list) else []
    slugs = [str(value or "").strip() for value in employee_slugs] if isinstance(employee_slugs, list) else []
    names = [value for value in names if value]
    slugs = [value for value in slugs if value]

    if target:
        if target in names:
            return target
        if target in slugs:
            index = slugs.index(target)
            if index < len(names):
                return names[index]
            return ""
        if len(names) <= 1:
            return first_non_empty_text(names[0] if names else "", target)
        return ""

    if len(names) == 1:
        return names[0]
    return ""


def voice_memo_status_candidate_job(candidate: dict[str, object], channel_metadata: dict) -> tuple[str, dict[str, object], str] | None:
    if candidate.get("candidate_type") != "voice_memo_operator_action":
        return None
    if channel_metadata.get("intent") not in {"site_status_change", "employee_status_change"}:
        return None
    if channel_metadata.get("action") != "set_inactive":
        return None

    intent = str(channel_metadata.get("intent") or "")
    reason = first_non_empty_text(candidate.get("summary"), candidate.get("rationale"), candidate.get("source_text"))
    source_text = str(candidate.get("source_text") or "").strip()
    payload: dict[str, object] = {
        "entity_type": "site" if intent == "site_status_change" else "employee",
        "entity_id": "",
        "status": "inactive",
        "reason": reason,
        "source": "voice_memo",
    }
    if source_text and source_text != reason:
        payload["details"] = source_text

    if intent == "site_status_change":
        target = first_non_empty_text(channel_metadata.get("target_id"), channel_metadata.get("site_id"))
        if not target:
            return "", {}, "voice_memo site status candidate missing target site_id"
        payload["entity_id"] = target
    else:
        target = voice_memo_employee_status_entity_id(channel_metadata)
        if not target:
            return "", {}, "voice_memo employee status candidate missing target employee"
        payload["entity_id"] = target

    if not validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}):
        return "", {}, "generated voice_memo set_entity_status payload failed queue_spec validation"
    return JOB_SET_ENTITY_STATUS, payload, ""


def site_inactive_employee_payload(candidate: dict[str, object], site_id: str, employee_name: str) -> dict[str, object]:
    reason = first_non_empty_text(candidate.get("summary"), candidate.get("rationale"), candidate.get("source_text"))
    source_text = str(candidate.get("source_text") or "").strip()
    employee_reason = f"Assigned site {site_id} is being set inactive."
    if reason:
        employee_reason = f"{employee_reason} {reason}"
    payload: dict[str, object] = {
        "entity_type": "employee",
        "entity_id": employee_name,
        "status": "inactive",
        "reason": employee_reason,
        "source": "voice_memo",
    }
    if source_text and source_text != reason:
        payload["details"] = source_text
    return payload


def validate_site_inactive_employee_job(payload: dict[str, object], employee_name: str) -> tuple[str, dict[str, object], str]:
    if not validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}):
        return "", {}, f"generated employee inactive cascade failed queue_spec validation for: {employee_name}"
    return JOB_SET_ENTITY_STATUS, payload, ""


def should_cascade_site_inactive_to_employee_doc(doc: dict[str, object]) -> bool:
    if doc.get("type") != "employee":
        return False
    if str(doc.get("status") or "").strip().lower() == "inactive":
        return False
    role = " ".join(str(doc.get("role") or "").strip().lower().split())
    if role in {"area manager", "operator", "owner", "admin", "administrator"}:
        return False
    return True


def couchdb_site_inactive_employee_status_jobs(
    candidate: dict[str, object],
    site_id: str,
) -> list[tuple[str, dict[str, object], str]] | None:
    if not (
        os.environ.get("BTQ_COUCHDB_URL")
        or os.environ.get("BTQ_COUCHDB_USER")
        or os.environ.get("BTQ_COUCHDB_PASSWORD")
    ):
        return None
    try:
        config = couchdb_config.from_env()
        rows = query_view(
            config.base_url,
            dict(config.auth_header()),
            couchdb_config.vault_database(),
            "btq_vault",
            "employees_by_site",
            startkey=[site_id],
            endkey=[site_id, {}],
            include_docs=True,
            timeout=config.timeout,
        )
    except (couchdb_config.CouchDBConfigError, ProjectorError):
        return None

    jobs: list[tuple[str, dict[str, object], str]] = []
    seen: set[str] = set()
    for row in rows:
        doc = row.get("doc") if isinstance(row.get("doc"), dict) else {}
        if not should_cascade_site_inactive_to_employee_doc(doc):
            continue
        employee_name = first_non_empty_text(doc.get("name"), doc.get("employee_id"), doc.get("_id"))
        if not employee_name or employee_name in seen:
            continue
        seen.add(employee_name)
        payload = site_inactive_employee_payload(candidate, site_id, employee_name)
        jobs.append(validate_site_inactive_employee_job(payload, employee_name))
    return jobs


def site_inactive_employee_status_jobs(
    candidate: dict[str, object],
    site_id: str,
) -> list[tuple[str, dict[str, object], str]]:
    site_id = str(site_id or "").strip()
    if not site_id:
        return []
    couchdb_jobs = couchdb_site_inactive_employee_status_jobs(candidate, site_id)
    if couchdb_jobs is not None:
        return couchdb_jobs
    return []


def voice_memo_status_candidate_jobs(candidate: dict[str, object], channel_metadata: dict) -> list[tuple[str, dict[str, object], str]]:
    if candidate.get("candidate_type") != "voice_memo_operator_action":
        return []
    if channel_metadata.get("intent") == "site_status_change" and channel_metadata.get("action") == "set_inactive":
        single = voice_memo_status_candidate_job(candidate, channel_metadata)
        if single is None:
            return []
        job_type, payload, error = single
        if error or job_type != JOB_SET_ENTITY_STATUS or not isinstance(payload, dict):
            return [single]
        site_id = str(payload.get("entity_id") or "")
        return [single, *site_inactive_employee_status_jobs(candidate, site_id)]
    if channel_metadata.get("intent") != "employee_status_change" or channel_metadata.get("action") != "set_inactive":
        single = voice_memo_status_candidate_job(candidate, channel_metadata)
        return [] if single is None else [single]

    employee_names = channel_metadata.get("employee_names")
    names = [str(value or "").strip() for value in employee_names] if isinstance(employee_names, list) else []
    names = [value for value in names if value]
    if len(names) <= 1:
        single = voice_memo_status_candidate_job(candidate, channel_metadata)
        return [] if single is None else [single]

    reason = first_non_empty_text(candidate.get("summary"), candidate.get("rationale"), candidate.get("source_text"))
    source_text = str(candidate.get("source_text") or "").strip()
    jobs: list[tuple[str, dict[str, object], str]] = []
    for name in names:
        payload: dict[str, object] = {
            "entity_type": "employee",
            "entity_id": name,
            "status": "inactive",
            "reason": reason,
            "source": "voice_memo",
        }
        if source_text and source_text != reason:
            payload["details"] = source_text
        if not validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}):
            jobs.append(("", {}, f"generated voice_memo set_entity_status payload failed queue_spec validation for employee: {name}"))
        else:
            jobs.append((JOB_SET_ENTITY_STATUS, payload, ""))
    return jobs


def proposed_queue_jobs(candidate: dict[str, object], *, runtime_root: Path | None = None) -> list[tuple[object, object, str]]:
    approval_metadata = candidate.get("approval_metadata")
    if isinstance(approval_metadata, dict):
        proposed = approval_metadata.get("proposed_queue_job")
        if isinstance(proposed, dict):
            return [(proposed.get("job_type"), proposed.get("payload"), "")]

    channel_metadata = candidate.get("channel_metadata")
    if isinstance(channel_metadata, dict):
        status_jobs = voice_memo_status_candidate_jobs(candidate, channel_metadata)
        if status_jobs:
            return status_jobs

    job_type, payload, error = proposed_queue_job(candidate, runtime_root=runtime_root)
    if job_type or payload or error:
        return [(job_type, payload, error)]
    return []


def default_field_capture_job(candidate: dict[str, object], *, runtime_root: Path | None = None) -> tuple[str, dict[str, object], str]:
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    if channel_metadata.get("visit_proposed"):
        visit_payload = default_field_capture_visit_payload(candidate)
        if visit_payload is not None:
            if not validate_job({"job_type": JOB_VISIT_CREATE, "payload": visit_payload}):
                return "", {}, "generated field_capture visit_create payload failed queue_spec validation"
            return JOB_VISIT_CREATE, visit_payload, ""
    issue_payload = default_field_capture_issue_payload(candidate, runtime_root=runtime_root)
    if issue_payload is not None:
        if not validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": issue_payload}):
            return "", {}, "generated field_capture log_site_issue payload failed queue_spec validation"
        return JOB_LOG_SITE_ISSUE, issue_payload, ""
    access_payload = default_field_capture_access_payload(candidate)
    if access_payload is not None:
        if not validate_job({"job_type": JOB_FLAG_ACCESS_CONSTRAINT, "payload": access_payload}):
            return "", {}, "generated field_capture flag_access_constraint payload failed queue_spec validation"
        return JOB_FLAG_ACCESS_CONSTRAINT, access_payload, ""
    supply_payload = default_field_capture_supply_payload(candidate, runtime_root=runtime_root)
    if supply_payload is not None:
        if not validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": supply_payload}):
            return "", {}, "generated field_capture log_supply_need payload failed queue_spec validation"
        return JOB_LOG_SUPPLY_NEED, supply_payload, ""
    equipment_payload = default_field_capture_equipment_payload(candidate, runtime_root=runtime_root)
    if equipment_payload is not None:
        if not validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": equipment_payload}):
            return "", {}, "generated field_capture log_equipment_request payload failed queue_spec validation"
        return JOB_LOG_EQUIPMENT_REQUEST, equipment_payload, ""
    return default_field_capture_append_job(candidate)


def default_field_capture_visit_payload(candidate: dict[str, object]) -> dict[str, object] | None:
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    site_id = str(channel_metadata.get("site_id") or "").strip()
    if not site_id:
        return None
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    audio_asset_id = str(provenance.get("audio_asset_id") or "").strip()
    evidence = str(candidate.get("rationale") or "").strip()
    if not evidence:
        evidence = str(candidate.get("summary") or "").strip()
    visit_type = str(channel_metadata.get("visit_type") or "").strip()
    visited_by = str(channel_metadata.get("submitter_person_id") or "").strip()
    payload = {
        "site": site_id,
        "confidence": "medium",
        "source": audio_asset_id or "field_capture_audio",
        "evidence": evidence or "Field audio completion note.",
    }
    if visit_type:
        payload["visit_type"] = visit_type
    if visited_by:
        payload["visited_by"] = visited_by
    return payload


def default_field_capture_append_job(candidate: dict[str, object]) -> tuple[str, dict[str, object], str]:
    site_id = resolve_candidate_site_id(candidate)
    if not site_id:
        return "", {}, "field_capture site_id is required for default append-to-site-note draft"
    note_path = site_note_path_for_site_id(site_id)
    if not note_path:
        return "", {}, f"could not resolve site note path for field_capture site_id: {site_id}"
    payload = {
        "path": note_path,
        "destination": "site_note",
        "content": draft_note_content(candidate),
    }
    if not validate_job({"job_type": JOB_APPEND_TO_NOTE, "payload": payload}):
        return "", {}, "generated field_capture append_to_note payload failed queue_spec validation"
    return JOB_APPEND_TO_NOTE, payload, ""


def default_field_capture_issue_payload(candidate: dict[str, object], *, runtime_root: Path | None = None) -> dict[str, object] | None:
    if not is_maintenance_issue_candidate(candidate):
        return None
    site_id = resolve_candidate_site_id(candidate)
    if not site_id:
        return None
    summary = issue_summary(candidate)
    observations = issue_observations(candidate, summary)
    if not summary and not observations:
        return None
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    capture_id = str(channel_metadata.get("upload_id") or "").strip()
    notification = load_client_notification(runtime_root, candidate_id)
    client_notified = bool(notification and notification.get("client_informed"))
    payload: dict[str, object] = {
        "site_id": site_id,
        "title": issue_title(candidate, summary, observations),
        "summary": summary,
        "observations": observations,
        "category": "maintenance",
        "priority": issue_priority(candidate),
        "status": "open",
        "observed_at": str(channel_metadata.get("captured_at") or candidate.get("created_at") or "").strip(),
        "reported_by": reported_by(candidate, runtime_root=runtime_root),
        "source": "field_capture",
        "client_notified": client_notified,
        "resolution_trigger": resolution_trigger(candidate, summary, observations),
        "related_capture_ids": [capture_id] if capture_id else [],
        "related_candidate_ids": [candidate_id] if candidate_id else [],
        "source_artifacts": [
            value
            for value in (
                str(provenance.get("semantic_artifact_path") or "").strip(),
                str(provenance.get("source_transcript_path") or "").strip(),
            )
            if value
        ],
    }
    if notification:
        payload.update(client_notification_payload_fields(notification))
    return payload


def default_field_capture_access_payload(candidate: dict[str, object]) -> dict[str, object] | None:
    if not is_access_constraint_candidate(candidate):
        return None
    site_id = resolve_candidate_site_id(candidate)
    if not site_id:
        return None
    details = access_constraint_details(candidate)
    if not details:
        return None
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    payload: dict[str, object] = {
        "site": site_id,
        "details": details,
    }
    observed_at = str(channel_metadata.get("captured_at") or candidate.get("created_at") or "").strip()
    if len(observed_at) >= 10:
        payload["date"] = observed_at[:10]
    return payload


def default_field_capture_supply_payload(candidate: dict[str, object], *, runtime_root: Path | None = None) -> dict[str, object] | None:
    if not is_supply_need_candidate(candidate):
        return None
    site_id = resolve_candidate_site_id(candidate)
    if not site_id:
        return None
    item_name = supply_item_name(candidate)
    if not item_name:
        return None
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    capture_id = str(channel_metadata.get("upload_id") or "").strip()
    notes = supply_notes(candidate, item_name)
    payload: dict[str, object] = {
        "site_id": site_id,
        "item_name": item_name,
        "urgency": supply_urgency(candidate),
        "requested_by": reported_by(candidate, runtime_root=runtime_root),
        "observed_at": str(channel_metadata.get("captured_at") or candidate.get("created_at") or datetime.now(timezone.utc).isoformat()).strip(),
        "source": str(candidate.get("source_context") or "").strip() or "field_capture",
        "related_capture_ids": [capture_id] if capture_id else [],
        "related_candidate_ids": [candidate_id] if candidate_id else [],
        "source_artifacts": [
            value
            for value in (
                str(provenance.get("semantic_artifact_path") or "").strip(),
                str(provenance.get("source_transcript_path") or "").strip(),
            )
            if value
        ],
    }
    if notes:
        payload["notes"] = notes
    return payload


def default_field_capture_equipment_payload(candidate: dict[str, object], *, runtime_root: Path | None = None) -> dict[str, object] | None:
    if not is_equipment_request_candidate(candidate):
        return None
    site_id = resolve_candidate_site_id(candidate)
    if not site_id:
        return None
    equipment_name = equipment_item_name(candidate)
    if not equipment_name:
        return None
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    capture_id = str(channel_metadata.get("upload_id") or "").strip()
    notes = equipment_notes(candidate, equipment_name)
    payload: dict[str, object] = {
        "site_id": site_id,
        "equipment_name": equipment_name,
        "reason": equipment_reason(candidate, equipment_name),
        "priority": equipment_priority(candidate),
        "requested_by": reported_by(candidate, runtime_root=runtime_root),
        "observed_at": str(channel_metadata.get("captured_at") or candidate.get("created_at") or datetime.now(timezone.utc).isoformat()).strip(),
        "source": "field_capture",
        "related_capture_ids": [capture_id] if capture_id else [],
        "related_candidate_ids": [candidate_id] if candidate_id else [],
        "source_artifacts": [
            value
            for value in (
                str(provenance.get("semantic_artifact_path") or "").strip(),
                str(provenance.get("source_transcript_path") or "").strip(),
            )
            if value
        ],
    }
    if notes:
        payload["notes"] = notes
    return payload


def candidate_review_text(candidate: dict[str, object]) -> str:
    values = [
        candidate.get("review_rationale"),
        candidate.get("summary"),
        candidate.get("source_context"),
        candidate.get("source_text"),
        candidate.get("rationale"),
    ]
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    values.extend([channel_metadata.get("area"), channel_metadata.get("phase"), channel_metadata.get("issue_type")])
    return " ".join(str(value or "") for value in values)


def candidate_supply_text(candidate: dict[str, object]) -> str:
    return " ".join(str(candidate.get(key) or "") for key in ("summary", "rationale", "source_text"))


def candidate_equipment_text(candidate: dict[str, object]) -> str:
    return " ".join(str(candidate.get(key) or "") for key in ("summary", "rationale", "source_text", "source_context"))


def is_maintenance_issue_candidate(candidate: dict[str, object]) -> bool:
    text = normalized_text(candidate_review_text(candidate))
    if not text:
        return False
    if equipment_text_has_non_site_tool(text):
        return False
    has_maintenance = any(term in text for term in MAINTENANCE_TERMS)
    if not has_maintenance:
        return False
    has_structural_or_water = any(term in text for term in ("backed up", "baseboard separation", "broken", "damage", "damaged", "drain", "hole", "inoperable", "leak", "stall", "water on floor"))
    has_context = any(term in text for term in MAINTENANCE_CONTEXT_TERMS)
    cleaning_only = any(term in text for term in CLEANING_ONLY_TERMS)
    if not has_structural_or_water and not has_context:
        return False
    return not cleaning_only or has_structural_or_water


def is_supply_need_candidate(candidate: dict[str, object]) -> bool:
    if is_maintenance_issue_candidate(candidate):
        return False
    text = normalized_text(candidate_supply_text(candidate))
    if not text:
        return False
    has_action = bool(SUPPLY_NEED_ACTION_PATTERN.search(text))
    has_item = any(term in text for term in SUPPLY_ITEM_TERMS)
    return has_action and has_item


def is_access_constraint_candidate(candidate: dict[str, object]) -> bool:
    if is_maintenance_issue_candidate(candidate):
        return False
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    if normalized_text(channel_metadata.get("issue_type")) == "access":
        return True
    text = normalized_text(candidate_equipment_text(candidate))
    if not text:
        return False
    return any(term in text for term in ("access", "alarm code", "badge", "door code", "key", "lockbox"))


def is_equipment_request_candidate(candidate: dict[str, object]) -> bool:
    if is_maintenance_issue_candidate(candidate) or is_supply_need_candidate(candidate):
        return False
    text = normalized_text(candidate_equipment_text(candidate))
    if not text:
        return False
    has_action = bool(EQUIPMENT_REQUEST_ACTION_PATTERN.search(text))
    has_item = any(term in text for term in EQUIPMENT_ITEM_TERMS)
    return has_action and has_item


def equipment_text_has_non_site_tool(text: str) -> bool:
    if not any(term in text for term in EQUIPMENT_ITEM_TERMS):
        return False
    site_fixture_terms = ("dispenser", "trash can", "sign holder")
    if any(term in text for term in site_fixture_terms):
        return False
    return True


def access_constraint_details(candidate: dict[str, object]) -> str:
    return concise_text(
        first_non_empty_text(
            candidate.get("source_text"),
            candidate.get("source_context"),
            candidate.get("review_rationale"),
            candidate.get("rationale"),
            candidate.get("summary"),
        ),
        limit=500,
    )


def supply_item_name(candidate: dict[str, object]) -> str:
    for value in (
        candidate.get("summary"),
        candidate.get("source_text"),
        candidate.get("rationale"),
    ):
        text = " ".join(str(value or "").split())
        if not text:
            continue
        match = SUPPLY_ITEM_EXTRACT_PATTERN.search(text)
        if match:
            item = cleanup_supply_item_name(match.group("item"))
            if item:
                return item
    for term in SUPPLY_ITEM_TERMS:
        if term in normalized_text(candidate_supply_text(candidate)):
            return term
    return concise_text(candidate.get("summary") or candidate_supply_text(candidate), limit=80)


def cleanup_supply_item_name(value: str) -> str:
    text = re.split(r"\b(?:at|for|here|please|asap|today|now|urgent|critical)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"^[\s:,-]+|[\s.,;:!-]+$", "", text)
    text = re.sub(r"\b(?:more|some|the|a|an)\b", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    return concise_text(text, limit=80)


def supply_notes(candidate: dict[str, object], item_name: str) -> str:
    parts = []
    for value in (candidate.get("summary"), candidate.get("rationale")):
        text = " ".join(str(value or "").split())
        if text and normalized_text(text) != normalized_text(item_name) and text not in parts:
            parts.append(text)
    return concise_text(" ".join(parts), limit=500)


def supply_urgency(candidate: dict[str, object]) -> str:
    text = normalized_text(candidate_supply_text(candidate))
    if any(term in text for term in ("critical", "emergency", "immediately")):
        return "critical"
    if any(term in text for term in ("asap", "now", "today", "urgent")):
        return "high"
    if "low on" in text or "running low on" in text:
        return "low"
    return "normal"


def equipment_item_name(candidate: dict[str, object]) -> str:
    for value in (
        candidate.get("summary"),
        candidate.get("source_text"),
        candidate.get("rationale"),
        candidate.get("source_context"),
    ):
        text = " ".join(str(value or "").split())
        if not text:
            continue
        match = EQUIPMENT_ITEM_EXTRACT_PATTERN.search(text)
        if match:
            item = cleanup_equipment_item_name(match.group("item"))
            if item:
                return item
    for term in EQUIPMENT_ITEM_TERMS:
        if term in normalized_text(candidate_equipment_text(candidate)):
            return term
    return concise_text(candidate.get("summary") or candidate_equipment_text(candidate), limit=80)


def cleanup_equipment_item_name(value: str) -> str:
    text = re.split(r"\b(?:at|for|here|please|asap|today|now|urgent|critical|because|since)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"^[\s:,-]+|[\s.,;:!-]+$", "", text)
    text = re.sub(r"\b(?:more|some|the|a|an|new|replacement)\b", "", text, flags=re.IGNORECASE)
    text = " ".join(text.split())
    return concise_text(text, limit=80)


def equipment_notes(candidate: dict[str, object], equipment_name: str) -> str:
    parts = []
    for value in (candidate.get("summary"), candidate.get("rationale")):
        text = " ".join(str(value or "").split())
        if text and normalized_text(text) != normalized_text(equipment_name) and text not in parts:
            parts.append(text)
    return concise_text(" ".join(parts), limit=500)


def equipment_reason(candidate: dict[str, object], equipment_name: str) -> str:
    reason = equipment_notes(candidate, equipment_name)
    return reason or concise_text(candidate_equipment_text(candidate), limit=500)


def equipment_priority(candidate: dict[str, object]) -> str:
    text = normalized_text(candidate_equipment_text(candidate))
    if any(term in text for term in ("critical", "emergency", "immediately", "asap", "urgent")):
        return "urgent"
    if any(term in text for term in ("now", "today")):
        return "high"
    if any(term in text for term in ("can wait", "this quarter", "low priority")):
        return "low"
    return "normal"


def issue_summary(candidate: dict[str, object]) -> str:
    candidate_summary = str(candidate.get("summary") or "").strip()
    review_rationale = str(candidate.get("review_rationale") or "").strip()
    source_context = str(candidate.get("source_context") or "").strip()
    source_text = str(candidate.get("source_text") or "").strip()
    rationale = str(candidate.get("rationale") or "").strip()
    operational_review = operational_summary_from_review_rationale(review_rationale)
    for value in (
        "" if normalized_text(operational_review) in GENERIC_ISSUE_LABELS else operational_review,
        source_context,
        source_text,
        candidate_summary if not is_generic_candidate_label(candidate_summary) else "",
        rationale,
    ):
        cleaned = concise_text(value, limit=280)
        if cleaned:
            return cleaned
    return ""


def concise_text(value: object, *, limit: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def issue_observations(candidate: dict[str, object], summary: str) -> list[str]:
    text = " ".join(
        str(value or "")
        for value in (
            candidate.get("review_rationale"),
            candidate.get("source_text"),
            candidate.get("source_context"),
            candidate.get("rationale"),
            candidate.get("summary"),
        )
    )
    normalized = normalized_text(text)
    observations: list[str] = []
    phrase_map = (
        ("drain backed up", "Drain backed up."),
        ("drain is backed up", "Drain backed up."),
        ("sink drain pushed water onto floor", "Sink drain pushed water onto the floor."),
        ("sink drain pushed water onto the floor", "Sink drain pushed water onto the floor."),
        ("sink drain backing up onto floor", "Sink drain backed up onto the floor."),
        ("sink drain backed up onto the floor", "Sink drain backed up onto the floor."),
        ("sink must be backed up", "Sink drain appears backed up."),
        ("coming out on the floor", "Water is coming out onto the floor."),
        ("water on floor", "Water was observed on the floor."),
        ("missing mop", "Mop is missing."),
        ("mop's missing", "Mop is missing."),
        ("metal stall inoperable", "Metal stall is inoperable."),
        ("metal stall is inoperable", "Metal stall is inoperable."),
        ("stall inoperable", "Stall is inoperable."),
        ("dusty wire drawing area", "Wire drawing area is dusty."),
        ("baseboard separation", "Baseboard separation was observed."),
        ("leak", "Leak or moisture source requires follow-up."),
        ("broken", "Broken item requires repair follow-up."),
        ("damage", "Damage requires repair follow-up."),
        ("hole", "Hole requires repair follow-up."),
    )
    for phrase, observation in phrase_map:
        if phrase in normalized and observation not in observations:
            observations.append(observation)
    if "dusty" in normalized and "wire drawing" in normalized and "Wire drawing area is dusty." not in observations:
        observations.append("Wire drawing area is dusty.")
    if not observations and summary:
        observations.append(summary)
    return observations[:8]


def issue_title(candidate: dict[str, object], summary: str, observations: list[str]) -> str:
    text = normalized_text(" ".join([summary, *observations, candidate_review_text(candidate)]))
    if "drain" in text and "stall" in text:
        return "Restroom drain backup and inoperable stall"
    if "drain" in text:
        return "Restroom drain backup"
    if "leak" in text or "water on floor" in text:
        return "Restroom water or leak follow-up"
    if "baseboard separation" in text:
        return "Baseboard separation repair follow-up"
    if "broken" in text or "inoperable" in text or "damage" in text:
        return "Site maintenance repair follow-up"
    return concise_text(summary or "Site maintenance issue", limit=80)


def issue_priority(candidate: dict[str, object]) -> str:
    text = normalized_text(candidate_review_text(candidate))
    if any(term in text for term in ("water on floor", "backed up", "leak", "safety", "urgent")):
        return "high"
    return "normal"


def resolution_trigger(candidate: dict[str, object], summary: str, observations: list[str]) -> str:
    text = normalized_text(" ".join([summary, *observations, candidate_review_text(candidate)]))
    if "drain" in text and "stall" in text:
        return "Maintenance confirms the drain is clear and the stall is operable."
    if "drain" in text:
        return "Maintenance confirms the drain is clear."
    if "leak" in text or "water on floor" in text:
        return "Maintenance confirms the leak or moisture source is corrected and the floor is dry."
    return "Maintenance confirms the issue is corrected."


def reported_by(candidate: dict[str, object], *, runtime_root: Path | None = None) -> str:
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    for key in ("person_name", "submitter_name", "reported_by", "person_id", "submitter_id"):
        value = str(channel_metadata.get(key) or "").strip()
        if value:
            return value
    capture_id = str(channel_metadata.get("upload_id") or "").strip()
    capture = load_intake_capture(runtime_root, capture_id)
    if capture:
        metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
        payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else {}
        for source in (metadata, payload):
            for key in ("person_name", "submitter_name", "reported_by", "person_id", "submitter_id"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
    return "Unknown field reporter"


def load_intake_capture(runtime_root: Path | None, capture_id: str) -> dict[str, object] | None:
    if runtime_root is None or not capture_id:
        return None
    intake_dir = runtime_root / "field_capture" / "intake"
    if not intake_dir.exists():
        return None
    for path in sorted(intake_dir.glob("**/*.json")):
        payload = read_json_object(path)
        if not payload or payload.get("job_type") != "photo_capture":
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        found = str(metadata.get("capture_id") or body.get("capture_id") or "").strip()
        if found == capture_id:
            return payload
    return None


def load_client_notification(runtime_root: Path | None, candidate_id: str) -> dict[str, object] | None:
    if runtime_root is None or not candidate_id:
        return None
    return client_notifications.load_notification(client_notifications.default_notification_dir(runtime_root), candidate_id)


def client_notification_payload_fields(notification: dict[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {}
    mapping = {
        "client_informed_at": "client_notified_at",
        "client_informed_by": "client_notified_by",
        "client_informed_method": "client_notified_method",
        "client_informed_note": "client_notified_note",
    }
    for source, target in mapping.items():
        value = str(notification.get(source) or "").strip()
        if value:
            fields[target] = value
    return fields


def resolve_candidate_site_id(candidate: dict[str, object]) -> str:
    channel_metadata = candidate.get("channel_metadata")
    if isinstance(channel_metadata, dict):
        site_id = str(channel_metadata.get("site_id") or "").strip()
        if site_id:
            return site_id
    provenance = candidate.get("provenance")
    if isinstance(provenance, dict):
        site_id = str(provenance.get("site_id") or "").strip()
        if site_id:
            return site_id
        semantic_path = str(provenance.get("semantic_artifact_path") or "").strip()
        if semantic_path:
            semantic_payload = read_json_object(Path(semantic_path))
            if semantic_payload is not None:
                site_id = str(semantic_payload.get("site_id") or "").strip()
                if site_id:
                    return site_id
    return ""


def site_note_path_for_site_id(site_id: str) -> str:
    for site in SITES:
        if str(site.get("site_id") or "") == site_id:
            note_path = site.get("note_path")
            return str(note_path) if isinstance(note_path, str) else ""
    return ""


def draft_note_content(candidate: dict[str, object]) -> str:
    channel_metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    provenance = candidate.get("provenance") if isinstance(candidate.get("provenance"), dict) else {}
    candidate_summary = str(candidate.get("summary") or "").strip()
    review_rationale = str(candidate.get("review_rationale") or "").strip()
    source_context = str(candidate.get("source_context") or "").strip()
    rationale = str(candidate.get("rationale") or "").strip()
    primary_summary = note_primary_summary(candidate_summary, review_rationale, source_context, rationale)
    lines = ["---", "", "## Field Capture Reviews", "", f"### Field Capture Review - {field_capture_entry_label(channel_metadata, candidate)}"]
    field_timestamp = str(channel_metadata.get("captured_at") or candidate.get("created_at") or "").strip()
    if field_timestamp:
        lines.append(f"- field_capture_timestamp: {field_timestamp}")
    site_id = resolve_candidate_site_id(candidate)
    if site_id:
        lines.append(f"- site_id: {site_id}")
    area = note_area_value(channel_metadata, primary_summary, review_rationale, source_context)
    lines.append(f"- area: {area}")
    for key, label in (("phase", "phase"), ("upload_id", "capture_id")):
        value = str(channel_metadata.get(key) or "").strip()
        if value:
            lines.append(f"- {label}: {value}")
    audio_asset_id = str(provenance.get("audio_asset_id") or "").strip()
    if audio_asset_id:
        lines.append(f"- audio_asset_id: {audio_asset_id}")
    reviewer = str(candidate.get("reviewer") or "").strip()
    if reviewer:
        lines.append(f"- reviewer: {reviewer}")
    if primary_summary:
        lines.extend(["", f"Summary: {primary_summary}"])
    if review_rationale:
        lines.extend(["", f"Review rationale: {review_rationale}"])
    elif source_context:
        lines.extend(["", f"Reviewed context: {source_context}"])
    elif rationale:
        lines.extend(["", f"Reviewed context: {rationale}"])
    if candidate_summary and candidate_summary != primary_summary and not is_generic_candidate_label(candidate_summary):
        lines.append(f"Candidate label: {candidate_summary}")
    semantic_path = str(provenance.get("semantic_artifact_path") or "").strip()
    transcript_path = str(provenance.get("source_transcript_path") or "").strip()
    if semantic_path or transcript_path:
        lines.append("")
    if semantic_path:
        lines.append(f"Source semantic artifact: {semantic_path}")
    if transcript_path:
        lines.append(f"Source transcript artifact: {transcript_path}")
    return "\n".join(lines).rstrip() + "\n"


def note_primary_summary(candidate_summary: str, review_rationale: str, source_context: str, rationale: str) -> str:
    if review_rationale:
        return operational_summary_from_review_rationale(review_rationale)
    return candidate_summary or source_context or rationale


def operational_summary_from_review_rationale(review_rationale: str) -> str:
    if ":" in review_rationale:
        prefix, rest = review_rationale.split(":", 1)
        if prefix.strip().lower() in {"operational supply note", "useful site documentation note", "operational note"}:
            cleaned = rest.strip()
            if cleaned:
                return cleaned[0].upper() + cleaned[1:]
    return review_rationale


def is_generic_candidate_label(summary: str) -> bool:
    return summary.strip().lower() in GENERIC_CANDIDATE_LABELS


def field_capture_entry_label(channel_metadata: dict[str, object], candidate: dict[str, object]) -> str:
    timestamp = str(channel_metadata.get("captured_at") or candidate.get("created_at") or "").strip()
    return timestamp or "untimed capture"


def note_area_value(channel_metadata: dict[str, object], primary_summary: str, review_rationale: str, source_context: str) -> str:
    area = str(channel_metadata.get("area") or "").strip()
    if not area:
        return "Unknown"
    if area.lower() == "supply levels" and not is_supply_note_text(" ".join([primary_summary, review_rationale, source_context])):
        return "Unknown"
    return area


def is_supply_note_text(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(
        term in normalized
        for term in (
            "bathroom cleaner",
            "cleaner",
            "fresh mop head",
            "fresh mop heads",
            "mop head",
            "mop heads",
            "order",
            "restock",
            "supply",
            "supplies",
            *load_brand_keywords("supply"),
        )
    )


def draft_payload_from_candidate(path: Path, candidate: dict[str, object], *, runtime_root: Path | None = None) -> dict[str, object] | None:
    if candidate.get("type") != "action_candidate_review":
        return None
    if candidate.get("status") != STATUS_APPROVED:
        return None
    proposed_job_type, proposed_payload, mapping_error = proposed_queue_job(candidate, runtime_root=runtime_root)
    payload = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path=str(path),
        proposed_job_type=proposed_job_type,
        proposed_payload=proposed_payload,
        rationale=candidate.get("rationale") or "",
        confidence=candidate.get("confidence") or "unknown",
        approval_metadata=candidate.get("approval_metadata") if isinstance(candidate.get("approval_metadata"), dict) else {},
    )
    if mapping_error and payload.get("status") == DRAFT_STATUS_FAILED:
        payload["error"] = {"type": "ValueError", "message": mapping_error}
    return payload


def draft_payloads_from_candidate(path: Path, candidate: dict[str, object], *, runtime_root: Path | None = None) -> list[dict[str, object]]:
    if candidate.get("type") != "action_candidate_review":
        return []
    if candidate.get("status") != STATUS_APPROVED:
        return []
    payloads: list[dict[str, object]] = []
    for proposed_job_type, proposed_payload, mapping_error in proposed_queue_jobs(candidate, runtime_root=runtime_root):
        payload = approved_job_draft_payload(
            candidate=candidate,
            candidate_artifact_path=str(path),
            proposed_job_type=proposed_job_type,
            proposed_payload=proposed_payload,
            rationale=candidate.get("rationale") or "",
            confidence=candidate.get("confidence") or "unknown",
            approval_metadata=candidate.get("approval_metadata") if isinstance(candidate.get("approval_metadata"), dict) else {},
        )
        if mapping_error and payload.get("status") == DRAFT_STATUS_FAILED:
            payload["error"] = {"type": "ValueError", "message": mapping_error}
        payloads.append(payload)
    return payloads


def draft_generation_result(
    *,
    candidate: dict[str, object],
    candidate_path: Path,
    status: str,
    reason: str,
    draft_path: Path | None = None,
    draft: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "candidate_path": str(candidate_path),
        "draft_id": "" if draft is None else str(draft.get("draft_id") or ""),
        "draft_path": "" if draft_path is None else str(draft_path),
        "status": status,
        "reason": reason,
        "draft": draft,
    }


def plan_approved_job_draft(candidate_path: Path, candidate: dict[str, object], draft_dir: Path, *, runtime_root: Path | None = None) -> dict[str, object]:
    if candidate.get("type") != "action_candidate_review":
        return draft_generation_result(
            candidate=candidate,
            candidate_path=candidate_path,
            status=DRAFT_PLAN_SKIPPED,
            reason="not an action candidate review artifact",
        )
    if candidate.get("status") != STATUS_APPROVED:
        return draft_generation_result(
            candidate=candidate,
            candidate_path=candidate_path,
            status=DRAFT_PLAN_SKIPPED,
            reason=f"candidate status is not approved: {candidate.get('status')}",
        )

    payloads = draft_payloads_from_candidate(candidate_path, candidate, runtime_root=runtime_root)
    if not payloads:
        return draft_generation_result(
            candidate=candidate,
            candidate_path=candidate_path,
            status=DRAFT_PLAN_SKIPPED,
            reason="candidate did not produce a draft payload",
        )
    payload = payloads[0]

    draft_path = approved_job_draft_path(draft_dir, str(payload.get("draft_id") or ""))
    if payload.get("status") == "failed":
        error = payload.get("error")
        reason = "approved candidate cannot produce a valid draft"
        if isinstance(error, dict) and error.get("message"):
            reason = str(error["message"])
        return draft_generation_result(
            candidate=candidate,
            candidate_path=candidate_path,
            status=DRAFT_PLAN_FAILED,
            reason=reason,
            draft_path=draft_path,
            draft=payload,
        )
    if draft_path.exists():
        return draft_generation_result(
            candidate=candidate,
            candidate_path=candidate_path,
            status=DRAFT_PLAN_SKIPPED,
            reason="approved draft already exists",
            draft_path=draft_path,
            draft=payload,
        )
    return draft_generation_result(
        candidate=candidate,
        candidate_path=candidate_path,
        status=DRAFT_PLAN_WOULD_CREATE,
        reason="would create approved queue-job draft",
        draft_path=draft_path,
        draft=payload,
    )


def create_approved_job_drafts(
    candidate_dir: Path,
    draft_dir: Path,
    *,
    runtime_root: Path | None = None,
    candidate_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    candidate_filter = {str(item) for item in candidate_ids or [] if str(item)}
    runtime_resolved = runtime_root.expanduser().resolve(strict=False) if runtime_root is not None else None
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_resolved)
        resolve_within_root(draft_root, runtime_resolved)

    for candidate_path, candidate in iter_candidate_artifacts(candidate_root):
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_filter and candidate_id not in candidate_filter:
            if candidate.get("type") == "action_candidate_review" and candidate.get("status") == STATUS_APPROVED:
                counts["discovered"] += 1
                counts["skipped"] += 1
            continue
        if candidate.get("type") != "action_candidate_review":
            counts["skipped"] += 1
            continue
        if candidate.get("status") != STATUS_APPROVED:
            counts["skipped"] += 1
            continue
        counts["discovered"] += 1
        payloads = draft_payloads_from_candidate(candidate_path, candidate, runtime_root=runtime_root)
        if not payloads:
            counts["skipped"] += 1
            continue
        for payload in payloads:
            write_approved_job_draft(draft_root, payload)
            if payload.get("status") == "failed":
                counts["failed"] += 1
            else:
                counts["completed"] += 1
    return counts


def approved_job_drafts_report(
    candidate_dir: Path,
    draft_dir: Path,
    *,
    runtime_root: Path | None = None,
    dry_run: bool = False,
    candidate_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    counts = result_counts("discovered", "skipped", "completed", "failed")
    results: list[dict[str, object]] = []
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    candidate_filter = {str(item) for item in candidate_ids or [] if str(item)}
    runtime_resolved = runtime_root.expanduser().resolve(strict=False) if runtime_root is not None else None
    if runtime_root is not None:
        resolve_within_root(candidate_root, runtime_resolved)
        resolve_within_root(draft_root, runtime_resolved)

    for candidate_path, candidate in iter_candidate_artifacts(candidate_root):
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_filter and candidate_id not in candidate_filter:
            if candidate.get("type") == "action_candidate_review" and candidate.get("status") == STATUS_APPROVED:
                counts["discovered"] += 1
                counts["skipped"] += 1
            continue
        result = plan_approved_job_draft(candidate_path, candidate, draft_root, runtime_root=runtime_resolved)
        results.append(result)
        if candidate.get("type") != "action_candidate_review":
            counts["skipped"] += 1
            continue
        if candidate.get("status") != STATUS_APPROVED:
            counts["skipped"] += 1
            continue
        counts["discovered"] += 1
        if result["status"] == DRAFT_PLAN_WOULD_CREATE:
            counts["completed"] += 1
        elif result["status"] == DRAFT_PLAN_FAILED:
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    return {"dry_run": dry_run, "counts": counts, "results": results}


def display_draft_status(status: object) -> str:
    if status == DRAFT_STATUS_APPROVED:
        return "approved_draft"
    if status == DRAFT_STATUS_FAILED:
        return "failed_draft"
    return str(status or "")


def preview_text(value: object, *, limit: int = 120) -> str:
    text = " ".join(json.dumps(value, sort_keys=True, default=str).split()) if isinstance(value, (dict, list)) else " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def records_by_draft_id(records: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    by_id: dict[str, list[dict[str, object]]] = {}
    for record in records:
        draft_id = str(record.get("draft_id") or "")
        if draft_id:
            by_id.setdefault(draft_id, []).append(record)
    return by_id


def matching_records(records: list[dict[str, object]], *, draft_id: str, computed_job_id: str) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for record in records:
        if draft_id and draft_id == str(record.get("draft_id") or ""):
            matches.append(record)
            continue
        if computed_job_id and computed_job_id == str(record.get("computed_job_id") or ""):
            matches.append(record)
    return matches


def historical_failure_fields(failed_matches: list[dict[str, object]]) -> dict[str, object]:
    return {
        "historical_failed": bool(failed_matches),
        "historical_failed_count": len(failed_matches),
        "historical_failed_paths": [str(record.get("path") or "") for record in failed_matches if record.get("path")],
    }


def draft_queue_state(
    draft_path: Path,
    draft: dict[str, object],
    *,
    runtime_root: Path,
    staging_dir: Path,
    queue_dir: Path,
    processed_dir: Path,
    failed_dir: Path,
) -> dict[str, object]:
    draft_id = str(draft.get("draft_id") or "")
    staging = read_json_object(staging_result_path(staging_dir, draft_id)) if draft_id else None
    queue_record_list = queue_job_records(queue_dir, "queued")
    processed_record_list = queue_job_records(processed_dir, "processed")
    failed_record_list = queue_job_records(failed_dir, "failed")
    queue_records = records_by_draft_id(queue_record_list)
    processed_records = records_by_draft_id(processed_record_list)
    failed_records = records_by_draft_id(failed_record_list)
    index_records, index_error = processed_index_records(runtime_root)
    indexed_ids = {str(record.get("computed_job_id")) for record in index_records if record.get("computed_job_id")}
    computed_job_id = ""
    try:
        job = queue_job_from_draft(draft, draft_path)
        computed_job_id = compute_job_id(job)
    except Exception:
        pass

    if queue_records.get(draft_id):
        failed_matches = matching_records(failed_record_list, draft_id=draft_id, computed_job_id=computed_job_id)
        return {
            "state": "queued",
            "evidence_path": str(queue_records[draft_id][0].get("path") or ""),
            "computed_job_id": computed_job_id,
            **historical_failure_fields(failed_matches),
        }
    processed_matches = matching_records(processed_record_list, draft_id=draft_id, computed_job_id=computed_job_id)
    failed_matches = matching_records(failed_record_list, draft_id=draft_id, computed_job_id=computed_job_id)
    if processed_matches:
        return {
            "state": "processed",
            "evidence_path": str(processed_matches[0].get("path") or ""),
            "computed_job_id": computed_job_id,
            **historical_failure_fields(failed_matches),
        }
    if failed_matches:
        return {
            "state": "failed",
            "evidence_path": str(failed_matches[0].get("path") or ""),
            "computed_job_id": computed_job_id,
            **historical_failure_fields([]),
        }
    if computed_job_id and computed_job_id in indexed_ids:
        return {
            "state": "indexed",
            "evidence_path": str(runtime_root / "processed_index.jsonl"),
            "computed_job_id": computed_job_id,
            **historical_failure_fields(failed_matches),
        }
    if staging:
        return {
            "state": "staged_status_exists",
            "evidence_path": str(staging_result_path(staging_dir, draft_id)),
            "computed_job_id": str(staging.get("computed_job_id") or computed_job_id),
        }
    if index_error is not None:
        return {"state": "unknown", "evidence_path": str(runtime_root / "processed_index.jsonl"), "computed_job_id": computed_job_id, "error": index_error}
    return {"state": "not_staged", "evidence_path": "", "computed_job_id": computed_job_id}


def draft_list_item(
    path: Path,
    draft: dict[str, object],
    *,
    runtime_root: Path,
    staging_dir: Path,
    queue_dir: Path,
    processed_dir: Path,
    failed_dir: Path,
    include_payload: bool = False,
    include_source: bool = False,
) -> dict[str, object]:
    provenance = draft.get("provenance") if isinstance(draft.get("provenance"), dict) else {}
    proposed_payload = draft.get("proposed_payload")
    item: dict[str, object] = {
        "draft_id": str(draft.get("draft_id") or ""),
        "status": display_draft_status(draft.get("status")),
        "candidate_id": str(draft.get("candidate_id") or ""),
        "proposed_job_type": str(draft.get("proposed_job_type") or ""),
        "proposed_payload_preview": preview_text(proposed_payload),
        "rationale": str(draft.get("rationale") or ""),
        "confidence": draft.get("confidence"),
        "artifact_path": str(path),
        "candidate_artifact_path": str(draft.get("candidate_artifact_path") or provenance.get("candidate_artifact_path") or ""),
        "semantic_artifact_path": str(provenance.get("semantic_artifact_path") or ""),
        "source_transcript_path": str(provenance.get("source_transcript_path") or ""),
        "reviewer": str(draft.get("reviewer") or ""),
        "approved_at": str(draft.get("approved_at") or ""),
        "queue_state": draft_queue_state(
            path,
            draft,
            runtime_root=runtime_root,
            staging_dir=staging_dir,
            queue_dir=queue_dir,
            processed_dir=processed_dir,
            failed_dir=failed_dir,
        ),
    }
    if include_payload:
        item["proposed_payload"] = proposed_payload if isinstance(proposed_payload, dict) else {}
    if include_source:
        item["source_context"] = str(draft.get("source_context") or "")
        item["provenance"] = provenance
        item["approval_metadata"] = draft.get("approval_metadata") if isinstance(draft.get("approval_metadata"), dict) else {}
    return item


def list_approved_drafts_report(
    draft_dir: Path,
    *,
    runtime_root: Path,
    status: str | None = None,
    limit: int | None = None,
    include_payload: bool = False,
    include_source: bool = False,
    staging_dir: Path | None = None,
    queue_dir: Path | None = None,
    processed_dir: Path | None = None,
    failed_dir: Path | None = None,
) -> dict[str, object]:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    draft_root = draft_dir.expanduser().resolve(strict=False)
    staging_root = (staging_dir or runtime_resolved / "reviews" / "staging" / "field_capture").expanduser().resolve(strict=False)
    queue_root = (queue_dir or runtime_resolved / "queue").expanduser().resolve(strict=False)
    processed_root = (processed_dir or runtime_resolved / "processed").expanduser().resolve(strict=False)
    failed_root = (failed_dir or runtime_resolved / "failed").expanduser().resolve(strict=False)
    for root in (draft_root, staging_root, queue_root, processed_root, failed_root):
        resolve_within_root(root, runtime_resolved)

    counts = {"total": 0, "approved_draft": 0, "failed_draft": 0, "other": 0}
    items: list[dict[str, object]] = []
    for path, draft in iter_draft_artifacts(draft_root):
        display_status = display_draft_status(draft.get("status"))
        counts["total"] += 1
        if display_status in {"approved_draft", "failed_draft"}:
            counts[display_status] += 1
        else:
            counts["other"] += 1
        if status is not None and display_status != status:
            continue
        items.append(
            draft_list_item(
                path,
                draft,
                runtime_root=runtime_resolved,
                staging_dir=staging_root,
                queue_dir=queue_root,
                processed_dir=processed_root,
                failed_dir=failed_root,
                include_payload=include_payload,
                include_source=include_source,
            )
        )
    items.sort(key=lambda item: (str(item["draft_id"]), str(item["artifact_path"])))
    if limit is not None:
        items = items[: max(0, limit)]
    return {"channel": "field_capture", "counts": counts, "drafts": items}


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Generate approved field-capture queue-job draft artifacts.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def build_list_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="List field-capture approved queue-job drafts for staging review.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--staging-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--processed-dir", type=Path)
    parser.add_argument("--failed-dir", type=Path)
    parser.add_argument("--status", choices=DRAFT_LIST_STATUSES)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--include-payload", action="store_true")
    parser.add_argument("--include-source", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    candidate_dir = args.candidate_dir.expanduser() if args.candidate_dir is not None else default_candidate_dir(runtime_root)
    draft_dir = args.draft_dir.expanduser() if args.draft_dir is not None else default_draft_dir(runtime_root)
    if args.dry_run:
        report = approved_job_drafts_report(candidate_dir, draft_dir, runtime_root=runtime_root, dry_run=True)
        counts = report["counts"]
    else:
        report = None
        counts = create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    if args.json:
        print(json.dumps(report if args.dry_run else counts, indent=2, sort_keys=True))
    else:
        prefix = "field approved drafts dry-run" if args.dry_run else "field approved drafts"
        print(
            f"{prefix}: "
            f"discovered={counts['discovered']} skipped={counts['skipped']} "
            f"completed={counts['completed']} failed={counts['failed']}"
        )
        if args.dry_run and report is not None:
            for result in report["results"]:
                print(f"- {result['candidate_id']} {result['status']}: {result['reason']}")
    return 0


def run_list(argv: list[str] | None = None) -> int:
    args = build_list_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    draft_dir = args.draft_dir.expanduser() if args.draft_dir is not None else default_draft_dir(runtime_root)
    report = list_approved_drafts_report(
        draft_dir,
        runtime_root=runtime_root,
        status=args.status,
        limit=args.limit,
        include_payload=args.include_payload,
        include_source=args.include_source,
        staging_dir=args.staging_dir.expanduser() if args.staging_dir is not None else None,
        queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
        processed_dir=args.processed_dir.expanduser() if args.processed_dir is not None else None,
        failed_dir=args.failed_dir.expanduser() if args.failed_dir is not None else None,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        counts = report["counts"]
        print(
            "field approved drafts: "
            f"total={counts['total']} approved_draft={counts['approved_draft']} "
            f"failed_draft={counts['failed_draft']} other={counts['other']}"
        )
        for item in report["drafts"]:
            queue_state = item["queue_state"] if isinstance(item["queue_state"], dict) else {}
            print(
                f"- {item['draft_id']} [{item['status']}] candidate={item['candidate_id']} "
                f"job_type={item['proposed_job_type']} queue_state={queue_state.get('state', 'unknown')}"
            )
            if queue_state.get("historical_failed"):
                print(f"  historical_failed_count={queue_state.get('historical_failed_count', 0)}")
            if item["proposed_payload_preview"]:
                print(f"  payload: {item['proposed_payload_preview']}")
            if item["rationale"]:
                print(f"  rationale: {item['rationale']}")
            print(f"  artifact: {item['artifact_path']}")
            if item["candidate_artifact_path"]:
                print(f"  candidate_artifact: {item['candidate_artifact_path']}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
