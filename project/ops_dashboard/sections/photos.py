from __future__ import annotations

import html
import json
import logging
import os
import sys
from pathlib import Path

from ops_dashboard.common import (
    first_filter_value,
    load_photo_vision_sidecars,
    render_relative_time,
    render_short_id,
    render_table,
    render_thumb,
    resolve_site_label,
    safe_media_url,
    significant_warnings,
    string_list,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page

logger = logging.getLogger(__name__)

PAGE_LIMIT = 200
PENDING_LIMIT = 12


def _photo_vision_couchdb_config() -> object:
    """Return CouchDBConfig when BTQ_COUCHDB_URL is set, else None."""
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        return None
    from event_pipeline import couchdb_config as _cdb
    return _cdb.from_env()


def _build_mango_selector(
    q: str,
    site_id: str,
    area_guess: str,
    severity: str,
    flag: str,
    status: str,
    confidence_min: str,
    confidence_max: str,
    date_from: str,
    date_to: str,
) -> dict[str, object]:
    must: list[dict[str, object]] = [{"doc_type": "photo_vision_sidecar"}]

    if q:
        must.append({"search_text": {"$regex": q.lower()}})
    if site_id:
        must.append({"site_id": site_id})
    if area_guess:
        must.append({"area_guess": area_guess})
    if severity:
        must.append({"quality.severity": severity})
    if flag:
        must.append({"quality.flags": {"$elemMatch": {"$eq": flag}}})
    if status:
        must.append({"status": status})

    conf_clause: dict[str, object] = {}
    try:
        if confidence_min:
            conf_clause["$gte"] = float(confidence_min)
        if confidence_max:
            conf_clause["$lte"] = float(confidence_max)
    except ValueError:
        pass
    if conf_clause:
        must.append({"confidence": conf_clause})

    date_clause: dict[str, object] = {}
    if date_from:
        date_clause["$gte"] = date_from
    if date_to:
        date_clause["$lte"] = date_to + "Z"
    if date_clause:
        must.append({"generated_at": date_clause})

    selector: dict[str, object] = {"$and": must} if len(must) > 1 else must[0]
    return {
        "selector": selector,
        "limit": PAGE_LIMIT + 1,
        "sort": [{"generated_at": "desc"}],
    }


def _query_couchdb(config: object, mango: dict[str, object]) -> list[dict[str, object]] | None:
    """Return docs list or None on error (caller falls back to disk)."""
    try:
        from field_capture.photo_vision_couchdb import PhotoVisionCouchDBError, query_photo_vision
        response = query_photo_vision(config, mango)
        docs = response.get("docs")
        if isinstance(docs, list):
            return docs
        return None
    except Exception as exc:
        logger.warning("photo-vision CouchDB query failed, falling back to disk: %s", exc)
        return None


def _pending_asset_matches_filters(
    asset: object,
    *,
    q: str,
    site_id: str,
    area_guess: str,
    date_from: str,
    date_to: str,
    submitter_name: str,
) -> bool:
    asset_site_id = str(getattr(asset, "site_id", "") or "")
    captured_at = str(getattr(asset, "captured_at", "") or "")
    submitted_area = str(getattr(asset, "area", "") or "")
    if site_id and asset_site_id != site_id:
        return False
    if area_guess and submitted_area.lower() != area_guess.lower():
        return False
    if date_from and captured_at < date_from:
        return False
    if date_to and captured_at > date_to + "Z":
        return False
    if q:
        haystack = " ".join(
            [
                str(getattr(asset, "capture_id", "") or ""),
                str(getattr(asset, "filename", "") or ""),
                asset_site_id,
                submitted_area,
                submitter_name,
            ]
        ).lower()
        return q.lower() in haystack
    return True


def _pending_photo_records(
    runtime_root: Path,
    *,
    processed_asset_ids: set[str],
    q: str,
    site_id: str,
    area_guess: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, object]]:
    from field_capture import photo_vision as field_photo_vision

    intake_dir = field_photo_vision.default_intake_dir(runtime_root)
    upload_dir = field_photo_vision.default_upload_dir(runtime_root)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    disk_sidecar_ids = {str(s.get("photo_asset_id") or "") for s in load_photo_vision_sidecars(photo_vision_dir)}
    submitters = submitters_by_capture(runtime_root)

    records: list[dict[str, object]] = []
    for asset in field_photo_vision.discover_photo_assets(intake_dir, upload_dir):
        if asset.photo_asset_id in processed_asset_ids:
            continue
        submitter_name = submitters.get(asset.capture_id, {}).get("submitter_name", "")
        if not _pending_asset_matches_filters(
            asset,
            q=q,
            site_id=site_id,
            area_guess=area_guess,
            date_from=date_from,
            date_to=date_to,
            submitter_name=submitter_name,
        ):
            continue
        records.append(
            {
                "capture_id": asset.capture_id,
                "photo_asset_id": asset.photo_asset_id,
                "site_id": asset.site_id,
                "area": asset.area,
                "captured_at": asset.captured_at,
                "filename": asset.filename,
                "image_media_url": asset.image_media_url,
                "submitter_name": submitter_name,
                "state": "Saving vision result" if asset.photo_asset_id in disk_sidecar_ids else "Awaiting vision",
            }
        )
    records.sort(key=lambda item: str(item.get("captured_at") or ""), reverse=True)
    return records[:PENDING_LIMIT]


