from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote

from btq_vault.projector import DDOC, query_view
from ops_dashboard.common import first_query_value, render_table
from ops_dashboard.layout import html_page
from .employee_detail import _cdb, _load_vault_doc  # noqa: F401


def _string(value: object) -> str:
    return str(value or "").strip()


def _display_name(doc: dict[str, Any]) -> str:
    first = _string(doc.get("first"))
    last = _string(doc.get("last"))
    fallback = _string(doc.get("name")) or _string(doc.get("_id"))
    if first and last:
        return f"{last}, {first}"
    if last:
        return last
    if first:
        return first
    return fallback


def _sort_key(doc: dict[str, Any]) -> tuple[str, str]:
    first = _string(doc.get("first"))
    last = _string(doc.get("last"))
    fallback = _string(doc.get("name")) or _string(doc.get("_id"))
    if first and last:
        return last.lower(), first.lower()
    if last:
        return last.lower(), ""
    if first:
        return first.lower(), ""
    return fallback.lower(), ""


def load_employees() -> list[dict[str, Any]]:
    base, headers, database, timeout = _cdb()
    rows = query_view(base, headers, database, DDOC, "employees_by_status", include_docs=True, timeout=timeout)
    docs: dict[str, dict[str, Any]] = {}
    for row in rows:
        doc = row.get("doc") if isinstance(row, dict) else None
        if isinstance(doc, dict) and str(doc.get("type")) == "employee":
            docs[str(doc.get("_id") or "")] = doc
    return sorted(docs.values(), key=_sort_key)


def _employee_name_link(value: object, row: dict[str, object]) -> str:
    name = html.escape(_string(value))
    employee_id = _string(row.get("_id"))
    if not employee_id:
        return name
    bare_id = employee_id.removeprefix("employee_")
    return f'<a href="/employees/{quote(bare_id, safe="")}">{name}</a>'


def _primary_site_link(value: object, _row: dict[str, object]) -> str:
    site_id = _string(value)
    if not site_id:
        return ""
    escaped_site_id = html.escape(site_id)
    return f'<a href="/sites/{quote(site_id, safe="")}">{escaped_site_id}</a>'


def _status_pill(value: object, _row: dict[str, object]) -> str:
    status = _string(value)
    label = status.title() if status else "Unknown"
    pill_class = "success" if status.lower() == "active" else "warning"
    return f'<span class="pill {pill_class}">{html.escape(label)}</span>'


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    try:
        docs = load_employees()
        error_html = ""
    except Exception as exc:  # noqa: BLE001
        docs = []
        error_html = f'<section class="error"><p>{html.escape(str(exc))}</p></section>'

    status_filter = first_query_value(query, "status") or "all"
    name_filter = first_query_value(query, "name_contains").strip().lower()
    primary_site_filter = first_query_value(query, "primary_site_contains").strip().lower()

    if status_filter == "active":
        docs = [doc for doc in docs if _string(doc.get("status")).lower() == "active"]
    elif status_filter == "inactive":
        docs = [doc for doc in docs if _string(doc.get("status")).lower() != "active"]
    if name_filter:
        docs = [doc for doc in docs if name_filter in _display_name(doc).lower()]
    if primary_site_filter:
        docs = [doc for doc in docs if primary_site_filter in _string(doc.get("job")).lower()]

    rows = [
        {
            "_id": _string(doc.get("_id")),
            "name": _display_name(doc),
            "primary_site": _string(doc.get("job")),
            "status": _string(doc.get("status")),
            "phone": _string(doc.get("phone")),
            "email": _string(doc.get("email")),
        }
        for doc in docs
    ]
    table = render_table(
        rows,
        [
            {"key": "name", "label": "Name", "format": _employee_name_link},
            {"key": "primary_site", "label": "Primary Site", "format": _primary_site_link},
            {"key": "status", "label": "Status", "format": _status_pill, "nowrap": True},
            {"key": "phone", "label": "Phone", "priority": 2},
            {"key": "email", "label": "Email", "priority": 3},
        ],
        empty_text="No employee records.",
    )
    filter_html = f"""
      <form method="get" action="/employees" data-submit-on-change>
        <label><input type="radio" name="status" value="all" {'checked' if status_filter == 'all' else ''}> All</label>
        <label><input type="radio" name="status" value="active" {'checked' if status_filter == 'active' else ''}> Active</label>
        <label><input type="radio" name="status" value="inactive" {'checked' if status_filter == 'inactive' else ''}> Inactive</label>
        <label>Name contains <input name="name_contains" value="{html.escape(first_query_value(query, 'name_contains'))}"></label>
        <label>Primary site contains <input name="primary_site_contains" value="{html.escape(first_query_value(query, 'primary_site_contains'))}"></label>
        <button>Apply</button>
      </form>
    """
    body = f'<header><h1>Employees</h1><p class="muted">Browse employee records from the operational vault.</p><p><a class="button" href="/clients">Clients / Devices</a></p></header>{error_html}<div class="content-with-rail"><aside class="filter-rail"><section><h2>Filters</h2>{filter_html}</section></aside><section><h2>All employees</h2>{table}</section></div>'
    return html_page("Employees", body, active_section="employees")
