from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from event_pipeline.domain_resolver import normalize_text
from event_pipeline.site_registry_data import load_brand_keywords
from event_pipeline.couchdb_registry import CouchDBRegistryError
from event_pipeline.extraction_terms import get_extraction_terms
from event_pipeline.sites import resolve_site_id, resolve_site_note_path
from processing_core.extracted_actions import ExtractedAction, validate_extracted_action
from processing_core.hashing import text_sha256
from queue_spec import (
    ALLOWED_JOB_TYPES,
    JOB_APPEND_TO_NOTE,
    JOB_FLAG_RETENTION_RISK,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    JOB_LOG_PERSONNEL_EVENT,
    JOB_REMOVE_FROM_SCHEDULE,
    JOB_SET_ENTITY_STATUS,
    JOB_TRIGGER_RECRUITING,
    JOB_UPDATE_SITE_EQUIPMENT,
    LOG_EQUIPMENT_REQUEST_ALLOWED_PAYLOAD_FIELDS,
    LOG_SITE_ISSUE_ALLOWED_PAYLOAD_FIELDS,
    LOG_SUPPLY_NEED_ALLOWED_PAYLOAD_FIELDS,
    LOG_PERSONNEL_EVENT_ALLOWED_PAYLOAD_FIELDS,
    PERSONNEL_EVENT_TYPES,
    SET_ENTITY_STATUS_ALLOWED_PAYLOAD_FIELDS,
    validate_job,
)


SOURCE_KINDS = frozenset({"ops_dashboard_text", "voice_memo", "field_capture_audio"})
ISSUE_TYPES = {
    "supplies",
    "access",
    "damage",
    "safety",
    "water",
    "quality",
    "client_request",
    "other",
}
URGENCIES = {"low", "normal", "high"}
AFTER_PHOTO_ACTION = "Capture after photo once corrected."
SUPPLY_REVIEW_ACTION = "Review supply/order follow-up."
GENERIC_REVIEW_ACTION = "Review the field audio note and decide whether follow-up is needed."
PROFANITY_REPLACEMENTS = {
    "shit": "issue",
    "fucked": "not acceptable",
    "fuck": "problem",
    "damn": "",
}
NON_FIELD_OPERATION_ACTION_TERMS = (
    "add journal",
    "journal entry",
    "personal journal",
    "vault",
    "transcription process",
    "semantic layer",
    "model",
    "pipeline",
    "processing",
)
EQUIPMENT_TERMS = ("vacuum", "equipment", "machine", "backpack vac", "extractor", "auto scrubber")
OPERATOR_CLASSIFICATION_DECLARATION_RE = re.compile(
    r"^\s*(?:[>\-*\u2022]\s*)?(?P<label>supply|supplies|equipment)\s*"
    r"(?P<need>needs?)?\s*(?:[:;,.\-]|\u2013|\u2014)\s*(?P<item>.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)
SUPPLY_ITEM_TERMS = (
    "supply",
    "supplies",
    "towel",
    "towels",
    "soap",
    "paper",
    "liner",
    "liners",
    "mop head",
    "mop heads",
    "bathroom cleaner",
    "cleaner refill",
) + tuple(load_brand_keywords("supply"))
LOSS_TERMS = (
    "missing",
    "lost",
    "gone",
    "went missing",
    "stolen",
    "theft",
    "shrinkage",
    "disappeared",
    "walked off",
    "can't find",
    "cannot find",
)
MOVEMENT_TERMS = (
    "took with me",
    "took it with me",
    "took them with me",
    "taken the other",
    "removed for",
    "moved to",
    "brought to",
    "for use at",
    "for next week",
)
ROUTINE_SUPPLY_NEED_TERMS = ("low", "out of", "running low", "restock", "need more", "reorder")
COMPLETION_SIGNAL_TERMS = (
    "pulled",
    "swept",
    "mopped",
    "emptied",
    "restocked",
    "vacuumed",
    "dusted",
    "wiped",
    "cleaned",
    "scrubbed",
    "done",
    "complete",
    "completed",
    "finished",
    "all good",
    "no issues",
    "qa",
    "trash pull",
    "qa trash pull",
)
REQUEST_PROBLEM_TERMS = (
    "need",
    "needs",
    "needed",
    "low",
    "out of",
    "out",
    "order",
    "reorder",
    "restock",
    "broken",
    "repair",
    "replace",
    "leak",
    "clog",
    "damage",
    "damaged",
    "safety",
    "issue",
    "problem",
    "missing",
    "lost",
    "stolen",
    "no-show",
    "no show",
    "late",
    "lockout",
    "locked out",
    "key",
    "code",
    'mess',
    'messy',
    'dirty',
    'filthy',
    'stain',
    'stained',
    'spill',
    'spilled',
    'spilling',
    'overflow',
    'overflowing',
    'smell',
    'smells',
    'smelly',
    'odor',
    'complaint',
    'complained',
    'complain',
    'jammed',
    'stuck',
    'clogged',
    'redo',
    'not clean',
    'still dirty',
    "wasn't",
    "isn't",
    "didn't",
    "won't",
)
ATTENDANCE_TERMS = ("late", "attendance", "no-show", "no show", "call off", "called off", "callout", "did not respond", "no response")
RETENTION_TERMS = ("retention", "quit", "quitting", "termination review", "replacement coverage", "may need replacement", "walk out")
REPLACE_INTENT_TERMS = (
    "replace",
    "replace him",
    "replace her",
    "replace them",
    "let him go",
    "let her go",
    "let go",
    "fire",
    "fired",
    "firing",
    "terminate",
    "terminated",
    "termination",
    "returned to his old ways",
    "returned to her old ways",
    "returned to their old ways",
    "old ways",
    "may need to replace",
    "need a replacement",
)
RECRUITING_TERMS = ("recruiting", "recruit", "hiring", "hire", "opening", "coverage")
REMOVE_FROM_SCHEDULE_TERMS = ("remove from schedule", "take off schedule", "off the schedule")
INACTIVE_STATUS_TERMS = ("inactive", "deactivate", "deactivated", "no longer", "turn account off", "turn accounts off", "turn employee off", "turn employees off")


@dataclass(frozen=True)
class FieldAudioTranscript:
    site_id: str
    upload_id: str
    area: str
    phase: str
    audio_asset_id: str
    source_transcript_path: Path
    raw_text: str
    person_id: str = ""
    captured_at: str = ""


@dataclass(frozen=True)
class CaptureSemanticInput:
    capture_id: str
    source_kind: str
    source_text: str
    site_id: str = ""
    site_label: str = ""
    selected_employees: list[dict[str, object]] | None = None
    source_transcript_path: Path | str | None = None
    submitter_person_id: str = ""
    captured_at: str = ""
    area: str = ""
    phase: str = ""
    audio_asset_id: str = ""


@dataclass(frozen=True)
class SemanticResult:
    cleaned_internal_note: str
    client_safe_note: str
    operational_summary: str
    issue_detected: bool
    issue_type: str
    urgency: str
    suggested_tags: list[str]
    action_candidates: list[str]
    visit_proposed: bool = False
    visit_type: str = ""
    submitter_person_id: str = ""
    extracted_actions: list[ExtractedAction] | None = None
    source_kind: str = ""
    capture_id: str = ""


CaptureSemanticResult = SemanticResult


def capture_input_from_field_audio(transcript: FieldAudioTranscript) -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id=transcript.upload_id,
        source_kind="field_capture_audio",
        source_text=transcript.raw_text,
        site_id=transcript.site_id,
        source_transcript_path=transcript.source_transcript_path,
        submitter_person_id=transcript.person_id,
        captured_at=transcript.captured_at,
        area=transcript.area,
        phase=transcript.phase,
        audio_asset_id=transcript.audio_asset_id,
    )


def _coerce_input(value: CaptureSemanticInput | FieldAudioTranscript) -> CaptureSemanticInput:
    if isinstance(value, CaptureSemanticInput):
        if value.source_kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {value.source_kind}")
        return value
    return capture_input_from_field_audio(value)


