from __future__ import annotations

import re
import yaml
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from event_pipeline import couchdb_config
from event_pipeline.extraction_terms import get_extraction_terms
from field_capture.audio_semantics import ISSUE_TYPES
from field_capture.approved_job_drafts import (
    MAINTENANCE_TERMS,
    SUPPLY_NEED_ACTION_PATTERN,
    EQUIPMENT_REQUEST_ACTION_PATTERN,
)
from voice_memo.core import record_upload, UploadedVoiceMemo, safe_copy
from voice_memo.couchdb_watcher import (
    destination_audio_path as voice_memo_inbox_audio_path,
    sidecar_path_for as voice_memo_sidecar_path_for,
    sidecar_payload as voice_memo_sidecar_payload,
    write_sidecar as write_voice_memo_sidecar,
)
from field_capture.text_semantics import run_text_semantic_pipeline
from btq_vault.projector import (
    DDOC,
    ProjectorError,
    query_view,
    _build_site_records,
    _esc,
    _format_phone,
    _url_path,
    _string,
    _site_id,
    _section,
    _location_list as _projector_location_list,
    _grouped_rows,
    _opportunity_label,
    _rows_by_site,
    _status_rows,
    _row_date,
    _Location,
    _SiteRecord,
)
from btq_vault.entity_types import current_operator_identity
from event_pipeline.visit_coverage import coverage_report
import ops_dashboard.sections.inbox as _inbox_mod
import ops_dashboard.sections.field_photos as _field_photos_mod

from ops_dashboard.common import render_table
from ops_dashboard.layout import html_page

inbox_cards = _inbox_mod.inbox_cards
INBOX_SHAPE_CAPTURE = _inbox_mod.INBOX_SHAPE_CAPTURE
INBOX_SHAPE_STRUCTURED = _inbox_mod.INBOX_SHAPE_STRUCTURED

HOME_CARD_IDS = frozenset({
    "captures_with_note",
    "pending_candidates",
    "open_site_issues",
    "open_supply_needs",
    "open_equipment_requests",
    "unknown_captures",
    "failed_queue_jobs",
    "pipeline_health",
})

COVERAGE_PANEL_LIMIT = 5


