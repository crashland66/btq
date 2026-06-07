from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EXTRACTED_ACTIONS_KEY = "extracted_actions"
EXTRACTED_ACTION_SCHEMA_VERSION = "extracted-action-v1"
TARGET_TYPES = frozenset({"employee", "site", "general"})


@dataclass(frozen=True)
class ExtractedAction:
    action_key: str
    candidate_type: str
    target_type: str
    target_id: str
    target_label: str
    summary: str
    rationale: str
    confidence: object
    source_excerpt: str
    job_type: str = ""
    payload_fields: dict[str, object] | None = None
    evidence_terms: tuple[str, ...] = ()
    source_kind: str = ""
    proposed_queue_job: dict[str, object] | None = None
    proposed_queue_job_error: str = ""


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _optional_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value.strip()


def _optional_string_list(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of str")
    return tuple(item.strip() for item in value if item.strip())


def proposed_queue_job_from_action_payload(payload: dict[str, object]) -> dict[str, object] | None:
    direct = payload.get("proposed_queue_job")
    if isinstance(direct, dict):
        return dict(direct)

    approval_metadata = payload.get("approval_metadata")
    if isinstance(approval_metadata, dict):
        proposed = approval_metadata.get("proposed_queue_job")
        if isinstance(proposed, dict):
            return dict(proposed)
    return None


def validate_extracted_action(value: object) -> ExtractedAction:
    if not isinstance(value, dict):
        raise TypeError("extracted action must be an object")

    payload: dict[str, object] = value
    target_type = _required_text(payload, "target_type")
    if target_type not in TARGET_TYPES:
        raise ValueError(f"target_type must be one of {sorted(TARGET_TYPES)}")

    payload_fields = payload.get("payload_fields")
    if payload_fields is not None and not isinstance(payload_fields, dict):
        raise TypeError("payload_fields must be an object")

    return ExtractedAction(
        action_key=_required_text(payload, "action_key"),
        candidate_type=_required_text(payload, "candidate_type"),
        job_type=_optional_text(payload, "job_type"),
        target_type=target_type,
        target_id=_optional_text(payload, "target_id"),
        target_label=_optional_text(payload, "target_label"),
        summary=_required_text(payload, "summary"),
        rationale=_optional_text(payload, "rationale"),
        confidence=payload.get("confidence", "unknown"),
        payload_fields=dict(payload_fields) if isinstance(payload_fields, dict) else None,
        source_excerpt=_required_text(payload, "source_excerpt"),
        evidence_terms=_optional_string_list(payload, "evidence_terms"),
        source_kind=_optional_text(payload, "source_kind"),
        proposed_queue_job=proposed_queue_job_from_action_payload(payload),
        proposed_queue_job_error=_optional_text(payload, "proposed_queue_job_error"),
    )


def validated_extracted_actions(value: object) -> list[ExtractedAction]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("extracted_actions must be a list")
    return [validate_extracted_action(item) for item in value]