class RuleCaptureEngine:
    engine_name = "local-rule-semantic"

    def __call__(self, capture: CaptureSemanticInput | FieldAudioTranscript) -> SemanticResult:
        source = _coerce_input(capture)
        raw = collapse_whitespace(source.source_text)
        normalized, _corrections = normalize_text(raw)
        cleaned = clean_internal_note(normalized)
        issue_type = classify_issue(cleaned)
        qc_visit = classify_qc_visit(cleaned)
        service_completion = issue_type == "other" and classify_visit(cleaned)
        visit_proposed = qc_visit or service_completion
        visit_type = "qc_inspection" if qc_visit else ("service_completion" if service_completion else "")
        issue_detected = issue_type != "other" or any(
            word in cleaned.lower() for word in ("issue", "problem", "needs", "out", "water")
        )
        urgency = classify_urgency(cleaned, issue_type)
        actions = action_candidates_for(cleaned, issue_type)
        result = SemanticResult(
            cleaned_internal_note=cleaned,
            client_safe_note=client_safe_note(cleaned, issue_type),
            operational_summary=operational_summary(cleaned, issue_type, source.source_kind),
            issue_detected=issue_detected,
            issue_type=issue_type,
            urgency=urgency,
            suggested_tags=suggested_tags_for(issue_type, urgency, source.source_kind),
            action_candidates=actions,
            visit_proposed=visit_proposed,
            visit_type=visit_type,
            submitter_person_id=source.submitter_person_id,
            extracted_actions=_rule_extracted_actions(source, cleaned, issue_type, actions),
            source_kind=source.source_kind,
            capture_id=source.capture_id,
        )
        validate_semantic_result(result)
        return result


class LocalModelCaptureEngine:
    prompt_version: str = "capture-actions-v1"

    def __init__(self, client: object, fallback: Callable[[CaptureSemanticInput | FieldAudioTranscript], SemanticResult] | None = None) -> None:
        self._client = client
        self._fallback = fallback or RuleCaptureEngine()

    @property
    def engine_name(self) -> str:
        provider = str(getattr(self._client, "provider", "local"))
        model = str(getattr(self._client, "model", "unknown"))
        return f"{provider}:{model}:capture-semantics"

    def __call__(self, capture: CaptureSemanticInput | FieldAudioTranscript) -> SemanticResult:
        source = _coerce_input(capture)
        fallback_result = self._fallback(source)
        try:
            raw = self._client.generate_json(_build_semantic_prompt(source))
            if isinstance(raw, dict) and "extracted_actions" not in raw and "issue_type" in raw:
                legacy_result = _semantic_result_from_legacy_dict(raw, source)
                return replace(legacy_result, extracted_actions=fallback_result.extracted_actions)
            extracted = extracted_actions_from_model_payload(raw, source)
        except Exception:
            return fallback_result
        return replace(fallback_result, extracted_actions=extracted)


LocalRuleSemanticEngine = RuleCaptureEngine
LocalModelSemanticEngine = LocalModelCaptureEngine


def _build_semantic_prompt(capture: CaptureSemanticInput | FieldAudioTranscript) -> str:
    source = _coerce_input(capture)
    allowed_job_types = sorted(
        job_type
        for job_type in ALLOWED_JOB_TYPES
        if job_type
        in {
            "trigger_recruiting",
            "remove_from_schedule",
            "flag_access_constraint",
            "flag_retention_risk",
            "add_person",
            "visit_create",
            "append_to_note",
            "log_site_issue",
            "log_supply_need",
            "log_equipment_request",
            "log_personnel_event",
            "set_entity_status",
            "update_site_equipment",
        }
    )
    ctx = {
        "site_id": source.site_id, "site_label": source.site_label, "area": source.area,
        "selected_employees": source.selected_employees or [], "source_kind": source.source_kind,
    }
    return (
        "You extract reviewable ACTIONS from an operator's field-cleaning note as executable jobs.\n"
        "Return ONLY a JSON object (no prose, no markdown, no <think>): "
        '{"extracted_actions":[ <action>, ... ]}\n'
        "Emit MULTIPLE actions when the note has multiple distinct facts (e.g. a broken machine AND "
        "low supplies = 2). Never merge distinct facts.\n\n"
        "Each <action>: {\n"
        '  "action_key": snake_case label,\n'
        '  "job_type": one of ' + json.dumps(allowed_job_types) + ' (best executable job; "" only if none truly applies),\n'
        '  "target_type": "site"|"employee"|"general",\n'
        '  "target_label": site or employee name from context,\n'
        '  "summary": one concise sentence,\n'
        '  "source_excerpt": exact note span,\n'
        '  "payload_fields": object with ONLY the named real-world items, never lead-in words '
        '("a few things","a new"). For log_supply_need set item_name to the supply items only '
        '(e.g. "vacuum, lint brushes"); exclude any commentary or speech AFTER the items. For log_equipment_request set equipment_name. For '
        'log_site_issue set summary.\n'
        '  "evidence_terms": [keywords]\n}\n\n'
        "Routing: supply low/out/order/restock -> log_supply_need. equipment broken/repair/replace "
        "-> log_equipment_request. a site problem/damage/leak/safety/clog -> log_site_issue. "
        "lost/stolen/missing -> log_site_issue. attendance late/no-show -> log_personnel_event "
        "(ADD flag_retention_risk if replace/fire intent). access/lockout/key/code -> "
        "flag_access_constraint.\n\n"
        "Completion reports: if a fact describes routine work ALREADY DONE (e.g. trash pulled, "
        "QA trash pull, swept, mopped, emptied, restocked, vacuumed, dusted, wiped, cleaned, scrubbed, "
        "\"done\", \"complete\", \"finished\") and is NOT a new request or problem, emit NO action for "
        "it. If the entire note is only completion/acknowledgement reports, return "
        '{"extracted_actions": []}.\n\n'
        "Context: " + json.dumps(ctx) + "\nNote: " + source.source_text + "\n"
    )


def extracted_actions_from_model_payload(raw: object, source: CaptureSemanticInput) -> list[ExtractedAction]:
    if note_is_completion_report(source.source_text):
        return []
    if isinstance(raw, dict) and isinstance(raw.get("extracted_actions"), list):
        raw_actions = raw["extracted_actions"]
    else:
        raw_actions = raw
    if not isinstance(raw_actions, list):
        raise TypeError("model semantic response must be a JSON array")
    actions: list[ExtractedAction] = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        payload = _normalized_action_payload(item, source)
        try:
            action = validate_extracted_action(payload)
        except (TypeError, ValueError):
            continue
        excerpt = action.source_excerpt or source.source_text
        if note_is_completion_report(excerpt):
            continue
        action = action_with_operator_classification_override(action, source)
        actions.append(action_with_valid_proposed_queue_job(action, source))
    actions = supply_category_actions(actions, source)
    return actions


OPERATOR_CLASSIFICATION_OVERRIDE_JOB_TYPES = frozenset({JOB_LOG_SUPPLY_NEED, JOB_LOG_EQUIPMENT_REQUEST})
OPERATOR_PROPOSED_JOB_TYPES = frozenset(
    {
        JOB_APPEND_TO_NOTE,
        JOB_LOG_PERSONNEL_EVENT,
        JOB_LOG_EQUIPMENT_REQUEST,
        JOB_LOG_SITE_ISSUE,
        JOB_LOG_SUPPLY_NEED,
        JOB_FLAG_RETENTION_RISK,
        JOB_TRIGGER_RECRUITING,
        JOB_REMOVE_FROM_SCHEDULE,
        JOB_SET_ENTITY_STATUS,
    }
)
DEFAULT_REPORTED_BY = "operator"


def action_with_operator_classification_override(action: ExtractedAction, source: CaptureSemanticInput) -> ExtractedAction:
    if action.job_type not in OPERATOR_CLASSIFICATION_OVERRIDE_JOB_TYPES:
        return action
    parsed = operator_classification_for_action(action, source)
    if parsed is None:
        return action

    forced_job_type, declared_item_text = parsed
    payload_fields = payload_fields_with_operator_classification_item(
        action.payload_fields,
        forced_job_type,
        declared_item_text,
    )
    return replace(
        action,
        job_type=forced_job_type,
        payload_fields=payload_fields,
        proposed_queue_job=None,
        proposed_queue_job_error="",
    )


def operator_classification_for_action(action: ExtractedAction, source: CaptureSemanticInput) -> tuple[str, str] | None:
    for text in (action.source_excerpt, source.source_text):
        parsed = parse_operator_classification_token(text)
        if parsed is not None:
            return parsed
    return None


def parse_operator_classification_token(text: str) -> tuple[str, str] | None:
    match = OPERATOR_CLASSIFICATION_DECLARATION_RE.match(text or "")
    if not match:
        return None
    label = match.group("label").strip().lower()
    job_type = JOB_LOG_SUPPLY_NEED if label.startswith("suppl") else JOB_LOG_EQUIPMENT_REQUEST
    return job_type, operator_declared_item_text(match.group("item") or "", job_type)


def operator_declared_item_text(text: str, job_type: str) -> str:
    cleaned = collapse_whitespace(re.sub(r"\s*\n+\s*", ". ", text.strip())).strip(" ,.;:-")
    if not cleaned:
        return ""
    if job_type == JOB_LOG_SUPPLY_NEED:
        return clean_supply_item_phrase(cleaned)
    return cleaned


