from __future__ import annotations

import html
import json
import logging
import os
from pathlib import Path

from ops_dashboard.common import (
    first_filter_value,
    load_photo_vision_sidecars,
    render_relative_time,
    render_short_id,
    resolve_site_label,
    safe_media_url,
    string_list,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page

logger = logging.getLogger(__name__)

PAGE_LIMIT = 120
PENDING_LIMIT = 12


def _photo_vision_couchdb_config() -> object:
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        return None
    from event_pipeline import couchdb_config as _cdb
    return _cdb.from_env()


def _build_mango_selector(
    q: str,
    site_id: str,
    area_guess: str,
    date_from: str,
    date_to: str,
    *,
    target_type: str = "",
    target_id: str = "",
) -> dict[str, object]:
    must: list[dict[str, object]] = [{"doc_type": "photo_vision_sidecar"}]
    if q:
        must.append({"search_text": {"$regex": q.lower()}})
    if site_id:
        must.append({"site_id": site_id})
    if target_type:
        must.append({"target_type": target_type})
        if target_id:
            must.append({"target_id": target_id})
    if area_guess:
        must.append({"area_guess": area_guess})
    date_clause: dict[str, object] = {}
    if date_from:
        date_clause["$gte"] = date_from
    if date_to:
        date_clause["$lte"] = date_to + "Z"
    if date_clause:
        must.append({"generated_at": date_clause})
    selector: dict[str, object] = {"$and": must} if len(must) > 1 else must[0]
    return {"selector": selector, "limit": PAGE_LIMIT + 1, "sort": [{"generated_at": "desc"}]}


def _query_couchdb(config: object, mango: dict[str, object]) -> list[dict[str, object]] | None:
    try:
        from field_capture.photo_vision_couchdb import query_photo_vision
        response = query_photo_vision(config, mango)
        docs = response.get("docs")
        return docs if isinstance(docs, list) else None
    except Exception as exc:
        logger.warning("field-photos CouchDB query failed, falling back to disk: %s", exc)
        return None


def _query_processed_asset_ids(config: object) -> set[str] | None:
    mango = {
        "selector": {"doc_type": "photo_vision_sidecar"},
        "fields": ["photo_asset_id"],
        "limit": 5000,
    }
    docs = _query_couchdb(config, mango)
    if docs is None:
        return None
    return {str(doc.get("photo_asset_id") or "").strip() for doc in docs if str(doc.get("photo_asset_id") or "").strip()}


def _search_text(sidecar: dict[str, object]) -> str:
    parts = [
        str(sidecar.get("description") or ""),
        str(sidecar.get("area_guess") or ""),
    ]
    parts.extend(string_list(sidecar.get("visible_objects")))
    parts.extend(string_list(sidecar.get("possible_conditions")))
    parts.extend(string_list(sidecar.get("possible_issues")))
    return " ".join(parts).lower()


def _in_memory_filter(
    sidecars: list[dict[str, object]],
    q: str,
    site_id: str,
    area_guess: str,
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
    if date_from:
        results = [s for s in results if str(s.get("generated_at") or "") >= date_from]
    if date_to:
        results = [s for s in results if str(s.get("generated_at") or "") <= date_to + "Z"]
    results.sort(key=lambda s: str(s.get("generated_at") or ""), reverse=True)
    return results[:PAGE_LIMIT]


def _load_site_options() -> list[tuple[str, str]]:
    try:
        from ops_dashboard.site_service import canonical_name, load_sites, site_id_from_doc
        result = []
        for doc in load_sites():
            sid = site_id_from_doc(doc)
            if not sid:
                continue
            name = canonical_name(doc)
            result.append((sid, f"{sid} — {name}" if name else sid))
        return sorted(result, key=lambda t: t[0])
    except Exception as exc:
        logger.warning("could not load site options: %s", exc)
        return []


def _load_area_options(cdb_config: object) -> list[str]:
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


def _first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.strip() else ""


def _render_card(sidecar: dict[str, object], submitters: dict[str, dict[str, str]], vault_root: Path) -> str:
    capture_id = str(sidecar.get("capture_id") or "")
    provenance = sidecar.get("provenance") if isinstance(sidecar.get("provenance"), dict) else {}
    raw_url = (provenance.get("image_media_url") if isinstance(provenance, dict) else None) or sidecar.get("image_media_url")
    url = safe_media_url(raw_url)

    area = str(sidecar.get("area_guess") or "")
    site_id_val = str(sidecar.get("site_id") or "")
    description = str(sidecar.get("description") or "")
    generated_at = str(sidecar.get("generated_at") or "")

    full_name = submitters.get(capture_id, {}).get("submitter_name", "")
    first = _first_name(full_name)

    conditions = string_list(sidecar.get("possible_conditions"))
    issues = string_list(sidecar.get("possible_issues"))
    tags = (conditions + issues)[:5]

    if url:
        escaped_url = html.escape(url, quote=True)
        js_arg = html.escape(json.dumps(url), quote=True)
        img_html = (
            f'<a href="#" onclick="openLb({js_arg});return false" style="cursor:zoom-in;display:block">'
            f'<img src="{escaped_url}" alt="field photo" loading="lazy"'
            f' style="width:100%;aspect-ratio:4/3;object-fit:cover;display:block;border-radius:6px 6px 0 0">'
            f"</a>"
        )
    else:
        img_html = (
            '<div style="width:100%;aspect-ratio:4/3;background:#e4e7eb;border-radius:6px 6px 0 0;'
            'display:flex;align-items:center;justify-content:center">'
            '<span style="color:#627d98;font-size:.85rem">No image</span></div>'
        )

    meta_parts = []
    if area:
        meta_parts.append(f"<strong>{html.escape(area)}</strong>")
    if site_id_val:
        import re as _re
        label = _re.sub(r"<[^>]+>", "", resolve_site_label(site_id_val, vault_root))
        meta_parts.append(html.escape(label))
    meta_line = " · ".join(meta_parts)

    sub_parts = []
    if first:
        sub_parts.append(html.escape(first))
    if generated_at:
        sub_parts.append(render_relative_time(generated_at))
    sub_line = " · ".join(sub_parts)

    pills_html = " ".join(f'<span class="pill">{html.escape(t)}</span>' for t in tags)

    inner = ""
    if meta_line:
        inner += f"<p style='margin:0 0 4px'>{meta_line}</p>"
    if sub_line:
        inner += f'<p class="muted" style="margin:0 0 6px;font-size:.85rem">{sub_line}</p>'
    if description:
        inner += f'<p style="margin:0 0 6px;font-size:.9rem;line-height:1.45">{html.escape(description)}</p>'
    if pills_html:
        inner += f'<div style="margin-top:4px">{pills_html}</div>'

    return (
        f'<article style="border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)">'
        f"{img_html}"
        f'<div style="padding:10px">{inner}</div>'
        f"</article>"
    )


def _asset_matches_filters(
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
        if not _asset_matches_filters(
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
                "state": "saving_result" if asset.photo_asset_id in disk_sidecar_ids else "awaiting_vision",
            }
        )
    records.sort(key=lambda item: str(item.get("captured_at") or ""), reverse=True)
    return records[:PENDING_LIMIT]


def _render_pending_card(record: dict[str, object], vault_root: Path) -> str:
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

    state = str(record.get("state") or "")
    state_text = "Saving vision result" if state == "saving_result" else "Awaiting vision"
    site_id_val = str(record.get("site_id") or "")
    site_label = ""
    if site_id_val:
        import re as _re
        site_label = _re.sub(r"<[^>]+>", "", resolve_site_label(site_id_val, vault_root))
    capture_id = str(record.get("capture_id") or "")
    detail_url = f"/captures?capture_id={html.escape(capture_id, quote=True)}"
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
        f'<p style="margin:0 0 4px"><span class="pill status-pending">{html.escape(state_text)}</span></p>'
        f'<p style="margin:0 0 3px"><a href="{detail_url}">{render_short_id(capture_id)}</a></p>'
        f'<p class="muted" style="margin:0;font-size:.85rem">{meta}</p>'
        "</div></article>"
    )


def _render_pending_section(records: list[dict[str, object]], vault_root: Path) -> str:
    if not records:
        return ""
    cards = "".join(_render_pending_card(record, vault_root) for record in records)
    count = len(records)
    return (
        '<section style="margin-bottom:18px">'
        f'<h2 style="margin-bottom:4px">Pending Photos</h2>'
        f'<p class="muted" style="margin-top:0">{count} photo{"s" if count != 1 else ""} uploaded and waiting to appear as processed.</p>'
        '<div style="display:flex;gap:10px;overflow-x:auto;padding-bottom:4px">'
        f"{cards}"
        "</div></section>"
    )


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
    date_from: str,
    date_to: str,
    *,
    site_options: list[tuple[str, str]],
    area_options: list[str],
) -> str:
    site_opts = _select_options(site_options, site_id, any_label="All sites")
    area_opts = _select_options([(a, a) for a in area_options], area_guess, any_label="All areas")
    return (
        '<form method="get" action="/field-photos" data-submit-on-change'
        ' style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end">'
        f'<label style="flex:1 1 18em">Search<input name="q" value="{html.escape(q)}" placeholder="keyword…" style="width:100%"></label>'
        f'<label style="flex:1 1 14em">Site<select name="site_id" style="width:100%">{site_opts}</select></label>'
        f'<label style="flex:1 1 10em">Area<select name="area_guess" style="width:100%">{area_opts}</select></label>'
        f'<label>From<input type="date" name="date_from" value="{html.escape(date_from)}"></label>'
        f'<label>To<input type="date" name="date_to" value="{html.escape(date_to)}"></label>'
        '<button type="submit">Search</button>'
        "</form>"
    )