def _render_card_rows(rows: list[dict], shape: str) -> str:
    if not rows:
        return '<p class="empty">No rows.</p>'
    row_htmls = []
    for row in rows:
        site = _esc(str(row.get("site") or ""))
        area = _esc(str(row.get("area") or ""))
        summary = _esc(str(row.get("summary") or ""))
        deep_link = str(row.get("deep_link") or "#")
        candidate_id = str(row.get("candidate_id") or "")

        # Summary cell — link to deep_link
        visit_proposed = bool(row.get("visit_proposed"))
        completion_marker = ' <span style="font-size:.78rem;opacity:.6">[completion]</span>' if (visit_proposed and shape == INBOX_SHAPE_CAPTURE and candidate_id) else ""
        summary_cell = f'<a href="{_esc(deep_link)}">{summary or "(no summary)"}</a>{completion_marker}'

        # Action buttons — only for capture-shape rows with a candidate_id
        actions = ""
        if shape == INBOX_SHAPE_CAPTURE and candidate_id:
            actions = (
                '<form method="POST" action="/field-capture/review/approve" style="display:inline">'
                f'<input type="hidden" name="candidate_id" value="{_esc(candidate_id)}">'
                '<input type="hidden" name="reviewer" value="operator">'
                '<button type="submit" class="icon-btn" title="Open Issue" aria-label="Open Issue">⚑</button>'
                '</form>'
                ' '
                '<form method="POST" action="/field-capture/review/reject" style="display:inline">'
                f'<input type="hidden" name="candidate_id" value="{_esc(candidate_id)}">'
                '<input type="hidden" name="reviewer" value="operator">'
                '<button type="submit" class="icon-btn" title="Dismiss" aria-label="Dismiss">✕</button>'
                '</form>'
            )
            if visit_proposed:
                actions += (
                    ' '
                    '<form method="POST" action="/field-capture/review/dismiss-completion" style="display:inline">'
                    f'<input type="hidden" name="candidate_id" value="{_esc(candidate_id)}">'
                    '<input type="hidden" name="reviewer" value="operator">'
                    '<button type="submit" class="icon-btn" title="Mark as completed and dismiss" aria-label="Done">✓</button>'
                    '</form>'
                )

        row_html = (
            "<tr>"
            f"<td>{site}</td>"
            f"<td>{area}</td>"
            f"<td>{summary_cell}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
        row_htmls.append(row_html)

    headers = "<tr><th>Site</th><th>Area</th><th>Summary</th><th>Actions</th></tr>"
    return f"<table>{headers}{''.join(row_htmls)}</table>"


def _render_home_cards(cards: list[dict]) -> str:
    non_empty = [c for c in cards if int(c.get("count") or 0) > 0]
    empty = [c for c in cards if int(c.get("count") or 0) == 0]
    parts = [_render_full_card(c) for c in non_empty]
    if empty:
        parts.append(_render_empty_strip(empty))
    return "\n".join(parts)


def _render_full_card(card: dict) -> str:
    count = int(card.get("count") or 0)
    title = str(card.get("title") or card.get("id") or "")
    see_all = str(card.get("see_all") or "#")
    top = card.get("top") or []
    shape = str(card.get("shape") or "")

    rows_html = _render_card_rows(top, shape)
    see_link = f'<p><a href="{_esc(see_all)}">See all →</a></p>'
    body = rows_html + see_link
    count_badge = f' <span class="count">({count})</span>'
    return f"<section><h2>{_esc(title)}{count_badge}</h2>{body}</section>"


def _render_empty_strip(empty_cards: list[dict]) -> str:
    items = []
    for card in empty_cards:
        title = str(card.get("title") or card.get("id") or "")
        see_all = str(card.get("see_all") or "#")
        items.append(
            f'<li><a href="{_esc(see_all)}">'
            f'<span class="empty-check" aria-hidden="true">✓</span> {_esc(title)}'
            f'</a></li>'
        )
    return (
        '<section class="empty-strip">'
        '<h2 class="muted">All Clear</h2>'
        f'<ul class="empty-pills">{"".join(items)}</ul>'
        '</section>'
    )


def _inactive_location_site_ids(by_type_rows: list[dict]) -> set[str]:
    site_ids: set[str] = set()
    for row in by_type_rows:
        doc = row.get("doc") if isinstance(row.get("doc"), dict) else {}
        if doc.get("type") != "location" or doc.get("active") is not False:
            continue
        site_id = _string(doc.get("job") or doc.get("site_id"))
        if not site_id:
            site_id = _string(doc.get("_id")).removeprefix("location_")
        if site_id:
            site_ids.add(site_id)
    return site_ids


def _location_list(locations: list[_Location], *, empty: str, site_prefix: str = "sites/", site_suffix: str = ".html") -> str:
    if site_suffix == ".html":
        return _projector_location_list(locations, empty=empty, site_prefix=site_prefix)
    if not locations:
        return f'<p class="empty">{_esc(empty)}</p>'
    items = [
        f'<li><a href="{site_prefix}{_url_path(loc.site_id)}{site_suffix}">{_esc(loc.name)}</a></li>'
        for loc in locations
    ]
    return f"<ul>{''.join(items)}</ul>"


def _group(tag: str, inner_html: str) -> str:
    return f'<div class="home-group home-group--{tag}">{inner_html}</div>'


def _render_account_directory(by_account: dict[str, list[_SiteRecord]]) -> str:
    parts = [
        '<table class="account-directory">',
        "<thead><tr><th>Site</th><th>Contact</th></tr></thead>",
        "<tbody>",
    ]
    for acct in sorted(by_account, key=lambda a: (a == "", a.lower())):
        sites = sorted(by_account[acct], key=lambda r: (r.name.lower(), r.site_id))
        parts.append(
            f'<tr class="acct-divider"><th colspan="2" scope="rowgroup">{_esc(acct or "Other")}</th></tr>'
        )
        for rec in sites:
            parts.append(
                "<tr>"
                f'<td data-label="Site"><a href="/sites/{_url_path(rec.site_id)}">{_esc(rec.name)}</a></td>'
                f'<td data-label="Contact">{_esc(rec.contact_name)}</td>'
                "</tr>"
            )
    parts.append("</tbody></table>")
    return "".join(parts)


def _collapsible_directory(dom_id: str, title: str, inner_html: str, storage_key: str) -> str:
    """A sticky (state persisted in localStorage), obviously-collapsible directory
    block. `dom_id` and `storage_key` are caller-controlled literals; `title` is
    escaped."""
    return (
        f'<details class="home-collapsible" id="{dom_id}">'
        f"<summary>{_esc(title)}</summary>"
        f"{inner_html}"
        "</details>"
        "<script>"
        "(function(){"
        f"const details=document.getElementById('{dom_id}');"
        "if(!details)return;"
        f"try{{const saved=localStorage.getItem('{storage_key}');"
        "if(saved==='0'){details.open=true;}"
        "else if(saved==='1'){details.open=false;}}catch(_error){}"
        "details.addEventListener('toggle',function(){"
        f"try{{localStorage.setItem('{storage_key}',details.open?'0':'1');}}catch(_error){{}}"
        "});"
        "})();"
        "</script>"
    )


def _render_employee_directory(employee_rows: list[dict], site_records: dict[str, _SiteRecord]) -> str:
    def employee_label(doc: dict) -> tuple[str, tuple[str, str]]:
        first = _string(doc.get("first"))
        last = _string(doc.get("last"))
        doc_id = _string(doc.get("_id"))
        fallback = _string(doc.get("name")) or doc_id
        if first and last:
            return f"{last}, {first}", (last.lower(), first.lower())
        if last:
            return last, (last.lower(), "")
        if first:
            return first, (first.lower(), "")
        return fallback, (fallback.lower(), "")

    def primary_site_label(value: object) -> tuple[str, str]:
        site_id = _string(value)
        if not site_id:
            return "", ""
        site_name = (
            site_records.get(site_id.removeprefix("location_"))
            or site_records.get(site_id)
        )
        label = site_name.name if site_name and site_name.name else site_id
        return site_id, label

    employees: list[tuple[tuple[str, str], dict[str, object]]] = []
    seen_ids: set[str] = set()
    for row in employee_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict):
            continue
        if _string(doc.get("status")).lower() not in ("", "active"):
            continue
        employee_id = _string(doc.get("_id"))
        if employee_id in seen_ids:
            continue
        seen_ids.add(employee_id)

        display_name, sort_key = employee_label(doc)
        primary_site_id, primary_site_display = primary_site_label(doc.get("job"))
        employees.append(
            (
                sort_key,
                {
                    "_id": employee_id,
                    "name": display_name,
                    "name_sort": "|".join(sort_key),
                    "primary_site": primary_site_id,
                    "primary_site_label": primary_site_display,
                    "primary_site_sort": primary_site_display.lower(),
                    "phone": _format_phone(doc.get("phone")),
                    "email": _string(doc.get("email")),
                },
            )
        )

    items = [item for _sort_key, item in sorted(employees, key=lambda entry: entry[0])]

    def primary_site_link(value: object, _item: dict) -> str:
        site_id = _string(value)
        if not site_id:
            return ""
        label = _string(_item.get("primary_site_label")) or site_id
        escaped_site_id = _esc(site_id)
        return f'<a href="/sites/{escaped_site_id}">{_esc(label)}</a>'

    def employee_name_link(value: object, item: dict) -> str:
        name = _esc(_string(value))
        employee_id = _string(item.get("_id"))
        if not employee_id:
            return name
        # Link the bare id; employee_detail re-prepends "employee_" on load/save
        # (same convention as /sites/<id> -> _load_location prepends "location_").
        # Passing the full doc _id here would double the prefix -> "not found".
        detail_id = employee_id.removeprefix("employee_")
        return f'<a href="/employees/{_url_path(detail_id)}">{name}</a>'

    if not items:
        table_html = '<p class="zero-state">No employee records.</p>'
    else:
        row_htmls = []
        for item in items:
            row_htmls.append(
                "<tr"
                f' data-sort-name="{_esc(_string(item.get("name_sort")))}"'
                f' data-sort-site="{_esc(_string(item.get("primary_site_sort")))}"'
                ">"
                f'<td data-label="Name">{employee_name_link(item.get("name"), item)}</td>'
                f'<td data-label="Primary site">{primary_site_link(item.get("primary_site"), item)}</td>'
                f'<td data-label="Phone">{_esc(_string(item.get("phone")))}</td>'
                f'<td data-label="Email">{_esc(_string(item.get("email")))}</td>'
                "</tr>"
            )
        table_html = (
            '<table class="data-table">'
            "<thead><tr>"
            '<th scope="col" aria-sort="ascending">'
            '<button type="button" data-employee-sort="name">Name</button>'
            "</th>"
            '<th scope="col" aria-sort="none">'
            '<button type="button" data-employee-sort="site">Primary site</button>'
            "</th>"
            '<th scope="col">Phone</th>'
            '<th scope="col">Email</th>'
            "</tr></thead>"
            f"<tbody>{''.join(row_htmls)}</tbody>"
            "</table>"
        )

    return (
        table_html
        +
        "<script>"
        "(function(){"
        "const details=document.getElementById('employee-directory');"
        "if(!details)return;"
        "const table=details.querySelector('table.data-table');"
        "if(!table)return;"
        "const tbody=table.querySelector('tbody');"
        "const buttons=table.querySelectorAll('[data-employee-sort]');"
        "const storageKey='btq-home-employees-sort';"
        "let state={column:'name',direction:'ascending'};"
        "function validColumn(value){return value==='name'||value==='site';}"
        "function compareRows(column,direction){return function(a,b){"
        "const av=a.dataset[column==='site'?'sortSite':'sortName']||'';"
        "const bv=b.dataset[column==='site'?'sortSite':'sortName']||'';"
        "let result=av.localeCompare(bv,undefined,{sensitivity:'base'});"
        "if(result===0&&column==='site'){"
        "result=(a.dataset.sortName||'').localeCompare(b.dataset.sortName||'',undefined,{sensitivity:'base'});"
        "}"
        "if(result===0){result=Number(a.dataset.originalIndex||0)-Number(b.dataset.originalIndex||0);}"
        "return direction==='descending'?-result:result;"
        "};}"
        "function updateAria(){buttons.forEach(function(button){"
        "const th=button.closest('th');"
        "if(!th)return;"
        "const column=button.getAttribute('data-employee-sort');"
        "th.setAttribute('aria-sort',column===state.column?state.direction:'none');"
        "});}"
        "function sortRows(column,direction,persist){"
        "state={column:column,direction:direction};"
        "Array.from(tbody.querySelectorAll('tr')).sort(compareRows(column,direction)).forEach(function(row){tbody.appendChild(row);});"
        "updateAria();"
        "if(persist){try{localStorage.setItem(storageKey,JSON.stringify(state));}catch(_error){}}"
        "}"
        "Array.from(tbody.querySelectorAll('tr')).forEach(function(row,index){row.dataset.originalIndex=String(index);});"
        "buttons.forEach(function(button){button.addEventListener('click',function(){"
        "const column=button.getAttribute('data-employee-sort');"
        "if(!validColumn(column))return;"
        "const direction=state.column===column&&state.direction==='ascending'?'descending':'ascending';"
        "sortRows(column,direction,true);"
        "});});"
        "try{const saved=JSON.parse(localStorage.getItem(storageKey)||'null');"
        "if(saved&&validColumn(saved.column)&&(saved.direction==='ascending'||saved.direction==='descending')){"
        "sortRows(saved.column,saved.direction,false);return;"
        "}}catch(_error){}"
        "updateAria();"
        "})();"
        "</script>"
    )


