from __future__ import annotations

import html
import json
import re
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote
from urllib import parse as urlparse
from urllib import request as urlrequest

from event_pipeline import couchdb_config
from ops_dashboard.common import field_value, first_query_value, other_section, record_section
from ops_dashboard.layout import html_page
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


_SUPPRESSED = {
    "_id",
    "_rev",
    "type",
    "operator",
    "vault_path",
    "content",
    "btq_job_ids",
    "voice_memo_capture_ids",
}

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


def _format_value(value: object) -> str:
    return field_value(value)


def _summary_section(doc: dict[str, Any], site_id: str, edit_section: str) -> str:
    sections: list[str] = []
    ordered_keys = {key for _title, keys in _SUMMARY_GROUPS for key in keys}
    site_id_escaped = html.escape(site_id, quote=True)
    editable_sections = {
        "Contact": "contact",
        "Schedule": "schedule",
        "Billing & Wages": "billing_wages",
    }
    for title, keys in _SUMMARY_GROUPS:
        if title in {"Identity", "Supply Budget"}:
            section = record_section(title, doc, keys)
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
            )
            if section:
                sections.append(section)

    other = other_section(doc, ordered_keys, _SUPPRESSED)
    if other:
        sections.append(other)

    provenance = _capture_provenance(doc)
    if provenance:
        sections.append(provenance)

    if not sections:
        return ""
    return _section("Summary", "".join(sections))


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


def _related_sections(site_id: str) -> list[str]:
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
    return [
        _section("Employees Assigned", _employee_table(employee_rows, include_sites=False)),
        _section("Open Opportunities", _simple_table(["Opportunity"], [_opportunity_label(row) for row in opportunity_rows])),
        _section("Recent Visits", _visit_details(recent_visits)),
    ]


def _photos_section(ctx: object, site_id: str) -> str:
    filter_form_html = field_photos.render_filter_form(site_id=site_id)
    cards_html, fallback = field_photos.latest_photo_cards(ctx, limit=4, site_id=site_id)
    if fallback:
        strip = '<p class="muted">Photos unavailable.</p>'
    elif cards_html:
        strip = (
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));'
            f'gap:16px;margin-top:12px">{cards_html}</div>'
        )
    else:
        strip = '<p class="zero-state">No photos yet for this site.</p>'
    escaped_id = html.escape(site_id, quote=True)
    return (
        "<section>"
        "<h3>Photos</h3>"
        f"{filter_form_html}"
        f"{strip}"
        f'<p><a href="/field-photos?site_id={escaped_id}">See all photos for this site &rarr;</a></p>'
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

    vault_path = f"{couchdb_config.vault_database()}/location_{site_id}"
    try:
        sites.request_json("PUT", vault_path, updated)
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


def render(ctx: object, site_id: str) -> str:
    """Render the per-site detail page wrapped by html_page(active_section='site_detail')."""
    try:
        edit_section = first_query_value(getattr(ctx, "query", {}), "edit")
        doc = _load_location(site_id)
        if not isinstance(doc, dict) or doc.get("type") != "location":
            return _not_found(site_id)

        primary_name = sites.canonical_name(doc) or str(doc.get("location") or site_id)
        title = f"{primary_name} · {site_id}"
        escaped_id = html.escape(site_id, quote=True)
        sections = [
            (
                f"<header><h1>{html.escape(title)}</h1>"
                '<p class="actions">'
                f'<a class="button" href="/sites?site_id={escaped_id}">Admin metadata</a>'
                f'<a class="button" href="/vault/sites/{escaped_id}.html">Vault page</a>'
                f'<a class="button" href="/field-photos?site_id={escaped_id}">Field Photos for this site</a>'
                "</p></header>"
            )
        ]
        summary = _summary_section(doc, site_id, edit_section)
        if summary:
            sections.append(summary)
        raw_content = str(doc.get("content") or "")
        if edit_section == "about":
            escaped_id = html.escape(site_id, quote=True)
            rev = html.escape(str(doc.get("_rev", "")), quote=True)
            sections.append(
                '<section><h3>About</h3>'
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
                sections.append(_section("About", render_markdown(stripped)))
        sections.extend(_related_sections(site_id))
        sections.append(_photos_section(ctx, site_id))
        body = "".join(sections)
        return html_page(f"Site {site_id} — BTQ", body, active_section="site_detail")
    except Exception as exc:  # noqa: BLE001
        return _degraded(site_id, exc)