def _render_pending_photo_card(record: dict[str, object], vault_root: Path) -> str:
    media_url = safe_media_url(record.get("image_media_url"))
    if media_url:
        escaped_url = html.escape(media_url, quote=True)
        js_arg = html.escape(json.dumps(media_url), quote=True)
        preview = (
            f'<a href="#" onclick="openLb({js_arg});return false" style="cursor:zoom-in;display:block">'
            f'<img src="{escaped_url}" alt="pending field photo" loading="lazy"'
            f' style="width:84px;height:84px;object-fit:cover;border-radius:6px;border:1px solid var(--line)">'
            f"</a>"
        )
    else:
        preview = (
            '<div style="width:84px;height:84px;border-radius:6px;border:1px solid var(--line);'
            'background:#eef2f7;display:flex;align-items:center;justify-content:center;color:#627d98;font-size:.8rem">'
            "Photo</div>"
        )
    site_id_val = str(record.get("site_id") or "")
    site_label = ""
    if site_id_val:
        import re as _re
        site_label = _re.sub(r"<[^>]+>", "", resolve_site_label(site_id_val, vault_root))
    capture_id = str(record.get("capture_id") or "")
    meta = " · ".join(
        part
        for part in [
            html.escape(str(record.get("area") or "")),
            html.escape(site_label or site_id_val),
            render_relative_time(str(record.get("captured_at") or "")),
        ]
        if part
    )
    return (
        '<article style="display:flex;gap:10px;align-items:center;border:1px solid var(--line);'
        'border-radius:8px;background:var(--panel);padding:8px;min-width:260px">'
        f"{preview}"
        '<div style="min-width:0">'
        f'<p style="margin:0 0 4px"><span class="pill status-pending">{html.escape(str(record.get("state") or ""))}</span></p>'
        f'<p style="margin:0 0 3px"><a href="/captures?capture_id={html.escape(capture_id, quote=True)}">{render_short_id(capture_id)}</a></p>'
        f'<p class="muted" style="margin:0;font-size:.85rem">{meta}</p>'
        "</div></article>"
    )


def _render_pending_photo_section(records: list[dict[str, object]], vault_root: Path) -> str:
    if not records:
        return ""
    cards = "".join(_render_pending_photo_card(record, vault_root) for record in records)
    count = len(records)
    return (
        '<section style="margin-bottom:18px">'
        '<h2 style="margin-bottom:4px">Pending Photos</h2>'
        f'<p class="muted" style="margin-top:0">{count} photo{"s" if count != 1 else ""} uploaded and waiting to appear as processed.</p>'
        '<div style="display:flex;gap:10px;overflow-x:auto;padding-bottom:4px">'
        f"{cards}"
        "</div></section>"
    )


def _load_area_options(cdb_config: object) -> list[str]:
    """Return sorted unique area_guess values from CouchDB, or [] on failure."""
    if cdb_config is None:
        return []
    try:
        from field_capture.photo_vision_couchdb import query_photo_vision
        response = query_photo_vision(
            cdb_config,
            {"selector": {"doc_type": "photo_vision_sidecar"}, "fields": ["area_guess"], "limit": 5000},
        )
        docs = response.get("docs") or []
        return sorted({str(d.get("area_guess") or "").strip() for d in docs if d.get("area_guess")})
    except Exception as exc:
        logger.warning("could not load area options: %s", exc)
        return []


