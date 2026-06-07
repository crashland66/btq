from __future__ import annotations

from pathlib import Path

from processing_core.artifacts import write_json_object
from processing_core.slugs import lower_dash_slug


ALLOWED_EVENT_TYPES = {
    "employee_callout",
    "employee_resigned",
    "employee_onboarding",
    "employee_retention_risk",
    "interview_note",
    "incident",
    "staffing_risk",
    "access_constraint",
    "site_observation",
}

VALID_CONFIDENCE = {"high", "medium", "low"}

REQUIRED_FIELDS = {
    "event_id",
    "type",
    "site",
    "details",
    "confidence",
    "timestamp",
    "source_excerpt",
}

OPTIONAL_FIELDS = {
    "category",
    "capture_id",
    "employee",
    "blocking",
    "observations",
    "severity",
    "open_positions",
    "relationship",
    "role",
    "affected_role",
    "epistemic_state",
}

VALID_SEVERITY = {"low", "medium", "high", "critical"}
VALID_OBSERVATION_CATEGORIES = {"condition", "material", "layout"}


def slugify(value: str) -> str:
    return lower_dash_slug(value, fallback="event")


def event_path_for(output_dir: Path, event_id: str) -> Path:
    return output_dir / f"{slugify(event_id)}.json"


def write_event(output_dir: Path, event: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = event_path_for(output_dir, str(event["event_id"]))
    write_json_object(path, event)
    return path
