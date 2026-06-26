from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from typing import Any


FACILITY_HOURS_STATUSES: frozenset[str] = frozenset({"verified", "reference", "stale"})
FACILITY_HOURS_RULES: frozenset[str] = frozenset({"date", "nth_weekday"})
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_INDEX: dict[str, int] = {weekday: index for index, weekday in enumerate(WEEKDAYS)}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class FacilityHoursError(ValueError):
    """Raised when facility hours do not match the canonical shape."""


@dataclass(frozen=True)
class FacilityOpenState:
    known: bool
    is_open: bool
    next_open: datetime | None = None
    next_close: datetime | None = None
    reason: str = ""


def unknown_facility_hours() -> dict[str, Any]:
    return {
        "status": "unknown",
        "last_verified_at": None,
        "last_verified_by": None,
        "source": None,
        "note": "",
        "weekly": {weekday: [] for weekday in WEEKDAYS},
        "exceptions": [],
    }


def facility_hours_from_doc(doc: dict[str, Any] | None) -> dict[str, Any]:
    raw_hours = (doc or {}).get("facility_hours")
    if raw_hours is None:
        return unknown_facility_hours()
    if not isinstance(raw_hours, dict):
        return unknown_facility_hours()
    try:
        return normalize_facility_hours(raw_hours)
    except FacilityHoursError:
        return unknown_facility_hours()