def _load_site_options() -> list[tuple[str, str]]:
    """Return sorted list of (site_id, label) from btq_sites. Empty on failure."""
    try:
        from ops_dashboard.site_service import canonical_name, load_sites, site_id_from_doc
        docs = load_sites()
        result = []
        for doc in docs:
            sid = site_id_from_doc(doc)
            if not sid:
                continue
            name = canonical_name(doc)
            label = f"{sid} — {name}" if name else sid
            result.append((sid, label))
        return sorted(result, key=lambda t: t[0])
    except Exception as exc:
        logger.warning("could not load site options: %s", exc)
        return []


def _in_memory_filter(
    sidecars: list[dict[str, object]],
    q: str,
    site_id: str,
    area_guess: str,
    severity: str,
    flag: str,
    status: str,
    confidence_min: str,
    confidence_max: str,
    date_from: str,
    date_to: str,
) -> list[dict[str, object]]:
    results = sidecars
    if q:
        ql = q.lower()
        results = [s for s in results if ql in _search_text(s)]
    if site_id:
        results = [s for s in results if str(s.get("site_id") or "") == site_id]
    if area_guess:
        results = [s for s in results if str(s.get("area_guess") or "") == area_guess]
    if status:
        results = [s for s in results if str(s.get("status") or "") == status]
    if severity:
        results = [s for s in results if _sidecar_severity(s) == severity]
    if flag:
        results = [s for s in results if flag in _sidecar_flags(s)]
    if confidence_min:
        try:
            cmin = float(confidence_min)
            results = [s for s in results if _float_or_none(s.get("confidence")) is not None and _float_or_none(s.get("confidence")) >= cmin]
        except ValueError:
            pass
    if confidence_max:
        try:
            cmax = float(confidence_max)
            results = [s for s in results if _float_or_none(s.get("confidence")) is not None and _float_or_none(s.get("confidence")) <= cmax]
        except ValueError:
            pass
    if date_from:
        results = [s for s in results if str(s.get("generated_at") or "") >= date_from]
    if date_to:
        results = [s for s in results if str(s.get("generated_at") or "") <= date_to + "Z"]
    results.sort(key=lambda s: str(s.get("generated_at") or ""), reverse=True)
    return results[:PAGE_LIMIT]


def _search_text(sidecar: dict[str, object]) -> str:
    parts = [
        str(sidecar.get("description") or ""),
        str(sidecar.get("area_guess") or ""),
    ]
    parts.extend(string_list(sidecar.get("visible_objects")))
    parts.extend(string_list(sidecar.get("possible_conditions")))
    parts.extend(string_list(sidecar.get("possible_issues")))
    return " ".join(parts).lower()


def _sidecar_severity(sidecar: dict[str, object]) -> str:
    quality = sidecar.get("quality")
    if isinstance(quality, dict):
        return str(quality.get("severity") or "")
    return ""


def _sidecar_flags(sidecar: dict[str, object]) -> list[str]:
    quality = sidecar.get("quality")
    if isinstance(quality, dict):
        flags = quality.get("flags")
        return [str(f) for f in flags] if isinstance(flags, list) else []
    return []


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _severity_pill(sidecar: dict[str, object]) -> str:
    sev = _sidecar_severity(sidecar)
    if not sev:
        return ""
    css = {"ok": "neutral", "degraded": "pending", "unusable": "danger"}.get(sev, "neutral")
    return f'<span class="pill status-{html.escape(css)}">{html.escape(sev)}</span>'


def _flags_html(sidecar: dict[str, object]) -> str:
    flags = _sidecar_flags(sidecar)
    if not flags:
        return ""
    return " ".join(f'<span class="pill">{html.escape(f)}</span>' for f in flags)


def _warnings_html(sidecar: dict[str, object]) -> str:
    warnings = significant_warnings(sidecar.get("warnings"))
    if not warnings:
        return ""
    return "; ".join(html.escape(w) for w in warnings[:2])