def _active_employee_count(employee_rows: list[dict]) -> int:
    seen_ids: set[str] = set()
    for row in employee_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict):
            continue
        if _string(doc.get("status")).lower() not in ("", "active"):
            continue
        employee_id = _string(doc.get("_id"))
        if not employee_id or employee_id in seen_ids:
            continue
        seen_ids.add(employee_id)
    return len(seen_ids)


def _greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:
        daypart = "morning"
    elif hour < 17:
        daypart = "afternoon"
    else:
        daypart = "evening"
    return f"Good {daypart}, Greg"


def _count_bucket(count: int) -> str:
    if count == 0:
        return "empty"
    if count <= 5:
        return "low"
    if count <= 20:
        return "warn"
    return "high"


def _home_action_cards(ctx: object) -> tuple[list[dict[str, object]], str]:
    notice = ""
    try:
        counts = _inbox_mod.console_counts(ctx)
    except Exception as exc:  # noqa: BLE001 - homepage must degrade instead of 500ing.
        counts = {}
        notice = f'<p class="muted">Needs-you counts temporarily unavailable: {_esc(str(exc))}</p>'
    cards: list[dict[str, object]] = [
        {
            "id": "pending_candidates",
            "title": "Needs approval",
            "count": int(counts.get("review", 0) or 0),
            "see_all": "/candidates?status=pending_approval",
            "subline": "Job drafts awaiting operator review",
        },
        {
            "id": "open_site_issues",
            "title": "Open issues",
            "count": int(counts.get("issues", 0) or 0),
            "see_all": "/field-capture/issues",
            "subline": "Site issues still open",
        },
        {
            "id": "open_supply_needs",
            "title": "Supply needs",
            "count": int(counts.get("supplies", 0) or 0),
            "see_all": "/supplies?status=open",
            "subline": "Open supply requests",
        },
        {
            "id": "open_equipment_requests",
            "title": "Equipment",
            "count": int(counts.get("equipment", 0) or 0),
            "see_all": "/equipment?status=open",
            "subline": "Open equipment requests",
        },
    ]
    if "failed_captures" in counts:
        cards.insert(
            1,
            {
                "id": "failed_captures",
                "title": "Captures failed",
                "count": int(counts.get("failed_captures", 0) or 0),
                "see_all": "/failed",
                "subline": "Field captures that did not process",
            },
        )
    return cards, notice