def normalize_facility_hours(hours: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(hours, dict):
        raise FacilityHoursError("facility_hours must be an object")
    allowed_fields = {
        "status",
        "last_verified_at",
        "last_verified_by",
        "source",
        "note",
        "weekly",
        "exceptions",
    }
    if set(hours) - allowed_fields:
        raise FacilityHoursError("facility_hours contains unsupported fields")

    status = str(hours.get("status") or "reference").strip()
    if status not in FACILITY_HOURS_STATUSES:
        raise FacilityHoursError("facility_hours requires an allowed status")

    weekly = _normalize_weekly(hours.get("weekly"))
    exceptions = _normalize_exceptions(hours.get("exceptions", []))
    return {
        "status": status,
        "last_verified_at": _nullable_string(hours.get("last_verified_at")),
        "last_verified_by": _nullable_string(hours.get("last_verified_by")),
        "source": _nullable_string(hours.get("source")),
        "note": str(hours.get("note") or "").strip(),
        "weekly": weekly,
        "exceptions": exceptions,
    }


def facility_open_state(hours: dict[str, Any] | None, at: datetime) -> FacilityOpenState:
    if at.tzinfo is None or at.utcoffset() is None:
        raise FacilityHoursError("facility hours lookup requires a timezone-aware datetime")
    try:
        normalized = normalize_facility_hours(hours or {})
    except FacilityHoursError:
        return FacilityOpenState(known=False, is_open=False, reason="unknown")

    current_date = at.date()
    current_time = at.time().replace(tzinfo=None)
    todays_intervals = _intervals_for_date(normalized, current_date)
    for interval in todays_intervals:
        open_time = _parse_time(interval["open"])
        close_time = _parse_time(interval["close"])
        if open_time <= current_time < close_time:
            return FacilityOpenState(
                known=True,
                is_open=True,
                next_close=_combine(at, current_date, close_time),
                reason="within_interval",
            )

    next_open = _next_open_after(normalized, at)
    return FacilityOpenState(known=True, is_open=False, next_open=next_open, reason="closed")


def facility_open_state_dict(hours: dict[str, Any] | None, at: datetime) -> dict[str, Any]:
    state = facility_open_state(hours, at)
    return {
        "known": state.known,
        "is_open": state.is_open,
        "next_open": state.next_open.isoformat() if state.next_open else None,
        "next_close": state.next_close.isoformat() if state.next_close else None,
        "reason": state.reason,
    }


def _normalize_weekly(raw_weekly: object) -> dict[str, list[dict[str, str]]]:
    if not isinstance(raw_weekly, dict):
        raise FacilityHoursError("facility_hours.weekly must be an object")
    if set(raw_weekly) - set(WEEKDAYS):
        raise FacilityHoursError("facility_hours.weekly contains unsupported weekday keys")
    return {weekday: _normalize_intervals(raw_weekly.get(weekday, [])) for weekday in WEEKDAYS}


def _normalize_exceptions(raw_exceptions: object) -> list[dict[str, Any]]:
    if raw_exceptions is None:
        return []
    if not isinstance(raw_exceptions, list):
        raise FacilityHoursError("facility_hours.exceptions must be a list")
    exceptions: list[dict[str, Any]] = []
    for raw_exception in raw_exceptions:
        if not isinstance(raw_exception, dict):
            raise FacilityHoursError("facility_hours exceptions must be objects")
        rule = str(raw_exception.get("rule") or "").strip()
        if rule not in FACILITY_HOURS_RULES:
            raise FacilityHoursError("facility_hours exceptions require an allowed rule")
        allowed_fields = {"rule", "hours", "note"}
        if rule == "date":
            allowed_fields.add("date")
        if rule == "nth_weekday":
            allowed_fields.update({"weekday", "ordinals"})
        if set(raw_exception) - allowed_fields:
            raise FacilityHoursError("facility_hours exception contains unsupported fields")

        normalized: dict[str, Any] = {
            "rule": rule,
            "hours": _normalize_intervals(raw_exception.get("hours", [])),
            "note": str(raw_exception.get("note") or "").strip(),
        }
        if rule == "date":
            date_value = str(raw_exception.get("date") or "").strip()
            if DATE_RE.match(date_value) is None:
                raise FacilityHoursError("date exceptions require ISO YYYY-MM-DD date")
            try:
                date.fromisoformat(date_value)
            except ValueError as exc:
                raise FacilityHoursError("date exceptions require a valid ISO date") from exc
            normalized["date"] = date_value
        elif rule == "nth_weekday":
            weekday = str(raw_exception.get("weekday") or "").strip().lower()
            if weekday not in WEEKDAY_INDEX:
                raise FacilityHoursError("nth_weekday exceptions require an allowed weekday")
            ordinals = raw_exception.get("ordinals")
            if not isinstance(ordinals, list) or not ordinals:
                raise FacilityHoursError("nth_weekday exceptions require ordinals")
            normalized_ordinals: list[int] = []
            for ordinal in ordinals:
                if not isinstance(ordinal, int) or ordinal < 1 or ordinal > 5:
                    raise FacilityHoursError("nth_weekday ordinals must be integers 1-5")
                if ordinal not in normalized_ordinals:
                    normalized_ordinals.append(ordinal)
            normalized["weekday"] = weekday
            normalized["ordinals"] = normalized_ordinals
        exceptions.append(normalized)
    return exceptions


def _normalize_intervals(raw_intervals: object) -> list[dict[str, str]]:
    if raw_intervals is None:
        return []
    if not isinstance(raw_intervals, list):
        raise FacilityHoursError("facility hour intervals must be a list")
    intervals: list[dict[str, str]] = []
    for raw_interval in raw_intervals:
        if not isinstance(raw_interval, dict):
            raise FacilityHoursError("facility hour intervals must be objects")
        if set(raw_interval) - {"open", "close"}:
            raise FacilityHoursError("facility hour intervals contain unsupported fields")
        open_value = _normalize_time_string(raw_interval.get("open"))
        close_value = _normalize_time_string(raw_interval.get("close"))
        if _parse_time(open_value) >= _parse_time(close_value):
            raise FacilityHoursError("facility hour intervals require open before close")
        intervals.append({"open": open_value, "close": close_value})
    return intervals


def _normalize_time_string(value: object) -> str:
    if not isinstance(value, str):
        raise FacilityHoursError("facility hour times must be strings")
    text = value.strip()
    if TIME_RE.match(text) is None:
        raise FacilityHoursError("facility hour times must use HH:MM 24-hour format")
    return text


def _intervals_for_date(hours: dict[str, Any], target_date: date) -> list[dict[str, str]]:
    date_text = target_date.isoformat()
    for exception in hours["exceptions"]:
        if exception["rule"] == "date" and exception["date"] == date_text:
            return exception["hours"]
    weekday = WEEKDAYS[target_date.weekday()]
    ordinal = ((target_date.day - 1) // 7) + 1
    for exception in hours["exceptions"]:
        if (
            exception["rule"] == "nth_weekday"
            and exception["weekday"] == weekday
            and ordinal in exception["ordinals"]
        ):
            return exception["hours"]
    return hours["weekly"][weekday]


def _next_open_after(hours: dict[str, Any], at: datetime) -> datetime | None:
    current_date = at.date()
    current_time = at.time().replace(tzinfo=None)
    for day_offset in range(0, 371):
        candidate_date = current_date + timedelta(days=day_offset)
        for interval in _intervals_for_date(hours, candidate_date):
            open_time = _parse_time(interval["open"])
            if day_offset > 0 or open_time > current_time:
                return _combine(at, candidate_date, open_time)
    return None


def _combine(reference: datetime, day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=reference.tzinfo)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _nullable_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
