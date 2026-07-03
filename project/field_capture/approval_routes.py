from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from queue_spec import (
    EQUIPMENT_REQUEST_PRIORITIES,
    JOB_FLAG_ACCESS_CONSTRAINT,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    SITE_ISSUE_CATEGORIES,
    SITE_ISSUE_PRIORITIES,
    SUPPLY_NEED_URGENCIES,
    validate_job,
)


APPROVAL_ROUTE_OPEN_ISSUE = "open_issue"
APPROVAL_ROUTE_ACCESS_CONSTRAINT = "access_constraint"
APPROVAL_ROUTE_SUPPLY_NEED = "supply_need"
APPROVAL_ROUTE_EQUIPMENT_REQUEST = "equipment_request"
APPROVAL_ROUTE_NO_ACTION = "no_action"

APPROVAL_ROUTE_TO_JOB_TYPE = {
    APPROVAL_ROUTE_OPEN_ISSUE: JOB_LOG_SITE_ISSUE,
    APPROVAL_ROUTE_ACCESS_CONSTRAINT: JOB_FLAG_ACCESS_CONSTRAINT,
    APPROVAL_ROUTE_SUPPLY_NEED: JOB_LOG_SUPPLY_NEED,
    APPROVAL_ROUTE_EQUIPMENT_REQUEST: JOB_LOG_EQUIPMENT_REQUEST,
    APPROVAL_ROUTE_NO_ACTION: None,
}

DEFAULT_APPROVAL_ROUTE_BY_JOB_TYPE = {
    JOB_LOG_SITE_ISSUE: APPROVAL_ROUTE_OPEN_ISSUE,
    JOB_FLAG_ACCESS_CONSTRAINT: APPROVAL_ROUTE_ACCESS_CONSTRAINT,
    JOB_LOG_SUPPLY_NEED: APPROVAL_ROUTE_SUPPLY_NEED,
    JOB_LOG_EQUIPMENT_REQUEST: APPROVAL_ROUTE_EQUIPMENT_REQUEST,
}


def normalize_approval_route(route: object) -> str | None:
    route_text = str(route or "").strip()
    return route_text or None


def is_valid_approval_route(route: object) -> bool:
    route_text = normalize_approval_route(route)
    return route_text in APPROVAL_ROUTE_TO_JOB_TYPE


def build_approval_route_job(
    doc: dict[str, Any],
    route: str,
    *,
    reviewer: str,
    runtime_root: Path | None = None,
) -> tuple[str, dict[str, Any], str]:
    del runtime_root
    route_text = normalize_approval_route(route)
    if not route_text:
        return "", {}, "approval route is required"
    if route_text not in APPROVAL_ROUTE_TO_JOB_TYPE:
        return "", {}, f"invalid approval route: {route}"
    if route_text == APPROVAL_ROUTE_NO_ACTION:
        return "", {}, ""

    payload = _payload(doc)
    site_id = _site_id(doc, payload)
    if not site_id:
        return "", {}, "approval route cannot build a queue job without site_id"
    message = _message(doc, payload)
    if not message:
        return "", {}, "approval route cannot build a queue job without message/details"
    reporter = _reporter(doc, reviewer)
    if not reporter:
        return "", {}, "approval route cannot build a queue job without reviewer or submitter"

    if route_text == APPROVAL_ROUTE_OPEN_ISSUE:
        job_type = JOB_LOG_SITE_ISSUE
        built_payload = _open_issue_payload(doc, payload, site_id, message, reporter)
    elif route_text == APPROVAL_ROUTE_ACCESS_CONSTRAINT:
        job_type = JOB_FLAG_ACCESS_CONSTRAINT
        built_payload = {"site": site_id, "details": _concise(message, 500)}
    elif route_text == APPROVAL_ROUTE_SUPPLY_NEED:
        job_type = JOB_LOG_SUPPLY_NEED
        item_name = _supply_item_name(payload, message)
        if not item_name:
            return "", {}, "approval route cannot build supply_need without item_name"
        built_payload = {
            "site_id": site_id,
            "item_name": item_name,
            "urgency": _valid_choice(payload.get("urgency"), SUPPLY_NEED_URGENCIES, _supply_urgency(message)),
            "requested_by": reporter,
            "source": "field_capture",
            "notes": _concise(message, 500),
            "status": "open",
            **_observed_at_field(doc),
            **_provenance_fields(doc, payload),
        }
        quantity = _clean_text(payload.get("quantity_needed") or payload.get("quantity"))
        if quantity:
            built_payload["quantity_needed"] = _concise(quantity, 80)
    elif route_text == APPROVAL_ROUTE_EQUIPMENT_REQUEST:
        job_type = JOB_LOG_EQUIPMENT_REQUEST
        equipment_name = _equipment_name(payload, message)
        if not equipment_name:
            return "", {}, "approval route cannot build equipment_request without equipment_name"
        built_payload = {
            "site_id": site_id,
            "equipment_name": equipment_name,
            "reason": _concise(message, 500),
            "priority": _valid_choice(payload.get("priority"), EQUIPMENT_REQUEST_PRIORITIES, _equipment_priority(message)),
            "requested_by": reporter,
            "source": "field_capture",
            "status": "open",
            **_observed_at_field(doc),
            **_provenance_fields(doc, payload),
        }
        notes = _clean_text(payload.get("notes"))
        if notes:
            built_payload["notes"] = _concise(notes, 500)
    else:
        return "", {}, f"unsupported approval route: {route_text}"

    if not validate_job({"job_type": job_type, "payload": built_payload}):
        return "", {}, f"generated {job_type} payload failed queue_spec validation"
    return job_type, built_payload, ""