def _render_home_header() -> str:
    return (
        '<header class="site-detail-header">'
        "<div>"
        f"<h1>{_esc(_greeting())}</h1>"
        '<p class="subline">What needs attention first, with the rest kept close.</p>'
        "</div>"
        '<p class="site-header-actions">'
        '<a class="button" href="#quick-capture" '
        "onclick=\"var el=document.getElementById('quick-capture');if(el){el.open=true;}\">Capture observation</a>"
        "</p>"
        "</header>"
    )


def _render_action_center(cards: list[dict[str, object]], notice: str = "") -> str:
    card_html = []
    for card in cards:
        count = int(card.get("count") or 0)
        card_id = str(card.get("id") or "")
        count_class = "count-neutral" if count <= 0 else "count-pending"
        status = ""
        if count > 0 and card_id == "open_site_issues":
            status = '<p><em class="pill warning">Needs attention</em></p>'
        card_html.append(
            f'<a class="metric-card stat-badge" data-stat-id="{_esc(card_id)}" '
            f'data-count-bucket="{_count_bucket(count)}" href="{_esc(str(card.get("see_all") or "#"))}">'
            f'<strong class="{count_class}">{_esc(str(count))}</strong>'
            f'<span>{_esc(str(card.get("title") or ""))}</span>'
            f'<p class="subline">{_esc(str(card.get("subline") or ""))}</p>'
            f"{status}"
            "</a>"
        )
    action_section = (
        '<section>'
        '<div class="section-heading-row"><h2>Needs you</h2></div>'
        f"{notice}"
        f'<div class="metric-grid" aria-label="Needs you queues">{"".join(card_html)}</div>'
        "</section>"
    )
    all_clear = _render_empty_strip(cards) if cards and all(int(card.get("count") or 0) == 0 for card in cards) else ""
    return action_section + all_clear


def _coverage_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coverage_site_label(row: dict, site_names: dict[str, str] | None = None) -> str:
    site_id = _string(row.get("site_id"))
    site_names = site_names or {}
    raw_label = _string(row.get("site") or row.get("site_name") or row.get("account"))
    if site_id and (not raw_label or raw_label == site_id):
        raw_label = _string(site_names.get(site_id)) or raw_label
    return raw_label or site_id or "Unknown site"


def _coverage_date_label(value: object) -> str:
    text = _string(value)
    return text or "no date"


def _render_coverage_panel(report: dict, site_records: dict[str, _SiteRecord] | None = None) -> str:
    """Render the Visit/QC coverage read model without loading data."""
    weekly = report.get("weekly") if isinstance(report.get("weekly"), dict) else {}
    completed = weekly.get("completed") if isinstance(weekly.get("completed"), list) else []
    overdue = report.get("overdue") if isinstance(report.get("overdue"), list) else []
    gaps = report.get("gaps") if isinstance(report.get("gaps"), list) else []
    accounts = report.get("accounts") if isinstance(report.get("accounts"), list) else []
    site_names = {
        _string(item.get("site_id")): _string(item.get("site_name"))
        for item in accounts
        if isinstance(item, dict) and _string(item.get("site_id")) and _string(item.get("site_name"))
    }
    if site_records:
        for site_id, record in site_records.items():
            normalized_site_id = _string(site_id)
            if normalized_site_id and normalized_site_id not in site_names:
                site_names[normalized_site_id] = _string(getattr(record, "name", ""))

    count = _coverage_int(weekly.get("count"))
    target = _coverage_int(weekly.get("target"))
    remaining = _coverage_int(weekly.get("remaining"))
    overdue_qc_days = _coverage_int(report.get("overdue_qc_days") or 30)
    gap_window_days = _coverage_int(report.get("gap_window_days") or 30)
    pill_class = "success" if target > 0 and count >= target else "warning"
    remaining_text = f"{max(0, remaining)} remaining"

    completed_rows: list[dict[str, object]] = []
    for item in completed[:COVERAGE_PANEL_LIMIT]:
        if not isinstance(item, dict):
            continue
        completed_rows.append(
            {
                "site": _coverage_site_label(item, site_names),
                "date": _coverage_date_label(item.get("date")),
            }
        )

    overdue_rows: list[dict[str, object]] = []
    for item in overdue[:COVERAGE_PANEL_LIMIT]:
        if not isinstance(item, dict):
            continue
        days = item.get("days_since_last_qc")
        days_text = "never" if days is None else f"{_coverage_int(days)}d since last QC"
        overdue_rows.append({"site": _coverage_site_label(item, site_names), "status": days_text})

    gap_rows: list[dict[str, object]] = []
    for item in gaps[:COVERAGE_PANEL_LIMIT]:
        if not isinstance(item, dict):
            continue
        gap_rows.append(
            {
                "site": _coverage_site_label(item, site_names),
                "date": _coverage_date_label(item.get("date")),
            }
        )

    completed_more = max(0, len(completed) - COVERAGE_PANEL_LIMIT)
    overdue_more = max(0, len(overdue) - COVERAGE_PANEL_LIMIT)
    gaps_more = max(0, len(gaps) - COVERAGE_PANEL_LIMIT)
    completed_note = f'<p class="subline">+{_esc(str(completed_more))} more completed this week</p>' if completed_more else ""
    overdue_note = f'<p class="subline">+{_esc(str(overdue_more))} more overdue accounts</p>' if overdue_more else ""
    gaps_note = f'<p class="subline">+{_esc(str(gaps_more))} more open gaps</p>' if gaps_more else ""

    completed_table = render_table(
        completed_rows,
        [
            {"key": "site", "label": f"Completed QCs · {len(completed)}"},
            {"key": "date", "label": "Date", "nowrap": True},
        ],
        empty_text="No QCs completed this week.",
        table_class="coverage-table",
    )
    overdue_table = render_table(
        overdue_rows,
        [
            {"key": "site", "label": f"Overdue accounts · {len(overdue)}"},
            {"key": "status", "label": "QC age", "nowrap": True},
        ],
        empty_text=f"All accounts have a recorded QC within {overdue_qc_days} days.",
        table_class="coverage-table",
    )
    gaps_table = render_table(
        gap_rows,
        [
            {"key": "site", "label": f"Open visit gaps · {len(gaps)}"},
            {"key": "date", "label": "Date", "nowrap": True},
        ],
        empty_text=f"No open visit gaps in the last {gap_window_days} days.",
        table_class="coverage-table",
    )

    body = (
        '<div class="section-heading-row">'
        f'<p><span class="pill {pill_class}">QCs this week: {_esc(str(count))} / {_esc(str(target))}</span> '
        f'<span class="muted">{_esc(remaining_text)}</span></p>'
        "</div>"
        '<div class="dir-grid">'
        f"<div>{completed_table}{completed_note}</div>"
        f'<div><p class="subline">No recorded QC in the last {_esc(str(overdue_qc_days))} days</p>'
        f"{overdue_table}{overdue_note}</div>"
        f'<div><p class="subline">Activity logged with no visit recorded that day, last '
        f"{_esc(str(gap_window_days))} days</p>{gaps_table}{gaps_note}</div>"
        "</div>"
    )
    return _section("Visit/QC Coverage", body)


