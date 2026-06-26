from __future__ import annotations

import html
import json
import logging
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib import parse as urlparse
from urllib import request as urlrequest

from btq_vault.location_urls import (
    LocationUrlError,
    location_urls_from_doc,
    normalize_location_url_entry,
)
from btq_vault.facility_hours import (
    FacilityHoursError,
    facility_hours_from_doc,
    normalize_facility_hours,
    unknown_facility_hours,
)
from event_pipeline import couchdb_config
from ops_dashboard.common import (
    default_actor,
    first_query_value,
    load_photo_vision_sidecars,
    record_section,
    render_count_badge,
    render_relative_time,
    safe_media_url,
    submitters_by_capture,
)
from ops_dashboard.layout import _demo_mode, html_page
import ops_dashboard.sections.entity_edit as entity_edit
import ops_dashboard.sections.field_photos as field_photos
import ops_dashboard.sections.sites as sites
from btq_vault.projector import (  # noqa: PLC2701 - reuse projector renderers for parity with static pages.
    DDOC,
    _employee_table,
    _opportunity_label,
    _row_date,
    _section,
    _simple_table,
    _site_id,
    _status,
    _visit_details,
    query_view,
    render_markdown,
)


_SUMMARY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Identity",
        (
            "account",
            "job",
            "location",
            "address",
            "facility_type",
            "status",
            "operational_status",
            "status_date",
            "updated",
        ),
    ),
    (
        "Contact",
        (
            "customer_name",
            "customer_title",
            "customer_phone",
            "customer_email",
        ),
    ),
    (
        "Schedule",
        (
            "service_days",
            "hours_per_week",
            "hours_per_day",
            "hours_per_day_weekday",
            "hours_per_day_weekend",
            "shift_start",
            "shift_end",
            "shift_start_aft",
            "shift_end_aft",
            "shift_start_day",
            "shift_end_day",
            "summer_hours",
            "floorwork",
        ),
    ),
    (
        "Billing & Wages",
        (
            "billing_monthly",
            "billing_per_day",
            "rate",
            "rate_per_hour",
            "wage_regular",
            "wage_lead",
            "wage_porter",
            "wage_1st_shift",
            "wage_2nd_shift",
            "wage_3rd_shift_range",
            "notes_billing",
            "background_check_fee",
        ),
    ),
    (
        "Supply Budget",
        (
            "supply_budget_type",
            "monthly_supply_budget",
            "supply_budget_monthly",
            "supply_budget_notes",
            "budget_last_verified",
            "supplies",
            "supplies_note",
        ),
    ),
)

_DATAVIEW_HEADINGS = {"Employees Assigned", "Open Issues", "Recent Visits"}
_FENCE_RE = re.compile(r"^(\s*)```(.*)$")
_CLOSE_FENCE_RE = re.compile(r"^\s*```\s*$")
_ACCESS_CONSTRAINT_RE = re.compile(r"(?im)^\s*#+\s*Access Constraints\b")
_CAPTURE_GALLERY_LIMIT = 6
_CAPTURE_COUNT_LIMIT = 5000
_FIELD_CAPTURE_PAGE_SIZE = 5000
_COMPLETE_CAPTURE_STATES = frozenset({"complete", "completed"})
_URL_KIND_OPTIONS = (
    "official_location_page",
    "client_homepage",
    "maps",
    "portal",
    "document",
    "other",
)
_URL_STATUS_OPTIONS = ("reference", "verified", "stale", "deprecated")
_FACILITY_HOURS_DAYS = (
    ("mon", "Mon"),
    ("tue", "Tue"),
    ("wed", "Wed"),
    ("thu", "Thu"),
    ("fri", "Fri"),
    ("sat", "Sat"),
    ("sun", "Sun"),
)
_EMPTY_ABOUT_SECTION = (
    '<section><h2>About &amp; operational notes</h2>'
    '<p class="zero-state">No operational notes yet.</p></section>'
)
logger = logging.getLogger(__name__)

_BUILTIN_LOCATION_DOCS: dict[str, dict[str, Any]] = {
    "SANDBOX": {
        "_id": "location_SANDBOX",
        "type": "location",
        "site_id": "SANDBOX",
        "location": "Sandbox Site",
        "account": "Sandbox Site",
        "active": True,
        "content": (
            "Demo / test sandbox site for exercising capture, semantic extraction, and "
            "job review end-to-end. Treat as a generic commercial cleaning site."
        ),
        "facility_hours": {
            "status": "verified",
            "last_verified_at": "2026-06-26",
            "last_verified_by": "Greg",
            "source": "public_safe_fixture",
            "note": "Public-safe synthetic facility hours for dashboard QA.",
            "weekly": {
                "mon": [{"open": "08:30", "close": "17:00"}],
                "tue": [{"open": "08:30", "close": "17:00"}],
                "wed": [{"open": "08:30", "close": "17:00"}],
                "thu": [{"open": "08:30", "close": "17:00"}],
                "fri": [{"open": "08:30", "close": "15:00"}],
                "sat": [],
                "sun": [],
            },
            "exceptions": [
                {
                    "rule": "nth_weekday",
                    "weekday": "tue",
                    "ordinals": [2, 4],
                    "hours": [{"open": "10:00", "close": "19:00"}],
                    "note": "Second and fourth Tuesday",
                }
            ],
        },
        "_builtin": True,
    },
}


def _cdb() -> tuple[str, dict[str, str], str, float]:
    base = sites.couchdb_base_url()
    headers = sites.auth_headers()
    database = couchdb_config.vault_database()
    timeout = couchdb_config.timeout()
    return base, headers, database, timeout


def _load_location(site_id: str) -> dict[str, Any] | None:
    base, headers, database, timeout = _cdb()
    doc_id = f"location_{site_id}"
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/{urlparse.quote(doc_id, safe='')}"
    req = urlrequest.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _builtin_location_doc(site_id: str) -> dict[str, Any] | None:
    doc = _BUILTIN_LOCATION_DOCS.get(site_id)
    return dict(doc) if doc is not None else None