def _render_photo_row(sidecar: dict[str, object]) -> dict[str, object]:
    """Flatten a sidecar summary into a table-renderable row."""
    return {
        "_asset_id": str(sidecar.get("photo_asset_id") or ""),
        "_site_id": str(sidecar.get("site_id") or ""),
        "_area_guess": str(sidecar.get("area_guess") or ""),
        "_status": str(sidecar.get("status") or ""),
        "_severity": _severity_pill(sidecar),
        "_flags": _flags_html(sidecar),
        "_description": html.escape(str(sidecar.get("description") or "")[:120]),
        "_generated_at": str(sidecar.get("generated_at") or ""),
        "_warnings": _warnings_html(sidecar),
        "_capture_id": str(sidecar.get("capture_id") or ""),
        "_image": _image_thumb(sidecar),
    }


def _image_thumb(sidecar: dict[str, object]) -> str:
    provenance = sidecar.get("provenance")
    raw_url = (provenance.get("image_media_url") if isinstance(provenance, dict) else None) or sidecar.get("image_media_url")
    return render_thumb(safe_media_url(raw_url))


def _render_results(sidecars: list[dict[str, object]], *, fallback: bool) -> str:
    notice = '<p class="muted">CouchDB unavailable — results from disk cache.</p>' if fallback else ""
    rows = [_render_photo_row(s) for s in sidecars]
    columns = [
        {"key": "_image", "label": "", "format": lambda v, _r: v},
        {"key": "_asset_id", "label": "Asset ID", "format": lambda v, _r: render_short_id(v), "nowrap": True},
        {"key": "_site_id", "label": "Site", "nowrap": True},
        {"key": "_area_guess", "label": "Area", "nowrap": True},
        {"key": "_status", "label": "Status", "nowrap": True},
        {"key": "_severity", "label": "Vision Quality", "format": lambda v, _r: v, "nowrap": True},
        {"key": "_flags", "label": "Flags", "format": lambda v, _r: v, "priority": 2},
        {"key": "_description", "label": "Description", "format": lambda v, _r: v, "priority": 2},
        {"key": "_generated_at", "label": "Generated", "format": lambda v, _r: render_relative_time(v), "priority": 2, "nowrap": True},
        {"key": "_warnings", "label": "Warnings", "format": lambda v, _r: v, "priority": 3},
    ]
    table = render_table(rows, columns, empty_text="No photo-vision sidecars match this query.")
    return notice + table


def _select_options(choices: list[tuple[str, str]], current: str, any_label: str = "Any") -> str:
    out = f'<option value="">{html.escape(any_label)}</option>'
    for value, label in choices:
        sel = " selected" if value == current else ""
        out += f'<option value="{html.escape(value)}"{sel}>{html.escape(label)}</option>'
    return out


def _filter_form(
    q: str,
    site_id: str,
    area_guess: str,
    severity: str,
    flag: str,
    status: str,
    confidence_min: str,
    confidence_max: str,
    date_from: str,
    date_to: str,
    *,
    site_options: list[tuple[str, str]],
    area_options: list[str],
) -> str:
    from field_capture.photo_vision import QUALITY_FLAGS_ENUM
    flag_options = _select_options([(f, f) for f in sorted(QUALITY_FLAGS_ENUM)], flag)
    severity_options = _select_options([("ok", "ok"), ("degraded", "degraded"), ("unusable", "unusable")], severity)
    status_options = _select_options([("completed", "completed"), ("failed", "failed"), ("skipped", "skipped"), ("malformed", "malformed")], status)
    site_select_options = _select_options(site_options, site_id, any_label="All sites")
    area_select_options = _select_options([(a, a) for a in area_options], area_guess, any_label="All areas")

    return f"""<form class="filter-form" method="get" action="/photos" data-submit-on-change style="display:flex;flex-direction:column;gap:.5rem">
  <div style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end">
    <label style="flex:1 1 18em">Search
      <input name="q" value="{html.escape(q)}" placeholder="keyword…" style="width:100%">
    </label>
    <label style="flex:1 1 14em">Site
      <select name="site_id" style="width:100%">
        {site_select_options}
      </select>
    </label>
    <label style="flex:1 1 10em">Area
      <select name="area_guess" style="width:100%">
        {area_select_options}
      </select>
    </label>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end">
    <label>Vision Quality
      <select name="severity">
        {severity_options}
      </select>
    </label>
    <label>Flag
      <select name="flag">
        {flag_options}
      </select>
    </label>
    <label>Status
      <select name="status">
        {status_options}
      </select>
    </label>
    <label>Conf&nbsp;&#8805;
      <input name="confidence_min" value="{html.escape(confidence_min)}" placeholder="0.0" style="width:4em">
    </label>
    <label>Conf&nbsp;&#8804;
      <input name="confidence_max" value="{html.escape(confidence_max)}" placeholder="1.0" style="width:4em">
    </label>
    <label>From
      <input type="date" name="date_from" value="{html.escape(date_from)}">
    </label>
    <label>To
      <input type="date" name="date_to" value="{html.escape(date_to)}">
    </label>
    <button type="submit">Search</button>
  </div>
</form>"""


