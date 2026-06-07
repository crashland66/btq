from __future__ import annotations

from typing import Any

from btq_vault.projector import DDOC, ProjectorError, query_view


ENTITY_TYPES = (
    "visit",
    "employee",
    "site_issue",
    "supply_need",
    "equipment_request",
    "personnel_event",
)


def export_today(
    base_url: str,
    auth_headers: dict,
    database: str,
    date: str,
    timeout: float = 10.0,
) -> str:
    """
    Returns a markdown-formatted summary of btq_vault entity activity for date.
    Raises ProjectorError on CouchDB failure.
    """
    docs_by_type = {type_name: _docs_for_type(base_url, auth_headers, database, type_name, timeout) for type_name in ENTITY_TYPES}
    sections: list[tuple[str, list[str]]] = []

    visits = [
        f"- {_display(doc.get('site') or doc.get('site_id'))} — {_display(doc.get('visit_type') or 'visit')} "
        f"(confidence: {_display(doc.get('confidence'))}) by {_display(doc.get('visited_by') or 'unknown')}"
        for doc in docs_by_type["visit"]
        if doc.get("date") == date
    ]
    if visits:
        sections.append(("Visits", visits))

    new_employees = [
        f"- {_display(doc.get('name') or doc.get('person_id'))} — hired {_display(doc.get('hire_date'))}"
        for doc in docs_by_type["employee"]
        if doc.get("hire_date") == date
    ]
    if new_employees:
        sections.append(("New Employees", new_employees))

    site_issues = [
        f"- {_display(doc.get('title') or doc.get('issue_id'))} at {_display(doc.get('site_name') or doc.get('site_id'))} [{_display(doc.get('status'))}]"
        for doc in docs_by_type["site_issue"]
        if str(doc.get("created_at") or "").startswith(date)
    ]
    if site_issues:
        sections.append(("Site Issues", site_issues))

    supply_needs = [
        f"- {_display(doc.get('item') or doc.get('item_name') or doc.get('supply_id'))} at {_display(doc.get('site_name') or doc.get('site_id'))} [{_display(doc.get('status'))}]"
        for doc in docs_by_type["supply_need"]
        if str(doc.get("created_at") or "").startswith(date)
    ]
    if supply_needs:
        sections.append(("Supply Needs", supply_needs))

    equipment_requests = [
        f"- {_display(doc.get('item') or doc.get('equipment_name') or doc.get('equipment_id'))} at {_display(doc.get('site_name') or doc.get('site_id'))} [{_display(doc.get('status'))}]"
        for doc in docs_by_type["equipment_request"]
        if str(doc.get("created_at") or "").startswith(date)
    ]
    if equipment_requests:
        sections.append(("Equipment Requests", equipment_requests))

    personnel_events = [
        f"- {_display(doc.get('event_type') or 'event')}: {_display(doc.get('name') or doc.get('person_id'))}"
        for doc in docs_by_type["personnel_event"]
        if str(doc.get("created_at") or "").startswith(date)
    ]
    if personnel_events:
        sections.append(("Personnel Events", personnel_events))

    if not sections:
        return f"No entity activity recorded in btq_vault for {date}."

    lines = [f"# BTQ Entity Activity — {date}", ""]
    for title, items in sections:
        lines.extend([f"## {title}", *items, ""])
    return "\n".join(lines).rstrip() + "\n"


def _docs_for_type(base_url: str, auth_headers: dict, database: str, type_name: str, timeout: float) -> list[dict[str, Any]]:
    rows = query_view(
        base_url,
        auth_headers,
        database,
        DDOC,
        "by_type",
        startkey=[type_name, None],
        endkey=[type_name, {}],
        include_docs=True,
        timeout=timeout,
    )
    docs: list[dict[str, Any]] = []
    for row in rows:
        doc = row.get("doc")
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def _display(value: object) -> str:
    text = str(value or "").strip()
    return text or "unknown"