def _payload(doc: dict[str, Any]) -> dict[str, Any]:
    payload = doc.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _site_id(doc: dict[str, Any], payload: dict[str, Any]) -> str:
    return _clean_text(doc.get("site_id") or payload.get("site_id") or payload.get("site"))


def _message(doc: dict[str, Any], payload: dict[str, Any]) -> str:
    evidence = doc.get("evidence") if isinstance(doc.get("evidence"), dict) else {}
    return _concise(
        _first_text(
            doc.get("message"),
            evidence.get("source_text"),
            payload.get("summary"),
            payload.get("notes"),
            payload.get("details"),
            payload.get("reason"),
            payload.get("title"),
        ),
        500,
    )


def _reporter(doc: dict[str, Any], reviewer: str) -> str:
    return _clean_text(
        doc.get("submitter_name")
        or doc.get("person_name")
        or doc.get("submitter_id")
        or doc.get("person_id")
        or reviewer
    )


def _open_issue_payload(
    doc: dict[str, Any],
    original_payload: dict[str, Any],
    site_id: str,
    message: str,
    reporter: str,
) -> dict[str, Any]:
    access_origin = _is_access_origin(doc, original_payload, message)
    category = "access" if access_origin else _valid_choice(original_payload.get("category"), SITE_ISSUE_CATEGORIES, "other")
    priority = _access_issue_priority(message) if access_origin else _valid_choice(original_payload.get("priority"), SITE_ISSUE_PRIORITIES, "normal")
    title = _clean_text(original_payload.get("title"))
    if access_origin:
        title = "Access issue follow-up"
    payload: dict[str, Any] = {
        "site_id": site_id,
        "title": _concise(title or message or "Site issue follow-up", 80),
        "summary": message,
        "observations": [message],
        "category": category,
        "priority": priority,
        "status": "open",
        "reported_by": reporter,
        "source": "field_capture",
        "client_notified": False,
        "resolution_trigger": (
            "access issue resolved and service can proceed normally"
            if access_origin
            else _clean_text(original_payload.get("resolution_trigger")) or "Issue is resolved and service can proceed normally."
        ),
        **_observed_at_field(doc),
        **_provenance_fields(doc, original_payload),
    }
    notes = _clean_text(original_payload.get("notes"))
    if notes and notes != message:
        payload["notes"] = _concise(notes, 500)
    return payload


def _observed_at_field(doc: dict[str, Any]) -> dict[str, str]:
    observed_at = _clean_text(doc.get("created_at"))
    return {"observed_at": observed_at} if observed_at else {}


