"""Operator-scoped read model for canonical site operational calendars."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from typing import Any

from btq_vault.operational_calendar import (
    OperationalCalendarError,
    normalize_operational_calendar,
)
from event_pipeline import btq_client, couchdb_config
from event_pipeline.context_resolver import operator_context_snapshot


Doc = Mapping[str, Any]

INACTIVE_LOCATION_STATUSES = frozenset(
    {"inactive", "closed", "lost", "archived", "deprecated", "terminated"}
)
VERIFICATION_FIELDS = ("last_verified_at", "last_verified_by")


class SiteOperationalCalendarError(ValueError):
    """Raised when a site-calendar report cannot be built safely."""


def site_calendar_report(
    operator: object,
    from_date: dt.date | str,
    through_date: dt.date | str,
    *,
    snapshot: Mapping[str, Any] | None = None,
    docs: Iterable[Doc] | Mapping[str, Any] | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Return upcoming operational-calendar events for an operator's active sites.

    ``snapshot`` and ``docs`` are injection seams for deterministic verification.
    Without them, operator scope and canonical location documents are read from
    the configured CouchDB stores.
    """

    window_start = _coerce_date(from_date, "from_date")
    window_end = _coerce_date(through_date, "through_date")
    if window_end < window_start:
        raise SiteOperationalCalendarError("through_date must be on or after from_date")

    resolved_snapshot = _resolve_snapshot(
        operator,
        snapshot=snapshot,
        docs=docs,
        config=config,
    )
    _require_resolved_snapshot(resolved_snapshot)

    account_rows = resolved_snapshot.get("accounts")
    if not isinstance(account_rows, list):
        raise SiteOperationalCalendarError("operator context snapshot accounts must be a list")

    scoped_sites = _snapshot_sites(account_rows)
    location_docs = _resolve_location_docs(
        docs=docs,
        snapshot=resolved_snapshot,
        site_ids=set(scoped_sites),
        config=config,
    )
    active_locations = _active_scoped_locations(
        location_docs,
        scoped_sites=scoped_sites,
    )

    grouped_events: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    for location in active_locations:
        site = {"site_id": location["site_id"], "site_name": location["site_name"]}
        raw_calendars = location["doc"].get("operational_calendars")
        if raw_calendars is None:
            continue
        if not isinstance(raw_calendars, list):
            diagnostics.append(
                _diagnostic(
                    site,
                    kind="malformed",
                    message="operational_calendars must be a list",
                )
            )
            continue

        duplicate_ids = _duplicate_calendar_ids(raw_calendars)
        for raw_calendar in _sorted_raw_calendars(raw_calendars):
            raw_id = _calendar_text(raw_calendar, "calendar_id")
            raw_label = _calendar_text(raw_calendar, "label")
            raw_status = _calendar_text(raw_calendar, "status")
            calendar_fields = {
                "calendar_id": raw_id or None,
                "calendar_label": raw_label or None,
                "calendar_status": raw_status or None,
            }

            if raw_id and raw_id in duplicate_ids:
                diagnostics.append(
                    _diagnostic(
                        site,
                        kind="malformed",
                        message=f"duplicate calendar_id: {raw_id}",
                        **calendar_fields,
                    )
                )
                continue

            missing_verification = _missing_verification_fields(raw_calendar)
            if missing_verification:
                diagnostics.append(
                    _diagnostic(
                        site,
                        kind="missing_verification",
                        message=(
                            "calendar is missing verification metadata: "
                            + ", ".join(missing_verification)
                        ),
                        **calendar_fields,
                    )
                )
                continue

            try:
                calendar = normalize_operational_calendar(_calendar_dict(raw_calendar))
            except (OperationalCalendarError, TypeError, ValueError) as exc:
                diagnostics.append(
                    _diagnostic(
                        site,
                        kind="malformed",
                        message=str(exc),
                        **calendar_fields,
                    )
                )
                continue

            issues = _calendar_issues(calendar, window_start)
            effective_state = _effective_calendar_state(calendar, issues)
            for issue in issues:
                diagnostics.append(
                    _diagnostic(
                        site,
                        kind=issue,
                        message=_calendar_issue_message(issue),
                        effective_calendar_state=effective_state,
                        calendar_id=calendar["calendar_id"],
                        calendar_label=calendar["label"],
                        calendar_status=calendar["status"],
                    )
                )

            for event in calendar["events"]:
                event_start = dt.date.fromisoformat(event["start_date"])
                event_end = dt.date.fromisoformat(event["end_date"])
                if event_start > window_end or event_end < window_start:
                    continue
                key = _event_group_key(calendar["calendar_id"], event)
                row = grouped_events.get(key)
                if row is None:
                    row = {
                        "calendar_id": calendar["calendar_id"],
                        "calendar_label": calendar["label"],
                        "calendar_status": calendar["status"],
                        "effective_calendar_state": effective_state,
                        "source_url": _source_url(calendar["source"]),
                        **event,
                        "days_until_start": (event_start - window_start).days,
                        "affected_sites": [],
                    }
                    grouped_events[key] = row
                row["affected_sites"].append(site)

    events = list(grouped_events.values())
    for event in events:
        event["affected_sites"] = _sorted_sites(event["affected_sites"])
    events.sort(key=_event_sort_key)
    diagnostics.sort(key=_diagnostic_sort_key)

    return {
        "operator": str(resolved_snapshot.get("operator") or operator),
        "from_date": window_start.isoformat(),
        "through_date": window_end.isoformat(),
        "events": events,
        "diagnostics": diagnostics,
    }


