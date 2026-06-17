from __future__ import annotations

import html
import json
from typing import Any
from urllib import parse as urlparse
from urllib import request as urlrequest

from event_pipeline import couchdb_config
from btq_vault.entity_types import current_operator_id
from ops_dashboard.common import render_back_link, render_table
from ops_dashboard.layout import html_page
import ops_dashboard.sections.sites as sites
from btq_vault.projector import render_markdown


SHIFT_REPORT_TYPE = "shift_report"
DAY_RECORD_TYPE = "day_record"
RECORD_TYPES = (SHIFT_REPORT_TYPE, DAY_RECORD_TYPE)


def _cdb() -> tuple[str, dict[str, str], str, float]:
    base = sites.couchdb_base_url()
    headers = sites.auth_headers()
    database = couchdb_config.vault_database()
    timeout = couchdb_config.timeout()
    return base, headers, database, timeout


def _find(payload: dict[str, object]) -> list[dict[str, Any]]:
    base, headers, database, timeout = _cdb()
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/_find"
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        response = json.loads(resp.read().decode("utf-8"))
    docs = response.get("docs") if isinstance(response, dict) else []
    return [doc for doc in docs if isinstance(doc, dict)]


def _type_label(record_type: object) -> str:
    if record_type == SHIFT_REPORT_TYPE:
        return "Shift Report"
    return str(record_type or "").replace("_", " ").title()


def _shift_report_docs() -> list[dict[str, Any]]:
    payload = {
        "selector": {"type": SHIFT_REPORT_TYPE, "operator": current_operator_id()},
        "fields": ["_id", "date", "type", "prepared_by"],
        "limit": 5000,
    }
    try:
        return _find(payload)
    except Exception:  # noqa: BLE001 - degrade to empty, never break the page
        return []


def _day_record_docs() -> list[dict[str, Any]]:
    payload = {
        "selector": {"type": DAY_RECORD_TYPE, "operator": current_operator_id()},
        "fields": ["_id", "date", "type"],
        "limit": 5000,
    }
    try:
        return _find(payload)
    except Exception:  # noqa: BLE001 - degrade to empty, never break the page
        return []


def _record_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for doc in [*_shift_report_docs(), *_day_record_docs()]:
        doc_id = str(doc.get("_id") or "").strip()
        if not doc_id:
            continue
        record_type = doc.get("type")
        if record_type not in RECORD_TYPES:
            continue
        rows.append(
            {
                "id": doc_id,
                "date": str(doc.get("date") or "").strip(),
                "type": _type_label(record_type),
                "prepared_by": str(doc.get("prepared_by") or "").strip(),
            }
        )
    rows.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    return rows


def _date_link(value: object, item: dict[str, object]) -> str:
    doc_id = str(item.get("id") or "")
    href = f"/records/{urlparse.quote(doc_id, safe='')}"
    label = str(value or "") or doc_id
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def render(ctx: object) -> str:
    rows = _record_rows()
    table = render_table(
        rows,
        [
            {"key": "date", "label": "Date", "format": _date_link, "nowrap": True},
            {"key": "type", "label": "Type"},
            {"key": "prepared_by", "label": "Prepared by"},
        ],
        empty_text="No records yet.",
    )
    body = f"<header><h1>Records</h1></header>{table}"
    return html_page("Records", body, active_section="records")


def _load_record(record_id: str) -> dict[str, Any] | None:
    doc_id = urlparse.unquote(record_id)
    if not doc_id:
        return None
    payload = {
        "selector": {"_id": doc_id},
        "limit": 1,
    }
    try:
        docs = _find(payload)
    except Exception:  # noqa: BLE001 - not found is safer than a dashboard 500
        return None
    if not docs:
        return None
    doc = docs[0]
    if doc.get("type") not in RECORD_TYPES or doc.get("operator") != current_operator_id():
        return None
    return doc


def _not_found(record_id: str) -> str:
    escaped_id = html.escape(urlparse.unquote(record_id))
    body = (
        f"{render_back_link('/records', 'Records')}"
        "<header><h1>Record not found</h1>"
        f'<p class="muted">No operational record found for {escaped_id}.</p></header>'
    )
    return html_page("Record not found", body, active_section="records")


def render_detail(ctx: object, record_id: str) -> str:
    doc = _load_record(record_id)
    if doc is None:
        return _not_found(record_id)

    date_text = str(doc.get("date") or "").strip()
    type_text = _type_label(doc.get("type"))
    prepared_by = str(doc.get("prepared_by") or "").strip()
    title = date_text or type_text or "Record"
    content = str(doc.get("content") or "")
    prepared_by_row = (
        f"<dt>Prepared by</dt><dd>{html.escape(prepared_by)}</dd>" if prepared_by else ""
    )
    body = (
        f"{render_back_link('/records', 'Records')}"
        f"<header><h1>{html.escape(title)}</h1>"
        '<dl class="fields summary-fields">'
        f"<dt>Date</dt><dd>{html.escape(date_text)}</dd>"
        f"<dt>Type</dt><dd>{html.escape(type_text)}</dd>"
        f"{prepared_by_row}"
        "</dl></header>"
        f"{render_markdown(content)}"
    )
    return html_page(f"{title} - Records", body, active_section="records")