def render(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    runtime_root = getattr(ctx, "runtime_root", Path(".")).expanduser().resolve(strict=False)

    q = first_filter_value(query, "q")
    site_id = first_filter_value(query, "site_id")
    area_guess = first_filter_value(query, "area_guess")
    severity = first_filter_value(query, "severity")
    flag = first_filter_value(query, "flag")
    status = first_filter_value(query, "status")
    confidence_min = first_filter_value(query, "confidence_min")
    confidence_max = first_filter_value(query, "confidence_max")
    date_from = first_filter_value(query, "date_from")
    date_to = first_filter_value(query, "date_to")

    cdb_config = _photo_vision_couchdb_config()
    sidecars: list[dict[str, object]] = []
    fallback = False

    has_more = False
    if cdb_config is not None:
        mango = _build_mango_selector(q, site_id, area_guess, severity, flag, status, confidence_min, confidence_max, date_from, date_to)
        docs = _query_couchdb(cdb_config, mango)
        if docs is not None:
            has_more = len(docs) > PAGE_LIMIT
            sidecars = docs[:PAGE_LIMIT]
        else:
            fallback = True

    processed_asset_ids: set[str] | None = None
    if cdb_config is not None and not fallback:
        asset_docs = _query_couchdb(
            cdb_config,
            {
                "selector": {"doc_type": "photo_vision_sidecar"},
                "fields": ["photo_asset_id"],
                "limit": 5000,
            },
        )
        if asset_docs is None:
            fallback = True
        else:
            processed_asset_ids = {
                str(doc.get("photo_asset_id") or "").strip()
                for doc in asset_docs
                if str(doc.get("photo_asset_id") or "").strip()
            }

    if cdb_config is None or fallback:
        from field_capture import photo_vision as field_photo_vision
        photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
        all_sidecars = load_photo_vision_sidecars(photo_vision_dir)
        sidecars = _in_memory_filter(
            all_sidecars, q, site_id, area_guess, severity, flag, status,
            confidence_min, confidence_max, date_from, date_to,
        )
        processed_asset_ids = {str(s.get("photo_asset_id") or "") for s in all_sidecars}

    site_options = _load_site_options()
    area_options = _load_area_options(cdb_config)

    filter_form = _filter_form(
        q, site_id, area_guess, severity, flag, status,
        confidence_min, confidence_max, date_from, date_to,
        site_options=site_options,
        area_options=area_options,
    )

    results_html = _render_results(sidecars, fallback=fallback)
    pending_html = ""
    if not any([severity, flag, status, confidence_min, confidence_max]):
        config = getattr(ctx, "config", None)
        vault_root = Path(getattr(config, "vault_root", runtime_root / "vault")).expanduser()
        pending_records = _pending_photo_records(
            runtime_root,
            processed_asset_ids=processed_asset_ids or set(),
            q=q,
            site_id=site_id,
            area_guess=area_guess,
            date_from=date_from,
            date_to=date_to,
        )
        pending_html = _render_pending_photo_section(pending_records, vault_root)
    n = len(sidecars)
    if has_more:
        count_text = f"{PAGE_LIMIT}+ results (showing first {PAGE_LIMIT})"
    elif n == PAGE_LIMIT and not cdb_config:
        count_text = f"{n} result{'s' if n != 1 else ''} (showing first {PAGE_LIMIT})"
    else:
        count_text = f"{n} result{'s' if n != 1 else ''}"

    body = f"""
    <header>
      <h1>Photo Vision Search</h1>
      <p class="muted">Search and filter photo-vision sidecar data by content, area, quality, and date.</p>
    </header>
    <section>
      {filter_form}
      {pending_html}
      <p class="muted" style="margin-top:.5rem">{html.escape(count_text)}</p>
      {results_html}
    </section>
    """
    return html_page("Photo Vision Search — BTQ Ops", body, active_section="photos")