def upcoming_site_calendar_report(
    operator: object,
    from_date: dt.date | str,
    through_date: dt.date | str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compatibility name emphasizing that the report is window-based."""

    return site_calendar_report(operator, from_date, through_date, **kwargs)


def _resolve_snapshot(
    operator: object,
    *,
    snapshot: Mapping[str, Any] | None,
    docs: Iterable[Doc] | Mapping[str, Any] | None,
    config: object | None,
) -> Mapping[str, Any]:
    if snapshot is not None:
        return snapshot
    if isinstance(docs, Mapping):
        injected_snapshot = docs.get("snapshot")
        if isinstance(injected_snapshot, Mapping):
            return injected_snapshot
        resolver_docs = {
            key: docs[key]
            for key in ("accounts", "sites", "people", "employees")
            if key in docs
        }
        if resolver_docs:
            return operator_context_snapshot(operator, docs=resolver_docs, config=config)
    return operator_context_snapshot(operator, config=config)


def _require_resolved_snapshot(snapshot: Mapping[str, Any]) -> None:
    resolution = snapshot.get("resolution")
    if not isinstance(resolution, Mapping):
        return
    kind = str(resolution.get("kind") or "").strip()
    if kind in {"ambiguous", "not_found"}:
        query = str(resolution.get("query") or "").strip()
        detail = f" for {query!r}" if query else ""
        raise SiteOperationalCalendarError(f"operator resolution is {kind}{detail}")


def _snapshot_sites(accounts: list[Any]) -> dict[str, str]:
    sites: dict[str, str] = {}
    for account in accounts:
        if not isinstance(account, Mapping):
            continue
        site_id = _site_id(account)
        if not site_id:
            continue
        site_name = _site_name(account, fallback=site_id)
        sites.setdefault(site_id, site_name)
    return sites


def _resolve_location_docs(
    *,
    docs: Iterable[Doc] | Mapping[str, Any] | None,
    snapshot: Mapping[str, Any],
    site_ids: set[str],
    config: object | None,
) -> list[Doc]:
    injected = _location_docs_from_injection(docs)
    if injected is not None:
        return injected

    snapshot_locations = snapshot.get("locations")
    if isinstance(snapshot_locations, list):
        return [doc for doc in snapshot_locations if isinstance(doc, Mapping)]

    snapshot_accounts = snapshot.get("accounts")
    if isinstance(snapshot_accounts, list) and any(
        isinstance(doc, Mapping)
        and (
            doc.get("type") == "location"
            or "operational_calendars" in doc
        )
        for doc in snapshot_accounts
    ):
        return [doc for doc in snapshot_accounts if isinstance(doc, Mapping)]

    configured = _location_docs_from_config(config)
    if configured is not None:
        return configured
    return _load_location_docs(site_ids, config=config)


def _location_docs_from_injection(
    docs: Iterable[Doc] | Mapping[str, Any] | None,
) -> list[Doc] | None:
    if docs is None:
        return None
    if isinstance(docs, Mapping):
        for key in ("locations", "location_docs", "vault_docs"):
            value = docs.get(key)
            if value is not None:
                return _mapping_docs(value)
        if docs.get("type") == "location":
            return [docs]
        mapped_docs = [
            value
            for value in docs.values()
            if isinstance(value, Mapping) and value.get("type") == "location"
        ]
        if mapped_docs:
            return mapped_docs
        return None
    return [doc for doc in docs if isinstance(doc, Mapping)]


def _location_docs_from_config(config: object | None) -> list[Doc] | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        for key in ("locations", "location_docs", "vault_docs"):
            if key in config:
                return _mapping_docs(config[key])
        return None
    for key in ("locations", "location_docs", "vault_docs"):
        value = getattr(config, key, None)
        if value is not None:
            return _mapping_docs(value)
    return None


def _mapping_docs(value: object) -> list[Doc]:
    if isinstance(value, Mapping):
        return [doc for doc in value.values() if isinstance(doc, Mapping)]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return [doc for doc in value if isinstance(doc, Mapping)]
    return []


def _load_location_docs(
    site_ids: set[str],
    *,
    config: object | None,
) -> list[dict[str, Any]]:
    if not site_ids:
        return []
    database = _vault_database(config)
    selector = {
        "type": "location",
        "_id": {"$in": [f"location_{site_id}" for site_id in sorted(site_ids, key=_site_sort_key)]},
    }
    return btq_client.find(
        database,
        selector,
        limit=max(len(site_ids), 1),
    )


def _vault_database(config: object | None) -> str:
    if isinstance(config, Mapping):
        for key in ("vault_database", "database", "couchdb_vault_db"):
            value = config.get(key)
            if str(value or "").strip():
                return str(value).strip()
    if config is not None:
        for key in ("vault_database", "database", "couchdb_vault_db"):
            value = getattr(config, key, None)
            if str(value or "").strip():
                return str(value).strip()
    return couchdb_config.vault_database()


def _active_scoped_locations(
    docs: list[Doc],
    *,
    scoped_sites: Mapping[str, str],
) -> list[dict[str, Any]]:
    by_site: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if str(doc.get("type") or "").strip() != "location":
            continue
        site_id = _site_id(doc)
        if site_id not in scoped_sites or not _is_active_location(doc):
            continue
        candidate = {
            "site_id": site_id,
            "site_name": _site_name(doc, fallback=scoped_sites[site_id]),
            "doc": doc,
        }
        existing = by_site.get(site_id)
        if existing is None or _location_preference_key(candidate) > _location_preference_key(existing):
            by_site[site_id] = candidate
    return sorted(
        by_site.values(),
        key=lambda row: (_site_sort_key(row["site_id"]), row["site_name"].casefold()),
    )


def _is_active_location(doc: Doc) -> bool:
    if doc.get("active") is False:
        return False
    status = str(doc.get("status") or "").strip().lower()
    return status not in INACTIVE_LOCATION_STATUSES


def _location_preference_key(location: Mapping[str, Any]) -> tuple[int, int, str]:
    doc = location["doc"]
    site_id = location["site_id"]
    return (
        1 if str(doc.get("_id") or "").strip() == f"location_{site_id}" else 0,
        1 if "operational_calendars" in doc else 0,
        json.dumps(dict(doc), sort_keys=True, default=str),
    )


def _site_id(doc: Mapping[str, Any]) -> str:
    value = doc.get("site_id") or doc.get("job") or doc.get("job_number")
    if value is None:
        doc_id = str(doc.get("_id") or "").strip()
        if doc_id.startswith("location_"):
            value = doc_id.removeprefix("location_")
        elif doc_id.startswith("site_"):
            value = doc_id.removeprefix("site_")
    return str(value or "").strip()


def _site_name(doc: Mapping[str, Any], *, fallback: str) -> str:
    value = (
        doc.get("location")
        or doc.get("canonical_name")
        or doc.get("canonical")
        or doc.get("name")
        or doc.get("account")
        or fallback
    )
    return str(value or fallback).strip() or fallback


def _duplicate_calendar_ids(raw_calendars: list[Any]) -> set[str]:
    counts: dict[str, int] = {}
    for calendar in raw_calendars:
        calendar_id = _calendar_text(calendar, "calendar_id")
        if calendar_id:
            counts[calendar_id] = counts.get(calendar_id, 0) + 1
    return {calendar_id for calendar_id, count in counts.items() if count > 1}


def _sorted_raw_calendars(raw_calendars: list[Any]) -> list[Any]:
    return sorted(
        raw_calendars,
        key=lambda calendar: (
            _calendar_text(calendar, "calendar_id"),
            _calendar_text(calendar, "label"),
            json.dumps(calendar, sort_keys=True, default=str),
        ),
    )


def _calendar_text(calendar: object, field: str) -> str:
    if not isinstance(calendar, Mapping):
        return ""
    return str(calendar.get(field) or "").strip()


def _calendar_dict(calendar: object) -> dict[str, Any]:
    if not isinstance(calendar, Mapping):
        raise OperationalCalendarError("operational calendar must be an object")
    return dict(calendar)


def _missing_verification_fields(calendar: object) -> list[str]:
    if not isinstance(calendar, Mapping):
        return []
    missing = [
        field
        for field in VERIFICATION_FIELDS
        if not str(calendar.get(field) or "").strip()
    ]
    source = calendar.get("source")
    if isinstance(source, Mapping) and not str(source.get("retrieved_at") or "").strip():
        missing.append("source.retrieved_at")
    return missing


def _calendar_issues(
    calendar: Mapping[str, Any],
    from_date: dt.date,
) -> list[str]:
    issues: list[str] = []
    if calendar["status"] == "stale":
        issues.append("stale")
    if dt.date.fromisoformat(calendar["valid_through"]) < from_date:
        issues.append("expired")
    return issues


def _effective_calendar_state(
    calendar: Mapping[str, Any],
    issues: list[str],
) -> str:
    if "stale" in issues:
        return "stale"
    if "expired" in issues:
        return "expired"
    return str(calendar["status"])


def _calendar_issue_message(issue: str) -> str:
    if issue == "expired":
        return "calendar coverage expired before from_date"
    return "calendar status is explicitly stale"


def _source_url(source: Mapping[str, Any]) -> str:
    return str(source.get("document_url") or source.get("page_url") or "")


def _event_group_key(calendar_id: str, event: Mapping[str, Any]) -> str:
    return json.dumps(
        [calendar_id, dict(event)],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _diagnostic(
    site: Mapping[str, str],
    *,
    kind: str,
    message: str,
    calendar_id: str | None = None,
    calendar_label: str | None = None,
    calendar_status: str | None = None,
    effective_calendar_state: str | None = None,
) -> dict[str, Any]:
    return {
        "site_id": site["site_id"],
        "site_name": site["site_name"],
        "calendar_id": calendar_id,
        "calendar_label": calendar_label,
        "calendar_status": calendar_status,
        "kind": kind,
        "effective_calendar_state": effective_calendar_state or kind,
        "message": message,
    }


def _sorted_sites(sites: list[Mapping[str, str]]) -> list[dict[str, str]]:
    unique = {
        (str(site["site_id"]), str(site["site_name"])): {
            "site_id": str(site["site_id"]),
            "site_name": str(site["site_name"]),
        }
        for site in sites
    }
    return sorted(
        unique.values(),
        key=lambda site: (_site_sort_key(site["site_id"]), site["site_name"].casefold()),
    )


def _event_sort_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    sites = event.get("affected_sites")
    first_site = sites[0] if isinstance(sites, list) and sites else {}
    return (
        str(event["start_date"]),
        str(event["end_date"]),
        str(event["label"]).casefold(),
        _site_sort_key(first_site.get("site_id")),
        str(first_site.get("site_name") or "").casefold(),
        str(event.get("calendar_id") or ""),
        str(event.get("event_id") or ""),
        json.dumps(dict(event), sort_keys=True, default=str),
    )


def _diagnostic_sort_key(diagnostic: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _site_sort_key(diagnostic.get("site_id")),
        str(diagnostic.get("site_name") or "").casefold(),
        str(diagnostic.get("calendar_id") or ""),
        str(diagnostic.get("kind") or ""),
        str(diagnostic.get("message") or ""),
    )


def _site_sort_key(site_id: object) -> tuple[int, int | str]:
    value = str(site_id or "").strip()
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def _coerce_date(value: dt.date | str, field: str) -> dt.date:
    if isinstance(value, dt.datetime):
        raise SiteOperationalCalendarError(f"{field} must be a date, not a datetime")
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        raise SiteOperationalCalendarError(f"{field} must use strict ISO YYYY-MM-DD format")
    if len(value) != 10 or value[4:5] != "-" or value[7:8] != "-":
        raise SiteOperationalCalendarError(f"{field} must use strict ISO YYYY-MM-DD format")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SiteOperationalCalendarError(f"{field} must be a real calendar date") from exc
    if parsed.isoformat() != value:
        raise SiteOperationalCalendarError(f"{field} must use strict ISO YYYY-MM-DD format")
    return parsed
