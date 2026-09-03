from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote
from urllib import parse as urlparse, request as urlrequest

from event_pipeline import couchdb_config
from ops_dashboard.common import HtmlFragment, field_rows, first_query_value, humanize_key, render_count_badge, render_relative_time, render_short_id
from queue_spec import ENTITY_STATUSES
from ops_dashboard.layout import html_page
import ops_dashboard.sections.entity_edit as entity_edit
import ops_dashboard.sections.site_detail as site_detail
import ops_dashboard.sections.sites as sites
from btq_vault.projector import render_markdown

_cdb = site_detail._cdb


def _load_vault_doc(doc_id: str) -> dict[str, Any] | None:
    base, headers, database, timeout = _cdb()
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/{urlparse.quote(doc_id, safe='')}"
    req = urlrequest.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


_EMPLOYEE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Identity", ("first", "last", "preferred_name", "person_id", "status", "phone", "email")),
    ("Assignment", ("job", "additional_jobs", "sites", "role")),
)

_STATUS_CHOICES: tuple[str, ...] = tuple(sorted(ENTITY_STATUSES))
_EDITABLE_SECTIONS = frozenset({"identity", "assignment"})

_EMPLOYEE_SUPPRESSED = frozenset({
    "_id", "_rev", "type", "operator", "vault_path", "content", "name", "site_ids",
})
_FIELD_CAPTURE_LIMIT = 5000
_PERSONNEL_EVENT_LIMIT = 1000
_RECENT_CAPTURE_LIMIT = 5
_RECENT_EVENT_LIMIT = 8


def _clean(value: object) -> str:
    return str(value or "").strip()


def _string_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    return [text] if text else []


def _bare_site_id(value: object) -> str:
    text = _clean(value).strip('"')
    for prefix in ("location_", "site_"):
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _primary_site_id(doc: dict[str, Any]) -> str:
    values = [_bare_site_id(value) for value in _string_values(doc.get("job"))]
    return values[0] if values else ""


def _assigned_site_ids(doc: dict[str, Any]) -> list[str]:
    ids = [_bare_site_id(value) for value in _string_values(doc.get("site_ids"))]
    if not ids:
        ids.extend(_bare_site_id(value) for value in _string_values(doc.get("job")))
        ids.extend(_bare_site_id(value) for value in _string_values(doc.get("additional_jobs")))
        if not ids:
            ids.extend(_bare_site_id(value) for value in _string_values(doc.get("sites")))
    return _dedupe([site_id for site_id in ids if site_id])


def _load_location_name(site_id: str) -> str:
    bare = _bare_site_id(site_id)
    if not bare:
        return ""
    try:
        doc = site_detail._load_location(bare)  # noqa: SLF001 - employee detail should resolve names the site page already trusts.
        if not isinstance(doc, dict) and bare == "SANDBOX":
            doc = site_detail._builtin_location_doc(bare)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - unresolved names fall back to the id instead of breaking the page.
        doc = None
    if isinstance(doc, dict):
        return sites.canonical_name(doc) or _clean(doc.get("location")) or _clean(doc.get("account")) or bare
    return bare


def _site_name_map(site_ids: list[str]) -> dict[str, str]:
    return {site_id: _load_location_name(site_id) for site_id in site_ids}


def _site_link(site_id: str, site_names: dict[str, str]) -> str:
    bare = _bare_site_id(site_id)
    label = site_names.get(bare) or _load_location_name(bare) or bare
    return f'<a href="/sites/{html.escape(bare, quote=True)}">{html.escape(label)}</a>'


def _initials(name: str, fallback: str) -> str:
    words = [part for part in re.split(r"\s+", name.strip()) if part]
    if len(words) >= 2:
        return (words[0][:1] + words[-1][:1]).upper()
    source = words[0] if words else fallback
    return (source[:2] or "?").upper()


def _person_name(doc: dict[str, Any], employee_id: str) -> str:
    name = _clean(doc.get("name"))
    if name:
        return name
    first = _clean(doc.get("preferred_name")) or _clean(doc.get("first"))
    last = _clean(doc.get("last"))
    combined = " ".join(part for part in (first, last) if part)
    return combined or _clean(doc.get("person_id")) or employee_id


def _role(doc: dict[str, Any]) -> str:
    return _clean(doc.get("role") or doc.get("position") or doc.get("employment_type"))


def _ehub_id(doc: dict[str, Any]) -> str:
    return _clean(doc.get("employee_id") or doc.get("ehub_id") or doc.get("ehub"))


