from __future__ import annotations

import html
import io
import json
import logging
import re
import zipfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from field_capture.deep_analysis import DEEP_ANALYSIS_PRESETS
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_request, upload_id_for_path
from media_store import get_media_store
from ops_dashboard.common import (
    DEEP_ANALYSIS_LABELS,
    deep_analysis_prompt_label,
    default_actor,
    first_filter_value,
    load_photo_vision_sidecars,
    photo_vision_couchdb_config as _photo_vision_couchdb_config,
    render_deep_analysis_markdown,
    render_relative_time,
    render_short_id,
    resolve_site_label,
    safe_media_url,
    string_list,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page
from field_capture.photo_vision_categories import CATEGORY_AGREEMENT_MISMATCH

logger = logging.getLogger(__name__)

PAGE_LIMIT = 120
PENDING_LIMIT = 12
_TERMINAL_CAPTURE_STATES = frozenset({"failed", "complete", "completed"})
_FIELD_CAPTURE_PAGE_SIZE = 5000


def _build_mango_selector(
    q: str,
    site_id: str,
    area_guess: str,
    date_from: str,
    date_to: str,
    *,
    capture_id: str = "",
    qc_category: str = "",
    vision_category: str = "",
    vision_disagrees: bool = False,
    has_deep_analysis: bool = False,
    target_type: str = "",
    target_id: str = "",
) -> dict[str, object]:
    must: list[dict[str, object]] = [{"doc_type": "photo_vision_sidecar"}]
    if q:
        must.append({"search_text": {"$regex": q.lower()}})
    if site_id:
        must.append({"site_id": site_id})
    if capture_id:
        must.append({"capture_id": capture_id})
    if qc_category:
        must.append({"qc_category": qc_category})
    if vision_category:
        must.append({"vision_category": vision_category})
    if vision_disagrees:
        must.append({"category_agreement": CATEGORY_AGREEMENT_MISMATCH})
    if target_type:
        must.append({"target_type": target_type})
        if target_id:
            must.append({"target_id": target_id})
    if area_guess:
        must.append({"area_guess": area_guess})
    if has_deep_analysis:
        must.append({"deep_analysis.0": {"$exists": True}})
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
    # Fetch the COMPLETE set of processed photo_asset_ids via bookmark pagination.
    # A fixed `limit` silently truncates this set once btq_photo_vision grows past
    # it, which makes already-processed photos show up as "pending".
    from field_capture.photo_vision_couchdb import query_photo_vision

    ids: set[str] = set()
    bookmark: object = None
    page_size = 5000
    for _ in range(400):  # safety cap (400 * 5000 = 2M docs)
        mango: dict[str, object] = {
            "selector": {"doc_type": "photo_vision_sidecar"},
            "fields": ["photo_asset_id"],
            "limit": page_size,
        }
        if bookmark:
            mango["bookmark"] = bookmark
        try:
            response = query_photo_vision(config, mango)
        except Exception as exc:
            logger.warning("field-photos processed-id query failed, falling back to disk: %s", exc)
            return None
        docs = response.get("docs")
        if not isinstance(docs, list):
            return None
        for doc in docs:
            aid = str(doc.get("photo_asset_id") or "").strip()
            if aid:
                ids.add(aid)
        bookmark = response.get("bookmark")
        if len(docs) < page_size or not bookmark:
            break
    return ids


def _query_terminal_capture_ids(config: object) -> set[str] | None:
    from event_pipeline import couchdb_config
    from voice_memo.couchdb import query_couchdb_find

    ids: set[str] = set()
    bookmark: object = None
    for _ in range(400):  # safety cap (400 * 5000 = 2M docs)
        mango: dict[str, object] = {
            "selector": {
                "type": "field_capture",
                "processing_state": {"$in": sorted(_TERMINAL_CAPTURE_STATES)},
            },
            "fields": ["_id", "capture_id", "processing_state"],
            "limit": _FIELD_CAPTURE_PAGE_SIZE,
        }
        if bookmark:
            mango["bookmark"] = bookmark
        try:
            response = query_couchdb_find(config, couchdb_config.field_captures_database(), mango)
        except Exception as exc:
            logger.warning("field-capture terminal-id query failed; pending photos may include terminal captures: %s", exc)
            return None
        docs = response.get("docs")
        if not isinstance(docs, list):
            return None
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            state = str(doc.get("processing_state") or "").strip().lower()
            if state not in _TERMINAL_CAPTURE_STATES:
                continue
            capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
            if capture_id:
                ids.add(capture_id)
        bookmark = response.get("bookmark")
        if len(docs) < _FIELD_CAPTURE_PAGE_SIZE or not bookmark:
            break
    return ids


def _search_text(sidecar: dict[str, object]) -> str:
    parts = [
        str(sidecar.get("description") or ""),
        str(sidecar.get("qc_category") or ""),
        str(sidecar.get("vision_category") or ""),
        str(sidecar.get("category_agreement") or ""),
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
    *,
    capture_id: str = "",
    qc_category: str = "",
    vision_category: str = "",
    vision_disagrees: bool = False,
    has_deep_analysis: bool = False,
) -> list[dict[str, object]]:
    results = sidecars
    if q:
        ql = q.lower()
        results = [s for s in results if ql in _search_text(s)]
    if site_id:
        results = [s for s in results if str(s.get("site_id") or "") == site_id]
    if capture_id:
        results = [s for s in results if str(s.get("capture_id") or "") == capture_id]
    if qc_category:
        results = [s for s in results if str(s.get("qc_category") or "") == qc_category]
    if vision_category:
        results = [s for s in results if str(s.get("vision_category") or "") == vision_category]
    if vision_disagrees:
        results = [s for s in results if str(s.get("category_agreement") or "") == CATEGORY_AGREEMENT_MISMATCH]
    if area_guess:
        results = [s for s in results if str(s.get("area_guess") or "") == area_guess]
    if date_from:
        results = [s for s in results if str(s.get("generated_at") or "") >= date_from]
    if date_to:
        results = [s for s in results if str(s.get("generated_at") or "") <= date_to + "Z"]
    if has_deep_analysis:
        results = [s for s in results if isinstance(s.get("deep_analysis"), list) and s.get("deep_analysis")]
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


def _load_sidecar_field_options(cdb_config: object, field_name: str) -> list[str]:
    if cdb_config is None:
        return []
    try:
        from field_capture.photo_vision_couchdb import query_photo_vision
        values: set[str] = set()
        bookmark: object = None
        for _ in range(400):
            mango: dict[str, object] = {
                "selector": {"doc_type": "photo_vision_sidecar", field_name: {"$exists": True}},
                "fields": [field_name],
                "limit": 5000,
            }
            if bookmark:
                mango["bookmark"] = bookmark
            response = query_photo_vision(cdb_config, mango)
            docs = response.get("docs") or []
            if not isinstance(docs, list):
                return []
            values.update(str(d.get(field_name) or "").strip() for d in docs if str(d.get(field_name) or "").strip())
            bookmark = response.get("bookmark")
            if len(docs) < 5000 or not bookmark:
                break
        return sorted(values)
    except Exception as exc:
        logger.warning("could not load %s options: %s", field_name, exc)
        return []


def _load_area_options(cdb_config: object) -> list[str]:
    return _load_sidecar_field_options(cdb_config, "area_guess")


def _load_qc_category_options(cdb_config: object) -> list[str]:
    return _load_sidecar_field_options(cdb_config, "qc_category")


def _load_vision_category_options(cdb_config: object) -> list[str]:
    return _load_sidecar_field_options(cdb_config, "vision_category")


def _first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.strip() else ""


def _analysis_icon(kind: str) -> str:
    if kind == "view":
        return (
            '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z"></path>'
            '<circle cx="12" cy="12" r="3"></circle></svg>'
        )
    return (
        '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
        '<circle cx="11" cy="11" r="7"></circle><path d="m21 21-4.3-4.3"></path>'
        '<path d="M11 8v6"></path><path d="M8 11h6"></path></svg>'
    )


def _deep_analysis_payload(entries: object) -> list[dict[str, object]]:
    if not isinstance(entries, list):
        return []
    payload: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        error = entry.get("error") if isinstance(entry.get("error"), dict) else {}
        model = " / ".join(
            part
            for part in (
                str(entry.get("model_provider") or "").strip(),
                str(entry.get("model_name") or "").strip(),
            )
            if part
        )
        result = entry.get("result")
        payload.append(
            {
                "prompt": deep_analysis_prompt_label(entry),
                "prompt_id": str(entry.get("prompt_id") or "").strip(),
                "prompt_text": str(entry.get("prompt_text") or "").strip(),
                "status": str(entry.get("status") or "").strip() or "unknown",
                "model": model,
                "actor": str(entry.get("actor") or "").strip(),
                "generated_at": str(entry.get("generated_at") or "").strip(),
                "result": result,
                "result_html": render_deep_analysis_markdown(result),
                "error_type": str(error.get("type") or "").strip(),
                "error_message": str(error.get("message") or "").strip(),
            }
        )
    return payload


def _json_attr(value: object) -> str:
    text = json.dumps(value, separators=(",", ":"), sort_keys=True)
    return html.escape(text, quote=True)


def _render_deep_analysis_actions(sidecar: dict[str, object]) -> str:
    capture_id = str(sidecar.get("capture_id") or "").strip()
    photo_asset_id = str(sidecar.get("photo_asset_id") or "").strip()
    site_id = str(sidecar.get("site_id") or "").strip()
    analysis_payload = _deep_analysis_payload(sidecar.get("deep_analysis"))
    escaped_capture_id = html.escape(capture_id, quote=True)
    escaped_photo_asset_id = html.escape(photo_asset_id, quote=True)
    escaped_site_id = html.escape(site_id, quote=True)
    run_button = (
        '<button type="button" class="icon-btn" title="Run deeper analysis" aria-label="Run deeper analysis" '
        f'data-deep-analysis-run data-capture-id="{escaped_capture_id}" '
        f'data-photo-asset-id="{escaped_photo_asset_id}">{_analysis_icon("run")}</button>'
    )
    view_button = ""
    if analysis_payload:
        view_button = (
            '<button type="button" class="icon-btn" title="View deeper analysis" aria-label="View deeper analysis" '
            f'data-deep-analysis-view data-capture-id="{escaped_capture_id}" '
            f'data-photo-asset-id="{escaped_photo_asset_id}" data-site-id="{escaped_site_id}" '
            f'data-analysis="{_json_attr(analysis_payload)}">'
            f'{_analysis_icon("view")}</button>'
        )
    return (
        '<div style="display:flex;gap:6px;justify-content:flex-end;margin:0 0 8px">'
        f"{run_button}{view_button}"
        "</div>"
    )


def _media_key_from_url(url: str) -> str:
    return url.removeprefix("/media/") if url.startswith("/media/") else ""


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_part(value: object, *, fallback: str) -> str:
    text = _FILENAME_SAFE_RE.sub("-", str(value or "").strip()).strip("-._")
    return text[:80] if text else fallback


def _photo_filename_hint(sidecar: dict[str, object], index: int, media_key: str) -> str:
    area = _safe_filename_part(sidecar.get("area_guess"), fallback="area")
    stem = _safe_filename_part(Path(media_key).stem, fallback=f"photo-{index + 1:03d}")
    return f"{area}-{index + 1:03d}-{stem}.jpg"


def _render_card(
    sidecar: dict[str, object],
    submitters: dict[str, dict[str, str]],
    vault_root: Path,
    *,
    card_index: int = 0,
    show_selection_controls: bool = True,
    show_deep_analysis_controls: bool = True,
) -> str:
    capture_id = str(sidecar.get("capture_id") or "")
    provenance = sidecar.get("provenance") if isinstance(sidecar.get("provenance"), dict) else {}
    raw_url = (provenance.get("image_media_url") if isinstance(provenance, dict) else None) or sidecar.get("image_media_url")
    url = safe_media_url(raw_url)
    media_key = _media_key_from_url(url)

    area = str(sidecar.get("area_guess") or "")
    qc_category = str(sidecar.get("qc_category") or "").strip()
    vision_category = str(sidecar.get("vision_category") or "").strip()
    category_agreement = str(sidecar.get("category_agreement") or "").strip()
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

    category_text = html.escape(qc_category) if qc_category else "&mdash;"
    vision_category_text = html.escape(vision_category) if vision_category else "&mdash;"

    meta_parts = [
        f"QC Category: <strong>{category_text}</strong>",
        f"Vision: <strong>{vision_category_text}</strong>",
    ]
    if qc_category and vision_category and category_agreement:
        agreement_label = {
            "match": "matches",
            "mismatch": "disagrees",
            "unverifiable": "unverifiable",
        }.get(category_agreement, category_agreement)
        pill_class = "success" if category_agreement == "match" else "warning" if category_agreement == "mismatch" else ""
        class_attr = f"pill {pill_class}".strip()
        meta_parts.append(f'<span class="{class_attr}">Vision {html.escape(agreement_label)}</span>')
    if area:
        meta_parts.append(f"Vision area: {html.escape(area)}")
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
    if show_deep_analysis_controls:
        inner += _render_deep_analysis_actions(sidecar)
    if meta_line:
        inner += f"<p style='margin:0 0 4px'>{meta_line}</p>"
    if sub_line:
        inner += f'<p class="muted" style="margin:0 0 6px;font-size:.85rem">{sub_line}</p>'
    if description:
        inner += f'<p style="margin:0 0 6px;font-size:.9rem;line-height:1.45">{html.escape(description)}</p>'
    if pills_html:
        inner += f'<div style="margin-top:4px">{pills_html}</div>'

    selection_html = ""
    if show_selection_controls and media_key:
        filename_hint = _photo_filename_hint(sidecar, card_index, media_key)
        label = "Select photo"
        label_bits = [area, capture_id, filename_hint]
        label_detail = " · ".join(bit for bit in label_bits if bit)
        if label_detail:
            label = f"Select photo: {label_detail}"
        escaped_media_key = html.escape(media_key, quote=True)
        escaped_filename = html.escape(filename_hint, quote=True)
        selection_html = (
            '<div style="display:flex;align-items:center;gap:8px;margin:0 0 8px">'
            f'<input type="checkbox" name="media_key" value="{escaped_media_key}" '
            f'data-filename-hint="{escaped_filename}" aria-label="{html.escape(label, quote=True)}">'
            f'<span class="muted" style="font-size:.85rem">JPEG export: {html.escape(filename_hint)}</span>'
            "</div>"
        )

    return (
        f'<article style="border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)">'
        f"{img_html}"
        f'<div style="padding:10px">{selection_html}{inner}</div>'
        f"</article>"
    )


def _filename_hints_from_form(form: dict[str, list[str]]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for raw in form.get("filename_hint", []):
        key, sep, filename = raw.partition("\t")
        key = key.strip()
        if sep and key and filename.strip():
            hints[key] = _safe_zip_filename(filename.strip())
    return hints


def _safe_zip_filename(value: object) -> str:
    filename = Path(str(value or "").replace("\\", "/")).name
    stem = _safe_filename_part(Path(filename).stem, fallback="photo")
    suffix = ".jpg"
    return f"{stem}{suffix}"


def _zip_entry_name(media_key: str, hint: str, index: int, used: set[str]) -> str:
    name = _safe_zip_filename(hint) if hint else _safe_zip_filename(media_key)
    if not name or name == "photo.jpg":
        name = f"photo-{index + 1:03d}.jpg"
    base = Path(name).stem
    candidate = name
    counter = 2
    while candidate in used:
        candidate = f"{base}-{counter}.jpg"
        counter += 1
    used.add(candidate)
    return candidate


def handle_export_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    selected = [key.removeprefix("/media/").strip() for key in form.get("media_key", []) if key.strip()]
    if not selected:
        return (
            HTTPStatus.BAD_REQUEST,
            "application/json; charset=utf-8",
            json.dumps({"error": "no_photos_selected"}).encode("utf-8"),
            {},
        )

    upload_dir = ctx.runtime_root.expanduser().resolve(strict=False) / "uploads"
    resolved: list[tuple[str, str]] = []
    for key in selected:
        try:
            path = resolve_media_request(key, upload_dir)
        except UnsafeMediaPath:
            return (
                HTTPStatus.BAD_REQUEST,
                "application/json; charset=utf-8",
                json.dumps({"error": "unsafe_media_key"}).encode("utf-8"),
                {},
            )
        resolved.append((key, upload_id_for_path(path, upload_dir)))

    store = get_media_store(upload_dir)
    missing = [key for _posted_key, key in resolved if not store.exists(key)]
    if missing:
        return (
            HTTPStatus.NOT_FOUND,
            "application/json; charset=utf-8",
            json.dumps({"error": "media_not_found", "keys": missing}).encode("utf-8"),
            {},
        )

    hints = _filename_hints_from_form(form)
    used_names: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (posted_key, key) in enumerate(resolved):
            entry_name = _zip_entry_name(key, hints.get(posted_key, ""), index, used_names)
            archive.writestr(entry_name, store.read(key))

    archive_label = _safe_filename_part((form.get("capture_id") or ["selected"])[0], fallback="selected")
    headers = {
        "Content-Disposition": f'attachment; filename="qc-photos-{archive_label}.zip"',
        "Cache-Control": "no-store",
    }
    return HTTPStatus.OK, "application/zip", buffer.getvalue(), headers


def _asset_matches_filters(
    asset: object,
    *,
    q: str,
    site_id: str,
    capture_id: str = "",
    area_guess: str,
    qc_category: str = "",
    vision_category: str = "",
    vision_disagrees: bool = False,
    date_from: str = "",
    date_to: str = "",
    submitter_name: str = "",
) -> bool:
    asset_site_id = str(getattr(asset, "site_id", "") or "")
    asset_capture_id = str(getattr(asset, "capture_id", "") or "")
    captured_at = str(getattr(asset, "captured_at", "") or "")
    submitted_area = str(getattr(asset, "area", "") or "")
    if site_id and asset_site_id != site_id:
        return False
    if capture_id and asset_capture_id != capture_id:
        return False
    asset_qc_category = str(getattr(asset, "qc_category", "") or "")
    if qc_category and asset_qc_category != qc_category:
        return False
    if vision_category or vision_disagrees:
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
    terminal_capture_ids: set[str] | None = None,
    q: str,
    site_id: str,
    capture_id: str = "",
    area_guess: str,
    qc_category: str = "",
    vision_category: str = "",
    vision_disagrees: bool = False,
    date_from: str = "",
    date_to: str = "",
) -> list[dict[str, object]]:
    from field_capture import photo_vision as field_photo_vision

    intake_dir = field_photo_vision.default_intake_dir(runtime_root)
    upload_dir = field_photo_vision.default_upload_dir(runtime_root)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    disk_sidecar_ids = {str(s.get("photo_asset_id") or "") for s in load_photo_vision_sidecars(photo_vision_dir)}
    submitters = submitters_by_capture(runtime_root)
    terminal_capture_ids = terminal_capture_ids or set()

    records: list[dict[str, object]] = []
    for asset in field_photo_vision.discover_photo_assets(intake_dir, upload_dir):
        if asset.photo_asset_id in processed_asset_ids:
            continue
        if asset.capture_id in terminal_capture_ids:
            continue
        submitter_name = submitters.get(asset.capture_id, {}).get("submitter_name", "")
        if not _asset_matches_filters(
            asset,
            q=q,
            site_id=site_id,
            capture_id=capture_id,
            area_guess=area_guess,
            qc_category=qc_category,
            vision_category=vision_category,
            vision_disagrees=vision_disagrees,
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
    has_deep_analysis: bool,
    *,
    capture_id: str = "",
    site_options: list[tuple[str, str]] | None = None,
    qc_category: str = "",
    qc_category_options: list[str] | None = None,
    vision_category: str = "",
    vision_category_options: list[str] | None = None,
    vision_disagrees: bool = False,
    area_options: list[str] | None = None,
) -> str:
    site_options = site_options or []
    qc_category_options = qc_category_options or []
    vision_category_options = vision_category_options or []
    area_options = area_options or []
    site_opts = _select_options(site_options, site_id, any_label="All sites")
    qc_category_opts = _select_options([(c, c) for c in qc_category_options], qc_category, any_label="All QC categories")
    vision_category_opts = _select_options([(c, c) for c in vision_category_options], vision_category, any_label="All vision categories")
    area_opts = _select_options([(a, a) for a in area_options], area_guess, any_label="All vision areas")
    deep_checked = " checked" if has_deep_analysis else ""
    disagrees_checked = " checked" if vision_disagrees else ""
    return (
        '<form method="get" action="/field-photos" data-submit-on-change'
        ' style="display:flex;flex-wrap:wrap;gap:.5rem;align-items:flex-end">'
        f'<label style="flex:1 1 18em">Search<input name="q" value="{html.escape(q)}" placeholder="keyword…" style="width:100%"></label>'
        f'<label style="flex:1 1 14em">Site<select name="site_id" style="width:100%">{site_opts}</select></label>'
        f'<label style="flex:1 1 14em">QC Category<select name="qc_category" style="width:100%">{qc_category_opts}</select></label>'
        f'<label style="flex:1 1 14em">Vision category<select name="vision_category" style="width:100%">{vision_category_opts}</select></label>'
        f'<label style="flex:1 1 12em">Vision area<select name="area_guess" style="width:100%">{area_opts}</select></label>'
        f'<label style="flex:1 1 18em">Capture ID<input name="capture_id" value="{html.escape(capture_id, quote=True)}" placeholder="cap-…" style="width:100%"></label>'
        f'<label>From<input type="date" name="date_from" value="{html.escape(date_from)}"></label>'
        f'<label>To<input type="date" name="date_to" value="{html.escape(date_to)}"></label>'
        f'<label><input type="checkbox" name="vision_disagrees" value="1"{disagrees_checked}> Vision disagrees</label>'
        f'<label><input type="checkbox" name="deep_analysis" value="1"{deep_checked}> Flagged for deeper analysis</label>'
        '<button type="submit">Search</button>'
        "</form>"
    )


def render_filter_form(
    *,
    q: str = "",
    site_id: str = "",
    qc_category: str = "",
    vision_category: str = "",
    vision_disagrees: bool = False,
    area_guess: str = "",
    capture_id: str = "",
    date_from: str = "",
    date_to: str = "",
    has_deep_analysis: bool = False,
) -> str:
    cdb_config = _photo_vision_couchdb_config()
    site_options = _load_site_options()
    qc_category_options = _load_qc_category_options(cdb_config)
    vision_category_options = _load_vision_category_options(cdb_config)
    area_options = _load_area_options(cdb_config)
    return _filter_form(
        q,
        site_id,
        area_guess,
        date_from,
        date_to,
        has_deep_analysis,
        capture_id=capture_id,
        site_options=site_options,
        qc_category=qc_category,
        qc_category_options=qc_category_options,
        vision_category=vision_category,
        vision_category_options=vision_category_options,
        vision_disagrees=vision_disagrees,
        area_options=area_options,
    )


def _render_export_form(cards_html: str, capture_id: str) -> str:
    if not cards_html:
        return '<p class="zero-state">No photos match this query.</p>'
    return (
        '<form method="post" action="/field-photos/export" id="field-photo-export-form" data-photo-export-form>'
        f'<input type="hidden" name="capture_id" value="{html.escape(capture_id, quote=True)}">'
        '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:12px 0">'
        '<button type="button" data-select-all-photos>Select all in view</button>'
        '<button type="button" data-clear-photo-selection>Clear</button>'
        '<span class="muted" data-selected-photo-count>0 selected</span>'
        '<button type="submit" data-export-selected-photos disabled>Download selected as JPEGs</button>'
        '</div>'
        f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:12px">{cards_html}</div>'
        "</form>"
    )


def _render_selection_script() -> str:
    return """
    <script>
      document.addEventListener('DOMContentLoaded', function () {
        const form = document.querySelector('[data-photo-export-form]');
        if (!form) return;
        const checkboxes = Array.from(form.querySelectorAll('input[name="media_key"]'));
        const count = form.querySelector('[data-selected-photo-count]');
        const submit = form.querySelector('[data-export-selected-photos]');
        function selectedBoxes() {
          return checkboxes.filter(function (box) { return box.checked; });
        }
        function sync() {
          const total = selectedBoxes().length;
          if (count) count.textContent = total + ' selected';
          if (submit) submit.disabled = total === 0;
        }
        form.querySelector('[data-select-all-photos]')?.addEventListener('click', function () {
          checkboxes.forEach(function (box) { box.checked = true; });
          sync();
        });
        form.querySelector('[data-clear-photo-selection]')?.addEventListener('click', function () {
          checkboxes.forEach(function (box) { box.checked = false; });
          sync();
        });
        checkboxes.forEach(function (box) { box.addEventListener('change', sync); });
        form.addEventListener('submit', function () {
          form.querySelectorAll('input[data-generated-filename-hint]').forEach(function (node) { node.remove(); });
          selectedBoxes().forEach(function (box) {
            const hint = document.createElement('input');
            hint.type = 'hidden';
            hint.name = 'filename_hint';
            hint.value = (box.value || '') + '\\t' + (box.dataset.filenameHint || '');
            hint.setAttribute('data-generated-filename-hint', '1');
            form.appendChild(hint);
          });
        });
        sync();
      });
    </script>
    """


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
    return (
        "".join(
            _render_card(s, submitters, vault_root, show_selection_controls=False, show_deep_analysis_controls=False)
            for s in sidecars
        ),
        fallback,
    )


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


def _field_photos_return_to(query: object) -> str:
    if not isinstance(query, dict) or not query:
        return "/field-photos"
    qs = urlencode(query, doseq=True)
    return f"/field-photos?{qs}" if qs else "/field-photos"


def _render_deep_analysis_dialogs(return_to: str) -> str:
    preset_options = "".join(
        f'<option value="{html.escape(str(preset["id"]), quote=True)}">{html.escape(str(preset["label"]))}</option>'
        for preset in DEEP_ANALYSIS_PRESETS
    )
    default_actor_json = (
        json.dumps(default_actor()).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    return f"""
    <dialog id="field-photo-analysis-run-dialog" style="max-width:640px;width:min(640px,92vw)">
      <form method="post" action="/captures/analyze-deeper" class="review-action-form deep-analysis-form">
        <h3 style="margin-top:0">Run deeper analysis</h3>
        <input type="hidden" name="capture_id" value="">
        <input type="hidden" name="photo_asset_id" value="">
        <input type="hidden" name="confirm" value="1">
        <input type="hidden" name="return_to" value="{html.escape(return_to, quote=True)}">
        <label>Prompt preset
          <select name="preset_id" data-deep-analysis-preset style="width:100%">
            {preset_options}
            <option value="custom">Custom...</option>
          </select>
        </label>
        <label data-deep-analysis-custom-row hidden>Custom prompt
          <textarea name="custom_prompt" rows="5" disabled style="width:100%"></textarea>
        </label>
        <label>Actor <input type="text" name="actor" value="{html.escape(default_actor(), quote=True)}" required></label>
        <p style="display:flex;gap:8px;justify-content:flex-end;margin-bottom:0">
          <button type="button" data-close-dialog>Cancel</button>
          <button type="submit">Send</button>
        </p>
      </form>
    </dialog>
    <dialog id="field-photo-analysis-view-dialog" style="max-width:760px;width:min(760px,92vw)">
      <h3 id="field-photo-analysis-view-title" style="margin-top:0">Deeper analysis</h3>
      <div id="field-photo-analysis-view-body"></div>
      <p style="display:flex;justify-content:flex-end;margin-bottom:0">
        <button type="button" data-close-dialog>Close</button>
      </p>
    </dialog>
    <script>
      document.addEventListener('DOMContentLoaded', function () {{
        const runDialog = document.getElementById('field-photo-analysis-run-dialog');
        const viewDialog = document.getElementById('field-photo-analysis-view-dialog');
        const runForm = runDialog ? runDialog.querySelector('form') : null;
        const presetSelect = runForm ? runForm.querySelector('[data-deep-analysis-preset]') : null;
        const customRow = runForm ? runForm.querySelector('[data-deep-analysis-custom-row]') : null;
        const customPrompt = customRow ? customRow.querySelector('textarea') : null;
        const viewTitle = document.getElementById('field-photo-analysis-view-title');
        const viewBody = document.getElementById('field-photo-analysis-view-body');
        const defaultActor = {default_actor_json};
        function openDialog(dialog) {{
          if (!dialog) return;
          if (typeof dialog.showModal === 'function') dialog.showModal();
          else dialog.setAttribute('open', '');
        }}
        function closeDialog(dialog) {{
          if (!dialog) return;
          if (typeof dialog.close === 'function') dialog.close();
          else dialog.removeAttribute('open');
        }}
        function syncCustomPrompt() {{
          const custom = presetSelect && presetSelect.value === 'custom';
          if (customRow) customRow.hidden = !custom;
          if (customPrompt) {{
            customPrompt.disabled = !custom;
            if (!custom) customPrompt.value = '';
          }}
        }}
        function appendText(parent, tag, text, className) {{
          const el = document.createElement(tag);
          if (className) el.className = className;
          el.textContent = text || '';
          parent.appendChild(el);
          return el;
        }}
        function sendAnalysisToShiftReport(entry, context, button, feedback) {{
          if (!confirm('Send this analysis to today\\'s shift report?')) return;
          button.disabled = true;
          button.textContent = 'Sending...';
          if (feedback) feedback.textContent = '';
          const params = new URLSearchParams();
          params.set('content', entry.result === null || entry.result === undefined ? '' : String(entry.result));
          params.set('capture_id', context.captureId || '');
          params.set('photo_asset_id', context.photoAssetId || '');
          params.set('site_id', context.siteId || '');
          params.set('prompt_id', entry.prompt_id || '');
          params.set('prompt_label', entry.prompt || '');
          params.set('actor', defaultActor || '');
          params.set('confirm', '1');
          params.set('return_to', window.location.pathname + window.location.search);
          fetch('/captures/send-to-shift-report', {{
            method: 'POST',
            headers: {{'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}},
            body: params.toString(),
          }}).then(function (response) {{
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const responseUrl = new URL(response.url, window.location.origin);
            if (response.redirected && responseUrl.searchParams.get('message') !== 'shift_report_note_queued') {{
              throw new Error('shift report send failed');
            }}
            button.textContent = '✓ Sent';
          }}).catch(function () {{
            button.disabled = false;
            button.textContent = 'Send to shift report';
            if (feedback) feedback.textContent = 'Could not send to shift report.';
          }});
        }}
        function appendEntry(entry, context) {{
          const article = document.createElement('article');
          article.style.borderTop = '1px solid var(--line)';
          article.style.padding = '12px 0';
          const actions = document.createElement('div');
          actions.style.display = 'flex';
          actions.style.gap = '8px';
          actions.style.justifyContent = 'flex-end';
          actions.style.alignItems = 'center';
          const feedback = document.createElement('span');
          feedback.className = 'error';
          const sendButton = document.createElement('button');
          sendButton.type = 'button';
          sendButton.textContent = 'Send to shift report';
          sendButton.addEventListener('click', function () {{
            sendAnalysisToShiftReport(entry, context, sendButton, feedback);
          }});
          actions.appendChild(feedback);
          actions.appendChild(sendButton);
          article.appendChild(actions);
          appendText(article, 'h4', entry.prompt || 'Custom');
          const details = document.createElement('dl');
          [
            ['Prompt', entry.prompt_text],
            ['Status', entry.status],
            ['Model', entry.model],
            ['Actor', entry.actor],
            ['Generated', entry.generated_at],
          ].forEach(function (row) {{
            if (!row[1]) return;
            const dt = document.createElement('dt');
            const dd = document.createElement('dd');
            dt.textContent = row[0];
            dd.textContent = row[1];
            details.appendChild(dt);
            details.appendChild(dd);
          }});
          article.appendChild(details);
          if ((entry.status || '').toLowerCase() === 'failed' || entry.error_message) {{
            const bits = [entry.error_type, entry.error_message].filter(Boolean).join(': ');
            appendText(article, 'p', 'Failed: ' + (bits || 'Deep analysis failed.'), 'error');
          }}
          if (entry.result === null || entry.result === undefined || entry.result === '') {{
            appendText(article, 'p', 'No result text.', 'muted');
          }} else {{
            const result = document.createElement('div');
            result.innerHTML = entry.result_html || '';
            article.appendChild(result);
          }}
          viewBody.appendChild(article);
        }}
        presetSelect?.addEventListener('change', syncCustomPrompt);
        document.querySelectorAll('[data-deep-analysis-run]').forEach(function (button) {{
          button.addEventListener('click', function () {{
            if (!runForm) return;
            runForm.reset();
            runForm.querySelector('[name="capture_id"]').value = button.dataset.captureId || '';
            runForm.querySelector('[name="photo_asset_id"]').value = button.dataset.photoAssetId || '';
            syncCustomPrompt();
            openDialog(runDialog);
          }});
        }});
        document.querySelectorAll('[data-deep-analysis-view]').forEach(function (button) {{
          button.addEventListener('click', function () {{
            if (!viewBody) return;
            let entries = [];
            try {{ entries = JSON.parse(button.getAttribute('data-analysis') || '[]'); }} catch (_error) {{ entries = []; }}
            viewBody.textContent = '';
            if (viewTitle) viewTitle.textContent = 'Deeper analysis';
            if (!entries.length) appendText(viewBody, 'p', 'No deeper analysis entries.', 'muted');
            const context = {{
              captureId: button.dataset.captureId || '',
              photoAssetId: button.dataset.photoAssetId || '',
              siteId: button.dataset.siteId || '',
            }};
            entries.forEach(function (entry) {{ appendEntry(entry, context); }});
            openDialog(viewDialog);
          }});
        }});
        document.querySelectorAll('[data-close-dialog]').forEach(function (button) {{
          button.addEventListener('click', function () {{ closeDialog(button.closest('dialog')); }});
        }});
        syncCustomPrompt();
      }});
    </script>
    """


def render(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    runtime_root = ctx.runtime_root

    q = first_filter_value(query, "q")
    site_id = first_filter_value(query, "site_id")
    qc_category = first_filter_value(query, "qc_category")
    vision_category = first_filter_value(query, "vision_category")
    area_guess = first_filter_value(query, "area_guess")
    capture_id = first_filter_value(query, "capture_id")
    date_from = first_filter_value(query, "date_from")
    date_to = first_filter_value(query, "date_to")
    has_deep_analysis = first_filter_value(query, "deep_analysis").lower() in {"1", "true", "yes", "on"}
    vision_disagrees = first_filter_value(query, "vision_disagrees").lower() in {"1", "true", "yes", "on"}

    cdb_config = _photo_vision_couchdb_config()
    sidecars: list[dict[str, object]] = []
    fallback = False
    has_more = False

    if cdb_config is not None:
        mango = _build_mango_selector(
            q,
            site_id,
            area_guess,
            date_from,
            date_to,
            capture_id=capture_id,
            qc_category=qc_category,
            vision_category=vision_category,
            vision_disagrees=vision_disagrees,
            has_deep_analysis=has_deep_analysis,
        )
        docs = _query_couchdb(cdb_config, mango)
        if docs is not None:
            has_more = len(docs) > PAGE_LIMIT
            sidecars = docs[:PAGE_LIMIT]
        else:
            fallback = True

    processed_asset_ids: set[str] | None = None
    terminal_capture_ids: set[str] | None = None
    if cdb_config is not None and not fallback:
        processed_asset_ids = _query_processed_asset_ids(cdb_config)
        if processed_asset_ids is None:
            fallback = True
        else:
            terminal_capture_ids = _query_terminal_capture_ids(cdb_config)

    if cdb_config is None or fallback:
        from field_capture import photo_vision as field_photo_vision
        photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
        all_sidecars = load_photo_vision_sidecars(photo_vision_dir)
        sidecars = _in_memory_filter(
            all_sidecars,
            q,
            site_id,
            area_guess,
            date_from,
            date_to,
            capture_id=capture_id,
            qc_category=qc_category,
            vision_category=vision_category,
            vision_disagrees=vision_disagrees,
            has_deep_analysis=has_deep_analysis,
        )
        processed_asset_ids = {str(s.get("photo_asset_id") or "") for s in all_sidecars}

    vault_root = Path(getattr(ctx.config, "vault_root", runtime_root / "vault")).expanduser()
    submitters = submitters_by_capture(runtime_root)
    pending_records = []
    if not has_deep_analysis and not vision_category and not vision_disagrees:
        pending_records = _pending_photo_records(
            runtime_root,
            processed_asset_ids=processed_asset_ids or set(),
            terminal_capture_ids=terminal_capture_ids,
            q=q,
            site_id=site_id,
            capture_id=capture_id,
            area_guess=area_guess,
            qc_category=qc_category,
            date_from=date_from,
            date_to=date_to,
        )
    pending_html = _render_pending_section(pending_records, vault_root)

    filter_form = render_filter_form(
        q=q,
        site_id=site_id,
        qc_category=qc_category,
        vision_category=vision_category,
        vision_disagrees=vision_disagrees,
        area_guess=area_guess,
        capture_id=capture_id,
        date_from=date_from,
        date_to=date_to,
        has_deep_analysis=has_deep_analysis,
    )

    cards_html = "".join(_render_card(s, submitters, vault_root, card_index=index) for index, s in enumerate(sidecars))
    grid_html = _render_export_form(cards_html, capture_id)

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
    {_render_selection_script()}
    {_render_deep_analysis_dialogs(_field_photos_return_to(query))}
    """
    return html_page("Field Photos — BTQ Ops", body, active_section="field_photos")