def render_filter_form(
    *,
    q: str = "",
    site_id: str = "",
    area_guess: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    cdb_config = _photo_vision_couchdb_config()
    site_options = _load_site_options()
    area_options = _load_area_options(cdb_config)
    return _filter_form(
        q,
        site_id,
        area_guess,
        date_from,
        date_to,
        site_options=site_options,
        area_options=area_options,
    )


def latest_photo_cards(
    ctx: object,
    limit: int,
    *,
    site_id: str = "",
    target_type: str = "",
    target_id: str = "",
) -> tuple[str, bool]:
    """Return (cards_html, fallback_used) for recent photo_vision sidecars.

    cards_html is concatenated _render_card output, ready for a CSS grid.
    fallback_used is True when the CouchDB query failed and disk was used.
    """
    runtime_root = ctx.runtime_root
    cdb_config = _photo_vision_couchdb_config()
    sidecars: list[dict[str, object]] = []
    fallback = False

    if cdb_config is not None:
        mango = _build_mango_selector("", site_id, "", "", "", target_type=target_type, target_id=target_id)
        mango["limit"] = limit
        docs = _query_couchdb(cdb_config, mango)
        if docs is not None:
            sidecars = docs[:limit]
        else:
            fallback = True

    if cdb_config is None or fallback:
        from field_capture import photo_vision as field_photo_vision

        photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
        all_sidecars = load_photo_vision_sidecars(photo_vision_dir)
        if target_type:
            all_sidecars = [_with_target_fields(s) for s in all_sidecars]
            all_sidecars = [
                s
                for s in all_sidecars
                if str(s.get("target_type") or "") == target_type
                and (not target_id or str(s.get("target_id") or "") == target_id)
            ]
        elif site_id:
            all_sidecars = [s for s in all_sidecars if str(s.get("site_id") or "") == site_id]
        sidecars = all_sidecars[:limit]

    submitters = submitters_by_capture(runtime_root)
    vault_root = Path(getattr(ctx.config, "vault_root", runtime_root / "vault")).expanduser()
    return ("".join(_render_card(s, submitters, vault_root) for s in sidecars), fallback)


def _with_target_fields(sidecar: dict[str, object]) -> dict[str, object]:
    if sidecar.get("target_type") or sidecar.get("target_id"):
        return sidecar
    path = Path(str(sidecar.get("path") or ""))
    if not path.is_file():
        return sidecar
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return sidecar
    if not isinstance(payload, dict):
        return sidecar
    enriched = dict(sidecar)
    if payload.get("target_type") is not None:
        enriched["target_type"] = str(payload.get("target_type") or "")
    if payload.get("target_id") is not None:
        enriched["target_id"] = str(payload.get("target_id") or "")
    return enriched


def render(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    runtime_root = ctx.runtime_root

    q = first_filter_value(query, "q")
    site_id = first_filter_value(query, "site_id")
    area_guess = first_filter_value(query, "area_guess")
    date_from = first_filter_value(query, "date_from")
    date_to = first_filter_value(query, "date_to")

    cdb_config = _photo_vision_couchdb_config()
    sidecars: list[dict[str, object]] = []
    fallback = False
    has_more = False

    if cdb_config is not None:
        mango = _build_mango_selector(q, site_id, area_guess, date_from, date_to)
        docs = _query_couchdb(cdb_config, mango)
        if docs is not None:
            has_more = len(docs) > PAGE_LIMIT
            sidecars = docs[:PAGE_LIMIT]
        else:
            fallback = True

    processed_asset_ids: set[str] | None = None
    if cdb_config is not None and not fallback:
        processed_asset_ids = _query_processed_asset_ids(cdb_config)
        if processed_asset_ids is None:
            fallback = True

    if cdb_config is None or fallback:
        from field_capture import photo_vision as field_photo_vision
        photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
        all_sidecars = load_photo_vision_sidecars(photo_vision_dir)
        sidecars = _in_memory_filter(all_sidecars, q, site_id, area_guess, date_from, date_to)
        processed_asset_ids = {str(s.get("photo_asset_id") or "") for s in all_sidecars}

    vault_root = Path(getattr(ctx.config, "vault_root", runtime_root / "vault")).expanduser()
    submitters = submitters_by_capture(runtime_root)
    pending_records = _pending_photo_records(
        runtime_root,
        processed_asset_ids=processed_asset_ids or set(),
        q=q,
        site_id=site_id,
        area_guess=area_guess,
        date_from=date_from,
        date_to=date_to,
    )
    pending_html = _render_pending_section(pending_records, vault_root)

    filter_form = render_filter_form(q=q, site_id=site_id, area_guess=area_guess, date_from=date_from, date_to=date_to)

    cards_html = "".join(_render_card(s, submitters, vault_root) for s in sidecars)
    grid_html = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:12px">{cards_html}</div>'
        if cards_html
        else '<p class="zero-state">No photos match this query.</p>'
    )

    n = len(sidecars)
    if has_more:
        count_text = f"{PAGE_LIMIT}+ photos (showing first {PAGE_LIMIT})"
    else:
        count_text = f"{n} photo{'s' if n != 1 else ''}"

    fallback_notice = '<p class="muted">CouchDB unavailable — results from disk cache.</p>' if fallback else ""

    body = f"""
    <header>
      <h1>Field Photos</h1>
      <p class="muted">Browse photos captured in the field with vision analysis.</p>
    </header>
    <section>
      {filter_form}
      {fallback_notice}
      {pending_html}
      <p class="muted" style="margin-top:.5rem">{html.escape(count_text)}</p>
      {grid_html}
    </section>
    """
    return html_page("Field Photos — BTQ Ops", body, active_section="field_photos")