def _first_seen(doc: dict[str, Any]) -> str:
    return _clean(doc.get("hire_date") or doc.get("created_at") or doc.get("status_date"))


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    raw = value[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _first_seen_label(value: str) -> str:
    seen = _parse_date(value)
    if seen is None:
        return html.escape(value)
    days = max(0, (date.today() - seen).days)
    if days < 31:
        tenure = f"{days} day{'s' if days != 1 else ''}"
    elif days < 365:
        months = max(1, days // 30)
        tenure = f"{months} month{'s' if months != 1 else ''}"
    else:
        years = max(1, days // 365)
        tenure = f"{years} year{'s' if years != 1 else ''}"
    return f"{html.escape(seen.isoformat())} &middot; {html.escape(tenure)}"


def _normalize_person_name(value: object) -> str:
    text = _clean(value).lower()
    if "," in text:
        last, first = [part.strip() for part in text.split(",", 1)]
        text = f"{first} {last}".strip()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _mango_find(database: str, selector: dict[str, object], *, limit: int) -> list[dict[str, Any]]:
    base, headers, _vault_database, timeout = _cdb()
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/_find"
    payload = json.dumps({"selector": selector, "limit": limit}).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=max(timeout, 30.0)) as response:
        docs = json.loads(response.read().decode("utf-8")).get("docs", [])
    return [doc for doc in docs if isinstance(doc, dict)]


def _personnel_events(doc: dict[str, Any]) -> list[dict[str, Any]]:
    person_id = _clean(doc.get("person_id"))
    employee_id = _ehub_id(doc)
    normalized_name = _normalize_person_name(_person_name(doc, person_id or employee_id))
    try:
        docs = _mango_find(
            couchdb_config.vault_database(),
            {"type": "personnel_event"},
            limit=_PERSONNEL_EVENT_LIMIT,
        )
    except Exception:  # noqa: BLE001 - activity should degrade, not break the person record.
        return []

    matches: list[dict[str, Any]] = []
    for event in docs:
        if person_id and _clean(event.get("person_id")) == person_id:
            matches.append(event)
            continue
        if employee_id and _clean(event.get("employee_id")) == employee_id:
            matches.append(event)
            continue
        event_person = _normalize_person_name(event.get("employee") or event.get("person"))
        if normalized_name and event_person and event_person == normalized_name:
            matches.append(event)
    matches.sort(key=_event_sort_key, reverse=True)
    return matches


def _field_captures(person_id: str) -> list[dict[str, Any]]:
    if not person_id:
        return []
    try:
        return sorted(
            _mango_find(
                couchdb_config.field_captures_database(),
                {"type": "field_capture", "person_id": person_id},
                limit=_FIELD_CAPTURE_LIMIT,
            ),
            key=_capture_sort_key,
            reverse=True,
        )
    except Exception:  # noqa: BLE001 - capture activity is best-effort on this page.
        return []


def _event_sort_key(event: dict[str, Any]) -> str:
    return _clean(event.get("occurred_at") or event.get("created_at") or event.get("date"))


def _capture_sort_key(capture: dict[str, Any]) -> str:
    return _clean(capture.get("captured_at") or capture.get("exported_at") or capture.get("created_at"))


def _is_recognition(event: dict[str, Any]) -> bool:
    event_type = _clean(event.get("event_type")).lower().replace("_", "-")
    severity = _clean(event.get("severity")).lower().replace("_", "-")
    return event_type == "recognition" or severity == "recognition"


def _is_open_flag(event: dict[str, Any]) -> bool:
    if _is_recognition(event):
        return False
    status = _clean(event.get("status")).lower().replace("_", "-")
    if status in {"resolved", "closed", "dismissed", "no-action-needed"}:
        return False
    event_type = _clean(event.get("event_type")).lower().replace("_", "-")
    severity = _clean(event.get("severity")).lower().replace("_", "-")
    return (
        "warning" in event_type
        or "warning" in severity
        or event_type in {"disciplinary", "retention-risk"}
        or severity in {"concern", "separation"}
    )


def _data_quality_flags(doc: dict[str, Any], assigned_site_ids: list[str]) -> list[str]:
    flags: list[str] = []
    if not _ehub_id(doc):
        flags.append("eHub ID missing")
    if not assigned_site_ids:
        flags.append("No assigned site recorded")
    if not _clean(doc.get("phone")) and not _clean(doc.get("email")):
        flags.append("Phone/email missing")
    return flags


def _metric_cards(*, assigned_sites: int, captures: int, recognitions: int, open_flags: int) -> str:
    metrics = (
        ("Assigned sites", str(assigned_sites)),
        ("Captures", str(captures)),
        ("Recognitions", str(recognitions)),
        ("Open flags", str(open_flags)),
    )
    cards = "".join(
        (
            '<div class="metric-card">'
            f"<strong>{html.escape(value)}</strong>"
            f"<span>{html.escape(label)}</span>"
            "</div>"
        )
        for label, value in metrics
    )
    return f'<section class="metric-grid" aria-label="Employee metrics">{cards}</section>'


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


def _quick_fact_value(value: object) -> str:
    if isinstance(value, HtmlFragment):
        return str(value)
    return html.escape(str(value))


def _facts_head(title: str, slug: str, *, editing: bool) -> str:
    glyph = ""
    if not editing:
        escaped_slug = html.escape(slug, quote=True)
        glyph = (
            f'<a class="icon-btn" href="?edit={escaped_slug}" '
            f'title="Edit {escaped_slug}" aria-label="Edit {escaped_slug}">&#9998;</a>'
        )
    return f'<div class="facts-head"><h3>{html.escape(title)}</h3>{glyph}</div>'


def _facts_edit_form(title: str, slug: str, doc: dict[str, Any], keys: tuple[str, ...], employee_id: str) -> str:
    eid = html.escape(employee_id, quote=True)
    rev = html.escape(str(doc.get("_rev", "")), quote=True)
    controls = entity_edit.render_field_controls(
        doc,
        keys,
        select_fields={"status": _STATUS_CHOICES},
    )
    return (
        "<section>"
        f"{_facts_head(title, slug, editing=True)}"
        f'<form method="post" action="/employees/{eid}/save-section" class="admin-form entity-edit-form">'
        f'<input type="hidden" name="_rev" value="{rev}">'
        f'<input type="hidden" name="_entity_id" value="{eid}">'
        f'<input type="hidden" name="_section" value="{html.escape(slug, quote=True)}">'
        f"{controls}"
        '<button type="submit">Save</button>'
        '<a class="button" href="?">Cancel</a>'
        "</form></section>"
    )


def _quick_facts(
    doc: dict[str, Any],
    person_id: str,
    primary_site_id: str,
    assigned_site_ids: list[str],
    site_names: dict[str, str],
    *,
    edit_section: str = "",
    employee_id: str = "",
    edit_values: dict[str, str] | None = None,
) -> str:
    phone = _clean(doc.get("phone"))
    email = _clean(doc.get("email"))
    contact_parts = []
    if phone:
        contact_parts.append(html.escape(phone))
    if email:
        contact_parts.append(f'<a href="mailto:{html.escape(email, quote=True)}">{html.escape(email)}</a>')
    first_seen = _first_seen(doc)
    identity = {
        "role": _role(doc),
        "status": _clean(doc.get("status")),
        "eHub ID": _ehub_id(doc),
        "Phone / Email": HtmlFragment("<br>".join(contact_parts)) if contact_parts else "",
        "First seen / tenure": HtmlFragment(_first_seen_label(first_seen)) if first_seen else "",
        "Person ID": HtmlFragment(render_short_id(person_id)) if person_id else "",
    }
    assignment = {
        "primary_site": HtmlFragment(_site_link(primary_site_id, site_names)) if primary_site_id else "",
        "assigned_sites": str(len(assigned_site_ids)) if assigned_site_ids else "",
    }
    display_groups: dict[str, tuple[dict[str, object], tuple[str, ...]]] = {
        "identity": (identity, ("role", "status", "eHub ID", "Phone / Email", "First seen / tenure", "Person ID")),
        "assignment": (assignment, ("primary_site", "assigned_sites")),
    }
    groups: list[str] = []
    for title, keys in _EMPLOYEE_GROUPS:
        slug = title.lower()
        if edit_section == slug:
            edit_doc = dict(doc)
            if edit_values is not None:
                edit_doc.update(edit_values)
            groups.append(_facts_edit_form(title, slug, edit_doc, keys, employee_id))
            continue
        mapping, order = display_groups[slug]
        rows = field_rows(mapping, order, value_formatter=_quick_fact_value)
        body = f'<dl class="fields summary-fields">{rows}</dl>' if rows else '<p class="zero-state">&mdash;</p>'
        groups.append(f"<section>{_facts_head(title, slug, editing=False)}{body}</section>")
    sections = (
        "".join(groups)
        .replace("<dt>Ehub Id</dt>", "<dt>eHub ID</dt>")
        .replace("<dt>Person Id</dt>", "<dt>Person ID</dt>")
        .replace("<dt>Primary Site</dt>", "<dt>Primary site</dt>")
        .replace("<dt>Assigned Sites</dt>", "<dt>Assigned sites</dt>")
    )
    return f'<section class="quick-facts"><h2>Quick facts</h2>{sections}</section>'


def _event_pill(event: dict[str, Any]) -> str:
    label = _clean(event.get("event_type") or event.get("severity") or "Personnel event")
    css = re.sub(r"[^a-z0-9_-]+", "_", label.lower()).strip("_") or "personnel_event"
    css_class = "success" if _is_recognition(event) else f"status-{css}"
    return f'<span class="pill {html.escape(css_class, quote=True)}">{html.escape(humanize_key(label))}</span>'


def _event_item(event: dict[str, Any], site_names: dict[str, str]) -> str:
    summary = _clean(event.get("summary") or event.get("details") or event.get("notes") or event.get("event_type") or "Personnel event")
    when = render_relative_time(_event_sort_key(event))
    related_site = _bare_site_id(event.get("related_site") or event.get("site") or event.get("site_id"))
    site = site_names.get(related_site) or (_load_location_name(related_site) if related_site else "")
    reported_by = _clean(event.get("reported_by"))
    status = _clean(event.get("status"))
    meta = " &middot; ".join(part for part in (when, html.escape(site), html.escape(status), html.escape(reported_by)) if part)
    meta_html = f'<p class="subline">{meta}</p>' if meta else ""
    return f"<li>{_event_pill(event)} <strong>{html.escape(summary)}</strong>{meta_html}</li>"


def _capture_item(capture: dict[str, Any], site_names: dict[str, str]) -> str:
    capture_id = _clean(capture.get("capture_id") or capture.get("_id"))
    if not capture_id:
        return ""
    title = _clean(capture.get("qc_category") or capture.get("phase") or "Field capture")
    site_id = _bare_site_id(capture.get("site_id") or capture.get("target_id"))
    site = site_names.get(site_id) or _clean(capture.get("site")) or (_load_location_name(site_id) if site_id else "")
    when = render_relative_time(_capture_sort_key(capture))
    meta = " &middot; ".join(part for part in (html.escape(site), when) if part)
    meta_html = f'<p class="subline">{meta}</p>' if meta else ""
    href = f"/captures?capture_id={quote(capture_id)}"
    return (
        "<li>"
        '<span class="pill">Capture</span> '
        f'<a href="{href}"><strong>{html.escape(title)}</strong> {render_short_id(capture_id)}</a>'
        f"{meta_html}"
        "</li>"
    )


def _activity_section(events: list[dict[str, Any]], captures: list[dict[str, Any]], data_flags: list[str], site_names: dict[str, str]) -> str:
    activity_items: list[tuple[str, str]] = []
    activity_items.extend(
        (_event_sort_key(event), _event_item(event, site_names))
        for event in events[:_RECENT_EVENT_LIMIT]
    )
    activity_items.extend(
        (_capture_sort_key(capture), _capture_item(capture, site_names))
        for capture in captures[:_RECENT_CAPTURE_LIMIT]
    )
    activity_items.sort(key=lambda item: item[0], reverse=True)
    flag_items = [
        f'<li><span class="pill warning">Data quality</span> <strong>{html.escape(flag)}</strong></li>'
        for flag in data_flags
    ]
    items = "".join(item for _sort_key, item in activity_items if item) + "".join(flag_items)
    body = f"<ul>{items}</ul>" if items else '<p class="zero-state">No recent activity or flags yet.</p>'
    return f"<section><h2>Recent activity &amp; flags</h2>{body}</section>"


def _assigned_sites_section(site_ids: list[str], site_names: dict[str, str]) -> str:
    if not site_ids:
        return '<section><h2>Assigned sites</h2><p class="zero-state">&mdash;</p></section>'
    chips = "".join(
        f'<span class="pill">{_site_link(site_id, site_names)}</span>'
        for site_id in site_ids
    )
    return f"<section><h2>Assigned sites</h2><p>{chips}</p></section>"


_UNIFORM_STATUS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("unknown", "Awaiting response"),
    ("adequate", "Has enough shirts"),
    ("needs_shirts", "Needs shirts"),
)


def _uniform_status_label(value: object) -> str:
    status = _clean(value) or "unknown"
    return dict(_UNIFORM_STATUS_OPTIONS).get(status, humanize_key(status))


def _uniform_section(doc: dict[str, Any], employee_id: str, *, edit_active: bool, notice: str = "") -> str:
    eid = html.escape(employee_id, quote=True)
    status = _clean(doc.get("uniform_status")) or "unknown"
    shirt_count = doc.get("uniform_shirt_count")
    if isinstance(shirt_count, bool) or not isinstance(shirt_count, int):
        shirt_count = None
    shirt_size = _clean(doc.get("uniform_shirt_size"))
    updated_at = _clean(doc.get("uniform_updated_at"))
    notice_html = ""
    if notice == "staged":
        notice_html = '<p class="muted">Uniform status staged &mdash; the queue applies it within a few seconds. Reload to see it.</p>'
    elif notice == "invalid":
        notice_html = '<p class="muted">Uniform status was rejected. Shirt count is required for a completed response, and size is required when shirts are needed.</p>'
    elif notice == "queue_unavailable":
        notice_html = '<p class="muted">Could not stage the uniform change &mdash; the queue directory is unavailable.</p>'

    if not edit_active:
        head = _facts_head("Uniform", "uniform", editing=False)
        pill_class = "warning" if status in {"unknown", "needs_shirts"} else "success"
        rows = [
            f'<dt>Status</dt><dd><span class="pill {pill_class}">{html.escape(_uniform_status_label(status))}</span></dd>',
            f'<dt>B&amp;T T-shirts</dt><dd>{shirt_count if shirt_count is not None else "&mdash;"}</dd>',
            f'<dt>Required size</dt><dd>{html.escape(shirt_size) if shirt_size else "&mdash;"}</dd>',
        ]
        if updated_at:
            rows.append(f'<dt>Last updated</dt><dd>{render_relative_time(updated_at)}</dd>')
        body = f'<dl class="fields summary-fields">{"".join(rows)}</dl>'
        return f"<section>{head}{notice_html}{body}</section>"

    head = _facts_head("Uniform", "uniform", editing=True)
    options = "".join(
        f'<option value="{value}" {"selected" if status == value else ""}>{html.escape(label)}</option>'
        for value, label in _UNIFORM_STATUS_OPTIONS
    )
    count_value = str(shirt_count) if shirt_count is not None else ""
    form = (
        f'<form method="post" action="/employees/{eid}/uniform" class="admin-form entity-edit-form">'
        '<label class="field-row"><span class="field-row-label">Uniform status</span>'
        f'<select name="status">{options}</select></label>'
        '<label class="field-row"><span class="field-row-label">Current B&amp;T T-shirts</span>'
        f'<input type="number" name="shirt_count" min="0" max="99" value="{count_value}"></label>'
        '<label class="field-row"><span class="field-row-label">Required shirt size</span>'
        f'<input type="text" name="shirt_size" maxlength="32" placeholder="e.g. L or 2XL" value="{html.escape(shirt_size, quote=True)}"></label>'
        '<p class="subline">Count is required after an employee responds. Size is required when shirts are needed.</p>'
        '<button type="submit">Save uniform status</button>'
        '<a class="button" href="?">Cancel</a>'
        '</form>'
        '<p class="subline">Saves through the validated queue job, not a direct write.</p>'
    )
    return f"<section>{head}{notice_html}{form}</section>"


_HOME_ADDRESS_FORM_FIELDS: tuple[tuple[str, str], ...] = (
    ("line1", "Street"),
    ("line2", "Street 2"),
    ("city", "City"),
    ("state", "State"),
    ("postal_code", "Postal code"),
    ("country", "Country"),
)


def _format_home_address(address: dict[str, Any]) -> str:
    line1 = html.escape(_clean(address.get("line1")))
    line2 = html.escape(_clean(address.get("line2")))
    city = html.escape(_clean(address.get("city")))
    state = html.escape(_clean(address.get("state")))
    postal = html.escape(_clean(address.get("postal_code")))
    country = _clean(address.get("country"))
    locality = " ".join(part for part in (f"{city}," if city else "", state, postal) if part)
    lines = [line1, line2, locality]
    if country and country.upper() not in {"US", "USA"}:
        lines.append(html.escape(country))
    return "<br>".join(line for line in lines if line)


def _home_address_section(doc: dict[str, Any], employee_id: str, *, edit_active: bool, notice: str = "") -> str:
    eid = html.escape(employee_id, quote=True)
    sensitive = '<span class="pill warning">Sensitive</span>'
    notice_html = ""
    if notice == "staged":
        notice_html = '<p class="muted">Change staged &mdash; the queue applies it within a few seconds. Reload to see it.</p>'
    elif notice == "invalid":
        notice_html = '<p class="muted">That address was rejected &mdash; street, city, state, and a valid postal code are required.</p>'
    elif notice == "queue_unavailable":
        notice_html = '<p class="muted">Could not stage the change &mdash; the queue directory is unavailable. Check the runtime configuration.</p>'

    address = doc.get("home_address") if isinstance(doc.get("home_address"), dict) else {}
    if not edit_active:
        head = _facts_head("Home address", "home_address", editing=False)
        head = head.replace("</h3>", f"</h3>{sensitive}", 1)
        body = f"<p>{_format_home_address(address)}</p>" if address else '<p class="zero-state">No home address recorded.</p>'
        footer = '<p class="subline">Operator-only. Never shown on worker surfaces, projections, or reports.</p>'
        return f"<section>{head}{notice_html}{body}{footer}</section>"

    head = _facts_head("Home address", "home_address", editing=True)
    head = head.replace("</h3>", f"</h3>{sensitive}", 1)
    controls = "".join(
        (
            '<label class="field-row">'
            f'<span class="field-row-label">{html.escape(label)}</span>'
            f'<input type="text" name="{html.escape(field, quote=True)}" '
            f'value="{html.escape(_clean(address.get(field)), quote=True)}">'
            "</label>"
        )
        for field, label in _HOME_ADDRESS_FORM_FIELDS
    )
    set_form = (
        f'<form method="post" action="/employees/{eid}/home-address" class="admin-form entity-edit-form">'
        '<input type="hidden" name="_action" value="set">'
        f"{controls}"
        '<button type="submit">Save</button>'
        '<a class="button" href="?">Cancel</a>'
        "</form>"
    )
    clear_form = ""
    if address:
        clear_form = (
            f'<form method="post" action="/employees/{eid}/home-address">'
            '<input type="hidden" name="_action" value="clear">'
            '<button type="submit">Clear address</button>'
            "</form>"
        )
    note = '<p class="subline">Saves through the validated queue job, not a direct write.</p>'
    return f"<section>{head}{notice_html}{set_form}{clear_form}{note}</section>"


def _availability_section(employee_id: str, doc: dict[str, Any] | None = None) -> str:
    if doc is None:
        doc = _load_vault_doc(f"employee_{employee_id}")
    person_id = _clean((doc or {}).get("person_id") or employee_id)
    rows: list[dict[str, Any]] = []
    if person_id:
        try:
            base, headers, database, timeout = _cdb()
            rows = site_detail.query_view(
                base,
                headers,
                database,
                site_detail.DDOC,
                "availability_constraints_by_person",
                startkey=person_id,
                endkey=person_id,
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 - availability is additive and should never break the person page.
            rows = []

    today = date.today()
    unavailable_dates: set[date] = set()
    last_working_days: list[date] = []
    for row in rows:
        value = row.get("value")
        if not isinstance(value, dict):
            continue
        constraint_date = _parse_date(_clean(value.get("date")))
        if constraint_date is None or constraint_date < today:
            continue
        constraint_type = _clean(value.get("constraint_type"))
        if constraint_type == "unavailable_date":
            unavailable_dates.add(constraint_date)
        elif constraint_type == "last_working_day":
            last_working_days.append(constraint_date)

    parts: list[str] = []
    if unavailable_dates:
        dates = ", ".join(day.isoformat() for day in sorted(unavailable_dates))
        parts.append(f"<p><strong>Unavailable:</strong> {html.escape(dates)}</p>")
    if last_working_days:
        last_day = min(last_working_days).isoformat()
        parts.append(f"<p><strong>Last day:</strong> {html.escape(last_day)}</p>")
    body = "".join(parts) if parts else '<p class="zero-state">No upcoming unavailability recorded.</p>'
    return f"<section><h2>Availability constraints</h2>{body}</section>"


def _demote_about_headings(rendered_html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        closing, level, attrs = match.groups()
        demoted_level = min(int(level) + 3, 6)
        if closing:
            return f"</h{demoted_level}>"
        return f"<h{demoted_level}{attrs}>"

    return re.sub(r"<(/?)h([1-6])(\b[^>]*)>", replace, rendered_html, flags=re.IGNORECASE)


def _not_found(employee_id: str) -> str:
    body = (
        f'<header><h1>Employee not found</h1>'
        f'<p class="muted">No employee with id {html.escape(employee_id)}.</p>'
        '<p><a href="/">Return to Inbox</a></p></header>'
    )
    return html_page("Not Found — BTQ", body, active_section="employee_detail")


def _parse_token_ids(raw: str) -> list[str]:
    return _dedupe([part.strip() for part in raw.split(",") if part.strip()])


def _render_inactivation_confirm(
    employee_id: str,
    existing: dict[str, Any],
    form_values: dict[str, str],
    active: list[Any],
    *,
    inventory_changed: bool = False,
) -> str:
    import ops_dashboard.sections.tokens as tokens

    eid = html.escape(employee_id, quote=True)
    name = _person_name(existing, employee_id)
    count = len(active)
    noun = "token" if count == 1 else "tokens"
    notice = (
        '<section class="warning"><p>The token inventory changed. Review the current active tokens before continuing.</p></section>'
        if inventory_changed
        else ""
    )
    rows = "".join(
        "<tr>"
        f'<td><code title="{html.escape(str(record.token_id), quote=True)}">{html.escape(tokens.short_token_id(str(record.token_id)))}</code></td>'
        f"<td>{html.escape(str(record.label or ''))}</td>"
        f"<td>{html.escape(tokens.role_label(str(record.role or '')))}</td>"
        f"<td>{html.escape(', '.join(record.site_ids) if record.site_ids else 'All sites')}</td>"
        f"<td>{html.escape(str(record.expires_at or 'Never'))}</td>"
        "</tr>"
        for record in active
    )
    hidden_names = (*_EMPLOYEE_GROUPS[0][1], "_rev", "_entity_id", "_section")
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(key, quote=True)}" value="{html.escape(str(form_values.get(key, "")), quote=True)}">'
        for key in hidden_names
        if key in form_values
    )
    token_ids = html.escape(",".join(str(record.token_id) for record in active), quote=True)
    body = f"""
    <header><h1>Deactivate employee</h1><p class="muted">{html.escape(name)} &middot; ID {html.escape(employee_id)}</p></header>
    {notice}
    <section>
      <h2>Active field-capture tokens</h2>
      <p>This employee has {count} active {noun}. Deactivate those tokens too?</p>
      <table class="data-table"><thead><tr><th>Token ID</th><th>Label</th><th>Role</th><th>Site scope</th><th>Expires</th></tr></thead><tbody>{rows}</tbody></table>
      <form method="post" action="/employees/{eid}/save-section" class="admin-form">
        {hidden}
        <input type="hidden" name="_token_ids" value="{token_ids}">
        <button type="submit" name="_token_decision" value="revoke_tokens" class="reject">Deactivate employee and tokens</button>
        <button type="submit" name="_token_decision" value="keep_tokens">Deactivate employee only</button>
        <button type="submit" name="_token_decision" value="cancel">Cancel</button>
      </form>
    </section>
    """
    return html_page("Deactivate Employee", body, active_section="employees")


def render(ctx: object, employee_id: str, edit_values: dict[str, str] | None = None) -> str:
    try:
        import ops_dashboard.sections.tokens as tokens

        edit_section = first_query_value(getattr(ctx, "query", {}), "edit")
        if edit_values is not None:
            edit_section = "identity"
        doc = _load_vault_doc(f"employee_{employee_id}")
        if not isinstance(doc, dict) or doc.get("type") != "employee":
            return _not_found(employee_id)

        display_name = _person_name(doc, employee_id)
        person_id_raw = _clean(doc.get("person_id") or employee_id)
        person_id = html.escape(person_id_raw)
        eid = html.escape(employee_id, quote=True)

        primary_site = _primary_site_id(doc)
        assigned_site_ids = _assigned_site_ids(doc)
        all_site_ids = _dedupe([site_id for site_id in [primary_site, *assigned_site_ids] if site_id])
        site_names = _site_name_map(all_site_ids)
        primary_site_name = site_names.get(primary_site, "") if primary_site else ""
        captures = _field_captures(person_id_raw)
        events = _personnel_events(doc)
        data_flags = _data_quality_flags(doc, assigned_site_ids)
        recognitions = sum(1 for event in events if _is_recognition(event))
        open_flags = sum(1 for event in events if _is_open_flag(event)) + len(data_flags)

        primary_btn = (
            f'<a class="button" href="/sites/{html.escape(primary_site, quote=True)}">Primary site</a>'
            if primary_site else ""
        )
        all_employees_btn = '<a class="button" href="/employees">All employees</a>'
        if primary_site_name:
            subline_primary = primary_site_name
        else:
            subline_primary = "&mdash;"
        subline_parts = [
            html.escape(_role(doc)) if _role(doc) else "&mdash;",
            html.escape(subline_primary) if subline_primary != "&mdash;" else subline_primary,
            html.escape(_clean(doc.get("status"))) if _clean(doc.get("status")) else "&mdash;",
            f"ID {person_id}",
        ]
        header = (
            '<header class="site-detail-header">'
            '<div style="display:flex;gap:14px;align-items:flex-start;min-width:0">'
            '<span aria-hidden="true" style="display:inline-grid;place-items:center;'
            'width:48px;height:48px;border-radius:999px;border:1px solid var(--line-strong);'
            'background:var(--pill-bg);font-weight:800;flex:0 0 auto">'
            f"{html.escape(_initials(display_name, person_id_raw))}</span>"
            '<div style="min-width:0">'
            f"<h1>{html.escape(display_name)}</h1>"
            f'<p class="subline">{" &middot; ".join(subline_parts)}</p>'
            "</div></div>"
            f'<p class="actions site-header-actions">{all_employees_btn}{primary_btn}</p>'
            "</header>"
        )

        sections = [header]
        query = getattr(ctx, "query", {}) or {}
        tokens_kept = first_query_value(query, "tokens_kept")
        tokens_deactivated = first_query_value(query, "tokens_deactivated")
        pending_token_ids = _parse_token_ids(first_query_value(query, "token_deactivation_pending"))
        if tokens_kept:
            noun = "token" if tokens_kept == "1" else "tokens"
            sections.append(
                f'<section class="success">Employee inactive. {html.escape(tokens_kept)} active {noun} left in place.</section>'
            )
        if tokens_deactivated and not pending_token_ids:
            noun = "token" if tokens_deactivated == "1" else "tokens"
            sections.append(
                f'<section class="success">Employee inactive. {html.escape(tokens_deactivated)} {noun} deactivated locally and at the gateway.</section>'
            )
        if pending_token_ids:
            noun = "token" if len(pending_token_ids) == 1 else "tokens"
            links = "".join(
                '<li><a href="/tokens?token_id='
                f'{quote(token_id, safe="")}"><code>{html.escape(tokens.short_token_id(token_id))}</code></a></li>'
                for token_id in pending_token_ids
            )
            hidden_ids = html.escape(",".join(pending_token_ids), quote=True)
            sections.append(
                '<section class="warning">'
                f'<p>Employee saved as inactive, but the gateway revoke is unresolved for {len(pending_token_ids)} {noun}.</p>'
                f'<ul>{links}</ul>'
                f'<form method="post" action="/employees/{eid}/retry-token-deactivation">'
                f'<input type="hidden" name="token_ids" value="{hidden_ids}">'
                '<button type="submit">Retry deactivation</button>'
                '</form></section>'
            )
        sections.append(
            _metric_cards(
                assigned_sites=len(assigned_site_ids),
                captures=len(captures),
                recognitions=recognitions,
                open_flags=open_flags,
            )
        )
        sections.append(
            _quick_facts(
                doc,
                person_id_raw,
                primary_site,
                assigned_site_ids,
                site_names,
                edit_section=edit_section if edit_section in _EDITABLE_SECTIONS else "",
                employee_id=employee_id,
                edit_values=edit_values,
            )
        )
        staged = first_query_value(getattr(ctx, "query", {}), "staged")
        error = first_query_value(getattr(ctx, "query", {}), "error")
        uniform_notice = ""
        if staged == "uniform":
            uniform_notice = "staged"
        elif error == "invalid_uniform":
            uniform_notice = "invalid"
        elif error == "queue_unavailable" and edit_section == "uniform":
            uniform_notice = "queue_unavailable"
        sections.append(
            _uniform_section(
                doc,
                employee_id,
                edit_active=(edit_section == "uniform"),
                notice=uniform_notice,
            )
        )
        home_address_notice = ""
        if staged == "home_address":
            home_address_notice = "staged"
        elif error == "invalid_address":
            home_address_notice = "invalid"
        elif error == "queue_unavailable":
            home_address_notice = "queue_unavailable"
        sections.append(
            _home_address_section(
                doc,
                employee_id,
                edit_active=(edit_section == "home_address"),
                notice=home_address_notice,
            )
        )
        sections.append(_activity_section(events, captures, data_flags, site_names))
        sections.append(_assigned_sites_section(assigned_site_ids, site_names))
        sections.append(_availability_section(employee_id, doc))

        raw_content = str(doc.get("content") or "")
        if edit_section == "about":
            eid = html.escape(employee_id, quote=True)
            rev = html.escape(str(doc.get("_rev", "")), quote=True)
            sections.append(
                '<section><h3>About</h3>'
                f'<form method="post" action="/employees/{eid}/save-section">'
                f'<input type="hidden" name="_rev" value="{rev}">'
                f'<input type="hidden" name="_entity_id" value="{eid}">'
                '<input type="hidden" name="_section" value="about">'
                f'<textarea name="content">{html.escape(raw_content)}</textarea>'
                '<button type="submit">Save</button>'
                f'<a class="button" href="/employees/{eid}">Cancel</a>'
                '</form></section>'
            )
        elif raw_content.strip():
            sections.append(
                _details_block(
                    "About",
                    None,
                    f'<section><h3>About</h3>{_demote_about_headings(render_markdown(raw_content))}</section>',
                )
            )


        body = "".join(sections)
        return html_page(f"Employee {employee_id} — BTQ", body, active_section="employee_detail")
    except Exception as exc:  # noqa: BLE001
        body = (
            f'<header><h1>Error loading employee {html.escape(employee_id)}</h1>'
            f'<p class="muted">{html.escape(str(exc))}</p></header>'
        )
        return html_page("Error — BTQ", body, active_section="employee_detail")


def handle_uniform_post(ctx: object, employee_id: str, body: bytes):
    """Stage a validated uniform-status update for one canonical employee."""
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import default_actor, first_query_value, write_set_employee_uniform_job

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    status = first_query_value(form, "status").strip()
    shirt_count_text = first_query_value(form, "shirt_count").strip()
    shirt_size = first_query_value(form, "shirt_size").strip()
    uniform: dict[str, object] = {"status": status}
    if shirt_count_text:
        try:
            uniform["shirt_count"] = int(shirt_count_text)
        except ValueError:
            return ctx.redirect(f"/employees/{quote(employee_id)}?edit=uniform&error=invalid_uniform")
    if shirt_size:
        uniform["shirt_size"] = shirt_size

    doc = _load_vault_doc(f"employee_{employee_id}")
    if not isinstance(doc, dict) or doc.get("type") != "employee":
        return ctx.redirect(f"/employees/{quote(employee_id)}?error=not_found")
    person = _clean(doc.get("person_id")) or employee_id

    try:
        queue_doc_id = write_set_employee_uniform_job(
            person=person,
            actor=default_actor(),
            uniform=uniform,
        )
    except ValueError:
        ctx.audit(
            f"/employees/{employee_id}/uniform",
            {"status": status},
            "failed: invalid uniform status",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?edit=uniform&error=invalid_uniform")
    except Exception as exc:  # noqa: BLE001 - CouchDB enqueue unavailable: redirect, not 500.
        ctx.audit(
            f"/employees/{employee_id}/uniform",
            {"status": status},
            f"failed: queue enqueue {exc.__class__.__name__}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?edit=uniform&error=queue_unavailable")

    ctx.audit(
        f"/employees/{employee_id}/uniform",
        {"status": status},
        f"success: staged {queue_doc_id}",
    )
    return ctx.redirect(f"/employees/{quote(employee_id)}?staged=uniform")


def handle_home_address_post(ctx: object, employee_id: str, body: bytes):
    """Stage a set_employee_home_address queue job from the operator surface.

    The address mutates only through the validated queue path — never a direct
    CouchDB write — and the audit entry never contains the address itself.
    """
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import default_actor, first_query_value, write_set_employee_home_address_job

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    action = first_query_value(form, "_action").strip() or "set"

    doc = _load_vault_doc(f"employee_{employee_id}")
    if not isinstance(doc, dict) or doc.get("type") != "employee":
        return ctx.redirect(f"/employees/{quote(employee_id)}?error=not_found")
    person = _clean(doc.get("person_id")) or employee_id

    if action == "clear":
        job_kwargs: dict[str, object] = {"action": "clear"}
    else:
        home_address = {
            field: first_query_value(form, field).strip()
            for field, _label in _HOME_ADDRESS_FORM_FIELDS
        }
        job_kwargs = {"home_address": {k: v for k, v in home_address.items() if v}}

    try:
        queue_doc_id = write_set_employee_home_address_job(
            person=person,
            actor=default_actor(),
            **job_kwargs,
        )
    except ValueError:
        ctx.audit(
            f"/employees/{employee_id}/home-address",
            {"action": action},
            "failed: invalid address",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?edit=home_address&error=invalid_address")
    except Exception as exc:  # noqa: BLE001 - CouchDB enqueue unavailable: redirect, not 500,
        # and keep the address out of the audit line.
        ctx.audit(
            f"/employees/{employee_id}/home-address",
            {"action": action},
            f"failed: queue enqueue {exc.__class__.__name__}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?edit=home_address&error=queue_unavailable")

    ctx.audit(
        f"/employees/{employee_id}/home-address",
        {"action": action},
        f"success: staged {queue_doc_id}",
    )
    return ctx.redirect(f"/employees/{quote(employee_id)}?staged=home_address")


def handle_save_section(ctx: object, employee_id: str, body: bytes):
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import first_query_value
    import ops_dashboard.sections.entity_edit as entity_edit
    import ops_dashboard.sections.tokens as tokens

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    form_flat = {k: v[0] for k, v in form.items()}
    section = first_query_value(form, "_section").strip()

    _ALLOWED_KEYS: dict[str, frozenset[str]] = {
        "identity": frozenset(("first", "last", "preferred_name", "person_id", "status", "phone", "email")),
        "assignment": frozenset(("job", "additional_jobs", "sites", "role")),
        # "contact" retained for backward-compatible saves; phone/email now live in identity.
        "contact": frozenset(("phone", "email")),
        "about": frozenset(("content",)),
    }
    allowed_keys = _ALLOWED_KEYS.get(section)
    if allowed_keys is None:
        return 400, "text/plain; charset=utf-8", b"Unknown section", {}

    existing = _load_vault_doc(f"employee_{employee_id}")
    if not existing or existing.get("type") != "employee":
        return ctx.redirect(f"/employees/{quote(employee_id)}?error=not_found")

    token_decision = _clean(form_flat.get("_token_decision"))
    status_normalized = _clean(form_flat.get("status")).lower()
    existing_status_normalized = _clean(existing.get("status")).lower()
    if section == "identity":
        status_value = _clean(form_flat.get("status"))
        # The stored value stays valid even when it is outside the standard
        # vocabulary, so an unrelated identity edit never gets rejected.
        allowed_statuses = set(ENTITY_STATUSES) | {_clean(existing.get("status"))}
        if status_value and status_value not in allowed_statuses:
            return 400, "text/plain; charset=utf-8", b"Invalid status", {}

    inactivation = (
        section == "identity"
        and status_normalized == "inactive"
        and existing_status_normalized != "inactive"
    )
    if token_decision:
        if token_decision not in {"cancel", "keep_tokens", "revoke_tokens"} or not inactivation:
            return 400, "text/plain; charset=utf-8", b"Invalid token decision", {}
        if token_decision == "cancel":
            edit_values = {
                key: str(form_flat.get(key, ""))
                for key in _EMPLOYEE_GROUPS[0][1]
                if key in form_flat
            }
            return 200, "text/html; charset=utf-8", render(ctx, employee_id, edit_values).encode("utf-8"), {}

    active: list[Any] = []
    if inactivation:
        store = tokens.token_store(ctx.runtime_root)
        active = tokens.active_tokens_for_identity(store, tokens.employee_identity_keys(existing))
        if not token_decision and active:
            return (
                200,
                "text/html; charset=utf-8",
                _render_inactivation_confirm(employee_id, existing, form_flat, active).encode("utf-8"),
                {},
            )
        if token_decision == "revoke_tokens":
            submitted_token_ids = _parse_token_ids(_clean(form_flat.get("_token_ids")))
            if {str(record.token_id) for record in active} != set(submitted_token_ids):
                ctx.audit(
                    f"/employees/{employee_id}/save-section",
                    {
                        "section": "identity",
                        "decision": token_decision,
                        "token_ids": submitted_token_ids,
                    },
                    "failed: token inventory changed",
                )
                return (
                    200,
                    "text/html; charset=utf-8",
                    _render_inactivation_confirm(
                        employee_id,
                        existing,
                        form_flat,
                        active,
                        inventory_changed=True,
                    ).encode("utf-8"),
                    {},
                )

    if section == "assignment":
        for key in ("additional_jobs", "sites"):
            value = form_flat.get(key)
            if isinstance(value, str) and "," in value:
                parts = [part.strip() for part in value.split(",") if part.strip()]
                form_flat[key] = parts if parts else ""

    updated = entity_edit.apply_section_update(existing, form_flat, allowed_keys)

    # ALWAYS recompute derived fields after any employee section save.
    # A stale name/site_ids breaks the home directory and employees_by_site view.
    entity_edit.recompute_employee_derived(updated)

    # Assert validate_doc_update contract (btq_vault requires type + non-empty operator)
    assert updated.get("type") == "employee"
    assert updated.get("operator")

    updated["_rev"] = existing["_rev"]

    doc_path = f"{couchdb_config.vault_database()}/employee_{employee_id}"
    audit_payload: dict[str, object] = {"section": section}
    if token_decision:
        audit_payload["decision"] = token_decision
        audit_payload["token_ids"] = [str(record.token_id) for record in active]
    try:
        sites.request_json("PUT", doc_path, updated)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == 409:
            ctx.audit(
                f"/employees/{employee_id}/save-section",
                audit_payload,
                "failed: conflict",
            )
            return (
                200, "text/html; charset=utf-8",
                render(ctx, employee_id).encode("utf-8"),
                {},
            )
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            audit_payload,
            f"failed: {exc}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?error={quote(str(exc))}")

    if token_decision == "keep_tokens":
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            audit_payload,
            f"success: updated section=identity; tokens_kept={len(active)}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?tokens_kept={len(active)}")
    if token_decision == "revoke_tokens":
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            audit_payload,
            "success: updated section=identity; decision=revoke_tokens",
        )
        outcomes = [
            tokens.deactivate_token(
                ctx,
                store,
                str(record.token_id),
                via="employee_inactivation",
            )
            for record in active
        ]
        deactivated = sum(1 for outcome in outcomes if outcome["sync_ok"] is True)
        pending = [
            str(outcome["token_id"])
            for outcome in outcomes
            if outcome["sync_ok"] is False or outcome["local"] == "missing"
        ]
        redirect_query = f"tokens_deactivated={deactivated}"
        if pending:
            redirect_query += f"&token_deactivation_pending={quote(','.join(pending), safe='')}"
        return ctx.redirect(f"/employees/{quote(employee_id)}?{redirect_query}")
    ctx.audit(
        f"/employees/{employee_id}/save-section",
        audit_payload,
        f"success: updated section={section}",
    )
    return ctx.redirect(f"/employees/{quote(employee_id)}")


def handle_retry_token_deactivation(ctx: object, employee_id: str, body: bytes):
    from urllib.parse import parse_qs, quote
    import ops_dashboard.sections.tokens as tokens

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    token_ids = _parse_token_ids(first_query_value(form, "token_ids"))
    store = tokens.token_store(ctx.runtime_root)
    outcomes = [
        tokens.deactivate_token(
            ctx,
            store,
            token_id,
            via="employee_inactivation_retry",
        )
        for token_id in token_ids
    ]
    deactivated = sum(1 for outcome in outcomes if outcome["sync_ok"] is True)
    pending = [
        str(outcome["token_id"])
        for outcome in outcomes
        if outcome["sync_ok"] is False or outcome["local"] == "missing"
    ]
    redirect_query = f"tokens_deactivated={deactivated}"
    if pending:
        redirect_query += f"&token_deactivation_pending={quote(','.join(pending), safe='')}"
    return ctx.redirect(f"/employees/{quote(employee_id)}?{redirect_query}")
