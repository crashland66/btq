from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote
from urllib import parse as urlparse, request as urlrequest

from event_pipeline import couchdb_config
from ops_dashboard.common import HtmlFragment, first_query_value, humanize_key, record_section, render_count_badge, render_relative_time, render_short_id
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


def _quick_facts(doc: dict[str, Any], person_id: str, primary_site_id: str, assigned_site_ids: list[str], site_names: dict[str, str]) -> str:
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
    sections = "".join(
        part
        for part in (
            record_section(
                "Identity",
                identity,
                ("role", "status", "eHub ID", "Phone / Email", "First seen / tenure", "Person ID"),
                value_formatter=_quick_fact_value,
                dl_class="fields summary-fields",
            ),
            record_section(
                "Assignment",
                assignment,
                ("primary_site", "assigned_sites"),
                value_formatter=_quick_fact_value,
                dl_class="fields summary-fields",
            ),
        )
        if part
    )
    sections = (
        sections.replace("<dt>Ehub Id</dt>", "<dt>eHub ID</dt>")
        .replace("<dt>Person Id</dt>", "<dt>Person ID</dt>")
        .replace("<dt>Primary Site</dt>", "<dt>Primary site</dt>")
        .replace("<dt>Assigned Sites</dt>", "<dt>Assigned sites</dt>")
    )
    if not sections:
        sections = '<p class="zero-state">No quick facts yet.</p>'
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


def render(ctx: object, employee_id: str) -> str:
    try:
        edit_section = first_query_value(getattr(ctx, "query", {}), "edit")
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
        # Static entity pages are keyed by the FULL CouchDB doc _id
        # (projector emits entity/employee/<doc_id>.html), so the vault link must
        # use employee_<id>, not the bare route id. (Unlike /vault/sites/<bare_id>,
        # which has its own dedicated projection.)
        vault_doc_id = html.escape(str(doc.get("_id") or f"employee_{employee_id}"), quote=True)
        vault_btn = f'<a class="button" href="/vault/entity/employee/{vault_doc_id}.html">Vault page</a>'
        all_employees_btn = '<a class="button" href="/employees">All employees</a>'
        admin_links = (
            '<section><h3>Admin links</h3>'
            f'<p class="actions"><a class="button" href="?edit=identity">Edit identity</a>'
            f'<a class="button" href="?edit=assignment">Edit assignment</a>{vault_btn}</p>'
            "</section>"
        )
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
        sections.append(
            _metric_cards(
                assigned_sites=len(assigned_site_ids),
                captures=len(captures),
                recognitions=recognitions,
                open_flags=open_flags,
            )
        )
        sections.append(_quick_facts(doc, person_id_raw, primary_site, assigned_site_ids, site_names))
        if edit_section in {"identity", "assignment"}:
            editable_sections = {
                "Identity": "identity",
                "Assignment": "assignment",
            }
            edit_sections = []
            for title, keys in _EMPLOYEE_GROUPS:
                slug = editable_sections[title]
                edit_sections.append(
                    entity_edit.render_editable_section(
                        title,
                        doc,
                        keys,
                        edit_active=(edit_section == slug),
                        save_action=f"/employees/{html.escape(employee_id, quote=True)}/save-section",
                        entity_id=employee_id,
                    )
                )
            sections.append(f"<section><h2>Edit employee</h2>{''.join(edit_sections)}</section>")
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

        sections.append(_details_block("Admin metadata", None, admin_links))

        body = "".join(sections)
        return html_page(f"Employee {employee_id} — BTQ", body, active_section="employee_detail")
    except Exception as exc:  # noqa: BLE001
        body = (
            f'<header><h1>Error loading employee {html.escape(employee_id)}</h1>'
            f'<p class="muted">{html.escape(str(exc))}</p></header>'
        )
        return html_page("Error — BTQ", body, active_section="employee_detail")


def handle_save_section(ctx: object, employee_id: str, body: bytes):
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import first_query_value
    import ops_dashboard.sections.entity_edit as entity_edit

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

    updated = entity_edit.apply_section_update(existing, form_flat, allowed_keys)

    # ALWAYS recompute derived fields after any employee section save.
    # A stale name/site_ids breaks the home directory and employees_by_site view.
    entity_edit.recompute_employee_derived(updated)

    # Assert validate_doc_update contract (btq_vault requires type + non-empty operator)
    assert updated.get("type") == "employee"
    assert updated.get("operator")

    updated["_rev"] = existing["_rev"]

    doc_path = f"{couchdb_config.vault_database()}/employee_{employee_id}"
    try:
        sites.request_json("PUT", doc_path, updated)
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            {"section": section},
            f"success: updated section={section}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}")
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == 409:
            ctx.audit(
                f"/employees/{employee_id}/save-section",
                {"section": section},
                "failed: conflict",
            )
            return (
                200, "text/html; charset=utf-8",
                render(ctx, employee_id).encode("utf-8"),
                {},
            )
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            {"section": section},
            f"failed: {exc}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?error={quote(str(exc))}")
