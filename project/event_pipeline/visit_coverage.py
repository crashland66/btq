"""Visit/QC coverage read model.

Week and as-of anchors are evaluated in the operator's local operational week.
The current default mirrors visit_create's local date stamping; long term this
should come from a site/operator timezone source.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any
from zoneinfo import ZoneInfo

from btq_vault.entity_types import current_operator_id
from event_pipeline import couchdb_config
from event_pipeline.context_resolver import operator_context_snapshot


Doc = Mapping[str, Any]

DEFAULT_WEEKLY_QC_TARGET = 4
DEFAULT_FIND_LIMIT = 10000
DEFAULT_COVERAGE_TIMEZONE = "America/New_York"
DEFAULT_COVERAGE_ZONE = ZoneInfo(DEFAULT_COVERAGE_TIMEZONE)
QC_VISIT_TYPES = frozenset({"qc", "qc_inspection"})
OVERDUE_QC_DAYS = 30


class VisitCoverageError(Exception):
    """Raised when the Couch-backed read path cannot load coverage inputs."""


def weekly_qc_count(
    operator: object,
    *,
    now: dt.datetime | dt.date | str | None = None,
    week_of: dt.datetime | dt.date | str | None = None,
    target: int = DEFAULT_WEEKLY_QC_TARGET,
    visits: Iterable[Doc] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    config: couchdb_config.CouchDBConfig | None = None,
) -> dict[str, Any]:
    """Return this operator's completed QC visits for the ISO week.

    Only canonical visit docs with ``visit_type`` in ``QC_VISIT_TYPES`` after
    case-insensitive normalization count toward the weekly target. Generic
    visits remain recent activity but do not count as QCs.
    """

    as_of = _coerce_date(now)
    week_start, week_end = _iso_week_bounds(_coerce_date(week_of) if week_of is not None else as_of)
    effective_end = min(week_end, as_of)
    operator_aliases = _operator_aliases(operator, snapshot=snapshot)
    source_visits = list(visits) if visits is not None else _load_visit_docs(
        operator,
        start_date=week_start,
        end_date=effective_end,
        snapshot=snapshot,
        config=config,
    )

    completed_by_key: dict[tuple[str, str, str], tuple[dict[str, Any], Doc]] = {}
    operator_identity = _operator_coverage_identity(operator, snapshot=snapshot)
    for visit in source_visits:
        if _is_archived_visit(visit):
            continue
        visit_date = _doc_date(visit)
        if visit_date is None or visit_date < week_start or visit_date > effective_end:
            continue
        if not _visit_matches_operator(visit, operator_aliases):
            continue
        if not _is_qc_visit(visit):
            continue
        site_id = _doc_site_id(visit)
        key = (operator_identity, site_id, visit_date.isoformat())
        candidate = (_completed_visit_shape(visit, visit_date), visit)
        existing = completed_by_key.get(key)
        if existing is None or (
            _weekly_qc_preference_key(*candidate) > _weekly_qc_preference_key(*existing)
        ):
            completed_by_key[key] = candidate

    completed = [candidate[0] for candidate in completed_by_key.values()]
    completed.sort(
        key=lambda item: (item["date"], str(item.get("site_id") or ""), str(item.get("site") or ""))
    )
    count = len(completed)
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "count": count,
        "target": int(target),
        "remaining": max(0, int(target) - count),
        "completed": completed,
    }


def account_coverage(
    operator: object,
    *,
    now: dt.datetime | dt.date | str | None = None,
    visits: Iterable[Doc] | None = None,
    visit_gaps: Iterable[Doc] | None = None,
    accounts: Iterable[Doc] | Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    config: couchdb_config.CouchDBConfig | None = None,
) -> dict[str, Any]:
    """Return last-visit, last-QC, and open-gap coverage for operator accounts."""

    as_of = _coerce_date(now)
    resolved_snapshot = snapshot
    if accounts is None:
        resolved_snapshot = snapshot or operator_context_snapshot(operator, now=_generated_at(now))
        account_docs = resolved_snapshot.get("accounts", []) if isinstance(resolved_snapshot, Mapping) else []
    else:
        account_docs = _account_docs_from_injection(accounts)

    active_accounts = [_normalize_account(account) for account in account_docs]
    active_accounts = [account for account in active_accounts if account is not None and account["active"]]
    active_accounts.sort(key=lambda account: (_site_sort_key(account["site_id"]), account["site_name"]))

    site_ids = {account["site_id"] for account in active_accounts}
    operator_aliases = _operator_aliases(operator, snapshot=resolved_snapshot)
    source_visits = list(visits) if visits is not None else _load_visit_docs(
        operator,
        end_date=as_of,
        snapshot=resolved_snapshot,
        config=config,
    )
    source_gaps = list(visit_gaps) if visit_gaps is not None else _load_visit_gap_docs(
        site_ids,
        end_date=as_of,
        config=config,
    )

    visits_by_site: dict[str, list[tuple[dt.date, Doc]]] = {site_id: [] for site_id in site_ids}
    qc_by_site: dict[str, list[tuple[dt.date, Doc]]] = {site_id: [] for site_id in site_ids}
    for visit in source_visits:
        if _is_archived_visit(visit):
            continue
        site_id = _doc_site_id(visit)
        visit_date = _doc_date(visit)
        if site_id not in site_ids or visit_date is None or visit_date > as_of:
            continue
        if not _visit_matches_operator(visit, operator_aliases):
            continue
        visits_by_site.setdefault(site_id, []).append((visit_date, visit))
        if _is_qc_visit(visit):
            qc_by_site.setdefault(site_id, []).append((visit_date, visit))

    gap_dates_by_site: dict[str, list[str]] = {site_id: [] for site_id in site_ids}
    for gap in source_gaps:
        site_id = _doc_site_id(gap)
        gap_date = _doc_date(gap)
        if site_id not in site_ids or gap_date is None or gap_date > as_of:
            continue
        if not _is_open_gap(gap):
            continue
        gap_dates_by_site.setdefault(site_id, []).append(gap_date.isoformat())

    account_rows: list[dict[str, Any]] = []
    for account in active_accounts:
        site_id = account["site_id"]
        latest_visit = _latest_doc(visits_by_site.get(site_id, []))
        latest_qc = _latest_doc(qc_by_site.get(site_id, []))
        last_visit_date = latest_visit[0] if latest_visit is not None else None
        last_qc_date = latest_qc[0] if latest_qc is not None else None
        open_gap_dates = sorted(set(gap_dates_by_site.get(site_id, [])))
        account_rows.append(
            {
                "site_id": site_id,
                "site_name": account["site_name"],
                "last_visit_date": last_visit_date.isoformat() if last_visit_date else None,
                "last_qc_date": last_qc_date.isoformat() if last_qc_date else None,
                "days_since_last_visit": _days_since(last_visit_date, as_of),
                "days_since_last_qc": _days_since(last_qc_date, as_of),
                "open_gap_dates": open_gap_dates,
            }
        )

    overdue = sorted(
        (
            row
            for row in account_rows
            if row["days_since_last_qc"] is None
            or row["days_since_last_qc"] > OVERDUE_QC_DAYS
        ),
        key=_overdue_sort_key,
    )
    return {"accounts": account_rows, "overdue": overdue}


def coverage_report(
    operator: object,
    *,
    now: dt.datetime | dt.date | str | None = None,
    week_of: dt.datetime | dt.date | str | None = None,
    target: int = DEFAULT_WEEKLY_QC_TARGET,
    visits: Iterable[Doc] | None = None,
    visit_gaps: Iterable[Doc] | None = None,
    accounts: Iterable[Doc] | Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    config: couchdb_config.CouchDBConfig | None = None,
) -> dict[str, Any]:
    """Return a stable machine-readable Visit/QC coverage report."""

    generated_at = _generated_at(now)
    resolved_snapshot = snapshot
    if accounts is None and resolved_snapshot is None:
        resolved_snapshot = operator_context_snapshot(operator, now=generated_at)

    as_of = _coerce_date(now)
    source_visits = list(visits) if visits is not None else _load_visit_docs(
        operator,
        end_date=as_of,
        snapshot=resolved_snapshot,
        config=config,
    )
    account_result = account_coverage(
        operator,
        now=now,
        visits=source_visits,
        visit_gaps=visit_gaps,
        accounts=accounts,
        snapshot=resolved_snapshot,
        config=config,
    )
    weekly = weekly_qc_count(
        operator,
        now=now,
        week_of=week_of,
        target=target,
        visits=source_visits,
        snapshot=resolved_snapshot,
        config=config,
    )
    gaps = _flatten_gap_rows(account_result["accounts"])
    return {
        "operator": str(operator),
        "generated_at": generated_at,
        "weekly": weekly,
        "accounts": account_result["accounts"],
        "overdue": account_result["overdue"],
        "overdue_qc_days": OVERDUE_QC_DAYS,
        "gaps": gaps,
    }


def _load_visit_docs(
    operator: object,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    snapshot: Mapping[str, Any] | None = None,
    config: couchdb_config.CouchDBConfig | None = None,
) -> list[dict[str, Any]]:
    # Do not filter archived visits in Mango: CouchDB `$ne: true` excludes
    # documents missing `archived`, which are legitimate non-archived visits.
    # Archived exclusion happens in-memory via `_is_archived_visit`.
    selector: dict[str, Any] = {"type": "visit"}
    date_selector: dict[str, str] = {}
    if start_date is not None:
        date_selector["$gte"] = start_date.isoformat()
    if end_date is not None:
        date_selector["$lte"] = end_date.isoformat()
    if date_selector:
        selector["date"] = date_selector

    exact_aliases = sorted(_operator_exact_aliases(operator, snapshot=snapshot))
    if exact_aliases:
        selector["$or"] = [
            {"operator": {"$in": exact_aliases}},
            {"visited_by": {"$in": exact_aliases}},
        ]
    return _find_vault_docs(selector, config=config)


def _load_visit_gap_docs(
    site_ids: set[str],
    *,
    end_date: dt.date,
    config: couchdb_config.CouchDBConfig | None = None,
) -> list[dict[str, Any]]:
    if not site_ids:
        return []
    selector = {
        "type": "visit_gap",
        "site_id": {"$in": sorted(site_ids)},
        "date": {"$lte": end_date.isoformat()},
    }
    return _find_vault_docs(selector, config=config)


def _find_vault_docs(
    selector: Mapping[str, Any],
    *,
    config: couchdb_config.CouchDBConfig | None = None,
    limit: int = DEFAULT_FIND_LIMIT,
) -> list[dict[str, Any]]:
    cfg = config or couchdb_config.from_env()
    db = urllib.parse.quote(couchdb_config.vault_database(), safe="")
    url = f"{cfg.base_url.rstrip('/')}/{db}/_find"
    body = json.dumps({"selector": selector, "limit": limit}, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json", **cfg.auth_header()}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisitCoverageError(f"CouchDB vault _find failed: {exc}") from exc
    docs = payload.get("docs") if isinstance(payload, Mapping) else None
    if not isinstance(docs, list):
        raise VisitCoverageError("CouchDB vault _find returned no docs list")
    return [doc for doc in docs if isinstance(doc, dict)]


def is_qc_visit_type(value: object) -> bool:
    return str(value or "").strip().lower() in QC_VISIT_TYPES


def _is_qc_visit(visit: Doc) -> bool:
    return is_qc_visit_type(visit.get("visit_type"))


def _is_archived_visit(visit: Doc) -> bool:
    return bool(visit.get("archived"))


def _operator_coverage_identity(
    operator: object, *, snapshot: Mapping[str, Any] | None = None
) -> str:
    raw = _clean_string(operator)
    if raw:
        return _normalize_text(raw)
    aliases = sorted(_operator_aliases(operator, snapshot=snapshot))
    return aliases[0] if aliases else ""


def _weekly_qc_preference_key(
    row: Mapping[str, Any], visit: Doc
) -> tuple[int, int, str, str, str]:
    return (
        1 if _has_canonical_site_name(row) else 0,
        _evidence_length(row.get("evidence")),
        _timestamp_sort_value(visit.get("timestamp")),
        _timestamp_sort_value(visit.get("timestamp_local")),
        _stable_doc_sort_value(visit),
    )


def _has_canonical_site_name(row: Mapping[str, Any]) -> bool:
    site = _clean_string(row.get("site"))
    site_id = _clean_string(row.get("site_id"))
    return bool(site and site != site_id)


def _evidence_length(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        text = str(value)
    return len(text.strip())


def _timestamp_sort_value(value: object) -> str:
    parsed = _parse_timestamp(value)
    if parsed is not None:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.isoformat()
    return _clean_string(value)


def _parse_timestamp(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)
    text = _clean_string(value)
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _stable_doc_sort_value(visit: Doc) -> str:
    try:
        return json.dumps(visit, sort_keys=True, default=str)
    except TypeError:
        return str(sorted((str(key), str(value)) for key, value in visit.items()))


def _visit_matches_operator(visit: Doc, aliases: set[str]) -> bool:
    """Match the installation operator stamp or an existing name alias.

    In a multi-operator installation, a stamped visit whose ``visited_by``
    clearly names another resolved person may be over-attributed. Resolving
    that ambiguity is intentionally outside this single-operator read model.
    """

    operator = _clean_string(visit.get("operator"))
    visited_by = _clean_string(visit.get("visited_by"))
    if operator == current_operator_id():
        return True
    if operator and _normalize_text(operator) in aliases:
        return True
    if visited_by and _text_matches_alias(visited_by, aliases):
        return True
    return False


def _operator_aliases(operator: object, *, snapshot: Mapping[str, Any] | None = None) -> set[str]:
    values = _operator_exact_aliases(operator, snapshot=snapshot)
    aliases = {_normalize_text(value) for value in values if _normalize_text(value)}
    raw = _clean_string(operator)
    if raw.startswith("op_"):
        aliases.add(_normalize_text(raw[3:]))
    return aliases


def _operator_exact_aliases(operator: object, *, snapshot: Mapping[str, Any] | None = None) -> set[str]:
    values = {_clean_string(operator), current_operator_id()}
    if snapshot is not None:
        values.add(_clean_string(snapshot.get("operator")))
        resolution = snapshot.get("resolution")
        if isinstance(resolution, Mapping):
            for candidate in resolution.get("candidates", []) or []:
                if isinstance(candidate, Mapping):
                    values.add(_clean_string(candidate.get("canonical_name")))
                    values.add(_clean_string(candidate.get("display_name")))
    return {value for value in values if value}


def _text_matches_alias(value: object, aliases: set[str]) -> bool:
    normalized = _normalize_text(value)
    if normalized in aliases:
        return True
    words = set(normalized.split())
    for alias in aliases:
        alias_words = set(alias.split())
        if not alias_words:
            continue
        if alias_words <= words:
            return True
        if len(alias_words) == 1 and alias in words:
            return True
    return False


def _account_docs_from_injection(accounts: Iterable[Doc] | Mapping[str, Any]) -> list[Doc]:
    if isinstance(accounts, Mapping):
        injected = accounts.get("accounts")
        if isinstance(injected, Iterable) and not isinstance(injected, (str, bytes, Mapping)):
            return list(injected)
    return list(accounts)  # type: ignore[arg-type]


def _normalize_account(account: Doc) -> dict[str, Any] | None:
    site_id = _doc_site_id(account) or _clean_string(account.get("job_number"))
    if not site_id:
        return None
    site_name = _clean_string(
        account.get("site_name")
        or account.get("canonical_name")
        or account.get("account")
        or account.get("name")
        or account.get("site")
        or site_id
    )
    status = str(account.get("status") or "").strip().lower()
    active = account.get("active")
    is_active = active is not False and status not in {"inactive", "closed", "lost", "archived"}
    return {"site_id": site_id, "site_name": site_name or site_id, "active": is_active}


def _doc_site_id(doc: Doc) -> str:
    value = doc.get("site_id") or doc.get("job") or doc.get("job_number")
    if value is None:
        doc_id = _clean_string(doc.get("_id"))
        if doc_id.startswith("site_"):
            value = doc_id[5:]
        elif doc_id.startswith("location_"):
            value = doc_id[9:]
    return _clean_string(value)


def _doc_date(doc: Doc) -> dt.date | None:
    for key in ("date", "visit_date", "timestamp", "created_at"):
        parsed = _parse_date(doc.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean_string(value)
    if not text:
        return None
    if len(text) >= 10:
        text = text[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _coerce_date(value: dt.datetime | dt.date | str | None) -> dt.date:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return value.astimezone(DEFAULT_COVERAGE_ZONE).date()
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _clean_string(value)
    if text:
        parsed_datetime = _parse_anchor_datetime(text)
        if parsed_datetime is not None:
            if parsed_datetime.tzinfo is not None:
                return parsed_datetime.astimezone(DEFAULT_COVERAGE_ZONE).date()
            return parsed_datetime.date()
        parsed = _parse_date(text)
        if parsed is not None:
            return parsed
    return dt.datetime.now(dt.timezone.utc).astimezone(DEFAULT_COVERAGE_ZONE).date()


def _parse_anchor_datetime(text: str) -> dt.datetime | None:
    if len(text) <= 10:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _generated_at(value: dt.datetime | dt.date | str | None) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc).isoformat()
    text = _clean_string(value)
    if text:
        return text
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _iso_week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    week_start = day - dt.timedelta(days=day.weekday())
    return week_start, week_start + dt.timedelta(days=6)


def _completed_visit_shape(visit: Doc, visit_date: dt.date) -> dict[str, Any]:
    return {
        "site_id": _doc_site_id(visit) or None,
        "site": _clean_string(visit.get("site") or visit.get("site_name") or visit.get("account")) or None,
        "date": visit_date.isoformat(),
        "evidence": visit.get("evidence"),
    }


def _latest_doc(docs: list[tuple[dt.date, Doc]]) -> tuple[dt.date, Doc] | None:
    if not docs:
        return None
    return max(docs, key=lambda item: item[0])


def _days_since(day: dt.date | None, as_of: dt.date) -> int | None:
    if day is None:
        return None
    return max(0, (as_of - day).days)


def _overdue_sort_key(row: Mapping[str, Any]) -> tuple[int, int, str]:
    days = row.get("days_since_last_qc")
    if days is None:
        return (0, 0, str(row.get("site_name") or row.get("site_id") or ""))
    return (1, -int(days), str(row.get("site_name") or row.get("site_id") or ""))


def _flatten_gap_rows(accounts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for account in accounts:
        for gap_date in account.get("open_gap_dates") or []:
            gaps.append(
                {
                    "site_id": account.get("site_id"),
                    "site_name": account.get("site_name"),
                    "date": gap_date,
                }
            )
    gaps.sort(key=lambda item: (str(item.get("date") or ""), str(item.get("site_id") or "")))
    return gaps


def _is_open_gap(gap: Doc) -> bool:
    status = str(gap.get("status") or gap.get("state") or "").strip().lower()
    if status in {"closed", "resolved", "cleared", "dismissed"}:
        return False
    return not bool(gap.get("resolved_at") or gap.get("closed_at"))


def _clean_string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text(value: object) -> str:
    return " ".join(_clean_string(value).lower().replace("_", " ").replace("-", " ").split())


def _site_sort_key(site_id: str) -> tuple[int, object]:
    try:
        return (0, int(site_id))
    except ValueError:
        return (1, site_id)