def _provenance_fields(doc: dict[str, Any], payload: dict[str, Any]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    capture_ids = _string_list(payload.get("related_capture_ids"))
    capture_id = _clean_text(doc.get("source_capture_id") or payload.get("source_capture_id") or payload.get("capture_id"))
    if capture_id and capture_id not in capture_ids:
        capture_ids.insert(0, capture_id)
    if capture_ids:
        fields["related_capture_ids"] = capture_ids

    candidate_ids = _string_list(payload.get("related_candidate_ids"))
    if not candidate_ids:
        candidate_id = _clean_text(payload.get("candidate_id") or doc.get("candidate_id") or doc.get("draft_id"))
        if candidate_id:
            candidate_ids.append(candidate_id)
    if candidate_ids:
        fields["related_candidate_ids"] = candidate_ids

    artifacts = _string_list(payload.get("source_artifacts"))
    if artifacts:
        fields["source_artifacts"] = artifacts
    return fields


def _is_access_origin(doc: dict[str, Any], payload: dict[str, Any], message: str) -> bool:
    if _clean_text(doc.get("job_type")) == JOB_FLAG_ACCESS_CONSTRAINT:
        return True
    text = _normalized(" ".join([
        _clean_text(doc.get("message")),
        _clean_text(payload.get("title")),
        _clean_text(payload.get("summary")),
        _clean_text(payload.get("details")),
        message,
    ]))
    return any(term in text for term in ("access", "alarm code", "badge", "door code", "key", "locked out", "lockbox", "no access"))


def _access_issue_priority(message: str) -> str:
    text = _normalized(message)
    if any(
        term in text
        for term in (
            "alarm blocking",
            "blocked service",
            "cannot access",
            "can't access",
            "could not access",
            "emergency",
            "locked out",
            "no access",
            "unable to access",
            "urgent",
        )
    ):
        return "high"
    return "normal"


def _supply_item_name(payload: dict[str, Any], message: str) -> str:
    original = _clean_text(payload.get("item_name"))
    if original:
        return _concise(original, 80)
    match = re.search(r"\b(?:need|needs|needed|out of|low on|running low on)\s+(?P<item>.+)", message, re.IGNORECASE)
    item = match.group("item") if match else message
    item = re.split(r"\b(?:at|for|here|please|asap|today|now|urgent|critical)\b", item, maxsplit=1, flags=re.IGNORECASE)[0]
    item = re.sub(r"^[\s:,-]+|[\s.,;:!-]+$", "", item)
    item = re.sub(r"\b(?:more|some|the|a|an)\b", "", item, flags=re.IGNORECASE)
    return _concise(" ".join(item.split()), 80)


def _equipment_name(payload: dict[str, Any], message: str) -> str:
    original = _clean_text(payload.get("equipment_name"))
    if original:
        return _concise(original, 80)
    match = re.search(r"\b(?:need|needs|replace|replacement|broken|request)\s+(?P<item>.+)", message, re.IGNORECASE)
    item = match.group("item") if match else message
    item = re.split(r"\b(?:at|for|here|please|asap|today|now|urgent|critical|because|since)\b", item, maxsplit=1, flags=re.IGNORECASE)[0]
    item = re.sub(r"^[\s:,-]+|[\s.,;:!-]+$", "", item)
    item = re.sub(r"\b(?:more|some|the|a|an|new|replacement)\b", "", item, flags=re.IGNORECASE)
    return _concise(" ".join(item.split()), 80)


def _supply_urgency(message: str) -> str:
    text = _normalized(message)
    if any(term in text for term in ("critical", "emergency", "immediately")):
        return "critical"
    if any(term in text for term in ("asap", "now", "today", "urgent")):
        return "high"
    if "low on" in text or "running low on" in text:
        return "low"
    return "normal"


def _equipment_priority(message: str) -> str:
    text = _normalized(message)
    if any(term in text for term in ("critical", "emergency", "immediately", "asap", "urgent")):
        return "urgent"
    if any(term in text for term in ("now", "today")):
        return "high"
    if any(term in text for term in ("can wait", "low priority")):
        return "low"
    return "normal"


def _valid_choice(value: object, allowed: set[str], default: str) -> str:
    text = _clean_text(value)
    return text if text in allowed else default


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text and text not in result:
            result.append(text)
    return result


def _first_text(*values: object) -> str:
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return ""


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _concise(value: object, limit: int) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _normalized(value: object) -> str:
    return _clean_text(value).lower()
