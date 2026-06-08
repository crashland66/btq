from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from vault_markdown import frontmatter_list, read_typed_markdown_note


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
    related_capture_ids: tuple[str, ...]
    related_candidate_ids: tuple[str, ...]
    path: Path


def discover_site_issues(vault_root: Path, *, site_id: str | None = None) -> dict[str, object]:
    """Return site issues.

    CouchDB (``btq_vault``) is the canonical store for site issues. When CouchDB
    is configured (production) we read from it and never touch the filesystem
    vault — that vault is an iCloud-synced Obsidian folder, and a background
    launchd process scanning it (dataless materialization) blocks indefinitely.
    With no CouchDB env (dev/CI) we fall back to the on-disk Markdown glob.
    """
    if _couchdb_site_issues_enabled():
        return _discover_site_issues_couchdb(site_id=site_id)
    return _discover_site_issues_filesystem(vault_root, site_id=site_id)


def _couchdb_site_issues_enabled() -> bool:
    import os

    return bool(os.environ.get("BTQ_COUCHDB_URL", "").strip())


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
        related_capture_ids=tuple(str(x) for x in (doc.get("btq_job_ids") or []) if x),
        related_candidate_ids=tuple(str(x) for x in (doc.get("related_candidate_ids") or []) if x),
        path=Path(str(doc.get("_id") or issue_id)),
    )


def _discover_site_issues_couchdb(*, site_id: str | None = None) -> dict[str, object]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector: dict[str, object] = {"type": "site_issue"}
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
        if site_id is not None and issue.site_id != str(site_id):
            continue
        issues.append(issue)
    issues.sort(key=lambda item: (status_sort(item.status), item.site_id, item.priority, item.created_at, item.issue_id))
    return {"issues": issues, "warnings": [], "counts": issue_counts(issues)}


def _discover_site_issues_filesystem(vault_root: Path, *, site_id: str | None = None) -> dict[str, object]:
    issues: list[SiteIssue] = []
    warnings: list[dict[str, str]] = []
    root = vault_root.expanduser().resolve(strict=False)
    accounts_root = root / "Accounts"
    if not accounts_root.exists():
        return {"issues": [], "warnings": [{"path": str(accounts_root), "reason": "accounts_root_missing"}], "counts": issue_counts([])}

    for path in sorted(accounts_root.glob("*/Locations/*/Issues/*.md")):
        issue, warning = parse_site_issue(path)
        if warning is not None:
            warnings.append(warning)
            continue
        if issue is None:
            continue
        if site_id is not None and issue.site_id != str(site_id):
            continue
        issues.append(issue)
    issues.sort(key=lambda item: (status_sort(item.status), item.site_id, item.priority, item.created_at, item.issue_id))
    return {"issues": issues, "warnings": warnings, "counts": issue_counts(issues)}


def parse_site_issue(path: Path) -> tuple[SiteIssue | None, dict[str, str] | None]:
    frontmatter, body, warning = read_typed_markdown_note(path, "site_issue")
    if warning is not None or frontmatter is None:
        return None, warning
    site_id = clean_string(frontmatter.get("site_id"))
    issue_id = clean_string(frontmatter.get("issue_id")) or path.stem.split("__", 1)[0]
    title = clean_string(frontmatter.get("title")) or path.stem
    if not site_id or not issue_id:
        return None, {"path": str(path), "reason": "missing_required_site_id_or_issue_id"}
    status = clean_string(frontmatter.get("status")) or "open"
    if status not in ISSUE_STATUSES:
        return None, {"path": str(path), "reason": f"unknown_status:{status}"}
    return (
        SiteIssue(
            issue_id=issue_id,
            site_id=site_id,
            site=clean_string(frontmatter.get("site")),
            account=clean_string(frontmatter.get("account")),
            title=title,
            status=status,
            priority=clean_string(frontmatter.get("priority")) or "normal",
            category=clean_string(frontmatter.get("category")) or "other",
            client_notified=bool_from_frontmatter(frontmatter.get("client_notified")),
            client_notified_at=clean_string(frontmatter.get("client_notified_at")),
            client_notified_by=clean_string(frontmatter.get("client_notified_by")),
            client_notified_method=clean_string(frontmatter.get("client_notified_method")),
            reported_by=clean_string(frontmatter.get("reported_by")),
            observed_at=clean_string(frontmatter.get("observed_at")),
            created_at=clean_string(frontmatter.get("created_at")),
            updated_at=clean_string(frontmatter.get("updated_at")),
            summary=summary_from_body(body),
            resolution_trigger=clean_string(frontmatter.get("resolution_trigger")),
            related_capture_ids=tuple(frontmatter_list(frontmatter.get("related_capture_ids"))),
            related_candidate_ids=tuple(frontmatter_list(frontmatter.get("related_candidate_ids"))),
            path=path.expanduser().resolve(strict=False),
        ),
        None,
    )


def clean_string(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"", '""', "''", "null", "None"}:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def bool_from_frontmatter(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean_string(value).lower() == "true"


def summary_from_body(body: str) -> str:
    lines = body.splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        if line.strip().lower() == "## summary":
            collecting = True
            continue
        if collecting and line.startswith("## "):
            break
        if collecting:
            stripped = line.strip()
            if stripped:
                collected.append(stripped)
    return " ".join(collected).strip()


def issue_counts(issues: Iterable[SiteIssue]) -> dict[str, object]:
    items = list(issues)
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
        "related_capture_ids": list(issue.related_capture_ids),
        "related_candidate_ids": list(issue.related_candidate_ids),
    }
    if include_path:
        payload["vault_issue_path"] = str(issue.path)
    return payload