def _render_coverage_unavailable() -> str:
    return _section("Visit/QC Coverage", '<p class="muted">Coverage unavailable.</p>')


def _render_capture_surface(voice_card_html: str) -> str:
    return (
        '<details class="detail-block" id="quick-capture">'
        '<summary>Text or voice note</summary>'
        f'<div class="detail-block-body">{voice_card_html}</div>'
        "</details>"
    )


def _site_link(site_id: str, label: str) -> str:
    return f'<a href="/sites/{_url_path(site_id)}">{_esc(label)}</a>'


def _sorted_locations(locations: list[_Location]) -> list[_Location]:
    return sorted(locations, key=lambda loc: (loc.name.lower(), loc.site_id))


def _location_list_html(locations: list[_Location], *, empty: str, limit: int | None = None) -> str:
    rows = _sorted_locations(locations)
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return f'<p class="zero-state">{_esc(empty)}</p>'
    items = []
    for loc in rows:
        account = f'<p class="subline">{_esc(loc.account)}</p>' if loc.account else ""
        items.append(f"<li>{_site_link(loc.site_id, loc.name)}{account}</li>")
    return f"<ul>{''.join(items)}</ul>"


def _render_named_location_panel(
    *,
    title: str,
    locations: list[_Location],
    empty: str,
    details_id: str,
    full_body: str | None = None,
) -> str:
    count = len(locations)
    preview_limit = 6
    sorted_locations = _sorted_locations(locations)
    preview_locations = sorted_locations[:preview_limit]
    remaining_locations = sorted_locations[preview_limit:]
    view_all = ""
    details = ""
    if count and (remaining_locations or full_body):
        view_all = (
            f'<a href="#{_esc(details_id)}" '
            f"onclick=\"var el=document.getElementById('{_esc(details_id)}');if(el){{el.open=true;}}\">View all</a>"
        )
        details_body = full_body or _location_list_html(remaining_locations, empty=empty)
        details = (
            f'<details class="detail-block" id="{_esc(details_id)}">'
            f'<summary>View all <span class="details-count">{_esc(str(count))} site{"s" if count != 1 else ""}</span></summary>'
            f'<div class="detail-block-body">{details_body}</div>'
            "</details>"
        )
    return (
        "<section>"
        '<div class="section-heading-row">'
        f"<h2>{_esc(title)} &middot; {_esc(str(count))}</h2>"
        f"{view_all}"
        "</div>"
        f'{_location_list_html(preview_locations, empty=empty)}'
        f"{details}"
        "</section>"
    )


def _make_couchdb_put(cfg: object):
    import json
    from urllib import request as urllib_request, error as urllib_error

    db_name = "btq_voice_memos"

    def couchdb_put(doc: dict) -> dict:
        doc_id = str(doc.get("_id") or "")
        if not doc_id:
            return {}
        url = f"{cfg.base_url.rstrip('/')}/{db_name}/{doc_id}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(cfg.auth_header())
        data = json.dumps(doc, sort_keys=True).encode("utf-8")
        req = urllib_request.Request(url, data=data, headers=headers, method="PUT")
        try:
            with urllib_request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib_error.HTTPError:
            return {}

    return couchdb_put


