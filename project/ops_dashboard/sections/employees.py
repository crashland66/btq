from __future__ import annotations

import html
from typing import Any
from urllib.parse import quote

from btq_vault.entity_types import current_operator_id
from btq_vault.projector import DDOC, query_view
from ops_dashboard.common import employee_is_active, first_query_value, render_table
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


def _uniform_status(doc: dict[str, Any]) -> str:
    status = _string(doc.get("uniform_status")).lower()
    return status if status in {"adequate", "needs_shirts"} else "unknown"


def _uniform_cell(value: object, row: dict[str, object]) -> str:
    status = _string(value) or "unknown"
    labels = {"adequate": "Ready", "needs_shirts": "Needs shirts", "unknown": "Awaiting response"}
    pill_class = "success" if status == "adequate" else "warning"
    details: list[str] = []
    count = row.get("uniform_shirt_count")
    if isinstance(count, int) and not isinstance(count, bool):
        details.append(f'{count} shirt{"s" if count != 1 else ""}')
    size = _string(row.get("uniform_shirt_size"))
    if size:
        details.append(f"size {html.escape(size)}")
    detail_html = f' <span class="muted">{" · ".join(details)}</span>' if details else ""
    return f'<span class="pill {pill_class}">{html.escape(labels.get(status, status.title()))}</span>{detail_html}'


def _active_assigned_employees(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return Greg's real active roster, excluding operator and sandbox records."""
    excluded_ids = {"employee_stoltz_gregory", "employee_sandbox-user"}
    operator_id = current_operator_id()
    return [
        doc
        for doc in docs
        if employee_is_active(doc)
        and _string(doc.get("job"))
        and _string(doc.get("job")).upper() != "SANDBOX"
        and _string(doc.get("_id")) not in excluded_ids
        and (not _string(doc.get("operator")) or _string(doc.get("operator")) == operator_id)
    ]


def _contact_value(doc: dict[str, Any], field: str) -> str:
    value = doc.get(field)
    if isinstance(value, str):
        return value.strip()
    return ""


def _communication_panel(docs: list[dict[str, Any]]) -> str:
    roster = _active_assigned_employees(docs)
    emails = sorted({_contact_value(doc, "email").lower() for doc in roster if "@" in _contact_value(doc, "email")})
    phones = sorted({"".join(ch for ch in _contact_value(doc, "phone") if ch.isdigit()) for doc in roster})
    phones = [phone for phone in phones if len(phone) >= 10]
    missing_email = [_display_name(doc) for doc in roster if "@" not in _contact_value(doc, "email")]
    missing_phone = [
        _display_name(doc)
        for doc in roster
        if len("".join(ch for ch in _contact_value(doc, "phone") if ch.isdigit())) < 10
    ]
    uniform_counts = {
        status: sum(1 for doc in roster if _uniform_status(doc) == status)
        for status in ("adequate", "needs_shirts", "unknown")
    }

    email_list = ", ".join(emails)
    phone_list = ", ".join(phones)
    email_button = (
        f'<button type="button" data-copy-value="{html.escape(email_list, quote=True)}">Copy BCC emails</button>'
        if email_list
        else '<button type="button" disabled>Copy BCC emails</button>'
    )
    phone_button = (
        f'<button type="button" data-copy-value="{html.escape(phone_list, quote=True)}">Copy phone list</button>'
        if phone_list
        else '<button type="button" disabled>Copy phone list</button>'
    )
    draft_link = (
        f'<a class="button" href="mailto:?bcc={quote(email_list, safe="@,.")}">Draft BCC email</a>'
        if email_list
        else ""
    )

    gaps: list[str] = []
    if missing_email:
        gaps.append(f'Missing email: {html.escape(", ".join(missing_email))}.')
    if missing_phone:
        gaps.append(f'Missing phone: {html.escape(", ".join(missing_phone))}.')
    gap_html = f'<p class="notice">{" ".join(gaps)}</p>' if gaps else '<p class="muted">Every active assigned employee has both a phone number and email address.</p>'
    employee_label = "employee" if len(roster) == 1 else "employees"
    return f"""
      <section>
        <h2>Active employee communications</h2>
        <p class="muted">Canonical roster: {len(roster)} active assigned {employee_label} · {len(emails)} emails · {len(phones)} phone numbers. Use BCC so employees do not see one another's addresses.</p>
        <p class="muted">Uniforms: {uniform_counts['adequate']} ready · {uniform_counts['needs_shirts']} need shirts · {uniform_counts['unknown']} awaiting response.</p>
        <p>{email_button} {draft_link} {phone_button}</p>
        {gap_html}
      </section>
    """


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    try:
        docs = load_employees()
        error_html = ""
    except Exception as exc:  # noqa: BLE001
        docs = []
        error_html = f'<section class="error"><p>{html.escape(str(exc))}</p></section>'

    communication_html = _communication_panel(docs) if not error_html else ""
    status_filter = first_query_value(query, "status") or "all"
    uniform_status_filter = first_query_value(query, "uniform_status") or "all"
    name_filter = first_query_value(query, "name_contains").strip().lower()
    primary_site_filter = first_query_value(query, "primary_site_contains").strip().lower()

    if status_filter == "active":
        docs = [doc for doc in docs if employee_is_active(doc)]
    elif status_filter == "inactive":
        docs = [doc for doc in docs if not employee_is_active(doc)]
    if uniform_status_filter in {"adequate", "needs_shirts", "unknown"}:
        docs = [doc for doc in docs if _uniform_status(doc) == uniform_status_filter]
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
            "uniform_status": _uniform_status(doc),
            "uniform_shirt_count": doc.get("uniform_shirt_count"),
            "uniform_shirt_size": _string(doc.get("uniform_shirt_size")),
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
            {"key": "uniform_status", "label": "Uniform", "format": _uniform_cell},
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
        <label>Uniform status
          <select name="uniform_status">
            <option value="all" {'selected' if uniform_status_filter == 'all' else ''}>All</option>
            <option value="unknown" {'selected' if uniform_status_filter == 'unknown' else ''}>Awaiting response</option>
            <option value="needs_shirts" {'selected' if uniform_status_filter == 'needs_shirts' else ''}>Needs shirts</option>
            <option value="adequate" {'selected' if uniform_status_filter == 'adequate' else ''}>Ready</option>
          </select>
        </label>
        <label>Name contains <input name="name_contains" value="{html.escape(first_query_value(query, 'name_contains'))}"></label>
        <label>Primary site contains <input name="primary_site_contains" value="{html.escape(first_query_value(query, 'primary_site_contains'))}"></label>
        <button>Apply</button>
      </form>
    """
    body = f'<header><h1>Employees</h1><p class="muted">Browse employee records from the operational vault.</p><p><a class="button" href="/clients">Clients / Devices</a></p></header>{error_html}{communication_html}<div class="content-with-rail"><aside class="filter-rail"><section><h2>Filters</h2>{filter_html}</section></aside><section><h2>All employees</h2>{table}</section></div>'
    return html_page("Employees", body, active_section="employees")