def _is_blank(line: str) -> bool:
    return not line.strip()


def _drop_preceding_dataview_heading(output: list[str]) -> None:
    blank_count = 0
    index = len(output) - 1
    while index >= 0 and _is_blank(output[index]):
        blank_count += 1
        index -= 1
    if index < 0 or blank_count > 1:
        return
    stripped = output[index].strip()
    if not re.match(r"^###[^#]", stripped):
        return
    title = stripped.lstrip("#").strip()
    if title in _DATAVIEW_HEADINGS:
        del output[index]


def _trim_trailing_blank_run(lines: list[str]) -> None:
    blank_count = 0
    index = len(lines) - 1
    while index >= 0 and _is_blank(lines[index]):
        blank_count += 1
        index -= 1
    if blank_count > 1:
        del lines[index + 2 :]


def _strip_dataview_blocks(body: str) -> str:
    lines = body.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    just_dropped = False
    while index < len(lines):
        line = lines[index]
        match = _FENCE_RE.match(line)
        info = match.group(2).strip().lower() if match else ""
        if match and info.startswith("dataview"):
            _drop_preceding_dataview_heading(output)
            _trim_trailing_blank_run(output)
            index += 1
            while index < len(lines):
                if _CLOSE_FENCE_RE.match(lines[index]):
                    index += 1
                    break
                index += 1
            just_dropped = True
            continue
        if just_dropped and _is_blank(line) and output and _is_blank(output[-1]):
            index += 1
            continue
        output.append(line)
        just_dropped = False
        index += 1
    return "".join(output)


def _quick_facts_section(doc: dict[str, Any], site_id: str, edit_section: str) -> str:
    sections: list[str] = []
    site_id_escaped = html.escape(site_id, quote=True)
    editable_sections = {
        "Contact": "contact",
        "Schedule": "schedule",
        "Billing & Wages": "billing_wages",
    }
    for title, keys in _SUMMARY_GROUPS:
        if title in {"Identity", "Supply Budget"}:
            section = record_section(title, doc, keys, dl_class="fields summary-fields")
            if section:
                sections.append(section)
            continue
        slug = editable_sections.get(title)
        if slug is None:
            continue
        if edit_section == slug:
            sections.append(
                entity_edit.render_editable_section(
                    title,
                    doc,
                    keys,
                    edit_active=True,
                    save_action=f"/sites/{site_id_escaped}/save-section",
                    entity_id=site_id,
                )
            )
        else:
            section = record_section(
                title,
                doc,
                keys,
                actions_html=f'<p class="actions"><a class="button" href="?edit={html.escape(slug, quote=True)}">Edit</a></p>',
                dl_class="fields summary-fields",
            )
            if section:
                sections.append(section)

    provenance = _capture_provenance(doc)
    if provenance:
        sections.append(provenance)

    if not sections:
        sections.append('<p class="zero-state">No quick facts yet.</p>')
    return f'<section class="quick-facts"><h2>Quick facts</h2>{"".join(sections)}</section>'


def _capture_provenance(doc: dict[str, Any]) -> str:
    parts = []
    for key in ("btq_job_ids", "voice_memo_capture_ids"):
        values = doc.get(key)
        if not isinstance(values, list) or not values:
            continue
        text = ", ".join(str(value) for value in values if str(value).strip())
        if text:
            parts.append(f"<p>{html.escape(key)}: {html.escape(text)}</p>")
    if not parts:
        return ""
    return f"<details><summary>Capture provenance</summary>{''.join(parts)}</details>"


def _humanize_url_value(value: object) -> str:
    return str(value or "").strip().replace("_", " ").title()


def _select_options(values: tuple[str, ...], selected: str) -> str:
    options = []
    for value in values:
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{html.escape(value, quote=True)}"{selected_attr}>{html.escape(_humanize_url_value(value))}</option>')
    return "".join(options)


def _url_form_fields(entry: dict[str, Any] | None = None, *, action: str) -> str:
    current = entry or {}
    kind = str(current.get("kind") or "official_location_page")
    status = str(current.get("status") or "reference")
    url_value = html.escape(str(current.get("url") or ""), quote=True)
    label = html.escape(str(current.get("label") or ""), quote=True)
    verified_at = html.escape(str(current.get("last_verified_at") or ""), quote=True)
    verified_by = html.escape(str(current.get("last_verified_by") or ""), quote=True)
    note = html.escape(str(current.get("verification_note") or ""), quote=True)
    url_name = "new_url" if action == "edit" else "url"
    return (
        f'<label>URL <input type="url" name="{url_name}" value="{url_value}" required></label>'
        f'<label>Label <input name="label" value="{label}"></label>'
        f'<label>Kind <select name="kind">{_select_options(_URL_KIND_OPTIONS, kind)}</select></label>'
        f'<label>Status <select name="status">{_select_options(_URL_STATUS_OPTIONS, status)}</select></label>'
        f'<label>Verified at <input name="last_verified_at" value="{verified_at}" placeholder="YYYY-MM-DD or ISO timestamp"></label>'
        f'<label>Verified by <input name="last_verified_by" value="{verified_by}"></label>'
        f'<label>Note <input name="verification_note" value="{note}"></label>'
    )