def payload_fields_with_operator_classification_item(
    payload_fields: dict[str, object] | None,
    job_type: str,
    declared_item_text: str,
) -> dict[str, object]:
    updated = normalized_payload_fields(payload_fields)
    if job_type == JOB_LOG_SUPPLY_NEED:
        item_text = declared_item_text or payload_text(
            updated,
            "item_name",
            "supply_item",
            "supply_items",
            "item",
            "items",
            "equipment_name",
            "equipment",
        )
        updated.pop("equipment_name", None)
        updated.pop("equipment", None)
        if item_text:
            updated["item_name"] = item_text
    elif job_type == JOB_LOG_EQUIPMENT_REQUEST:
        item_text = declared_item_text or payload_text(updated, "equipment_name", "equipment", "item", "item_name")
        updated.pop("item_name", None)
        updated.pop("supply_item", None)
        updated.pop("supply_items", None)
        if item_text:
            updated["equipment_name"] = item_text
    return updated


def action_with_valid_proposed_queue_job(action: ExtractedAction, source: CaptureSemanticInput) -> ExtractedAction:
    try:
        proposed = proposed_queue_job_for_action(action, source)
    except CouchDBRegistryError:
        return replace(action, proposed_queue_job=None, proposed_queue_job_error="site_registry_unavailable")
    except Exception as exc:
        return replace(action, proposed_queue_job=None, proposed_queue_job_error=f"builder_error:{type(exc).__name__}")
    if proposed is None:
        return replace(action, proposed_queue_job_error="")
    return replace(action, proposed_queue_job=proposed, proposed_queue_job_error="")


def proposed_queue_job_for_action(action: ExtractedAction, source: CaptureSemanticInput) -> dict[str, object] | None:
    if isinstance(action.proposed_queue_job, dict):
        proposed = dict(action.proposed_queue_job)
        if validate_job(proposed):
            return proposed

    job_type = action.job_type.strip()
    if job_type not in OPERATOR_PROPOSED_JOB_TYPES:
        return None

    payload_fields = normalized_payload_fields(action.payload_fields)
    if job_type == JOB_LOG_PERSONNEL_EVENT:
        payload = log_personnel_event_payload(action, source, payload_fields)
    elif job_type == JOB_FLAG_RETENTION_RISK:
        payload = flag_retention_risk_payload(action, source, payload_fields)
    elif job_type == JOB_TRIGGER_RECRUITING:
        payload = trigger_recruiting_payload(action, source, payload_fields)
    elif job_type == JOB_REMOVE_FROM_SCHEDULE:
        payload = remove_from_schedule_payload(action, source, payload_fields)
    elif job_type == JOB_SET_ENTITY_STATUS:
        payload = set_entity_status_payload(action, source, payload_fields)
    elif job_type == JOB_LOG_SITE_ISSUE:
        payload = log_site_issue_payload(action, source, payload_fields)
    elif job_type == JOB_LOG_SUPPLY_NEED:
        payload = log_supply_need_payload(action, source, payload_fields)
    elif job_type == JOB_LOG_EQUIPMENT_REQUEST:
        payload = log_equipment_request_payload(action, source, payload_fields)
    elif job_type == JOB_APPEND_TO_NOTE:
        payload = append_to_note_payload(action, source, payload_fields)
    else:
        return None

    proposed = {"job_type": job_type, "payload": payload}
    if validate_job(proposed):
        return proposed
    return None


def normalized_payload_fields(value: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): field for key, field in value.items() if isinstance(key, str)}


def payload_text(payload_fields: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = payload_fields.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def action_text(action: ExtractedAction) -> str:
    return " ".join(
        value
        for value in (
            action.action_key,
            action.summary,
            action.rationale,
            action.source_excerpt,
            " ".join(action.evidence_terms),
        )
        if value
    )


def action_employee_value(action: ExtractedAction, payload_fields: dict[str, object]) -> str:
    explicit = payload_text(payload_fields, "employee", "employee_name", "entity_id")
    if explicit:
        return explicit
    if action.target_type != "employee":
        return ""
    return action.target_label or action.target_id


def source_site_value(source: CaptureSemanticInput, payload_fields: dict[str, object], *keys: str) -> str:
    explicit = payload_text(payload_fields, *keys)
    if explicit:
        return explicit
    return (source.site_label or source.site_id).strip()


def captured_at_value(source: CaptureSemanticInput, payload_fields: dict[str, object], *keys: str) -> str:
    explicit = payload_text(payload_fields, *keys)
    if explicit:
        return explicit
    return source.captured_at.strip()


def reported_by_value(source: CaptureSemanticInput, payload_fields: dict[str, object]) -> str:
    return payload_text(payload_fields, "reported_by", "operator", "requested_by") or source.submitter_person_id.strip() or DEFAULT_REPORTED_BY


def details_value(action: ExtractedAction, payload_fields: dict[str, object]) -> str:
    return payload_text(payload_fields, "details", "summary", "reason", "notes") or action.source_excerpt or action.summary


def log_personnel_event_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in payload_fields.items() if key in LOG_PERSONNEL_EVENT_ALLOWED_PAYLOAD_FIELDS}
    if validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": payload}):
        return payload
    if not payload_text(payload, "employee"):
        payload["employee"] = action_employee_value(action, payload_fields)
    if not payload_text(payload, "event_type"):
        payload["event_type"] = personnel_event_type(action)
    if not payload_text(payload, "summary"):
        payload["summary"] = action.summary or action.source_excerpt
    if not payload_text(payload, "occurred_at"):
        payload["occurred_at"] = captured_at_value(source, payload_fields, "occurred_at", "observed_at")
    if not payload_text(payload, "reported_by"):
        payload["reported_by"] = reported_by_value(source, payload_fields)
    if not payload_text(payload, "related_site"):
        related_site = source_site_value(source, payload_fields, "related_site", "site")
        if related_site:
            payload["related_site"] = related_site
    if not payload_text(payload, "source") and source.source_kind:
        payload["source"] = source.source_kind
    if "severity" not in payload:
        severity = personnel_event_severity(action)
        if severity:
            payload["severity"] = severity
    return payload


def flag_retention_risk_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = dict(payload_fields)
    if payload_text(payload, "employee") and validate_job({"job_type": JOB_FLAG_RETENTION_RISK, "payload": payload}):
        return payload
    if not payload_text(payload, "employee"):
        employee = action_employee_value(action, payload_fields)
        if employee:
            payload["employee"] = employee
        else:
            payload.pop("employee", None)
    if not payload_text(payload, "site"):
        payload["site"] = source_site_value(source, payload_fields, "site", "site_id", "related_site")
    if not payload_text(payload, "details"):
        payload["details"] = details_value(action, payload_fields)
    return payload


def trigger_recruiting_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = dict(payload_fields)
    if validate_job({"job_type": JOB_TRIGGER_RECRUITING, "payload": payload}):
        return payload
    if not payload_text(payload, "site"):
        payload["site"] = source_site_value(source, payload_fields, "site", "site_id", "related_site")
    if not payload_text(payload, "priority"):
        payload["priority"] = recruiting_priority(action)
    if not payload_text(payload, "details"):
        payload["details"] = details_value(action, payload_fields)
    return payload


def remove_from_schedule_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = dict(payload_fields)
    if validate_job({"job_type": JOB_REMOVE_FROM_SCHEDULE, "payload": payload}):
        return payload
    if not payload_text(payload, "employee"):
        payload["employee"] = action_employee_value(action, payload_fields)
    if not payload_text(payload, "site"):
        payload["site"] = source_site_value(source, payload_fields, "site", "site_id", "related_site")
    if not payload_text(payload, "details"):
        details = details_value(action, payload_fields)
        if details:
            payload["details"] = details
    return payload


def set_entity_status_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in payload_fields.items() if key in SET_ENTITY_STATUS_ALLOWED_PAYLOAD_FIELDS}
    if validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}):
        return payload
    entity_type = payload_text(payload, "entity_type") or (action.target_type if action.target_type in {"site", "employee"} else "")
    if entity_type:
        payload["entity_type"] = entity_type
    if not payload_text(payload, "entity_id"):
        if entity_type == "site":
            payload["entity_id"] = source.site_id or source.site_label or action.target_id or action.target_label
        elif entity_type == "employee":
            payload["entity_id"] = action_employee_value(action, payload_fields)
    if not payload_text(payload, "status"):
        status = entity_status(action)
        if status:
            payload["status"] = status
    if not payload_text(payload, "reason"):
        payload["reason"] = action.summary or action.source_excerpt
    if not payload_text(payload, "source"):
        payload["source"] = source.source_kind or "capture_semantics"
    if "observed_at" not in payload:
        observed_at = date_from_timestamp(source.captured_at)
        if observed_at:
            payload["observed_at"] = observed_at
    if "details" not in payload and action.source_excerpt and action.source_excerpt != payload.get("reason"):
        payload["details"] = action.source_excerpt
    return payload


