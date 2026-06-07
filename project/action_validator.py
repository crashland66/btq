from __future__ import annotations

from copy import deepcopy
from typing import Any


VAGUE_PHRASES = ("improve", "optimize", "fix things", "make better")
GENERIC_TARGETS = {"", "/", "*", "site"}
CONCRETE_VERBS = (
    "add",
    "create",
    "update",
    "insert",
    "replace",
    "remove",
    "write",
    "generate",
    "declare",
    "link",
    "document",
    "configure",
    "preview",
)


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def is_endpoint(target: str) -> bool:
    return target.startswith("http://") or target.startswith("https://") or target.startswith("/")


def is_file_path(target: str) -> bool:
    return "/" in target or "." in target


def has_actionable_payload(action_type: str, payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    if action_type == "update_file":
        return "change" in payload
    if action_type in {"create_file", "add_file"}:
        return any(key in payload for key in ("content", "body", "text", "fields", "data"))
    if action_type == "generate_agents_txt":
        return any(key in payload for key in ("capabilities", "agents", "structured_data", "data"))
    if action_type == "http_call":
        return any(key in payload for key in ("method", "body", "headers", "data"))
    return bool(payload)


def validate_actions(actions: list) -> list:
    if not isinstance(actions, list):
        raise ValidationError(["actions must be a list"])
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    cleaned: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        label = f"action {index}"
        if not isinstance(action, dict):
            errors.append(f"{label}: must be a mapping")
            continue
        action_type = action.get("type")
        target = action.get("target")
        description = action.get("description")
        payload = action.get("payload")
        if not isinstance(action_type, str) or not action_type.strip():
            errors.append(f"{label}: type must be a non-empty string")
            action_type = ""
        if not isinstance(description, str) or len(description.strip()) < 10:
            errors.append(f"{label}: description must be at least 10 characters")
            description_text = ""
        else:
            description_text = description.strip()
            lowered = description_text.lower()
            for phrase in VAGUE_PHRASES:
                if phrase in lowered:
                    errors.append(f"{label}: description contains vague phrase '{phrase}'")
                    break
            if not any(verb in lowered for verb in CONCRETE_VERBS):
                errors.append(f"{label}: description must include a concrete change")
        if not isinstance(target, str) or not target.strip():
            errors.append(f"{label}: target must be a non-empty file path or endpoint")
            target_text = ""
        else:
            target_text = target.strip()
            if target_text in GENERIC_TARGETS:
                errors.append(f"{label}: target is too generic")
            elif not is_file_path(target_text) and not is_endpoint(target_text):
                errors.append(f"{label}: target must be a file path or endpoint")
        if isinstance(action_type, str) and isinstance(target, str):
            duplicate_key = (action_type.strip(), target.strip())
            if duplicate_key in seen:
                errors.append(f"{label}: duplicate action for type and target")
            seen.add(duplicate_key)
        if not isinstance(payload, dict):
            errors.append(f"{label}: payload must be a mapping")
        elif not has_actionable_payload(str(action_type), payload):
            errors.append(f"{label}: payload has no actionable fields for {action_type}")
        cleaned.append(deepcopy(action))
    if errors:
        raise ValidationError(errors)
    return cleaned
