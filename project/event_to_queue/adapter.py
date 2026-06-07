from __future__ import annotations

import re
from datetime import datetime

from event_pipeline.sites import resolve_site_note_path
from queue_spec import (
    JOB_ADD_PERSON,
    JOB_APPEND_TO_NOTE,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_FLAG_RETENTION_RISK,
    JOB_REMOVE_FROM_SCHEDULE,
    JOB_TRIGGER_RECRUITING,
    JOB_VISIT_CREATE,
    JOB_VOICE_MEMO_NOTE,
)

JOURNAL_EVENT_TYPES = {"employee_callout", "incident", "interview_note"}


def event_date(event: dict) -> str:
    timestamp = str(event.get("timestamp", ""))
    match = re.match(r"^\d{4}-\d{2}-\d{2}", timestamp)
    if match:
        return match.group(0)
    return datetime.now().date().isoformat()


def render_journal_content(event: dict) -> str | None:
    details = event.get("details")
    if not isinstance(details, str) or not details.strip():
        return None

    lines = [details.strip()]
    observation_lines: list[str] = []
    observations = event.get("observations", [])
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            observation_text = observation.get("type")
            if not isinstance(observation_text, str) or not observation_text.strip():
                continue
            if observation.get("confidence") != "observed":
                continue
            observation_lines.append(f"- {observation_text.strip()}")
    if observation_lines:
        lines.extend(["", "**Observations:**", *observation_lines])
    return "\n".join(lines)


def render_unknown_capture_content(event: dict, reason: str) -> str:
    details = str(event.get("details") or event.get("source_excerpt") or "").strip()
    excerpt = str(event.get("source_excerpt") or details).strip()
    return (
        "---\n"
        "type: unknown_capture\n"
        f"timestamp: {event.get('timestamp', '')}\n"
        "status: unresolved\n"
        "retry_count: 0\n"
        "last_attempted: null\n"
        f"event_type: {event.get('type', '')}\n"
        f"event_id: {event.get('event_id', '')}\n"
        f"reason: {reason}\n"
        "---\n\n"
        "## Event Details\n"
        f"{details}\n\n"
        "## Source Excerpt\n"
        f"{excerpt}\n"
    )