def _couchdb_doc_exists(cfg: object, db_name: str, doc_id: str) -> bool:
    import json
    from urllib import parse as urllib_parse, request as urllib_request, error as urllib_error

    if not doc_id:
        return False
    encoded_doc_id = urllib_parse.quote(doc_id, safe="")
    url = f"{cfg.base_url.rstrip('/')}/{db_name}/{encoded_doc_id}"
    headers = {"Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, headers=headers, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read().decode("utf-8"))
            return True
    except urllib_error.HTTPError as exc:
        if exc.code == 404:
            return False
        return False
    except Exception:
        return False


def _voice_memo_sites_lookup() -> list[dict[str, str]]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector = {
        "selector": {"type": "location", "status": "active"},
        "fields": ["_id", "job", "account", "location", "site_id", "type", "status"],
        "limit": 1000,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(selector).encode("utf-8"), headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
        response = json.loads(resp.read().decode("utf-8"))

    sites = []
    for doc in response.get("docs", []):
        if not isinstance(doc, dict) or doc.get("type") != "location" or doc.get("status") != "active":
            continue
        # site_id marks a registered, routable site; stray location docs never get one.
        site_id = str(doc.get("site_id") or "").strip()
        if not site_id:
            continue
        account = str(doc.get("account") or "").strip()
        location = str(doc.get("location") or "").strip()
        sites.append({"id": site_id, "account": account, "location": location})
    return sites


def _voice_memo_employees_lookup() -> list[dict[str, str]]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    selector = {
        "selector": {"type": "employee", "status": "active"},
        "fields": ["_id", "person_id", "first", "last", "preferred_name", "job", "type", "status"],
        "limit": 1000,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(selector).encode("utf-8"), headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
        response = json.loads(resp.read().decode("utf-8"))

    employees = []
    for doc in response.get("docs", []):
        if not isinstance(doc, dict) or doc.get("type") != "employee" or doc.get("status") != "active":
            continue
        employee_id = str(doc.get("person_id") or "").strip() or str(doc.get("_id") or "").strip().removeprefix("employee_")
        if not employee_id:
            continue
        first = str(doc.get("first") or "").strip()
        last = str(doc.get("last") or "").strip()
        raw_preferred = doc.get("preferred_name")
        preferred = raw_preferred.strip() if isinstance(raw_preferred, str) else ""
        display_first = preferred or first
        job = str(doc.get("job") or "").strip()
        label = f"{display_first} {last}".strip()
        if job:
            label = f"{label} — {job}" if label else job
        employees.append({"id": employee_id, "label": label, "first": first, "last": last, "preferred_name": preferred, "job": job})
    return sorted(employees, key=lambda item: (item["last"].lower(), item["first"].lower()))


def _operator_site_label(site_id: str) -> str:
    site_id = str(site_id or "").strip()
    if not site_id:
        return ""
    try:
        sites = _voice_memo_sites_lookup()
    except Exception:
        return site_id
    for site in sites:
        if str(site.get("id") or "").strip() != site_id:
            continue
        account = str(site.get("account") or "").strip()
        location = str(site.get("location") or "").strip()
        return location or account or site_id
    return site_id


def _operator_employee_context(employee_ids: list[str]) -> list[dict[str, str]]:
    selected = {str(value or "").strip() for value in employee_ids if str(value or "").strip()}
    if not selected:
        return []
    try:
        employees = _voice_memo_employees_lookup()
    except Exception:
        return [{"id": employee_id, "name": employee_id} for employee_id in sorted(selected)]
    by_id = {str(employee.get("id") or "").strip(): employee for employee in employees}
    result: list[dict[str, str]] = []
    for employee_id in sorted(selected):
        employee = by_id.get(employee_id)
        if employee is None:
            result.append({"id": employee_id, "name": employee_id})
            continue
        first = str(employee.get("preferred_name") or employee.get("first") or "").strip()
        last = str(employee.get("last") or "").strip()
        name = f"{first} {last}".strip() or str(employee.get("label") or employee_id).strip()
        result.append({"id": employee_id, "name": name, "label": str(employee.get("label") or name).strip()})
    return result


def _render_help_popup() -> str:
    """Build an HTML <dialog> help popup sourced from actual term constants and yaml."""
    # Load yaml phrase lists via the extraction_terms API
    terms = get_extraction_terms()
    try:
        completion_phrases = terms._terms.get("visit_completion", {}).get("global", [])
        qc_phrases = terms._terms.get("qc_inspection", {}).get("global", [])
    except AttributeError:
        # Fall back to loading yaml directly if _terms is not accessible
        yaml_path = Path(__file__).parent.parent.parent / "event_pipeline" / "extraction_terms.yaml"
        data = yaml.safe_load(yaml_path.read_text())
        global_data = data.get("global", data)
        completion_phrases = global_data.get("visit_completion", [])
        qc_phrases = global_data.get("qc_inspection", [])

    # Extract literal words from SUPPLY_NEED_ACTION_PATTERN alternation group
    supply_words = re.findall(r"[a-z][a-z ']+", SUPPLY_NEED_ACTION_PATTERN.pattern, re.IGNORECASE)
    supply_words = [w.strip() for w in supply_words if len(w.strip()) > 1]

    # Extract literal words from EQUIPMENT_REQUEST_ACTION_PATTERN alternation group
    equip_words = re.findall(r"[a-z][a-z ']+", EQUIPMENT_REQUEST_ACTION_PATTERN.pattern, re.IGNORECASE)
    equip_words = [w.strip() for w in equip_words if len(w.strip()) > 1]

    def _phrase_list(phrases: list[str]) -> str:
        items = "".join(f"<li>{phrase}</li>" for phrase in phrases)
        return f"<ul>{items}</ul>"

    def _word_list(words: list[str]) -> str:
        items = "".join(f"<li>{w}</li>" for w in words)
        return f"<ul>{items}</ul>"

    # Build issue type sections (from ISSUE_TYPES and classify_issue inline terms)
    issue_type_examples = {
        "supplies": ["soap", "paper", "restock", "towel", "supply"],
        "water": ["water", "leak", "moisture", "sink", "wet"],
        "access": ["access", "badge", "key", "locked", "gate"],
        "damage": ["broken", "damage", "cracked"],
        "safety": ["safety", "hazard", "blood", "chemical"],
        "quality": ["dirty", "quality", "missed"],
        "client_request": ["client asked", "client requested"],
    }

    issue_sections = ""
    for issue_type in sorted(ISSUE_TYPES):
        if issue_type == "other":
            continue
        examples = issue_type_examples.get(issue_type, [])
        examples_html = _word_list(examples) if examples else ""
        label = issue_type.replace("_", " ").title()
        issue_sections += f"<h4>{label}</h4>{examples_html}"

    html = (
        '<dialog id="help-popup">'
        "<h3>What to say?</h3>"
        "<p>Say a phrase into the note box. The classifier picks the right action automatically.</p>"
        "<h4>Visit completion</h4>"
        "<p>Propose a visit completion record:</p>"
        + _phrase_list(completion_phrases)
        + "<h4>Damage / maintenance</h4>"
        "<p>Flag a site issue (words like):</p>"
        + _word_list(list(MAINTENANCE_TERMS))
        + "<h4>Supply needs</h4>"
        "<p>Log a supply need (trigger words):</p>"
        + _word_list(supply_words)
        + "<h4>Equipment requests</h4>"
        "<p>Log an equipment request (trigger words):</p>"
        + _word_list(equip_words)
        + "<h4>Issue types (damage / maintenance details)</h4>"
        + issue_sections
        + "<h4>QC inspection</h4>"
        "<p>Propose a QC inspection visit:</p>"
        + _phrase_list(qc_phrases)
        + '<p><button type="button" onclick="this.closest(\'dialog\').close()">Close</button></p>'
        "</dialog>"
    )
    return html


def _render_voice_card(site_records: dict, employee_records: list[dict[str, str]] | None = None) -> str:
    from ops_dashboard.common import format_site_options

    # Home already curates the active site set; the shared formatter keeps
    # labels and ordering identical to every other site selector.
    options = '<option value="">— none —</option>\n'
    for site_id, label in format_site_options((rec.site_id, rec.name) for rec in site_records.values()):
        options += f'<option value="{_esc(site_id)}">{_esc(label)}</option>\n'
    employee_options = ""
    for employee in employee_records or []:
        employee_id = _esc(str(employee.get("id") or ""))
        if not employee_id:
            continue
        label = _esc(str(employee.get("label") or employee.get("id") or ""))
        employee_options += f'<option value="{employee_id}">{label}</option>\n'

    return (
        "<section>"
        '<form id="captureForm" method="POST" action="/vault-home/voice-memo" enctype="multipart/form-data">'
        '<input type="hidden" name="capture_id" value="">'
        f'<p><label>Site<br><select name="site_id">{options}</select></label></p>'
        f'<p><label>Employee<br><select name="employee_slugs" multiple size="6">{employee_options}</select></label></p>'
        '<p><label>Note<br><textarea name="note" rows="3" placeholder="Type an observation…"></textarea></label></p>'
        '<p class="voice-panel-label">Voice note (optional)</p>'
        '<div class="tape-deck">'
        '<div class="tape-transport">'
        '<button class="tape-btn tape-record" id="captureRecordButton" type="button" title="Record">&#9679;</button>'
        '<button class="tape-btn tape-stop" id="captureStopButton" type="button" title="Stop" disabled>&#9632;</button>'
        "</div>"
        '<button class="tape-btn tape-clear" id="captureClearButton" type="button" title="Erase recording" disabled>&#10005;</button>'
        "</div>"
        '<p id="captureVoiceStatus" class="voice-status muted">Optional</p>'
        '<audio id="captureVoicePreview" controls hidden></audio>'
        '<p id="captureVoiceSupport" class="voice-support-message" hidden>Voice recording is not available in this browser.</p>'
        '<p><button type="submit">Submit</button></p>'
        '<p><button type="button" onclick="document.getElementById(\'help-popup\').showModal()">What to say?</button></p>'
        "</form>"
        + _render_help_popup()
        + "</section>"
        '<script>'
        'document.addEventListener("DOMContentLoaded", function () {'
        'const form = document.getElementById("captureForm");'
        'if (!form || typeof window.attachRecorder !== "function") return;'
        "window.attachRecorder({"
        "form,"
        'recordButton: document.getElementById("captureRecordButton"),'
        'stopButton: document.getElementById("captureStopButton"),'
        'clearButton: document.getElementById("captureClearButton"),'
        'preview: document.getElementById("captureVoicePreview"),'
        'statusEl: document.getElementById("captureVoiceStatus"),'
        'supportEl: document.getElementById("captureVoiceSupport"),'
        "});"
        "});"
        "</script>"
    )


def _render_photos_home_group(ctx: object) -> str:
    cards_html, fallback = _field_photos_mod.latest_photo_cards(ctx, limit=4)
    if fallback:
        strip_html = '<p class="muted">Photos unavailable.</p>'
    elif cards_html:
        strip_html = (
            '<div class="site-gallery site-gallery--strip">'
            f"{cards_html}"
            "</div>"
        )
    else:
        strip_html = '<p class="zero-state">No photos yet.</p>'
    return (
        "<section>"
        '<div class="section-heading-row">'
        "<h2>Recent field photos</h2>"
        '<a href="/field-photos">View all</a>'
        "</div>"
        f"{strip_html}"
        "</section>"
    )


def handle_voice_memo_post(
    ctx: object, body: bytes, *, content_type: str = ""
) -> tuple:
    import email.parser
    import email.policy
    import uuid
    from datetime import datetime, timezone
    from http import HTTPStatus
    from urllib.parse import urlencode
    import html as _html

    from btq_vault.entity_types import OPERATOR_PERSON_ID
    from processing_core.artifacts import write_json_object

    runtime_root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)

    def _redirect(location: str) -> tuple:
        body_bytes = f'<a href="{_html.escape(location)}">Return to Inbox</a>'.encode("utf-8")
        return HTTPStatus.SEE_OTHER, "text/html; charset=utf-8", body_bytes, {"Location": location}

    try:
        full_message = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
        parser = email.parser.BytesFeedParser(policy=email.policy.compat32)
        parser.feed(full_message)
        msg = parser.close()

        note = ""
        site_id = ""
        capture_id = ""
        audio_bytes = b""
        audio_filename = "memo.webm"
        audio_content_type = "audio/webm"
        employee_slugs: list[str] = []

        for part in msg.walk():
            disp = part.get_param("name", header="content-disposition") or ""
            if disp == "note":
                note = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
            elif disp == "site_id":
                site_id = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
            elif disp == "capture_id":
                capture_id = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
            elif disp == "employee_slugs":
                value = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace").strip()
                if value:
                    employee_slugs.append(value)
            elif disp == "audio":
                audio_bytes = part.get_payload(decode=True) or b""
                audio_filename = part.get_filename() or "memo.webm"
                audio_content_type = part.get_content_type() or "audio/webm"

        upload_id = capture_id or str(uuid.uuid4())
        captured_at = datetime.now(timezone.utc).isoformat()

        if audio_bytes and len(audio_bytes) >= 100:
            cfg = couchdb_config.from_env()
            if capture_id and _couchdb_doc_exists(cfg, "btq_voice_memos", capture_id):
                return _redirect(f"/?{urlencode({'message': 'memo recorded'})}")
            memo = UploadedVoiceMemo(
                filename=audio_filename,
                content_type=audio_content_type,
                content=audio_bytes,
            )
            result = record_upload(
                data_dir=runtime_root / "voice_memo_home",
                captured_at=captured_at,
                note=note,
                duration_seconds=None,
                memo=memo,
                site_id=site_id,
                employee_slugs=",".join(employee_slugs),
                capture_id=capture_id,
                person_id=OPERATOR_PERSON_ID,
                sites_lookup=_voice_memo_sites_lookup,
                employees_lookup=_voice_memo_employees_lookup,
                couchdb_put=_make_couchdb_put(cfg),
            )
            _handoff_home_voice_memo_to_transcription(runtime_root, result.record)
        elif note.strip():
            artifact = run_text_semantic_pipeline(
                note.strip(),
                site_id=site_id,
                upload_id=upload_id,
                area="",
                person_id=OPERATOR_PERSON_ID,
                site_label=_operator_site_label(site_id),
                selected_employees=_operator_employee_context(employee_slugs),
                captured_at=captured_at,
            )
            semantics_dir = runtime_root / "field_capture" / "semantics"
            semantics_dir.mkdir(parents=True, exist_ok=True)
            semantic_path = semantics_dir / f"{upload_id}.json"
            write_json_object(semantic_path, artifact)

    except Exception:
        return _redirect(f"/?{urlencode({'error': 'voice memo failed'})}")

    return _redirect(f"/?{urlencode({'message': 'memo recorded'})}")


def _handoff_home_voice_memo_to_transcription(runtime_root: Path, record: dict) -> None:
    audio_path = str(record.get("audio_path") or "").strip()
    if not audio_path:
        return
    source = runtime_root / "voice_memo_home" / audio_path
    if not source.exists():
        return
    destination = voice_memo_inbox_audio_path(record, runtime_root / "voice_inbox")
    safe_copy(source, destination)
    write_voice_memo_sidecar(
        voice_memo_sidecar_path_for(destination),
        voice_memo_sidecar_payload(record),
    )


def render(ctx: object) -> str:
    action_cards, action_notice = _home_action_cards(ctx)
    operator_identity = current_operator_identity()
    coverage_data: dict | None = None
    try:
        coverage_data = coverage_report(operator_identity)
        coverage_html = _render_coverage_panel(coverage_data)
    except Exception:  # noqa: BLE001 - homepage must degrade instead of 500ing.
        coverage_html = _render_coverage_unavailable()

    try:
        cfg = couchdb_config.from_env()
        base_url = cfg.base_url
        auth_headers = cfg.auth_header()
        database = couchdb_config.vault_database()
        timeout = 10.0

        by_type_rows = query_view(base_url, auth_headers, database, DDOC, "by_type", include_docs=True, timeout=timeout)
        site_records = _build_site_records(by_type_rows)
        inactive_site_ids = _inactive_location_site_ids(by_type_rows)
        location_rows = query_view(base_url, auth_headers, database, DDOC, "locations_all", timeout=timeout)
        employee_rows = query_view(
            base_url, auth_headers, database, DDOC, "employees_by_site", include_docs=True, timeout=timeout
        )
        opportunity_rows = query_view(base_url, auth_headers, database, DDOC, "opportunities_by_site_status", timeout=timeout)
        visit_rows = query_view(base_url, auth_headers, database, DDOC, "visits_by_site_date", timeout=timeout)

        for row in location_rows:
            key = row.get("key") if isinstance(row.get("key"), list) else []
            value = row.get("value") if isinstance(row.get("value"), dict) else {}
            sid = _string(value.get("site_id") or (key[2] if len(key) > 2 else row.get("id")))
            if not sid or sid in site_records:
                continue
            name = _string((key[1] if len(key) > 1 else None) or value.get("name")) or sid
            account = _string(value.get("account") or (key[0] if key else ""))
            site_records[sid] = _SiteRecord(sid, name, account)
        for rows in (employee_rows, opportunity_rows, visit_rows):
            for row in rows:
                sid = _site_id(row)
                if sid and sid not in site_records:
                    site_records[sid] = _SiteRecord(sid)
        for sid in inactive_site_ids:
            site_records.pop(sid, None)
        if coverage_data is not None:
            coverage_html = _render_coverage_panel(coverage_data, site_records)

        open_opportunities = _rows_by_site(_status_rows(opportunity_rows, {"open"}))
        for sid in inactive_site_ids:
            open_opportunities.pop(sid, None)
        inactive_cutoff = date.today() - timedelta(days=30)
        visited_recently = {_site_id(row) for row in visit_rows if (_row_date(row) or date.min) >= inactive_cutoff}

        by_account: dict[str, list[_SiteRecord]] = defaultdict(list)
        for record in site_records.values():
            by_account[record.account].append(record)

        locations_list = [_Location(record.site_id, record.name, record.account) for record in site_records.values()]
        inactive_locations = [loc for loc in locations_list if loc.site_id not in visited_recently]
        sites_with_open_opportunities = [loc for loc in locations_list if open_opportunities.get(loc.site_id)]

        try:
            employee_picker_rows = _voice_memo_employees_lookup()
        except Exception:
            employee_picker_rows = []
        voice_card_html = _render_voice_card(site_records, employee_picker_rows)
        capture_html = _group("capture", _render_capture_surface(voice_card_html))
        attention_html = _group(
            "operational",
            _render_named_location_panel(
                title="No visit in 30 days",
                locations=inactive_locations,
                empty="All sites have a visit in the last 30 days.",
                details_id="no-visit-sites-all",
            )
            + _render_named_location_panel(
                title="Open opportunities",
                locations=sites_with_open_opportunities,
                empty="No sites have open opportunities.",
                details_id="open-opportunities-all",
                full_body=_grouped_rows(open_opportunities, locations_list, _opportunity_label),
            ),
        )
        directory_html = _group(
            "directory",
            '<div class="dir-grid">'
            + _collapsible_directory(
                "site-directory",
                f"All sites · {len(site_records)}",
                _render_account_directory(by_account),
                "btq-home-sites-collapsed",
            )
            + _collapsible_directory(
                "employee-directory",
                f"Employee directory · {_active_employee_count(employee_rows)}",
                _render_employee_directory(employee_rows, site_records),
                "btq-home-employees-collapsed",
            )
            + "</div>",
        )
        recent_activity_html = _group(
            "operational",
            _render_photos_home_group(ctx),
        )

        body = (
            '<div class="home-grid">'
            + _render_home_header()
            + _render_action_center(action_cards, action_notice)
            + coverage_html
            + capture_html
            + '<section><div class="section-heading-row"><h2>Needs attention</h2></div>'
            + attention_html
            + "</section>"
            + directory_html
            + '<section><div class="section-heading-row"><h2>Recent activity</h2></div>'
            + recent_activity_html
            + "</section>"
            + "</div>"
        )
        return html_page("BTQ", body, active_section="home")

    except ProjectorError:
        body = (
            '<div class="home-grid">'
            + _render_home_header()
            + _render_action_center(action_cards, action_notice)
            + coverage_html
            + _group("capture", _render_capture_surface(_render_voice_card({}, [])))
            + "<p>Directory temporarily unavailable.</p>"
            + "</div>"
        )
        return html_page("BTQ", body, active_section="home")