def _reference_link_item(site_id: str, entry: dict[str, Any], index: int) -> str:
    escaped_site = html.escape(site_id, quote=True)
    url = str(entry.get("url") or "")
    escaped_url = html.escape(url, quote=True)
    label = str(entry.get("label") or "").strip() or url
    status = str(entry.get("status") or "reference")
    kind = str(entry.get("kind") or "other")
    note = str(entry.get("verification_note") or "").strip()
    note_attr = f' title="{html.escape(note, quote=True)}"' if note else ""
    status_class = re.sub(r"[^a-z0-9_-]+", "_", status.lower()).strip("_") or "reference"
    return (
        f'<li class="reference-link-item"{note_attr}>'
        '<div class="reference-link-main">'
        f'<a href="{escaped_url}" target="_blank" rel="noreferrer">{html.escape(label)}</a>'
        f'<span class="subline">{html.escape(url)}</span>'
        '</div>'
        '<div class="reference-link-meta">'
        f'<span class="pill">{html.escape(_humanize_url_value(kind))}</span>'
        f'<span class="pill status-{html.escape(status_class, quote=True)}">{html.escape(_humanize_url_value(status))}</span>'
        '</div>'
        '<details class="reference-link-edit">'
        '<summary>Edit</summary>'
        f'<form method="post" action="/sites/{escaped_site}/urls">'
        '<input type="hidden" name="action" value="edit">'
        f'<input type="hidden" name="url" value="{escaped_url}">'
        f'<input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">'
        f'{_url_form_fields(entry, action="edit")}'
        '<button type="submit">Save link</button>'
        '</form>'
        '</details>'
        f'<form class="reference-link-remove" method="post" action="/sites/{escaped_site}/urls">'
        '<input type="hidden" name="action" value="remove">'
        f'<input type="hidden" name="url" value="{escaped_url}">'
        f'<input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        f'<button class="reject" type="submit" aria-label="Remove reference link {index + 1}">Remove</button>'
        '</form>'
        '</li>'
    )


def _reference_links_section(doc: dict[str, Any], site_id: str) -> str:
    urls = location_urls_from_doc(doc)
    escaped_site = html.escape(site_id, quote=True)
    if urls:
        list_html = '<ul class="reference-link-list">' + "".join(_reference_link_item(site_id, entry, index) for index, entry in enumerate(urls)) + "</ul>"
    else:
        list_html = '<p class="zero-state">No reference links yet.</p>'
    add_form = (
        '<details class="reference-link-add">'
        '<summary>Add reference link</summary>'
        f'<form method="post" action="/sites/{escaped_site}/urls">'
        '<input type="hidden" name="action" value="add">'
        f'<input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">'
        f'{_url_form_fields(None, action="add")}'
        '<button type="submit">Add link</button>'
        '</form>'
        '</details>'
    )
    return f'<section class="reference-links"><h2>Reference Links</h2>{list_html}{add_form}</section>'


def _site_url_job_payload(form: dict[str, list[str]], site_id: str) -> dict[str, object]:
    action = first_query_value(form, "action").strip()
    payload: dict[str, object] = {
        "site_id": site_id,
        "action": action,
        "url": first_query_value(form, "url").strip(),
        "actor": first_query_value(form, "actor").strip() or default_actor(),
        "source": "ops_dashboard_site_detail",
    }
    if action == "edit":
        new_url = first_query_value(form, "new_url").strip()
        if new_url:
            payload["new_url"] = new_url
    for field in ("label", "kind", "status", "last_verified_at", "last_verified_by", "verification_note"):
        value = first_query_value(form, field).strip()
        if value or (field in {"label", "verification_note"} and action == "edit"):
            payload[field] = value
    if action == "add" and "status" not in payload:
        payload["status"] = "reference"
    return payload


def _write_site_url_job(runtime_root: Path, payload: dict[str, object]) -> Path:
    from queue_spec import JOB_SET_SITE_URL, validate_job

    suffix = str(uuid.uuid4())
    job = {
        "job_id": f"set-site-url-{suffix}",
        "job_type": JOB_SET_SITE_URL,
        "payload": payload,
    }
    if not validate_job(job):
        raise ValueError("invalid set_site_url payload")
    queue_dir = runtime_root.expanduser().resolve(strict=False) / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"set-site-url-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def _format_facility_interval(interval: dict[str, str]) -> str:
    return f"{html.escape(interval['open'])}-{html.escape(interval['close'])}"


def _facility_hours_json_for_form(hours: dict[str, Any]) -> str:
    form_hours = hours if hours.get("status") != "unknown" else unknown_facility_hours() | {"status": "reference"}
    return json.dumps(form_hours, indent=2, sort_keys=True).replace("[]", "[ ]")


def _facility_hours_weekly_table(hours: dict[str, Any]) -> str:
    weekly = hours.get("weekly") if isinstance(hours.get("weekly"), dict) else {}
    rows: list[str] = []
    for key, label in _FACILITY_HOURS_DAYS:
        intervals = weekly.get(key, [])
        if intervals:
            value = ", ".join(_format_facility_interval(interval) for interval in intervals)
        else:
            value = "Closed"
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{value}</td></tr>")
    return '<table class="facility-hours-table"><tbody>' + "".join(rows) + "</tbody></table>"


def _facility_hours_exceptions(hours: dict[str, Any]) -> str:
    exceptions = hours.get("exceptions") if isinstance(hours.get("exceptions"), list) else []
    if not exceptions:
        return '<p class="zero-state">No facility-hour exceptions recorded.</p>'
    items: list[str] = []
    for exception in exceptions:
        rule = str(exception.get("rule") or "")
        if rule == "date":
            label = str(exception.get("date") or "")
        elif rule == "nth_weekday":
            ordinals = ", ".join(str(value) for value in exception.get("ordinals", []))
            label = f"{ordinals} {str(exception.get('weekday') or '').title()}"
        else:
            label = rule
        intervals = exception.get("hours") if isinstance(exception.get("hours"), list) else []
        hours_label = ", ".join(_format_facility_interval(interval) for interval in intervals) if intervals else "Closed"
        note = str(exception.get("note") or "").strip()
        note_html = f' <span class="subline">{html.escape(note)}</span>' if note else ""
        items.append(f"<li><strong>{html.escape(label)}</strong>: {hours_label}{note_html}</li>")
    return '<ul class="facility-hours-exceptions">' + "".join(items) + "</ul>"