def log_site_issue_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in payload_fields.items() if key in LOG_SITE_ISSUE_ALLOWED_PAYLOAD_FIELDS}
    site_id = resolved_site_id_for_action(action, source, payload_fields)
    if site_id:
        payload["site_id"] = site_id
    else:
        payload.pop("site_id", None)
    if not payload_text(payload, "title"):
        payload["title"] = site_issue_title(action, source)
    if not payload_text(payload, "summary"):
        payload["summary"] = action.source_excerpt or action.summary
    if not payload_text(payload, "reported_by"):
        payload["reported_by"] = reported_by_value(source, payload_fields)
    if "client_notified" not in payload or not isinstance(payload.get("client_notified"), bool):
        payload["client_notified"] = False
    if not payload_text(payload, "category"):
        payload["category"] = "supply"
    if not payload_text(payload, "priority"):
        payload["priority"] = site_issue_priority(action)
    if not payload_text(payload, "resolution_trigger"):
        payload["resolution_trigger"] = "operator confirms item recovered or replaced"
    if not payload_text(payload, "source"):
        payload["source"] = source.source_kind or "capture_semantics"
    if "related_capture_ids" not in payload and source.capture_id:
        payload["related_capture_ids"] = [source.capture_id]
    observed_at = captured_at_value(source, payload_fields, "observed_at")
    if observed_at and not payload_text(payload, "observed_at"):
        payload["observed_at"] = observed_at
    return payload


def log_supply_need_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in payload_fields.items() if key in LOG_SUPPLY_NEED_ALLOWED_PAYLOAD_FIELDS}
    site_id = resolved_site_id_for_action(action, source, payload_fields)
    if site_id:
        payload["site_id"] = site_id
    else:
        payload.pop("site_id", None)
    if not payload_text(payload, "item_name"):
        payload["item_name"] = supply_item_name_for_action(action, source, payload_fields)
    if not payload_text(payload, "requested_by"):
        payload["requested_by"] = reported_by_value(source, payload_fields)
    if not payload_text(payload, "source"):
        payload["source"] = source.source_kind or "capture_semantics"
    if "related_capture_ids" not in payload and source.capture_id:
        payload["related_capture_ids"] = [source.capture_id]
    observed_at = captured_at_value(source, payload_fields, "observed_at")
    if observed_at and not payload_text(payload, "observed_at"):
        payload["observed_at"] = observed_at
    if not payload_text(payload, "notes"):
        payload["notes"] = action.source_excerpt or action.summary
    return payload


def log_equipment_request_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    payload = {key: value for key, value in payload_fields.items() if key in LOG_EQUIPMENT_REQUEST_ALLOWED_PAYLOAD_FIELDS}
    site_id = resolved_site_id_for_action(action, source, payload_fields)
    if site_id:
        payload["site_id"] = site_id
    else:
        payload.pop("site_id", None)
    if not payload_text(payload, "equipment_name"):
        payload["equipment_name"] = equipment_name_for_action(action, source, payload_fields)
    if not payload_text(payload, "requested_by"):
        payload["requested_by"] = reported_by_value(source, payload_fields)
    if not payload_text(payload, "source"):
        payload["source"] = source.source_kind or "capture_semantics"
    if "related_capture_ids" not in payload and source.capture_id:
        payload["related_capture_ids"] = [source.capture_id]
    observed_at = captured_at_value(source, payload_fields, "observed_at")
    if observed_at and not payload_text(payload, "observed_at"):
        payload["observed_at"] = observed_at
    if not payload_text(payload, "notes"):
        payload["notes"] = action.source_excerpt or action.summary
    return payload


