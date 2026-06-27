from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from http import HTTPStatus
from typing import Any, Callable
from urllib import parse as urlparse
from urllib import request as urlrequest

from btq_cli.shift_report_sc_mapping import build_prefill_payload, parse_shift_report
from btq_cli.shift_report_to_sc import (
    archive_inspection,
    inspection_link,
    load_safetyculture_token,
    submit_prefill_payload,
)
from event_pipeline import couchdb_config
from btq_vault.entity_types import current_operator_id
from ops_dashboard.common import render_back_link, render_table, write_edit_record_fields_job
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


def _detail_path(record_id: str, **query: str) -> str:
    base = f"/records/{urlparse.quote(str(record_id), safe='')}"
    params = {key: value for key, value in query.items() if value}
    if not params:
        return base
    return f"{base}?{urlparse.urlencode(params)}"


def _redirect(location: str) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return (
        HTTPStatus.SEE_OTHER,
        "text/html; charset=utf-8",
        f'<a href="{html.escape(location, quote=True)}">Return</a>'.encode("utf-8"),
        {"Location": location},
    )


def _first_query_value(ctx: object, key: str) -> str:
    query = getattr(ctx, "query", {})
    values = query.get(key) if isinstance(query, dict) else None
    if isinstance(values, list) and values:
        return str(values[0] or "")
    if isinstance(values, str):
        return values
    return ""


def _render_safetyculture_flash(ctx: object) -> str:
    error = _first_query_value(ctx, "sc_error").strip()
    if error:
        return f'<section class="error"><h2>SafetyCulture</h2><p>{html.escape(error)}</p></section>'

    message = _first_query_value(ctx, "sc_message").strip()
    if not message:
        return ""
    audit_id = _first_query_value(ctx, "sc_audit_id").strip()
    link = ""
    if audit_id:
        href = inspection_link(audit_id)
        link = f' <a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">Open draft</a>'
    return f'<section class="success"><h2>SafetyCulture</h2><p>{html.escape(message)}{link}</p></section>'


def _render_safetyculture_section(doc: dict[str, Any]) -> str:
    if doc.get("type") != SHIFT_REPORT_TYPE:
        return ""

    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        return ""
    action = f"/records/{urlparse.quote(doc_id, safe='')}/publish-sc"
    audit_id = str(doc.get("sc_audit_id") or "").strip()
    published_at = str(doc.get("sc_published_at") or "").strip()
    if audit_id:
        href = inspection_link(audit_id)
        return (
            '<section><h2>SafetyCulture</h2>'
            '<p><span class="pill success">Published ✓</span></p>'
            '<dl class="fields summary-fields">'
            f"<dt>Published at</dt><dd>{html.escape(published_at)}</dd>"
            f'<dt>Draft</dt><dd><a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">{html.escape(audit_id)}</a></dd>'
            "</dl>"
            f'<form method="post" action="{html.escape(action, quote=True)}" onsubmit="return confirm(\'Re-publish to SafetyCulture? This archives the existing draft and creates a replacement.\')">'
            '<button type="submit">Re-publish</button>'
            "</form>"
            "</section>"
        )

    return (
        '<section><h2>SafetyCulture</h2>'
        f'<form method="post" action="{html.escape(action, quote=True)}" onsubmit="return confirm(\'Publish to SafetyCulture? This creates a draft inspection.\')">'
        '<button type="submit">Publish to SafetyCulture</button>'
        "</form>"
        "</section>"
    )


def _utc_now_iso_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def handle_publish_sc_post(
    ctx: object,
    record_id: str,
    _body: bytes = b"",
    *,
    doc: dict[str, Any] | None = None,
    http_client: object | None = None,
    token_loader: Callable[[], str] = load_safetyculture_token,
    now: Callable[[], str] = _utc_now_iso_z,
) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    loaded = doc if doc is not None else _load_record(record_id)
    if loaded is None or loaded.get("type") != SHIFT_REPORT_TYPE or loaded.get("operator") != current_operator_id():
        return _redirect(_detail_path(record_id, sc_error="Shift report not found."))

    doc_id = str(loaded.get("_id") or "").strip()
    if not doc_id:
        return _redirect(_detail_path(record_id, sc_error="Shift report is missing an id."))

    try:
        sections = parse_shift_report(str(loaded.get("content") or ""))
        payload = build_prefill_payload(sections, str(loaded.get("date") or ""))
        token = token_loader()
        old_audit_id = str(loaded.get("sc_audit_id") or "").strip()
        if old_audit_id:
            archive_inspection(old_audit_id, token, http_client=http_client)
        audit_id = submit_prefill_payload(payload, token, http_client=http_client)
        published_at = now()
        root = getattr(ctx, "runtime_root", None)
        if root is None:
            raise ValueError("Missing dashboard runtime root.")
        write_edit_record_fields_job(
            root,
            record_type=SHIFT_REPORT_TYPE,
            record_id=doc_id,
            fields={"sc_audit_id": audit_id, "sc_published_at": published_at},
            actor=current_operator_id(),
        )
    except Exception as exc:  # noqa: BLE001 - publish must never 500 the dashboard
        return _redirect(_detail_path(doc_id, sc_error=f"SafetyCulture publish failed: {exc}"))

    return _redirect(
        _detail_path(
            doc_id,
            sc_message="SafetyCulture draft created. The record will show as published after the queue applies.",
            sc_audit_id=audit_id,
        )
    )


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
        f"{_render_safetyculture_flash(ctx)}"
        f"{_render_safetyculture_section(doc)}"
        f"{render_markdown(content)}"
    )
    return html_page(f"{title} - Records", body, active_section="records")