def event_to_job(event: dict) -> dict | None:
    event_type = event.get("type")
    event_id = str(event.get("event_id", "")).strip()
    if not event_id:
        return None

    if event_type == "visit_create":
        site = str(event.get("site") or "").strip()
        if not site or site == "unknown":
            return None
        source_excerpt = str(event.get("source_excerpt") or "").strip()
        details = str(event.get("details") or "").strip()
        evidence = source_excerpt or details or "Voice memo visit note."
        visit_type = str(event.get("visit_type") or "").strip()
        visited_by = str(event.get("visited_by") or "").strip()
        payload = {
            "site": site,
            "confidence": str(event.get("confidence") or "medium").strip(),
            "source": str(event.get("voice_memo_capture_id") or event.get("capture_id") or "voice_memo").strip(),
            "evidence": evidence,
        }
        if visit_type:
            payload["visit_type"] = visit_type
        if visited_by:
            payload["visited_by"] = visited_by
        return {
            "job_id": event_id,
            "job_type": JOB_VISIT_CREATE,
            "payload": payload,
        }

    if isinstance(event.get("voice_memo_capture_id"), str) and event.get("voice_memo_routing_flag") in {"site_tagged", "employee_tagged", "general"}:
        capture_id = str(event.get("voice_memo_capture_id") or event.get("capture_id") or "").strip()
        transcript_text = str(event.get("transcript_text") or event.get("source_excerpt") or event.get("details") or "").strip()
        raw_transcript_path = str(event.get("raw_transcript_path") or "").strip()
        audio_file = str(event.get("audio_file") or "").strip()
        timestamp = str(event.get("timestamp") or datetime.now().astimezone().isoformat()).strip()
        if not capture_id or not transcript_text or not raw_transcript_path or not audio_file:
            return None
        payload = {
            "capture_id": capture_id,
            "timestamp": timestamp,
            "audio_file": audio_file,
            "raw_transcript_path": raw_transcript_path,
            "transcript_text": transcript_text,
            "routing_flag": str(event.get("voice_memo_routing_flag") or ""),
            "site_id": str(event.get("site_id") or ""),
            "site": str(event.get("site") or ""),
            "note": str(event.get("note") or ""),
            "employees": event.get("employees") if isinstance(event.get("employees"), list) else [],
        }
        semantic_artifact_path = str(event.get("voice_memo_semantic_artifact_path") or "").strip()
        semantic_status = str(event.get("voice_memo_semantic_status") or "").strip()
        if semantic_artifact_path:
            payload["semantic_artifact_path"] = semantic_artifact_path
        if semantic_status:
            payload["semantic_status"] = semantic_status
        if isinstance(event.get("voice_memo_action_candidate_paths"), list):
            paths = [str(path).strip() for path in event["voice_memo_action_candidate_paths"] if str(path).strip()]
            if paths:
                payload["action_candidate_paths"] = paths
        if isinstance(event.get("voice_memo_action_candidate_count"), int):
            payload["action_candidate_count"] = event["voice_memo_action_candidate_count"]
        if isinstance(event.get("voice_memo_semantic_action_count"), int):
            payload["semantic_action_count"] = event["voice_memo_semantic_action_count"]
        if isinstance(event.get("voice_memo_semantic_actions"), list):
            payload["semantic_actions"] = event["voice_memo_semantic_actions"]
        for source_key, payload_key in (
            ("voice_memo_semantic_primary_intent", "semantic_primary_intent"),
            ("voice_memo_semantic_primary_action", "semantic_primary_action"),
            ("voice_memo_semantic_primary_target_type", "semantic_primary_target_type"),
            ("voice_memo_semantic_primary_target_id", "semantic_primary_target_id"),
            ("voice_memo_semantic_primary_confidence", "semantic_primary_confidence"),
            ("voice_memo_semantic_error", "semantic_error"),
            ("voice_memo_action_candidate_path", "action_candidate_path"),
        ):
            value = str(event.get(source_key) or "").strip()
            if value:
                payload[payload_key] = value
        for source_key, payload_key in (
            ("voice_memo_semantic_intent", "semantic_intent"),
            ("voice_memo_semantic_action", "semantic_action"),
            ("voice_memo_semantic_target_type", "semantic_target_type"),
            ("voice_memo_semantic_target_id", "semantic_target_id"),
            ("voice_memo_semantic_confidence", "semantic_confidence"),
        ):
            value = str(event.get(source_key) or "").strip()
            if value:
                payload[payload_key] = value
        if isinstance(event.get("voice_memo_semantic_review_required"), bool):
            payload["semantic_review_required"] = bool(event["voice_memo_semantic_review_required"])
        if isinstance(event.get("geolocation"), dict):
            payload["geolocation"] = event["geolocation"]
        metadata = {"capture_id": capture_id}
        if semantic_artifact_path:
            metadata["semantic_artifact_path"] = semantic_artifact_path
        candidate_path = str(event.get("voice_memo_action_candidate_path") or "").strip()
        if candidate_path:
            metadata["action_candidate_path"] = candidate_path
        if isinstance(event.get("voice_memo_action_candidate_paths"), list):
            paths = [str(path).strip() for path in event["voice_memo_action_candidate_paths"] if str(path).strip()]
            if paths:
                metadata["action_candidate_paths"] = paths
        return {
            "job_id": event_id,
            "job_type": JOB_VOICE_MEMO_NOTE,
            "metadata": metadata,
            "payload": payload,
        }

    if event_type == "employee_resigned":
        employee = event.get("employee")
        site = event.get("site")
        if not isinstance(employee, str) or not employee.strip() or not isinstance(site, str) or not site.strip():
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_REMOVE_FROM_SCHEDULE,
            "payload": {
                "employee": employee,
                "site": site,
                "date": event_date(event),
            },
        }

    if event_type == "employee_retention_risk":
        employee = event.get("employee")
        site = event.get("site")
        details = event.get("details")
        if not isinstance(employee, str) or not employee.strip():
            return None
        if not isinstance(site, str) or not site.strip() or not isinstance(details, str) or not details.strip():
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_FLAG_RETENTION_RISK,
            "payload": {
                "employee": employee,
                "site": site,
                "details": details,
                "date": event_date(event),
            },
        }

    if event_type == "staffing_risk":
        site = event.get("site")
        details = event.get("details")
        if not isinstance(site, str) or not site.strip() or not isinstance(details, str) or not details.strip():
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_TRIGGER_RECRUITING,
            "payload": {
                "site": site,
                "priority": "emergency" if event.get("severity") == "critical" else "normal",
                "details": details,
                "date": event_date(event),
                "open_positions": event.get("open_positions"),
            },
        }

    if event_type == "access_constraint":
        site = event.get("site")
        details = event.get("details")
        if not isinstance(site, str) or not site.strip() or not isinstance(details, str) or not details.strip():
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_FLAG_ACCESS_CONSTRAINT,
            "payload": {
                "site": site,
                "details": details,
                "blocking": bool(event.get("blocking", False)),
                "date": event_date(event),
            },
        }

    if event_type == "employee_onboarding":
        employee = event.get("employee") or event.get("name")
        role = event.get("role")
        if not isinstance(employee, str) or not employee.strip():
            return {
                "job_id": event_id,
                "job_type": JOB_APPEND_TO_NOTE,
                "payload": {
                    "path": f"Journal/{event_date(event)}-unknown.md",
                    "content": render_unknown_capture_content(event, "employee_onboarding_missing_employee"),
                    "destination": "journal_unknown",
                },
            }
        if not isinstance(role, str) or not role.strip():
            return {
                "job_id": event_id,
                "job_type": JOB_APPEND_TO_NOTE,
                "payload": {
                    "path": f"Journal/{event_date(event)}-unknown.md",
                    "content": render_unknown_capture_content(event, "employee_onboarding_missing_role"),
                    "destination": "journal_unknown",
                },
            }
        payload = {
            "name": employee,
            "role": role,
        }
        for key in ("employee_id", "employment_type", "status", "assignments", "contact", "metadata"):
            if key in event:
                payload[key] = event[key]
        return {
            "job_id": event_id,
            "job_type": JOB_ADD_PERSON,
            "payload": payload,
        }

    if event_type == "site_observation":
        details = event.get("details")
        site = event.get("site")
        if not isinstance(details, str) or not details.strip():
            return None
        if not isinstance(site, str) or not site.strip():
            return None
        if site.strip().lower() == "unknown":
            return {
                "job_id": event_id,
                "job_type": JOB_APPEND_TO_NOTE,
                "payload": {
                    "path": f"Journal/{event_date(event)}-unknown.md",
                    "content": details,
                    "destination": "journal_unknown",
                },
            }
        note_path = resolve_site_note_path(site)
        if note_path is None:
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": note_path,
                "content": details,
                "destination": "site_note",
            },
        }

    if event_type in JOURNAL_EVENT_TYPES:
        content = render_journal_content(event)
        if content is None:
            return None
        return {
            "job_id": event_id,
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": f"Journal/{event_date(event)}.md",
                "content": content,
                "destination": "journal",
            },
        }

    return None