def _facility_hours_section(doc: dict[str, Any], site_id: str) -> str:
    hours = facility_hours_from_doc(doc)
    escaped_site = html.escape(site_id, quote=True)
    status = str(hours.get("status") or "unknown")
    status_class = re.sub(r"[^a-z0-9_-]+", "_", status.lower()).strip("_") or "unknown"
    verified_bits = []
    if hours.get("last_verified_at"):
        verified_bits.append(f"verified {html.escape(str(hours['last_verified_at']))}")
    if hours.get("last_verified_by"):
        verified_bits.append(f"by {html.escape(str(hours['last_verified_by']))}")
    verified_line = " ".join(verified_bits) or "No operator verification recorded."
    note = str(hours.get("note") or "").strip()
    note_html = f'<p class="subline">{html.escape(note)}</p>' if note else ""
    form_json = html.escape(_facility_hours_json_for_form(hours))
    return (
        '<section class="facility-hours">'
        '<div class="section-heading-row">'
        '<h2>Facility Hours</h2>'
        f'<span class="pill status-{html.escape(status_class, quote=True)}">{html.escape(status.title())}</span>'
        '</div>'
        f'<p class="subline">{verified_line}</p>'
        f"{note_html}"
        f"{_facility_hours_weekly_table(hours)}"
        f"{_facility_hours_exceptions(hours)}"
        '<details class="facility-hours-edit">'
        '<summary>Edit facility hours</summary>'
        f'<form method="post" action="/sites/{escaped_site}/facility-hours">'
        '<input type="hidden" name="action" value="set">'
        f'<input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">'
        '<label>Structured facility_hours JSON'
        f'<textarea name="facility_hours_json" spellcheck="false" required>{form_json}</textarea>'
        '</label>'
        '<button type="submit">Queue facility hours update</button>'
        '</form>'
        '</details>'
        f'<form class="facility-hours-clear" method="post" action="/sites/{escaped_site}/facility-hours">'
        '<input type="hidden" name="action" value="clear">'
        f'<input type="hidden" name="actor" value="{html.escape(default_actor(), quote=True)}">'
        '<input type="hidden" name="confirm" value="1">'
        '<button class="reject" type="submit">Clear facility hours</button>'
        '</form>'
        '</section>'
    )


def _site_hours_job_payload(form: dict[str, list[str]], site_id: str) -> dict[str, object]:
    action = first_query_value(form, "action").strip() or "set"
    payload: dict[str, object] = {
        "site_id": site_id,
        "action": action,
        "actor": first_query_value(form, "actor").strip() or default_actor(),
        "source": "ops_dashboard_site_detail",
    }
    if action != "clear":
        raw_json = first_query_value(form, "facility_hours_json").strip()
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise FacilityHoursError("facility_hours JSON must be an object")
        payload["facility_hours"] = normalize_facility_hours(parsed)
    return payload


def _write_site_hours_job(runtime_root: Path, payload: dict[str, object]) -> Path:
    from queue_spec import JOB_SET_SITE_HOURS, validate_job

    suffix = str(uuid.uuid4())
    job = {
        "job_id": f"set-site-hours-{suffix}",
        "job_type": JOB_SET_SITE_HOURS,
        "payload": payload,
    }
    if not validate_job(job):
        raise ValueError("invalid set_site_hours payload")
    queue_dir = runtime_root.expanduser().resolve(strict=False) / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"set-site-hours-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def _site_notes(site_id: str) -> list[dict[str, Any]]:
    """Site-tagged ``note`` docs from CouchDB (``btq_vault``).

    Surfaces operator notes linked to this site (``site_id`` field). Note: legacy
    day-record notes are keyed by date with no site_id and won't appear here.
    """
    base, headers, database, timeout = _cdb()
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/_find"
    payload = {"selector": {"type": "note"}, "limit": 1000}
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            docs = json.loads(resp.read().decode("utf-8")).get("docs", [])
    except Exception:  # noqa: BLE001 — degrade to an empty section, never break the page
        docs = []
    target = str(site_id).strip()
    notes = [d for d in docs if str(d.get("site_id") or "").strip().strip('"') == target]
    notes.sort(key=lambda d: str(d.get("created_at") or d.get("date") or ""), reverse=True)
    return notes


def _notes_section(notes: list[dict[str, Any]]) -> str:
    """Render site notes with the same populated/empty output as the old site page."""
    if not notes:
        return _section("Notes", '<p class="muted">No site notes yet</p>')
    blocks = []
    for doc in notes:
        when = str(doc.get("created_at") or doc.get("date") or "")[:10]
        content = str(doc.get("content") or "")
        if content.startswith("---\n"):
            closing = content.find("\n---\n")
            if closing != -1:
                content = content[closing + 5:]
        meta = f'<div class="muted" style="font-size:12px">{html.escape(when)}</div>' if when else ""
        blocks.append(
            f'<article style="border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:12px;margin-bottom:10px">{meta}{render_markdown(content)}</article>'
        )
    return _section("Notes", "".join(blocks))


def _text(value: object) -> str:
    return str(value or "").strip()


def _row_doc_or_value(row: dict[str, Any]) -> dict[str, Any]:
    doc = row.get("doc")
    if isinstance(doc, dict):
        return doc
    value = row.get("value")
    return value if isinstance(value, dict) else {}


def _employee_person_id(row: dict[str, Any]) -> str:
    value = _row_doc_or_value(row)
    return _text(value.get("person_id") or row.get("id"))


def _employee_name(row: dict[str, Any]) -> str:
    value = _row_doc_or_value(row)
    name = _text(value.get("name"))
    if name:
        return name
    first = _text(value.get("preferred_name") or value.get("first"))
    last = _text(value.get("last"))
    combined = " ".join(part for part in (first, last) if part)
    return combined or _employee_person_id(row) or "Unknown cleaner"


