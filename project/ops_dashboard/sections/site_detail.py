from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote
from urllib import parse as urlparse
from urllib import request as urlrequest

from event_pipeline import couchdb_config
from ops_dashboard.common import (
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
_EMPTY_ABOUT_SECTION = (
    '<section><h2>About &amp; operational notes</h2>'
    '<p class="zero-state">No operational notes yet.</p></section>'
)

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
    return {
        "notes": notes,
        "employee_rows": employee_rows,
        "opportunity_rows": opportunity_rows,
        "visit_rows": visit_rows,
        "recent_visits": recent_visits,
    }


def _related_sections(data: dict[str, Any]) -> list[tuple[str, int, str]]:
    employee_rows = data["employee_rows"]
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
    return sidecars[:_CAPTURE_GALLERY_LIMIT], fallback, len(sidecars)


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
        sections.append(_quick_facts_section(doc, site_id, edit_section))
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
