from __future__ import annotations

import json
from pathlib import Path

from event_pipeline.schema import (
    ALLOWED_EVENT_TYPES,
    OPTIONAL_FIELDS,
    REQUIRED_FIELDS,
    VALID_CONFIDENCE,
    VALID_OBSERVATION_CATEGORIES,
    VALID_SEVERITY,
    write_event,
)


def validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("Event payload must be a JSON object")

    payload_keys = set(event.keys())
    missing = sorted(REQUIRED_FIELDS - payload_keys)
    extra = sorted(payload_keys - REQUIRED_FIELDS - OPTIONAL_FIELDS)
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")
    if extra:
        raise ValueError(f"Unexpected field(s): {', '.join(extra)}")

    if event["type"] not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"Invalid event type: {event['type']}")
    if event["confidence"] not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence: {event['confidence']}")
    if not isinstance(event["details"], str) or not event["details"].strip():
        raise ValueError("Field details must be a non-empty string")
    if not isinstance(event["site"], str) or not event["site"].strip():
        raise ValueError("Field site must be a non-empty string")
    if "relationship" in event and not isinstance(event["relationship"], str):
        raise ValueError("Field relationship must be a string")
    if "blocking" in event and not isinstance(event["blocking"], bool):
        raise ValueError("Field blocking must be a boolean")
    if "severity" in event and event["severity"] not in VALID_SEVERITY:
        raise ValueError(f"Invalid severity: {event['severity']}")
    if "open_positions" in event and (not isinstance(event["open_positions"], int) or event["open_positions"] < 1):
        raise ValueError("Field open_positions must be a positive integer")
    if event["type"] == "site_observation" and event.get("category") not in VALID_OBSERVATION_CATEGORIES:
        raise ValueError(f"Invalid site observation category: {event.get('category')}")
    if "observations" in event:
        observations = event["observations"]
        if not isinstance(observations, list):
            raise ValueError("Field observations must be a list")
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("Each observation must be an object")
            if set(observation.keys()) != {"type", "confidence"}:
                raise ValueError("Each observation must contain only type and confidence")
            if not isinstance(observation["type"], str) or not observation["type"].strip():
                raise ValueError("Observation type must be a non-empty string")
            if observation["confidence"] != "observed":
                raise ValueError("Observation confidence must be 'observed'")
    if "epistemic_state" in event:
        epistemic_state = event["epistemic_state"]
        if not isinstance(epistemic_state, dict):
            raise ValueError("Field epistemic_state must be an object")
        for field in ("classification", "source_type", "confidence", "derived_from", "timestamp_context"):
            if not isinstance(epistemic_state.get(field), str) or not epistemic_state[field].strip():
                raise ValueError(f"Epistemic field {field} must be a non-empty string")
        if "confidence_basis" in epistemic_state and not isinstance(epistemic_state["confidence_basis"], list):
            raise ValueError("Epistemic field confidence_basis must be a list")

    validated = dict(event)
    if "blocking" not in validated:
        validated["blocking"] = False
    return validated


def validate_event_path(path: Path, valid_dir: Path, failed_dir: Path) -> tuple[Path | None, Path | None]:
    event = json.loads(path.read_text(encoding="utf-8"))
    try:
        return write_event(valid_dir, validate_event(event)), None
    except ValueError as exc:
        failed_payload = dict(event)
        failed_payload["validation_error"] = str(exc)
        return None, write_event(failed_dir, failed_payload)


def validate_events(
    enriched_dir: Path,
    valid_dir: Path,
    failed_dir: Path,
    input_paths: list[Path] | None = None,
) -> tuple[list[Path], list[Path]]:
    valid_paths: list[Path] = []
    failed_paths: list[Path] = []
    failed_dir.mkdir(parents=True, exist_ok=True)

    paths = sorted(input_paths) if input_paths is not None else sorted(enriched_dir.glob("*.json"))
    for path in paths:
        valid_path, failed_path = validate_event_path(path, valid_dir, failed_dir)
        if valid_path is not None:
            valid_paths.append(valid_path)
        if failed_path is not None:
            failed_paths.append(failed_path)

    if valid_paths:
        unknown_site_count = 0
        for path in valid_paths:
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("site") == "unknown":
                unknown_site_count += 1
        if unknown_site_count > len(valid_paths) // 2:
            print(f"warning: high unknown-site rate ({unknown_site_count}/{len(valid_paths)})")

    return valid_paths, failed_paths