def _availability_constraint_date(value: dict[str, Any]) -> date | None:
    raw = _text(value.get("date"))[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _coverage_gap_rows(employee_rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    person_ids = [_employee_person_id(row) for row in employee_rows]
    person_ids = [person_id for person_id in dict.fromkeys(person_ids) if person_id]
    if not person_ids:
        return []

    person_id_set = set(person_ids)
    labels = {
        person_id: _employee_name(row)
        for row in employee_rows
        if (person_id := _employee_person_id(row))
    }
    try:
        base, headers, database, timeout = _cdb()
        constraint_rows = query_view(
            base,
            headers,
            database,
            DDOC,
            "availability_constraints_by_person",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - coverage gaps are additive and should degrade to empty.
        constraint_rows = []

    today = date.today()
    grouped: dict[str, dict[str, object]] = {
        person_id: {"unavailable_dates": set(), "last_working_days": []}
        for person_id in person_ids
    }
    for row in constraint_rows:
        person_id = _text(row.get("key"))
        if person_id not in person_id_set:
            continue
        value = row.get("value")
        if not isinstance(value, dict):
            continue
        constraint_date = _availability_constraint_date(value)
        if constraint_date is None or constraint_date < today:
            continue
        constraint_type = _text(value.get("constraint_type"))
        if constraint_type == "unavailable_date":
            unavailable_dates = grouped[person_id]["unavailable_dates"]
            if isinstance(unavailable_dates, set):
                unavailable_dates.add(constraint_date)
        elif constraint_type == "last_working_day":
            last_working_days = grouped[person_id]["last_working_days"]
            if isinstance(last_working_days, list):
                last_working_days.append(constraint_date)

    rows: list[dict[str, object]] = []
    for person_id in person_ids:
        availability = grouped[person_id]
        unavailable_dates = availability["unavailable_dates"]
        last_working_days = availability["last_working_days"]
        if not isinstance(unavailable_dates, set) or not isinstance(last_working_days, list):
            continue
        if not unavailable_dates and not last_working_days:
            continue
        rows.append({
            "person_id": person_id,
            "name": labels.get(person_id) or person_id,
            "unavailable_dates": sorted(unavailable_dates),
            "last_working_day": min(last_working_days) if last_working_days else None,
        })
    return rows


def _coverage_gaps_section(rows: list[dict[str, object]]) -> str:
    if not rows:
        return _section("Upcoming coverage gaps", '<p class="zero-state">No upcoming coverage gaps.</p>')
    items: list[str] = []
    for row in rows:
        dates = row.get("unavailable_dates")
        unavailable = ", ".join(day.isoformat() for day in dates if isinstance(day, date)) if isinstance(dates, list) else ""
        last_day = row.get("last_working_day")
        if isinstance(last_day, date):
            summary = f"{unavailable} (+ Last day: {last_day.isoformat()})" if unavailable else f"Last day: {last_day.isoformat()}"
        else:
            summary = unavailable
        name = _text(row.get("name") or row.get("person_id"))
        items.append(f"<li><strong>{html.escape(name)}</strong>: {html.escape(summary)}</li>")
    return _section("Upcoming coverage gaps", f"<ul>{''.join(items)}</ul>")


def _related_data(site_id: str) -> dict[str, Any]:
    base, headers, database, timeout = _cdb()
    employee_rows = [
        row
        for row in query_view(base, headers, database, DDOC, "employees_by_site", include_docs=True, timeout=timeout)
        if _site_id(row) == site_id
    ]
    opportunity_rows = [
        row
        for row in query_view(base, headers, database, DDOC, "opportunities_by_site_status", include_docs=True, timeout=timeout)
        if _site_id(row) == site_id and _status(row) == "open"
    ]
    visit_rows = [
        row
        for row in query_view(base, headers, database, DDOC, "visits_by_site_date", include_docs=True, timeout=timeout)
        if _site_id(row) == site_id
    ]
    recent_visits = sorted(visit_rows, key=lambda row: _row_date(row) or date.min, reverse=True)[:5]
    notes = _site_notes(site_id)
    coverage_gap_rows = _coverage_gap_rows(employee_rows)
    return {
        "notes": notes,
        "employee_rows": employee_rows,
        "coverage_gap_rows": coverage_gap_rows,
        "opportunity_rows": opportunity_rows,
        "visit_rows": visit_rows,
        "recent_visits": recent_visits,
    }


def _related_sections(data: dict[str, Any]) -> list[tuple[str, int, str]]:
    employee_rows = data["employee_rows"]
    coverage_gap_rows = data.get("coverage_gap_rows", [])
    opportunity_rows = data["opportunity_rows"]
    recent_visits = data["recent_visits"]
    notes = data["notes"]
    return [
        ("Notes", len(notes), _notes_section(notes)),
        (
            "Employees Assigned",
            len(employee_rows),
            _section("Employees Assigned", _employee_table(employee_rows, include_sites=False)),
        ),
        (
            "Upcoming coverage gaps",
            len(coverage_gap_rows),
            _coverage_gaps_section(coverage_gap_rows),
        ),
        (
            "Open Opportunities",
            len(opportunity_rows),
            _section(
                "Open Opportunities",
                _simple_table(["Opportunity"], [_opportunity_label(row) for row in opportunity_rows]),
            ),
        ),
        ("Recent Visits", len(recent_visits), _section("Recent Visits", _visit_details(recent_visits))),
    ]


def _metric_cards(
    *,
    open_opportunities: int,
    access_flags: int,
    field_captures: int,
    last_visit: str,
) -> str:
    metrics = (
        ("Open opportunities", str(open_opportunities)),
        ("Access flags", str(access_flags)),
        ("Field captures", str(field_captures)),
        ("Last visit", last_visit or "&mdash;"),
    )
    cards = "".join(
        (
            '<div class="metric-card">'
            f'<strong>{value}</strong>'
            f'<span>{html.escape(label)}</span>'
            "</div>"
        )
        for label, value in metrics
    )
    return f'<section class="metric-grid" aria-label="Site metrics">{cards}</section>'


def _site_capture_processing_counts(site_id: str) -> dict[str, int] | None:
    try:
        base, headers, _vault_database, timeout = _cdb()
        database = couchdb_config.field_captures_database()
    except Exception as exc:  # noqa: BLE001 - upload confirmation is best-effort.
        logger.warning("site capture processing config unavailable for site_id=%s: %s", site_id, exc)
        return None
    docs: list[dict[str, Any]] = []
    bookmark: object = None

    for _ in range(400):  # safety cap (400 * 5000 = 2M docs)
        payload: dict[str, object] = {
            "selector": {"type": "field_capture", "site_id": site_id},
            "fields": ["processing_state"],
            "limit": _FIELD_CAPTURE_PAGE_SIZE,
        }
        if bookmark:
            payload["bookmark"] = bookmark
        url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/_find"
        req = urlrequest.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=max(timeout, 30.0)) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - upload confirmation is best-effort.
            logger.warning("site capture processing counts unavailable for site_id=%s: %s", site_id, exc)
            return None
        page_docs = parsed.get("docs") if isinstance(parsed, dict) else None
        if not isinstance(page_docs, list):
            return None
        docs.extend(doc for doc in page_docs if isinstance(doc, dict))
        bookmark = parsed.get("bookmark")
        if len(page_docs) < _FIELD_CAPTURE_PAGE_SIZE or not bookmark:
            break

    received = len(docs)
    complete = 0
    failed = 0
    for doc in docs:
        state = str(doc.get("processing_state") or "").strip().lower()
        if state in _COMPLETE_CAPTURE_STATES:
            complete += 1
        elif state == "failed":
            failed += 1
    return {
        "received": received,
        "complete": complete,
        "failed": failed,
        "in_flight": max(0, received - complete - failed),
    }


def _capture_processing_breakdown(counts: dict[str, int] | None) -> str:
    if counts is None:
        return ""
    metrics = (
        ("Received", render_count_badge(counts.get("received", 0), kind="neutral")),
        ("Complete", render_count_badge(counts.get("complete", 0), kind="neutral")),
        ("In flight", render_count_badge(counts.get("in_flight", 0), kind="pending")),
        ("Failed", render_count_badge(counts.get("failed", 0), kind="danger")),
    )
    cards = "".join(
        (
            '<div class="metric-card">'
            f"<strong>{value}</strong>"
            f"<span>{html.escape(label)}</span>"
            "</div>"
        )
        for label, value in metrics
    )
    return (
        '<section class="metric-grid" aria-label="Field capture upload confirmation">'
        f"{cards}"
        "</section>"
    )


def _details_block(title: str, count: int | None, body: str) -> str:
    count_html = ""
    if count is not None:
        count_html = (
            '<span class="details-count">'
            f'{render_count_badge(count, kind="neutral")}'
            f' {html.escape("record" if count == 1 else "records")}'
            "</span>"
        )
    return (
        '<details class="detail-block">'
        f'<summary><span>{html.escape(title)}</span>{count_html}</summary>'
        f'<div class="detail-block-body">{body}</div>'
        "</details>"
    )


def _last_visit_label(visit_rows: list[dict[str, Any]]) -> str:
    dates = sorted([row_date for row in visit_rows if (row_date := _row_date(row))], reverse=True)
    if not dates:
        return ""
    return render_relative_time(dates[0].isoformat())


def _site_subline(doc: dict[str, Any], site_id: str) -> str:
    account = str(doc.get("account") or "").strip()
    status = str(doc.get("status") or doc.get("operational_status") or "").strip()
    parts = [
        html.escape(account) if account else "&mdash;",
        html.escape(site_id),
        html.escape(status) if status else "&mdash;",
    ]
    return '<p class="subline">' + " &middot; ".join(parts) + "</p>"


def _capture_sort_key(sidecar: dict[str, object]) -> str:
    return str(sidecar.get("captured_at") or sidecar.get("generated_at") or "")


def _site_capture_records(ctx: object, site_id: str) -> tuple[list[dict[str, object]], bool, int]:
    cdb_config = field_photos._photo_vision_couchdb_config()  # noqa: SLF001 - reuse existing field-photo source.
    sidecars: list[dict[str, object]] = []
    fallback = False

    if cdb_config is not None:
        mango = field_photos._build_mango_selector("", site_id, "", "", "")  # noqa: SLF001
        mango["limit"] = _CAPTURE_COUNT_LIMIT
        docs = field_photos._query_couchdb(cdb_config, mango)  # noqa: SLF001
        if docs is not None:
            sidecars = docs
        else:
            fallback = True

    if cdb_config is None or fallback:
        from field_capture import photo_vision as field_photo_vision

        photo_vision_dir = field_photo_vision.default_photo_vision_dir(ctx.runtime_root)
        sidecars = [
            sidecar
            for sidecar in load_photo_vision_sidecars(photo_vision_dir)
            if str(sidecar.get("site_id") or "") == site_id
        ]

    sidecars.sort(key=_capture_sort_key, reverse=True)
    capture_count = len({str(s.get("capture_id") or "") for s in sidecars if s.get("capture_id")})
    return sidecars[:_CAPTURE_GALLERY_LIMIT], fallback, capture_count


def _vision_status_chip(sidecar: dict[str, object]) -> str:
    status = str(sidecar.get("status") or "").strip() or "processed"
    css_status = re.sub(r"[^a-z0-9_-]+", "_", status.lower()).strip("_") or "processed"
    return f'<span class="pill status-{html.escape(css_status, quote=True)}">{html.escape(status)}</span>'


def _capture_gallery_card(sidecar: dict[str, object], submitters: dict[str, dict[str, str]]) -> str:
    capture_id = str(sidecar.get("capture_id") or "").strip()
    if not capture_id:
        return ""
    provenance = sidecar.get("provenance") if isinstance(sidecar.get("provenance"), dict) else {}
    raw_url = (
        provenance.get("image_media_url")
        if isinstance(provenance, dict)
        else None
    ) or sidecar.get("image_media_url")
    media_url = safe_media_url(raw_url)
    if media_url:
        preview = (
            f'<img src="{html.escape(media_url, quote=True)}" alt="field capture thumbnail" '
            'loading="lazy" class="site-gallery-thumb">'
        )
    else:
        preview = '<div class="site-gallery-thumb site-gallery-thumb--audio" aria-hidden="true">&#127908;</div>'

    title = (
        str(sidecar.get("area_guess") or "").strip()
        or str(sidecar.get("submitted_area") or "").strip()
        or "Field capture"
    )
    submitter = submitters.get(capture_id, {}).get("submitter_name", "").strip()
    when = render_relative_time(str(sidecar.get("captured_at") or sidecar.get("generated_at") or ""))
    meta = " &middot; ".join(part for part in (html.escape(submitter), when) if part)
    detail_url = f"/captures?capture_id={quote(capture_id)}"
    meta_html = (
        f'<p class="subline site-gallery-meta">{meta}</p>'
        if meta
        else '<p class="subline site-gallery-meta">&mdash;</p>'
    )
    return (
        f'<a class="site-gallery-card" href="{detail_url}">'
        f"{preview}"
        '<span class="site-gallery-card-body">'
        f'<strong>{html.escape(title)}</strong>'
        f"{meta_html}"
        f"{_vision_status_chip(sidecar)}"
        "</span>"
        "</a>"
    )


def _captures_section(
    ctx: object,
    site_id: str,
    records: list[dict[str, object]],
    fallback: bool,
    total_count: int,
) -> str:
    escaped_id = html.escape(site_id, quote=True)
    submitters = submitters_by_capture(ctx.runtime_root)
    cards = "".join(_capture_gallery_card(record, submitters) for record in records)
    fallback_notice = '<p class="muted">CouchDB unavailable; showing disk cache captures.</p>' if fallback else ""
    gallery = (
        f'<div class="site-gallery">{cards}</div>'
        if cards
        else '<p class="zero-state">No field captures yet for this site.</p>'
    )
    return (
        '<section class="site-captures">'
        '<div class="section-heading-row">'
        f"<h2>Field captures &middot; {html.escape(str(total_count))}</h2>"
        f'<a href="/field-photos?site_id={escaped_id}">View all &rarr;</a>'
        "</div>"
        f"{fallback_notice}"
        f"{gallery}"
        "</section>"
    )


def _not_found(site_id: str) -> str:
    escaped_id = html.escape(site_id)
    return html_page(
        "Site not found",
        f"<header><h1>Sites</h1><p>Site not found: {escaped_id}</p></header>",
        active_section="site_detail",
    )


def _degraded(site_id: str, exc: Exception) -> str:
    escaped_id = html.escape(site_id)
    return html_page(
        f"Site {site_id} — BTQ",
        (
            f"<header><h1>Site {escaped_id}</h1></header>"
            f'<section class="error"><p>{html.escape(str(exc))}</p></section>'
        ),
        active_section="site_detail",
    )


def handle_save_section(ctx: object, site_id: str, body: bytes):
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import first_query_value
    import ops_dashboard.sections.entity_edit as entity_edit

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    form_flat = {k: v[0] for k, v in form.items()}
    section = first_query_value(form, "_section").strip()

    _ALLOWED_KEYS: dict[str, frozenset[str]] = {
        "contact": frozenset((
            "customer_name", "customer_title", "customer_phone", "customer_email",
        )),
        "schedule": frozenset((
            "service_days", "hours_per_week", "hours_per_day",
            "hours_per_day_weekday", "hours_per_day_weekend",
            "shift_start", "shift_end", "shift_start_aft", "shift_end_aft",
            "shift_start_day", "shift_end_day", "summer_hours", "floorwork",
        )),
        "billing_wages": frozenset((
            "billing_monthly", "billing_per_day", "rate", "rate_per_hour",
            "wage_regular", "wage_lead", "wage_porter",
            "wage_1st_shift", "wage_2nd_shift", "wage_3rd_shift_range",
            "notes_billing", "background_check_fee",
        )),
        "about": frozenset(("content",)),
    }
    allowed_keys = _ALLOWED_KEYS.get(section)
    if allowed_keys is None:
        return 400, "text/plain; charset=utf-8", b"Unknown section", {}

    existing = _load_location(site_id)
    if not existing or existing.get("type") != "location":
        return ctx.redirect(f"/sites/{quote(site_id)}?error=not_found")

    updated = entity_edit.apply_section_update(existing, form_flat, allowed_keys)

    # Assert validate_doc_update contract (btq_vault requires type + non-empty operator)
    assert updated.get("type") == "location"
    assert updated.get("operator")

    updated["_rev"] = existing["_rev"]

    doc_path = f"{couchdb_config.vault_database()}/location_{site_id}"
    try:
        sites.request_json("PUT", doc_path, updated)
        ctx.audit(
            f"/sites/{site_id}/save-section",
            {"section": section},
            f"success: updated section={section}",
        )
        return ctx.redirect(f"/sites/{quote(site_id)}")
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == 409:
            ctx.audit(f"/sites/{site_id}/save-section", {"section": section}, "failed: conflict")
            return (
                200, "text/html; charset=utf-8",
                render(ctx, site_id).encode("utf-8"),
                {},
            )
        ctx.audit(f"/sites/{site_id}/save-section", {"section": section}, f"failed: {exc}")
        return ctx.redirect(f"/sites/{quote(site_id)}?error={quote(str(exc))}")


def handle_site_url_post(ctx: object, site_id: str, body: bytes):
    from urllib.parse import parse_qs, quote

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)
    form_payload = {key: values[0] if values else "" for key, values in form.items()}

    def _redirect(location: str) -> tuple:
        return 303, "text/html; charset=utf-8", f'<a href="{html.escape(location)}">Return</a>'.encode(), {"Location": location}

    action = first_query_value(form, "action").strip()
    if action == "remove" and first_query_value(form, "confirm") != "1":
        if hasattr(ctx, "audit"):
            ctx.audit(f"/sites/{site_id}/urls", form_payload, "failed: confirm_required")
        return _redirect(f"/sites/{quote(site_id)}?error=confirm_required")

    try:
        payload = _site_url_job_payload(form, site_id)
        if action in {"add", "edit"}:
            url_entry = dict(payload)
            if "new_url" in payload:
                url_entry["url"] = payload["new_url"]
            normalize_location_url_entry(url_entry)
        queue_path = _write_site_url_job(root, payload)
    except (LocationUrlError, ValueError, OSError) as exc:
        if hasattr(ctx, "audit"):
            ctx.audit(f"/sites/{site_id}/urls", form_payload, f"failed: {exc}")
        return _redirect(f"/sites/{quote(site_id)}?error={quote(str(exc))}")

    if hasattr(ctx, "audit"):
        ctx.audit(f"/sites/{site_id}/urls", form_payload, f"success: staged queue_path={queue_path}")
    return _redirect(f"/sites/{quote(site_id)}?message=url_queued")


def handle_site_hours_post(ctx: object, site_id: str, body: bytes):
    from urllib.parse import parse_qs, quote

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)
    form_payload = {key: values[0] if values else "" for key, values in form.items()}

    def _redirect(location: str) -> tuple:
        return 303, "text/html; charset=utf-8", f'<a href="{html.escape(location)}">Return</a>'.encode(), {"Location": location}

    action = first_query_value(form, "action").strip() or "set"
    if action == "clear" and first_query_value(form, "confirm") != "1":
        if hasattr(ctx, "audit"):
            ctx.audit(f"/sites/{site_id}/facility-hours", form_payload, "failed: confirm_required")
        return _redirect(f"/sites/{quote(site_id)}?error=confirm_required")

    try:
        payload = _site_hours_job_payload(form, site_id)
        queue_path = _write_site_hours_job(root, payload)
    except (json.JSONDecodeError, FacilityHoursError, ValueError, OSError) as exc:
        if hasattr(ctx, "audit"):
            ctx.audit(f"/sites/{site_id}/facility-hours", form_payload, f"failed: {exc}")
        return _redirect(f"/sites/{quote(site_id)}?error={quote(str(exc))}")

    if hasattr(ctx, "audit"):
        ctx.audit(f"/sites/{site_id}/facility-hours", form_payload, f"success: staged queue_path={queue_path}")
    return _redirect(f"/sites/{quote(site_id)}?message=facility_hours_queued")


