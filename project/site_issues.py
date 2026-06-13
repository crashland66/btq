from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CURRENT_STATUSES = {"open", "monitoring"}
ISSUE_STATUSES = {"open", "monitoring", "resolved"}


@dataclass(frozen=True)
class SiteIssue:
    issue_id: str
    site_id: str
    site: str
    account: str
    title: str
    status: str
    priority: str
    category: str
    client_notified: bool
    client_notified_at: str
    client_notified_by: str
    client_notified_method: str
    reported_by: str
    observed_at: str
    created_at: str
    updated_at: str
    summary: str
    resolution_trigger: str
    monitoring_at: str
    monitoring_by: str
    monitoring_note: str
    resolved_at: str
    resolved_by: str
    resolved_note: str
    archived: bool
    archived_at: str
    archived_by: str
    related_capture_ids: tuple[str, ...]
    related_candidate_ids: tuple[str, ...]
    path: Path


def discover_site_issues(
    vault_root: Path,
    *,
    site_id: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
) -> dict[str, object]:
    """Return site issues from CouchDB (``btq_vault``, canonical).

    ``vault_root`` is accepted for call-site compatibility but no longer used.
    """
    return _discover_site_issues_couchdb(site_id=site_id, include_archived=include_archived, archived_only=archived_only)


def _site_issue_from_couch_doc(doc: dict[str, Any]) -> SiteIssue | None:
    if not isinstance(doc, dict):
        return None
    site_id = clean_string(doc.get("site_id"))
    issue_id = clean_string(doc.get("issue_id"))
    if not site_id or not issue_id:
        return None
    status = clean_string(doc.get("status")) or "open"
    if status not in ISSUE_STATUSES:
        return None
    return SiteIssue(
        issue_id=issue_id,
        site_id=site_id,
        site=clean_string(doc.get("site_name") or doc.get("site")),
        account=clean_string(doc.get("account")),
        title=clean_string(doc.get("title")) or issue_id,
        status=status,
        priority=clean_string(doc.get("priority")) or "normal",
        category=clean_string(doc.get("category")) or "other",
        client_notified=bool(doc.get("client_notified")),
        client_notified_at=clean_string(doc.get("client_notified_at")),
        client_notified_by=clean_string(doc.get("client_notified_by")),
        client_notified_method=clean_string(doc.get("client_notified_method")),
        reported_by=clean_string(doc.get("reported_by")),
        observed_at=clean_string(doc.get("observed_at")),
        created_at=clean_string(doc.get("created_at")),
        updated_at=clean_string(doc.get("updated_at")),
        summary=clean_string(doc.get("summary")),
        resolution_trigger=clean_string(doc.get("resolution_trigger")),
        monitoring_at=clean_string(doc.get("monitoring_at")),
        monitoring_by=clean_string(doc.get("monitoring_by")),
        monitoring_note=clean_string(doc.get("monitoring_note")),
        resolved_at=clean_string(doc.get("resolved_at")),
        resolved_by=clean_string(doc.get("resolved_by")),
        resolved_note=clean_string(doc.get("resolved_note")),
        archived=bool(doc.get("archived")),
        archived_at=clean_string(doc.get("archived_at")),
        archived_by=clean_string(doc.get("archived_by")),
        related_capture_ids=tuple(str(x) for x in (doc.get("related_capture_ids") or []) if x),
        related_candidate_ids=tuple(str(x) for x in (doc.get("related_candidate_ids") or []) if x),
        path=Path(str(doc.get("_id") or issue_id)),
    )


def _include_archived_item(archived: bool, *, include_archived: bool, archived_only: bool) -> bool:
    if archived_only:
        return archived
    return include_archived or not archived


def _discover_site_issues_couchdb(
    *,
    site_id: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
) -> dict[str, object]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector: dict[str, object] = {"type": "site_issue"}
    if archived_only:
        selector["archived"] = True
    elif not include_archived:
        selector["archived"] = {"$ne": True}
    if site_id is not None:
        selector["site_id"] = str(site_id)
    payload = {"selector": selector, "limit": 100000}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, never hang the page
        return {
            "issues": [],
            "warnings": [{"path": db_name, "reason": f"couchdb_query_failed:{exc}"}],
            "counts": issue_counts([]),
        }
    issues: list[SiteIssue] = []
    for doc in response.get("docs", []):
        issue = _site_issue_from_couch_doc(doc)
        if issue is None:
            continue
        if not _include_archived_item(issue.archived, include_archived=include_archived, archived_only=archived_only):
            continue
        if site_id is not None and issue.site_id != str(site_id):
            continue
        issues.append(issue)
    issues.sort(key=lambda item: (status_sort(item.status), item.site_id, item.priority, item.created_at, item.issue_id))
    return {"issues": issues, "warnings": [], "counts": issue_counts(issues, include_archived=include_archived or archived_only)}


def clean_string(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", '""', "''", "null", "None"}:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def issue_counts(issues: Iterable[SiteIssue], *, include_archived: bool = False) -> dict[str, object]:
    items = [issue for issue in issues if include_archived or not issue.archived]
    current = [issue for issue in items if issue.status in CURRENT_STATUSES]
    return {
        "total": len(items),
        "current": len(current),
        "open": sum(1 for issue in items if issue.status == "open"),
        "monitoring": sum(1 for issue in items if issue.status == "monitoring"),
        "resolved": sum(1 for issue in items if issue.status == "resolved"),
        "by_site": count_by(items, "site_id"),
        "by_status": count_by(items, "status"),
        "by_category": count_by(items, "category"),
        "by_priority": count_by(items, "priority"),
        "client_notified": {
            "true": sum(1 for issue in current if issue.client_notified),
            "false": sum(1 for issue in current if not issue.client_notified),
        },
    }


def count_by(issues: Iterable[SiteIssue], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        value = str(getattr(issue, field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def status_sort(status: str) -> int:
    return {"open": 0, "monitoring": 1, "resolved": 2}.get(status, 9)


def issue_as_export(issue: SiteIssue, *, include_path: bool = False) -> dict[str, object]:
    payload = {
        "issue_id": issue.issue_id,
        "site_id": issue.site_id,
        "site": issue.site,
        "account": issue.account,
        "title": issue.title,
        "status": issue.status,
        "priority": issue.priority,
        "category": issue.category,
        "client_notified": issue.client_notified,
        "client_notified_at": issue.client_notified_at,
        "client_notified_by": issue.client_notified_by,
        "client_notified_method": issue.client_notified_method,
        "reported_by": issue.reported_by,
        "observed_at": issue.observed_at,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "summary": issue.summary,
        "resolution_trigger": issue.resolution_trigger,
        "monitoring_at": issue.monitoring_at,
        "monitoring_by": issue.monitoring_by,
        "monitoring_note": issue.monitoring_note,
        "resolved_at": issue.resolved_at,
        "resolved_by": issue.resolved_by,
        "resolved_note": issue.resolved_note,
        "archived": issue.archived,
        "archived_at": issue.archived_at,
        "archived_by": issue.archived_by,
        "related_capture_ids": list(issue.related_capture_ids),
        "related_candidate_ids": list(issue.related_candidate_ids),
    }
    if include_path:
        payload["vault_issue_path"] = str(issue.path)
    return payload
