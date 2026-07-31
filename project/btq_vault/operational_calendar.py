from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OPERATIONAL_CALENDAR_STATUSES: frozenset[str] = frozenset({"verified", "reference", "stale"})
OPERATIONAL_CALENDAR_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "first_student_day",
        "final_student_day",
        "no_student_day",
        "school_break",
        "teacher_in_service",
        "early_dismissal",
        "holiday_dismissal",
        "snow_makeup_reserved",
        "flexible_instruction_reserved",
        "informational",
    }
)
OPERATIONAL_CALENDAR_STUDENT_STATUSES: frozenset[str] = frozenset(
    {"in_session", "no_students", "early_dismissal", "unknown"}
)
OPERATIONAL_CALENDAR_FACILITY_STATUSES: frozenset[str] = frozenset({"open", "closed", "unknown"})
OPERATIONAL_CALENDAR_SERVICE_IMPACTS: frozenset[str] = frozenset(
    {"normal", "no_service", "modified", "confirm", "unknown"}
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_CALENDAR_REQUIRED_FIELDS = {
    "schema_version",
    "calendar_id",
    "label",
    "timezone",
    "status",
    "valid_from",
    "valid_through",
    "last_verified_at",
    "last_verified_by",
    "source",
    "events",
}
_CALENDAR_ALLOWED_FIELDS = _CALENDAR_REQUIRED_FIELDS | {"note"}
_SOURCE_REQUIRED_FIELDS = {"kind", "title", "retrieved_at"}
_SOURCE_URL_FIELDS = {"page_url", "document_url"}
_SOURCE_ALLOWED_FIELDS = _SOURCE_REQUIRED_FIELDS | _SOURCE_URL_FIELDS
_EVENT_REQUIRED_FIELDS = {
    "event_id",
    "start_date",
    "end_date",
    "kind",
    "label",
    "student_status",
    "facility_status",
    "bt_service_impact",
}
_EVENT_ALLOWED_FIELDS = _EVENT_REQUIRED_FIELDS | {"dismissal_time", "note"}


class OperationalCalendarError(ValueError):
    """Raised when an operational calendar does not match the canonical shape."""


def normalize_calendar_id(value: object, *, field: str = "calendar_id") -> str:
    text = _required_text(value, field)
    if _SLUG_RE.fullmatch(text) is None:
        raise OperationalCalendarError(
            f"{field} must be a lowercase stable slug using letters, digits, hyphens, or underscores"
        )
    return text


def normalize_operational_calendar(calendar: object) -> dict[str, Any]:
    if not isinstance(calendar, dict):
        raise OperationalCalendarError("operational calendar must be an object")
    missing_fields = _CALENDAR_REQUIRED_FIELDS - set(calendar)
    if missing_fields:
        raise OperationalCalendarError(
            f"operational calendar is missing required fields: {sorted(missing_fields)}"
        )
    unsupported_fields = set(calendar) - _CALENDAR_ALLOWED_FIELDS
    if unsupported_fields:
        raise OperationalCalendarError(
            f"operational calendar contains unsupported fields: {sorted(unsupported_fields)}"
        )
    if type(calendar["schema_version"]) is not int or calendar["schema_version"] != 1:
        raise OperationalCalendarError("operational calendar schema_version must be integer 1")

    calendar_id = normalize_calendar_id(calendar["calendar_id"])
    valid_from = _normalize_date(calendar["valid_from"], "valid_from")
    valid_through = _normalize_date(calendar["valid_through"], "valid_through")
    if valid_through < valid_from:
        raise OperationalCalendarError("valid_through must be on or after valid_from")

    timezone_name = _required_text(calendar["timezone"], "timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OperationalCalendarError("timezone must be an installed IANA timezone") from exc

    status = _required_text(calendar["status"], "status")
    if status not in OPERATIONAL_CALENDAR_STATUSES:
        raise OperationalCalendarError("status must be verified, reference, or stale")

    last_verified_at, last_verified_datetime = _normalize_datetime(
        calendar["last_verified_at"], "last_verified_at"
    )
    source = _normalize_source(calendar["source"])
    _, retrieved_datetime = _normalize_datetime(
        source["retrieved_at"], "source.retrieved_at"
    )
    if retrieved_datetime > last_verified_datetime:
        raise OperationalCalendarError(
            "source.retrieved_at must not be after last_verified_at"
        )

    raw_events = calendar["events"]
    if not isinstance(raw_events, list):
        raise OperationalCalendarError("events must be a list")
    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    for index, raw_event in enumerate(raw_events):
        event = _normalize_event(raw_event, index=index)
        event_id = event["event_id"]
        if event_id in event_ids:
            raise OperationalCalendarError(f"events contains duplicate event_id: {event_id}")
        event_ids.add(event_id)
        if event["start_date"] < valid_from or event["end_date"] > valid_through:
            raise OperationalCalendarError(
                f"event {event_id} falls outside the calendar coverage dates"
            )
        events.append(event)

    normalized: dict[str, Any] = {
        "schema_version": 1,
        "calendar_id": calendar_id,
        "label": _required_text(calendar["label"], "label"),
        "timezone": timezone_name,
        "status": status,
        "valid_from": valid_from,
        "valid_through": valid_through,
        "last_verified_at": last_verified_at,
        "last_verified_by": _required_text(calendar["last_verified_by"], "last_verified_by"),
        "source": source,
        "events": events,
    }
    if "note" in calendar:
        normalized["note"] = _text(calendar["note"], "note")
    return normalized


def normalize_operational_calendars(calendars: object) -> list[dict[str, Any]]:
    if not isinstance(calendars, list):
        raise OperationalCalendarError("operational_calendars must be a list")
    normalized: list[dict[str, Any]] = []
    calendar_ids: set[str] = set()
    for raw_calendar in calendars:
        calendar = normalize_operational_calendar(raw_calendar)
        calendar_id = calendar["calendar_id"]
        if calendar_id in calendar_ids:
            raise OperationalCalendarError(
                f"operational_calendars contains duplicate calendar_id: {calendar_id}"
            )
        calendar_ids.add(calendar_id)
        normalized.append(calendar)
    return normalized


def operational_calendars_from_doc(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    if doc is None or "operational_calendars" not in doc:
        return []
    return normalize_operational_calendars(doc["operational_calendars"])


def _normalize_source(raw_source: object) -> dict[str, str]:
    if not isinstance(raw_source, dict):
        raise OperationalCalendarError("source must be an object")
    missing_fields = _SOURCE_REQUIRED_FIELDS - set(raw_source)
    if missing_fields:
        raise OperationalCalendarError(f"source is missing required fields: {sorted(missing_fields)}")
    unsupported_fields = set(raw_source) - _SOURCE_ALLOWED_FIELDS
    if unsupported_fields:
        raise OperationalCalendarError(
            f"source contains unsupported fields: {sorted(unsupported_fields)}"
        )
    if not any(field in raw_source for field in _SOURCE_URL_FIELDS):
        raise OperationalCalendarError("source requires page_url or document_url")

    normalized = {
        "kind": _required_text(raw_source["kind"], "source.kind"),
        "title": _required_text(raw_source["title"], "source.title"),
        "retrieved_at": _normalize_datetime(
            raw_source["retrieved_at"], "source.retrieved_at"
        )[0],
    }
    for field in ("page_url", "document_url"):
        if field in raw_source:
            normalized[field] = _normalize_http_url(raw_source[field], f"source.{field}")
    return normalized


def _normalize_event(raw_event: object, *, index: int) -> dict[str, Any]:
    prefix = f"events[{index}]"
    if not isinstance(raw_event, dict):
        raise OperationalCalendarError(f"{prefix} must be an object")
    missing_fields = _EVENT_REQUIRED_FIELDS - set(raw_event)
    if missing_fields:
        raise OperationalCalendarError(f"{prefix} is missing required fields: {sorted(missing_fields)}")
    unsupported_fields = set(raw_event) - _EVENT_ALLOWED_FIELDS
    if unsupported_fields:
        raise OperationalCalendarError(
            f"{prefix} contains unsupported fields: {sorted(unsupported_fields)}"
        )

    event_id = normalize_calendar_id(raw_event["event_id"], field=f"{prefix}.event_id")
    start_date = _normalize_date(raw_event["start_date"], f"{prefix}.start_date")
    end_date = _normalize_date(raw_event["end_date"], f"{prefix}.end_date")
    if end_date < start_date:
        raise OperationalCalendarError(f"{prefix}.end_date must be on or after start_date")

    kind = _required_text(raw_event["kind"], f"{prefix}.kind")
    if kind not in OPERATIONAL_CALENDAR_EVENT_KINDS:
        raise OperationalCalendarError(f"{prefix}.kind is not allowed")
    student_status = _required_text(raw_event["student_status"], f"{prefix}.student_status")
    if student_status not in OPERATIONAL_CALENDAR_STUDENT_STATUSES:
        raise OperationalCalendarError(f"{prefix}.student_status is not allowed")
    facility_status = _required_text(raw_event["facility_status"], f"{prefix}.facility_status")
    if facility_status not in OPERATIONAL_CALENDAR_FACILITY_STATUSES:
        raise OperationalCalendarError(f"{prefix}.facility_status is not allowed")
    service_impact = _required_text(
        raw_event["bt_service_impact"], f"{prefix}.bt_service_impact"
    )
    if service_impact not in OPERATIONAL_CALENDAR_SERVICE_IMPACTS:
        raise OperationalCalendarError(f"{prefix}.bt_service_impact is not allowed")

    normalized: dict[str, Any] = {
        "event_id": event_id,
        "start_date": start_date,
        "end_date": end_date,
        "kind": kind,
        "label": _required_text(raw_event["label"], f"{prefix}.label"),
        "student_status": student_status,
        "facility_status": facility_status,
        "bt_service_impact": service_impact,
    }
    if "dismissal_time" in raw_event:
        dismissal_time = _required_text(raw_event["dismissal_time"], f"{prefix}.dismissal_time")
        if _TIME_RE.fullmatch(dismissal_time) is None:
            raise OperationalCalendarError(
                f"{prefix}.dismissal_time must use HH:MM 24-hour local time"
            )
        normalized["dismissal_time"] = dismissal_time
    if "note" in raw_event:
        normalized["note"] = _text(raw_event["note"], f"{prefix}.note")
    return normalized


def _normalize_date(value: object, field: str) -> str:
    text = _required_text(value, field)
    if _DATE_RE.fullmatch(text) is None:
        raise OperationalCalendarError(f"{field} must use strict ISO YYYY-MM-DD format")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise OperationalCalendarError(f"{field} must be a real calendar date") from exc
    return text


def _normalize_datetime(value: object, field: str) -> tuple[str, datetime]:
    text = _required_text(value, field)
    if _DATETIME_RE.fullmatch(text) is None:
        raise OperationalCalendarError(
            f"{field} must use strict timezone-aware ISO YYYY-MM-DDTHH:MM:SS format"
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalCalendarError(f"{field} must be a real ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationalCalendarError(f"{field} must include a UTC offset")
    return text, parsed


def _normalize_http_url(value: object, field: str) -> str:
    text = _required_text(value, field)
    if any(character.isspace() for character in text):
        raise OperationalCalendarError(f"{field} must be an absolute HTTP(S) URL")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise OperationalCalendarError(f"{field} must be an absolute HTTP(S) URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in text
    ):
        raise OperationalCalendarError(
            f"{field} must be an absolute HTTP(S) URL without credentials or a fragment"
        )
    return text


def _required_text(value: object, field: str) -> str:
    text = _text(value, field)
    if not text:
        raise OperationalCalendarError(f"{field} must be nonblank text")
    return text


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OperationalCalendarError(f"{field} must be text")
    return " ".join(value.split())