def render(ctx: object, site_id: str) -> str:
    """Render the per-site detail page wrapped by html_page(active_section='site_detail')."""
    try:
        edit_section = first_query_value(getattr(ctx, "query", {}), "edit")
        doc = _load_location(site_id)
        if not isinstance(doc, dict) or doc.get("type") != "location":
            if _demo_mode() and site_id == "SANDBOX":
                return _not_found(site_id)
            doc = _builtin_location_doc(site_id)
        if not isinstance(doc, dict) or doc.get("type") != "location":
            return _not_found(site_id)

        primary_name = sites.canonical_name(doc) or str(doc.get("location") or site_id)
        escaped_id = html.escape(site_id, quote=True)
        related_data = _related_data(site_id)
        capture_records, capture_fallback, capture_count = _site_capture_records(ctx, site_id)
        capture_processing_counts = _site_capture_processing_counts(site_id)
        raw_content = str(doc.get("content") or "")
        access_flags = len(_ACCESS_CONSTRAINT_RE.findall(raw_content))
        sections = [
            (
                '<header class="site-detail-header">'
                '<div>'
                f"<h1>{html.escape(primary_name)}</h1>"
                f"{_site_subline(doc, site_id)}"
                "</div>"
                '<p class="actions site-header-actions">'
                f'<a class="button" href="/sites?site_id={escaped_id}">Admin metadata</a>'
                f'<a class="button" href="/field-photos?site_id={escaped_id}">Field Photos</a>'
                "</p></header>"
            )
        ]
        sections.append(
            _metric_cards(
                open_opportunities=len(related_data["opportunity_rows"]),
                access_flags=access_flags,
                field_captures=capture_count,
                last_visit=_last_visit_label(related_data["visit_rows"]),
            )
        )
        sections.append(_capture_processing_breakdown(capture_processing_counts))
        sections.append(_quick_facts_section(doc, site_id, edit_section))
        sections.append(_reference_links_section(doc, site_id))
        sections.append(_facility_hours_section(doc, site_id))
        if edit_section == "about":
            escaped_id = html.escape(site_id, quote=True)
            rev = html.escape(str(doc.get("_rev", "")), quote=True)
            sections.append(
                '<section><h2>About &amp; operational notes</h2>'
                f'<form method="post" action="/sites/{escaped_id}/save-section">'
                f'<input type="hidden" name="_rev" value="{rev}">'
                f'<input type="hidden" name="_entity_id" value="{escaped_id}">'
                '<input type="hidden" name="_section" value="about">'
                f'<textarea name="content">{html.escape(raw_content)}</textarea>'
                '<button type="submit">Save</button>'
                f'<a class="button" href="/sites/{escaped_id}">Cancel</a>'
                '</form></section>'
            )
        elif raw_content.strip():
            stripped = _strip_dataview_blocks(raw_content)
            if stripped.strip():
                sections.append(
                    _details_block(
                        "About & operational notes",
                        None,
                        _section("About & operational notes", render_markdown(stripped)),
                    )
                )
            else:
                sections.append(
                    _details_block(
                        "About & operational notes",
                        None,
                        _EMPTY_ABOUT_SECTION,
                    )
                )
        else:
            sections.append(
                _details_block(
                    "About & operational notes",
                    None,
                    _EMPTY_ABOUT_SECTION,
                )
            )
        sections.extend(
            _details_block(title, count, body)
            for title, count, body in _related_sections(related_data)
        )
        sections.append(_captures_section(ctx, site_id, capture_records, capture_fallback, capture_count))
        body = "".join(sections)
        return html_page(f"Site {site_id} — BTQ", body, active_section="site_detail")
    except Exception as exc:  # noqa: BLE001
        return _degraded(site_id, exc)