def append_to_note_payload(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> dict[str, object]:
    note_path = resolved_site_note_path_for_source(source, payload_fields)
    payload: dict[str, object] = {}
    if note_path:
        payload["path"] = note_path
    content = payload_text(payload_fields, "content", "details", "summary", "notes") or action.source_excerpt or action.summary
    if content:
        payload["content"] = content
    payload["destination"] = "site_note"
    return payload


def resolved_site_id_for_action(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> str:
    candidates = (
        payload_text(payload_fields, "site_id"),
        payload_text(payload_fields, "site", "related_site"),
        source.site_id.strip(),
        source.site_label.strip(),
        action.target_id.strip(),
        action.target_label.strip(),
    )
    for candidate in candidates:
        if not candidate:
            continue
        site_id = resolve_site_id(candidate)
        if site_id:
            return site_id
    return ""


def resolved_site_note_path_for_source(source: CaptureSemanticInput, payload_fields: dict[str, object]) -> str:
    candidates = (
        payload_text(payload_fields, "site", "site_label", "related_site"),
        source.site_label.strip(),
        source.site_id.strip(),
    )
    for candidate in candidates:
        if not candidate:
            continue
        note_path = resolve_site_note_path(candidate)
        if note_path:
            return note_path
    return ""


def site_issue_title(action: ExtractedAction, source: CaptureSemanticInput) -> str:
    site = source.site_label or action.target_label or source.site_id
    suffix = f" at {site}" if site else ""
    return f"Equipment or supply loss reported{suffix}"


def site_issue_priority(action: ExtractedAction) -> str:
    text = action_text(action).lower()
    if any(term in text for term in ("urgent", "immediate", "emergency", "critical")):
        return "urgent"
    if any(term in text for term in ("missing", "lost", "gone", "stolen", "theft", "shrinkage", "disappeared", "walked off")):
        return "high"
    return "normal"


def personnel_event_type(action: ExtractedAction) -> str:
    text = action_text(action).lower()
    if any(term in text for term in ("late", "attendance", "call off", "called off", "callout", "no-show", "no show", "did not respond", "no response")):
        return "attendance"
    if any(term in text for term in ("performance", "complaint")):
        return "performance"
    if "accommodation" in text:
        return "accommodation"
    if any(term in text for term in ("disciplinary", "warning")):
        return "disciplinary"
    if any(term in text for term in ("recognition", "compliment")):
        return "recognition"
    if "incident" in text:
        return "incident"
    return "other" if "other" in PERSONNEL_EVENT_TYPES else sorted(PERSONNEL_EVENT_TYPES)[0]


def personnel_event_severity(action: ExtractedAction) -> str:
    text = action_text(action).lower()
    if "final warning" in text:
        return "final_warning"
    if "written warning" in text:
        return "written_warning"
    if "verbal warning" in text or "warning" in text:
        return "verbal_warning"
    if any(term in text for term in ("separation", "terminated", "fired")):
        return "separation"
    if any(term in text for term in ("concern", "complaint", "incident", "disciplinary", "no-show", "no show")):
        return "concern"
    return "info"


def recruiting_priority(action: ExtractedAction) -> str:
    text = action_text(action).lower()
    if any(term in text for term in ("urgent", "asap", "immediate", "emergency", "critical", "today", "short staffed")):
        return "urgent"
    return "normal"


def entity_status(action: ExtractedAction) -> str:
    text = action_text(action).lower()
    if any(term in text for term in ("inactive", "deactivate", "deactivated", "no longer", "set_inactive", "off schedule")):
        return "inactive"
    if any(term in text for term in ("active", "activate", "reactivate", "set_active")):
        return "active"
    return ""


def date_from_timestamp(value: str) -> str:
    text = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return ""


def supply_category_actions(actions: list[ExtractedAction], source: CaptureSemanticInput) -> list[ExtractedAction]:
    if not is_supply_area(source.area):
        return actions
    if supply_loss_or_movement_text(source.source_text):
        return actions
    if any(action.job_type == JOB_LOG_SITE_ISSUE for action in actions):
        return actions
    supply_action = None
    for action in actions:
        if action.job_type == JOB_LOG_SUPPLY_NEED:
            supply_action = action
            break
        text = action_text(action).lower()
        if action.action_key == "supply_review" or action.job_type == "" and ("supply" in text or "restock" in text):
            supply_action = action
            break
    raw_action = supply_need_action_payload(source, source.source_text, "model_supply_category", base=supply_action)
    try:
        action = validate_extracted_action(_normalized_action_payload(raw_action, source))
    except (TypeError, ValueError):
        return actions
    structured = action_with_valid_proposed_queue_job(action, source)
    replaced = False
    updated: list[ExtractedAction] = []
    for existing in actions:
        if existing is supply_action:
            updated.append(structured)
            replaced = True
        else:
            updated.append(existing)
    if not replaced:
        updated.append(structured)
    return updated


def is_supply_area(area: str) -> bool:
    return "supply" in _norm(area)


def supply_loss_or_movement_text(text: str) -> bool:
    supply_equipment_terms = _matched_supply_equipment_terms(text)
    loss_terms = _matched_loss_terms(text) if supply_equipment_terms else []
    movement_terms = _matched_movement_terms(text)
    return bool(loss_terms or (movement_terms and (supply_equipment_terms or _movement_has_implied_item(text, movement_terms))))


def supply_need_action_payload(
    source: CaptureSemanticInput,
    cleaned: str,
    candidate_type: str,
    *,
    base: ExtractedAction | None = None,
) -> dict[str, object]:
    item_name = supply_item_name_from_text(cleaned)
    summary = f"Supply request: {item_name}."
    return {
        "action_key": "log_supply_need",
        "candidate_type": candidate_type,
        "job_type": JOB_LOG_SUPPLY_NEED,
        "target_type": "site",
        "target_label": source.site_label or (base.target_label if base else ""),
        "summary": summary,
        "rationale": "Operator-selected supply area is a hard signal for a supply need.",
        "confidence": base.confidence if base else "normal",
        "source_excerpt": base.source_excerpt if base and base.source_excerpt else cleaned,
        "evidence_terms": ["supply", source.area.strip()] if source.area.strip() else ["supply"],
        "payload_fields": {
            "item_name": item_name,
            "notes": base.source_excerpt if base and base.source_excerpt else cleaned,
        },
    }


def supply_item_name_for_action(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> str:
    return (
        payload_text(payload_fields, "item_name", "supply_item", "supply_items", "item", "items")
        or supply_item_name_from_text(action.source_excerpt)
        or supply_item_name_from_text(source.source_text)
        or action.summary
        or action.source_excerpt
    )


def equipment_name_for_action(action: ExtractedAction, source: CaptureSemanticInput, payload_fields: dict[str, object]) -> str:
    return (
        payload_text(payload_fields, "equipment_name", "equipment", "item", "item_name")
        or action.summary
        or action.source_excerpt
    ).strip()


def supply_item_name_from_text(text: str) -> str:
    cleaned = clean_supply_item_phrase(text)
    lowered = cleaned.lower()
    patterns = (
        r"\b(?:need(?: more)?|needs(?: more)?|needed|low on|running low on|out of|restock|reorder|order|bring)\s+(?:some\s+|the\s+|a\s+|an\s+)?(?P<items>.+)",
        r"(?P<items>.+?)\s+\b(?:are|is)\s+(?:low|out|running low|needed|missing from order)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = clean_supply_item_phrase(match.group("items"))
        if candidate:
            return candidate
    return cleaned


def _supply_boundary_prefix_is_preamble(text: str) -> bool:
    return bool(
        re.match(
            r"^(?:to\s+)?(?:order|reorder|get|bring|need(?:s|ed)?|low on|running low on|out of)\s+"
            r"(?:(?:a|an|the|some|several|few|a few|couple of|a couple of)\s+)?"
            r"(?:things|items|stuff|products)\s*$",
            text,
            flags=re.IGNORECASE,
        )
        or re.match(
            r"^(?:(?:a|an|the|some|several|few|a few|couple of|a couple of)\s+)?"
            r"(?:things|items|stuff|products)\s*$",
            text,
            flags=re.IGNORECASE,
        )
    )


def _truncate_supply_sentence_tail(text: str) -> str:
    match = re.search(r"[.;]\s+(?=\S*\w)", text)
    if not match:
        return text
    prefix = text[: match.start()].strip(" ,")
    suffix = text[match.end() :].strip(" ,")
    if prefix and not _supply_boundary_prefix_is_preamble(prefix):
        return prefix
    return suffix or prefix or text


def _strip_supply_commentary_tail(text: str) -> str:
    patterns = (
        r"(?P<items>.+?)\s*(?:,|\s)\b(?:so|then|but|because|i|we|you|that|which|here|please)\b.+$",
        r"(?P<items>.+?)\s*(?:,|\s)\b(?:and|also)\s+"
        r"(?=(?:so|then|but|because|i|we|you|that|which|here|please|would|want|like|need|needs|needed|can|could|should|will|just)\b).+$",
    )
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match and match.group("items").strip(" ,"):
            return match.group("items").strip(" ,")
    return text


def clean_supply_item_phrase(text: str) -> str:
    cleaned = re.sub(r"\s*\n+\s*", ". ", text.strip())
    cleaned = collapse_whitespace(cleaned)
    cleaned = cleaned.rstrip(".!?;:")
    prior = cleaned
    cleaned = re.sub(r"^(?:please\s+)?(?:we\s+|we're\s+|we are\s+|i\s+|i'm\s+|i am\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:please|thanks|thank you)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\b(?:at|for)\s+.+$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+\b(?:are|is)\s+(?:low|out|running low|needed)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:need(?: more)?|needs(?: more)?|needed|low on|running low on|out of|restock|reorder|order|bring)\s+", "", cleaned, flags=re.IGNORECASE)
    preamble_stripped = re.sub(
        r"^(?:(?:a|few|a few|couple of|a couple of|some|several)\s+)?(?:things|items|stuff|supplies|products)\b[\s.,;:-]+(?=\S)",
        "",
        cleaned,
        count=1,
        flags=re.IGNORECASE,
    )
    if preamble_stripped.strip():
        cleaned = preamble_stripped
    cleaned = _truncate_supply_sentence_tail(cleaned)
    cleaned = _strip_supply_commentary_tail(cleaned)
    article_stripped = re.sub(r"^(?:(?:a|an|the|some|a new|an new|new)\s+)+", "", cleaned, flags=re.IGNORECASE)
    if article_stripped.strip():
        cleaned = article_stripped
    cleaned = cleaned.replace(" and ", ", ")
    cleaned = collapse_whitespace(cleaned).strip(" ,")
    return cleaned or collapse_whitespace(prior).strip(" ,")


def _semantic_result_from_legacy_dict(raw: dict[str, object], source: CaptureSemanticInput) -> SemanticResult:
    normalized = _normalize_legacy_payload(raw)
    result = SemanticResult(
        cleaned_internal_note=_required_str_or_empty(normalized, "cleaned_internal_note"),
        client_safe_note=_required_str_or_empty(normalized, "client_safe_note"),
        operational_summary=_required_str_or_empty(normalized, "operational_summary"),
        issue_detected=_required_bool(normalized, "issue_detected"),
        issue_type=_required_str_or_empty(normalized, "issue_type"),
        urgency=_required_str_or_empty(normalized, "urgency"),
        suggested_tags=_required_str_list(normalized, "suggested_tags"),
        action_candidates=_required_str_list(normalized, "action_candidates"),
        visit_proposed=_required_bool(normalized, "visit_proposed"),
        visit_type=_required_str_or_empty(normalized, "visit_type"),
        submitter_person_id=source.submitter_person_id,
        extracted_actions=[],
        source_kind=source.source_kind,
        capture_id=source.capture_id,
    )
    result = strip_non_field_operation_actions(result)
    validate_semantic_result(result)
    return result


def _normalize_legacy_payload(raw: dict[str, object]) -> dict[str, object]:
    normalized = dict(raw)
    issue_type = normalized.get("issue_type")
    if isinstance(issue_type, str):
        value = issue_type.strip().lower().replace("-", "_").replace(" ", "_")
        issue_aliases = {
            "supply": "supplies",
            "supplies_needed": "supplies",
            "equipment": "other",
            "moisture": "water",
            "leak": "water",
            "leaking": "water",
            "cleaning_quality": "quality",
            "request": "client_request",
            "none": "other",
            "unknown": "other",
            "general": "other",
        }
        normalized["issue_type"] = issue_aliases.get(value, value)
    urgency = normalized.get("urgency")
    if isinstance(urgency, str):
        value = urgency.strip().lower()
        normalized["urgency"] = {"medium": "normal", "med": "normal"}.get(value, value)
    for key in ("cleaned_internal_note", "client_safe_note", "operational_summary", "visit_type"):
        if normalized.get(key) is None:
            normalized[key] = ""
    for key in ("suggested_tags", "action_candidates"):
        if normalized.get(key) is None:
            normalized[key] = []
    return normalized


def _required_str_or_empty(raw: dict[str, object], key: str) -> str:
    if key not in raw:
        raise ValueError(f"model semantic response missing field: {key}")
    value = raw[key]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be str, got {type(value).__name__}")
    return value


def _required_bool(raw: dict[str, object], key: str) -> bool:
    if key not in raw:
        raise ValueError(f"model semantic response missing field: {key}")
    value = raw[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be bool, got {type(value).__name__}")
    return value


def _required_str_list(raw: dict[str, object], key: str) -> list[str]:
    if key not in raw:
        raise ValueError(f"model semantic response missing field: {key}")
    value = raw[key]
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of str")
    return value


def _normalized_action_payload(item: dict[str, object], source: CaptureSemanticInput) -> dict[str, object]:
    payload = dict(item)
    payload["source_kind"] = str(payload.get("source_kind") or source.source_kind)
    payload["target_type"] = _normalized_target_type(payload.get("target_type"))
    job_type = str(payload.get("job_type") or "").strip()
    if job_type and job_type not in ALLOWED_JOB_TYPES:
        payload["job_type"] = ""
    if not str(payload.get("candidate_type") or "").strip():
        payload["candidate_type"] = "field_capture_follow_up"
    if job_type == JOB_UPDATE_SITE_EQUIPMENT and is_attribute_less_loss_or_movement_action(payload):
        payload["job_type"] = ""
        payload.pop("proposed_queue_job", None)
        payload.pop("approval_metadata", None)
        payload.pop("proposed_queue_job_error", None)
    proposed_queue_job_error = str(payload.get("proposed_queue_job_error") or "").strip()
    if proposed_queue_job_error:
        payload["proposed_queue_job_error"] = proposed_queue_job_error
    else:
        payload.pop("proposed_queue_job_error", None)
    resolved = resolve_action_target(payload, source)
    payload.update(resolved)
    if payload.get("rationale") is None:
        payload["rationale"] = ""
    if payload.get("confidence") is None:
        payload["confidence"] = "unknown"
    return payload


def resolve_action_target(action: dict[str, object], source: CaptureSemanticInput) -> dict[str, str]:
    target_type = _normalized_target_type(action.get("target_type"))
    target_label = str(action.get("target_label") or "").strip()
    source_excerpt = str(action.get("source_excerpt") or "").strip()
    if target_type == "site":
        return _resolve_site_target(target_label, source_excerpt, source)
    if target_type == "employee":
        return _resolve_employee_target(target_label, source_excerpt, source)
    return {"target_type": "general", "target_id": "", "target_label": target_label}


def is_attribute_less_loss_or_movement_action(action: dict[str, object]) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            action.get("action_key"),
            action.get("candidate_type"),
            action.get("summary"),
            action.get("source_excerpt"),
            " ".join(action.get("evidence_terms") or []) if isinstance(action.get("evidence_terms"), list) else "",
        )
    )
    movement_terms = _matched_movement_terms(text)
    if not (_matched_loss_terms(text) or movement_terms):
        return False
    if not (_matched_supply_equipment_terms(text) or _movement_has_implied_item(text, movement_terms)):
        return False
    payload_fields = action.get("payload_fields")
    if not isinstance(payload_fields, dict):
        return True
    attribute_keys = {"brand", "color", "description", "serial_number", "model", "status"}
    return not any(str(key) in attribute_keys and str(value or "").strip() for key, value in payload_fields.items())


def _resolve_site_target(target_label: str, source_excerpt: str, source: CaptureSemanticInput) -> dict[str, str]:
    haystack = _norm(" ".join([target_label, source_excerpt]))
    site_label = str(source.site_label or "").strip()
    site_id = str(source.site_id or "").strip()
    if site_id and site_id in {target_label, source_excerpt.strip()}:
        return {"target_type": "site", "target_id": site_id, "target_label": site_label or target_label or site_id}
    if site_label and (_norm(site_label) in haystack or _norm(target_label) == _norm(site_label)):
        return {"target_type": "site", "target_id": site_id, "target_label": site_label}
    if site_id or site_label:
        return {"target_type": "site", "target_id": site_id, "target_label": target_label or site_label or site_id}
    return {"target_type": "site", "target_id": "", "target_label": target_label}


def _resolve_employee_target(target_label: str, source_excerpt: str, source: CaptureSemanticInput) -> dict[str, str]:
    employees = [_employee_identity(value) for value in source.selected_employees or []]
    employees = [employee for employee in employees if employee["label"] or employee["id"]]
    if not employees:
        return {"target_type": "employee", "target_id": "", "target_label": target_label}
    haystack = _norm(" ".join([target_label, source_excerpt]))
    matches: list[dict[str, str]] = []
    for employee in employees:
        terms = [employee["id"], employee["slug"], employee["label"], employee["name"], employee["first"], employee["last"]]
        if any(_contains_term(haystack, term) for term in terms):
            matches.append(employee)
    unique = _unique_employee_matches(matches)
    if len(unique) == 1:
        employee = unique[0]
        return {"target_type": "employee", "target_id": employee["id"], "target_label": employee["label"] or target_label}
    if len(employees) == 1 and not target_label and not source_excerpt:
        employee = next(iter(employees))
        return {"target_type": "employee", "target_id": employee["id"], "target_label": employee["label"]}
    return {"target_type": "employee", "target_id": "", "target_label": target_label}


def _unique_employee_matches(matches: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for employee in matches:
        key = employee["id"] or _norm(employee["label"])
        by_key[key] = employee
    return list(by_key.values())


def _employee_identity(value: dict[str, object]) -> dict[str, str]:
    employee_id = str(value.get("id") or value.get("slug") or value.get("_id") or "").strip()
    slug = str(value.get("slug") or employee_id).strip()
    first = str(value.get("preferred_name") or value.get("first") or "").strip()
    last = str(value.get("last") or value.get("last_name") or "").strip()
    name = str(value.get("name") or "").strip() or f"{first} {last}".strip()
    label = str(value.get("label") or name or employee_id).strip()
    if not first and name:
        parts = name.split()
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else last
    return {"id": employee_id, "slug": slug, "first": first, "last": last, "name": name, "label": label}


def _contains_term(haystack: str, term: str) -> bool:
    normalized = _norm(term)
    if not normalized:
        return False
    if " " in normalized or "-" in normalized or "_" in normalized:
        return normalized in haystack
    return re.search(rf"\b{re.escape(normalized)}\b", haystack) is not None


def note_is_completion_report(text: str) -> bool:
    haystack = _norm(text)
    if not haystack:
        return False
    has_completion_signal = any(_contains_term(haystack, term) for term in COMPLETION_SIGNAL_TERMS)
    has_request_problem_signal = any(_contains_term(haystack, term) for term in REQUEST_PROBLEM_TERMS)
    return has_completion_signal and not has_request_problem_signal


def _normalized_target_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in {"employee", "site", "general"} else "general"


def _rule_extracted_actions(source: CaptureSemanticInput, cleaned: str, issue_type: str, actions: list[str]) -> list[ExtractedAction]:
    if is_unclear_or_test_audio(cleaned) or note_is_completion_report(cleaned) or not cleaned.strip():
        return []
    lowered = cleaned.lower()
    raw_actions: list[dict[str, object]] = []
    if source.source_kind == "voice_memo":
        raw_actions.extend(_rule_voice_memo_operator_actions(source, cleaned))
    supply_equipment_terms = _matched_supply_equipment_terms(cleaned)
    loss_terms = _matched_loss_terms(cleaned) if supply_equipment_terms else []
    movement_terms = _matched_movement_terms(cleaned)
    routine_terms = _matched_routine_supply_need_terms(cleaned) if supply_equipment_terms else []
    handled_supply_equipment = False
    if loss_terms:
        raw_actions.append(
            _rule_site_action(
                source,
                "supply_equipment_loss",
                "Review equipment or supply loss/theft.",
                cleaned,
                [*loss_terms, *supply_equipment_terms],
                job_type=JOB_LOG_SITE_ISSUE,
                candidate_type="supply_equipment_loss",
                confidence="normal",
            )
        )
        handled_supply_equipment = True
    if movement_terms and (supply_equipment_terms or _movement_has_implied_item(cleaned, movement_terms)):
        raw_actions.append(
            _rule_site_action(
                source,
                "supply_equipment_movement",
                "Record equipment or supply movement in the site note.",
                cleaned,
                [*movement_terms, *supply_equipment_terms],
                job_type=JOB_APPEND_TO_NOTE,
                candidate_type="supply_equipment_movement",
                confidence="normal",
            )
        )
        handled_supply_equipment = True
    if is_supply_area(source.area) and not handled_supply_equipment:
        raw_actions.append(supply_need_action_payload(source, cleaned, "field_capture_supply_need"))
        handled_supply_equipment = True
    elif issue_type == "supplies" and not handled_supply_equipment:
        raw_actions.append(_rule_site_action(source, "supply_review", "Review supply/order follow-up.", cleaned, ["supply", *routine_terms]))
        handled_supply_equipment = True
    if any(term in lowered for term in EQUIPMENT_TERMS) and not handled_supply_equipment:
        raw_actions.append(
            _rule_site_action(
                source,
                "equipment_review",
                "Review equipment follow-up.",
                cleaned,
                [term for term in EQUIPMENT_TERMS if term in lowered],
            )
        )
    if any(term in lowered for term in ATTENDANCE_TERMS):
        raw_actions.append(
            _rule_employee_action(
                source,
                "personnel_attendance",
                "personnel_attendance",
                "log_personnel_event",
                "Review personnel attendance follow-up.",
                cleaned,
                [term for term in ATTENDANCE_TERMS if term in lowered],
            )
        )
    retention_terms = _matched_retention_terms(cleaned)
    if retention_terms:
        raw_actions.append(
            _rule_employee_action(
                source,
                "retention_risk",
                "retention_risk",
                "flag_retention_risk",
                "Review retention or coverage risk.",
                cleaned,
                retention_terms,
            )
        )
    if issue_type in {"water", "access", "damage", "safety", "quality", "client_request"} and not raw_actions:
        raw_actions.append(_rule_site_action(source, f"{issue_type}_review", actions[0] if actions else GENERIC_REVIEW_ACTION, cleaned, [issue_type]))
    extracted: list[ExtractedAction] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_actions:
        payload = _normalized_action_payload(raw, source)
        key = (str(payload.get("action_key")), str(payload.get("target_type")), str(payload.get("summary")))
        if key in seen:
            continue
        seen.add(key)
        try:
            action = validate_extracted_action(payload)
        except (TypeError, ValueError):
            continue
        extracted.append(action_with_valid_proposed_queue_job(action, source))
    return extracted


def _matched_supply_equipment_terms(text: str) -> list[str]:
    haystack = _norm(text)
    terms = [term for term in (*EQUIPMENT_TERMS, *SUPPLY_ITEM_TERMS) if _contains_term(haystack, term)]
    return list(dict.fromkeys(terms))


def _matched_loss_terms(text: str) -> list[str]:
    haystack = _norm(text)
    terms = [term for term in LOSS_TERMS if _contains_term(haystack, term)]
    return list(dict.fromkeys(terms))


def _matched_movement_terms(text: str) -> list[str]:
    lowered = text.lower()
    haystack = _norm(text)
    terms = [term for term in MOVEMENT_TERMS if _contains_term(haystack, term)]
    if re.search(r"\b(?:took|taken)\b.{0,80}\bwith me\b", lowered):
        terms.append("took with me")
    return list(dict.fromkeys(terms))


def _movement_has_implied_item(text: str, movement_terms: list[str]) -> bool:
    lowered = text.lower()
    if "taken the other" in movement_terms:
        return True
    return "took with me" in movement_terms and any(term in lowered for term in ("for use at", "for next week", "cleanup job"))


def _matched_routine_supply_need_terms(text: str) -> list[str]:
    haystack = _norm(text)
    terms = [term for term in ROUTINE_SUPPLY_NEED_TERMS if _contains_term(haystack, term)]
    return list(dict.fromkeys(terms))


def _matched_retention_terms(text: str) -> list[str]:
    haystack = _norm(text)
    return [term for term in (*RETENTION_TERMS, *REPLACE_INTENT_TERMS) if _contains_term(haystack, term)]


def _rule_voice_memo_operator_actions(source: CaptureSemanticInput, cleaned: str) -> list[dict[str, object]]:
    lowered = cleaned.lower()
    raw_actions: list[dict[str, object]] = []
    status_terms = [term for term in INACTIVE_STATUS_TERMS if term in lowered]
    if status_terms and source.site_id:
        target = source.site_label or source.site_id
        raw_actions.append(
            {
                "action_key": "site_status_change",
                "candidate_type": "voice_memo_operator_action",
                "job_type": JOB_SET_ENTITY_STATUS,
                "target_type": "site",
                "target_label": target,
                "summary": f"Review site status change: {target} -> inactive.",
                "rationale": "Deterministic voice-memo fallback detected an inactive site status request.",
                "confidence": "high",
                "source_excerpt": cleaned,
                "evidence_terms": status_terms,
                "payload_fields": {
                    "entity_type": "site",
                    "entity_id": source.site_id,
                    "status": "inactive",
                    "reason": cleaned,
                    "source": "voice_memo",
                },
            }
        )
    elif status_terms and source.selected_employees:
        employee = _employee_identity(next(iter(source.selected_employees)))
        target = employee["label"] or employee["id"]
        raw_actions.append(
            {
                "action_key": "employee_status_change",
                "candidate_type": "voice_memo_operator_action",
                "job_type": JOB_SET_ENTITY_STATUS,
                "target_type": "employee",
                "target_label": target,
                "summary": f"Review employee status change: {target} -> inactive.",
                "rationale": "Deterministic voice-memo fallback detected an inactive employee status request.",
                "confidence": "normal",
                "source_excerpt": cleaned,
                "evidence_terms": status_terms,
                "payload_fields": {
                    "entity_type": "employee",
                    "entity_id": employee["id"] or target,
                    "status": "inactive",
                    "reason": cleaned,
                    "source": "voice_memo",
                },
            }
        )

    if any(term in lowered for term in RECRUITING_TERMS) and source.site_id:
        raw_actions.append(
            {
                "action_key": "trigger_recruiting",
                "candidate_type": "voice_memo_operator_action",
                "job_type": JOB_TRIGGER_RECRUITING,
                "target_type": "site",
                "target_label": source.site_label,
                "summary": "Review recruiting coverage request.",
                "rationale": "Deterministic voice-memo fallback detected a recruiting or coverage request.",
                "confidence": "normal",
                "source_excerpt": cleaned,
                "evidence_terms": [term for term in RECRUITING_TERMS if term in lowered],
            }
        )

    if any(term in lowered for term in REMOVE_FROM_SCHEDULE_TERMS) and source.selected_employees:
        employee = _employee_identity(next(iter(source.selected_employees)))
        raw_actions.append(
            {
                "action_key": "remove_from_schedule",
                "candidate_type": "voice_memo_operator_action",
                "job_type": JOB_REMOVE_FROM_SCHEDULE,
                "target_type": "employee",
                "target_label": employee["label"],
                "summary": "Review schedule removal request.",
                "rationale": "Deterministic voice-memo fallback detected a schedule removal request.",
                "confidence": "normal",
                "source_excerpt": cleaned,
                "evidence_terms": [term for term in REMOVE_FROM_SCHEDULE_TERMS if term in lowered],
            }
        )
    return raw_actions


def _rule_site_action(
    source: CaptureSemanticInput,
    action_key: str,
    summary: str,
    excerpt: str,
    evidence_terms: list[str],
    *,
    job_type: str = "",
    candidate_type: str = "field_capture_follow_up",
    confidence: str = "low",
) -> dict[str, object]:
    return {
        "action_key": action_key,
        "candidate_type": candidate_type,
        "job_type": job_type,
        "target_type": "site",
        "target_label": source.site_label,
        "summary": summary,
        "rationale": "Deterministic rule fallback detected a site follow-up.",
        "confidence": confidence,
        "source_excerpt": excerpt,
        "evidence_terms": evidence_terms,
    }


def _rule_employee_action(
    source: CaptureSemanticInput,
    action_key: str,
    candidate_type: str,
    job_type: str,
    summary: str,
    excerpt: str,
    evidence_terms: list[str],
) -> dict[str, object]:
    target_label = ""
    employees = source.selected_employees or []
    if len(employees) == 1:
        target_label = _employee_identity(next(iter(employees)))["label"]
    return {
        "action_key": action_key,
        "candidate_type": candidate_type,
        "job_type": job_type,
        "target_type": "employee",
        "target_label": target_label,
        "summary": summary,
        "rationale": "Deterministic rule fallback detected a personnel follow-up.",
        "confidence": "low",
        "source_excerpt": excerpt,
        "evidence_terms": evidence_terms,
    }


def strip_non_field_operation_actions(result: SemanticResult) -> SemanticResult:
    actions = [action for action in result.action_candidates if not is_non_field_operation_action(action)]
    if actions == result.action_candidates:
        return result
    return replace(result, action_candidates=actions)


def is_non_field_operation_action(action: str) -> bool:
    lowered = collapse_whitespace(action).lower()
    return any(term in lowered for term in NON_FIELD_OPERATION_ACTION_TERMS)


def collapse_whitespace(value: str) -> str:
    return " ".join(value.strip().split())


def clean_internal_note(text: str) -> str:
    cleaned = text
    for word, replacement in PROFANITY_REPLACEMENTS.items():
        cleaned = re.sub(rf"\b{re.escape(word)}\b", replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bain't\b", "is not", cleaned, flags=re.IGNORECASE)
    cleaned = collapse_whitespace(cleaned)
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


def classify_issue(text: str) -> str:
    lowered = text.lower()
    if any(
        word in lowered
        for word in (
            "bathroom cleaner",
            "cleaner refill",
            "fresh mop head",
            "fresh mop heads",
            "mop head",
            "mop heads",
            "paper",
            "restock",
            "soap",
            "supply",
            "supplies",
            "towel",
            "liner",
            *load_brand_keywords("supply"),
        )
    ):
        return "supplies"
    if any(word in lowered for word in ("water", "leak", "moisture", "sink", "wet")):
        return "water"
    if any(word in lowered for word in ("access", "badge", "key", "locked", "gate")):
        return "access"
    if any(word in lowered for word in ("broken", "damage", "cracked")):
        return "damage"
    if any(word in lowered for word in ("safety", "hazard", "blood", "chemical")):
        return "safety"
    if any(word in lowered for word in ("dirty", "quality", "missed", "not acceptable")):
        return "quality"
    if any(word in lowered for word in ("client asked", "client requested")):
        return "client_request"
    return "other"


def classify_visit(text: str) -> bool:
    return get_extraction_terms().matches_any("visit_completion", text.lower())


def classify_qc_visit(text: str) -> bool:
    return get_extraction_terms().matches_any("qc_inspection", text.lower())


def classify_urgency(text: str, issue_type: str) -> str:
    lowered = text.lower()
    if issue_type == "safety" or any(word in lowered for word in ("hazard", "flood", "blood", "urgent")):
        return "high"
    if issue_type in {"water", "access", "damage"}:
        return "normal"
    return "normal" if issue_type != "other" else "low"


def client_safe_note(cleaned: str, issue_type: str) -> str:
    text = cleaned
    text = re.sub(r"\bnot acceptable\b", "requires follow-up", text, flags=re.IGNORECASE)
    text = re.sub(r"\brecurring problem\b", "recurring item requiring follow-up", text, flags=re.IGNORECASE)
    if issue_type == "supplies" and "requires follow-up" not in text.lower():
        return f"{text.rstrip('.')} requires supply follow-up."
    if issue_type == "water" and "requires follow-up" not in text.lower():
        return f"{text.rstrip('.')} requires follow-up for moisture or water source."
    return text


def operational_summary(cleaned: str, issue_type: str, source_kind: str = "field_capture_audio") -> str:
    source_label = "field audio" if source_kind == "field_capture_audio" else "capture text"
    if issue_type == "supplies":
        return f"Supply issue identified from {source_label}."
    if issue_type == "water":
        return f"Possible water or sink-area issue identified from {source_label}."
    if issue_type == "access":
        return f"Access constraint identified from {source_label}."
    if issue_type == "safety":
        return f"Safety-related issue identified from {source_label}."
    words = cleaned.split()
    return " ".join(words[:18]).rstrip(".") + ("." if words else "")


def suggested_tags_for(issue_type: str, urgency: str, source_kind: str = "field_capture_audio") -> list[str]:
    prefix = "field-audio" if source_kind == "field_capture_audio" else "capture-text"
    tags = [f"{prefix}/{issue_type}"]
    if urgency == "high":
        tags.append("urgency/high")
    return tags


def action_candidates_for(text: str, issue_type: str) -> list[str]:
    if is_unclear_or_test_audio(text):
        return []
    actions: list[str] = []
    lowered = text.lower()
    if issue_type == "supplies" or "towel" in lowered:
        actions.append(SUPPLY_REVIEW_ACTION)
    if issue_type == "water" or "sink" in lowered:
        actions.append("Inspect the affected sink or floor area for moisture source.")
    if issue_type == "access":
        actions.append("Confirm access requirements with the site contact.")
    if issue_type == "quality" and needs_after_photo(text):
        actions.append("Review the area and capture an after photo once corrected.")
    if not actions:
        actions.append(GENERIC_REVIEW_ACTION)
    if needs_after_photo(text) and not any("after photo" in action.lower() for action in actions):
        actions.append(AFTER_PHOTO_ACTION)
    return list(dict.fromkeys(actions))


def needs_after_photo(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "after photo",
            "before and after",
            "before photo",
            "once corrected",
            "when corrected",
            "after it is corrected",
            "take another photo",
            "capture another photo",
            "corrected photo",
            "remediate",
            "remediation",
        )
    )


def is_unclear_or_test_audio(text: str) -> bool:
    lowered = collapse_whitespace(text).lower()
    junk_phrases = (
        "just a test recording",
        "test recording",
        "blah, blah",
        "blah blah",
        "august 8th, guys",
        "contract can scare",
        "staged to scare",
        "more and more staged",
    )
    return any(phrase in lowered for phrase in junk_phrases)


def raw_text_hash(raw_text: str) -> str:
    return text_sha256(raw_text)


def validate_semantic_result(result: SemanticResult) -> None:
    if result.issue_type not in ISSUE_TYPES:
        raise ValueError(f"unsupported issue_type: {result.issue_type}")
    if result.urgency not in URGENCIES:
        raise ValueError(f"unsupported urgency: {result.urgency}")
    if not isinstance(result.issue_detected, bool):
        raise TypeError(f"issue_detected must be bool, got {type(result.issue_detected).__name__}")
    if not isinstance(result.visit_proposed, bool):
        raise TypeError(f"visit_proposed must be bool, got {type(result.visit_proposed).__name__}")
    if not isinstance(result.suggested_tags, list) or not all(isinstance(t, str) for t in result.suggested_tags):
        raise TypeError("suggested_tags must be a list of str")
    if not isinstance(result.action_candidates, list) or not all(isinstance(a, str) for a in result.action_candidates):
        raise TypeError("action_candidates must be a list of str")
    extracted_actions = result.extracted_actions if result.extracted_actions is not None else []
    if not isinstance(extracted_actions, list) or not all(isinstance(action, ExtractedAction) for action in extracted_actions):
        raise TypeError("extracted_actions must be a list of ExtractedAction")


def semantic_failure_fields() -> dict[str, object]:
    return {
        "cleaned_internal_note": "",
        "client_safe_note": "",
        "operational_summary": "",
        "issue_detected": False,
        "issue_type": "other",
        "urgency": "low",
        "suggested_tags": [],
        "action_candidates": [],
        "visit_proposed": False,
        "visit_type": "",
        "submitter_person_id": "",
        "extracted_actions": [],
        "source_kind": "",
        "capture_id": "",
    }


def build_semantic_engine() -> Callable[[CaptureSemanticInput | FieldAudioTranscript], SemanticResult]:
    engine_key = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")
    if engine_key == "local_rule":
        return RuleCaptureEngine()
    if engine_key == "local_model":
        provider = os.environ.get("BTQ_SEMANTIC_PROVIDER", "mlx")
        if provider == "mlx":
            from vision_backends import DEFAULT_MLX_MODEL, MlxTextClient

            model = (
                os.environ.get("BTQ_SEMANTIC_MODEL", "").strip()
                or os.environ.get("BTQ_FIELD_CAPTURE_VISION_MODEL", "").strip()
                or DEFAULT_MLX_MODEL
            )
            return LocalModelCaptureEngine(MlxTextClient(model))
        if provider == "ollama":
            from vision_backends import DEFAULT_TIMEOUT_SECONDS, OllamaTextClient

            model = os.environ.get("BTQ_SEMANTIC_MODEL", "").strip() or "qwen3:4b"
            base_url = (
                os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_URL", "").strip()
                or os.environ.get("BTQ_OLLAMA_URL", "").strip()
                or "http://127.0.0.1:11434"
            )
            timeout_raw = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS", "").strip()
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
            if timeout_raw:
                try:
                    timeout_seconds = float(timeout_raw)
                except ValueError as exc:
                    raise ValueError("BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS must be numeric") from exc
            keep_alive = os.environ.get("BTQ_FIELD_CAPTURE_SEMANTIC_KEEP_ALIVE", "10m").strip() or "10m"
            disable_thinking = _env_bool("BTQ_FIELD_CAPTURE_SEMANTIC_DISABLE_THINKING", default=True)
            return LocalModelCaptureEngine(
                OllamaTextClient(
                    model=model,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                    keep_alive=keep_alive,
                    disable_thinking=disable_thinking,
                )
            )
        raise ValueError(f"BTQ_SEMANTIC_PROVIDER={provider!r} is not implemented; supported: mlx, ollama")
    if engine_key == "cloud":
        raise ValueError("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE=cloud is not implemented")
    raise ValueError(
        f"BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE={engine_key!r} is not a recognized value; "
        "supported: local_rule, local_model (cloud is a reserved but unimplemented slot)"
    )


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())
