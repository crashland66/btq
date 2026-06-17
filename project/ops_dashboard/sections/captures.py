from __future__ import annotations

import html
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote, unquote

from field_capture import approved_job_drafts, audio_semantics, audio_transcription
from field_capture import photo_vision as field_photo_vision
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_path, resolve_media_request
from ops_dashboard.common import (
    UNKNOWN_SUBMITTER,
    audio_player,
    first_query_value,
    humanize_key,
    is_audio_file,
    load_photo_vision_sidecars,
    read_json_artifact,
    render_back_link,
    render_kv,
    record_section,
    render_relative_time,
    render_short_id,
    render_table,
    resolve_site_label,
    safe_media_url,
    safe_submitter,
    significant_warnings,
    string_list,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page


def intake_records(root: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    intake_dir = root / "field_capture" / "intake"
    if not intake_dir.exists():
        return records
    for path in sorted(intake_dir.glob("*.json")):
        payload, _error = read_json_artifact(path)
        if not payload:
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        capture_id = str(metadata.get("capture_id") or body.get("capture_id") or "").strip()
        if capture_id:
            records[capture_id] = {"path": path, "metadata": metadata, "payload": body}
    return records


def upload_capture_dirs(root: Path) -> list[Path]:
    uploads = root / "uploads"
    if not uploads.exists():
        return []

    def sort_mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0

    return sorted([path for path in uploads.glob("*/*") if path.is_dir()], key=sort_mtime, reverse=True)


def media_counts(path: Path) -> dict[str, int]:
    try:
        files = [item for item in path.glob("*") if item.is_file()]
    except OSError:
        files = []
    return {
        "files": len(files),
        "photos": sum(1 for item in files if (mimetypes.guess_type(item.name)[0] or "").startswith("image/")),
        "audio": sum(1 for item in files if is_audio_file(item)),
    }


def candidate_map(root: Path) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    candidate_dir = approved_job_drafts.default_candidate_dir(root)
    if not candidate_dir.exists():
        return out
    for path in sorted(candidate_dir.glob("*.json")):
        payload, _error = read_json_artifact(path)
        if not payload:
            continue
        metadata = payload.get("channel_metadata") if isinstance(payload.get("channel_metadata"), dict) else {}
        capture_id = str(metadata.get("upload_id") or "")
        if capture_id:
            payload["artifact_path"] = str(path)
            out.setdefault(capture_id, []).append(payload)
    return out


def sidecar_map(root: Path) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    sidecar_dir = root / "field_capture" / "photo_vision"
    if not sidecar_dir.exists():
        return out
    for path in sorted(sidecar_dir.glob("*.json")):
        payload, _error = read_json_artifact(path)
        if not payload:
            continue
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        capture_id = str(payload.get("capture_id") or provenance.get("capture_id") or "")
        if capture_id:
            payload["artifact_path"] = str(path)
            out.setdefault(capture_id, []).append(payload)
    return out


def draft_map(root: Path) -> dict[str, list[dict[str, object]]]:
    out: dict[str, list[dict[str, object]]] = {}
    draft_dir = approved_job_drafts.default_draft_dir(root)
    if not draft_dir.exists():
        return out
    for path in sorted(draft_dir.glob("*.json")):
        draft, _error = read_json_artifact(path)
        if not draft:
            continue
        candidate_id = str(draft.get("candidate_id") or "")
        if candidate_id:
            draft["draft_id"] = str(draft.get("draft_id") or path.stem)
            draft["queue_state"] = "not_staged"
            out.setdefault(candidate_id, []).append(draft)
    return out


def capture_records(root: Path) -> list[dict[str, object]]:
    intake = intake_records(root)
    candidates = candidate_map(root)
    sidecars = sidecar_map(root)
    rows = []
    for capture_dir in upload_capture_dirs(root):
        capture_id = capture_dir.name
        record = intake.get(capture_id, {})
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        body = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        submitter = safe_submitter(metadata, body)
        counts = media_counts(capture_dir)
        rows.append({"capture_id": capture_id, "date": capture_dir.parent.name, "path": capture_dir, "site": str(metadata.get("site_id") or body.get("site_id") or ""), "area": str(body.get("area") or ""), "submitter": submitter["submitter_name"], "captured_at": str(body.get("captured_at") or metadata.get("captured_at") or capture_dir.parent.name), "counts": counts, "candidate_count": len(candidates.get(capture_id, [])), "sidecar_count": len(sidecars.get(capture_id, []))})
    return rows


def filter_records(rows: list[dict[str, object]], query: dict[str, list[str]]) -> list[dict[str, object]]:
    site = first_query_value(query, "site").strip()
    date_from = first_query_value(query, "date_from").strip()
    date_to = first_query_value(query, "date_to").strip()
    has_photo = first_query_value(query, "has_photo")
    has_audio = first_query_value(query, "has_audio")
    has_candidate = first_query_value(query, "has_candidate")
    has_sidecar = first_query_value(query, "has_vision_sidecar")
    if site:
        rows = [row for row in rows if row["site"] == site]
    if date_from:
        rows = [row for row in rows if str(row["date"]) >= date_from]
    if date_to:
        rows = [row for row in rows if str(row["date"]) <= date_to]
    if has_photo:
        rows = [row for row in rows if row["counts"]["photos"] > 0]
    if has_audio:
        rows = [row for row in rows if row["counts"]["audio"] > 0]
    if has_candidate:
        rows = [row for row in rows if int(row["candidate_count"]) > 0]
    if has_sidecar:
        rows = [row for row in rows if int(row["sidecar_count"]) > 0]
    return rows[:100]


def plain_site_label(site_id: str, vault_dir: Path) -> str:
    return re.sub(r"<[^>]+>", "", resolve_site_label(site_id, vault_dir))


def render_list(ctx: object) -> str:
    root = ctx.runtime_root
    query = getattr(ctx, "query", {})
    vault_dir = ctx.config.vault_dir
    all_rows = capture_records(root)
    rows = filter_records(all_rows, query)
    table = render_table(
        rows,
        [
            {"key": "capture_id", "label": "Capture", "format": lambda value, _row: f'<a href="/captures?capture_id={quote(str(value))}">{render_short_id(value)}</a>', "nowrap": True},
            {"key": "site", "label": "Site", "format": lambda value, _row: resolve_site_label(value, vault_dir), "nowrap": True},
            {"key": "submitter", "label": "Submitter", "priority": 2},
            {"key": "captured_at", "label": "Time", "format": lambda value, _row: render_relative_time(value), "nowrap": True},
            {"key": "counts", "label": "Uploads", "format": lambda value, _row: f'{value["files"]} files; {value["photos"]} photos; {value["audio"]} audio' if isinstance(value, dict) else "", "priority": 2, "nowrap": True},
            {"key": "candidate_count", "label": "Candidates", "priority": 3, "nowrap": True},
            {"key": "sidecar_count", "label": "Sidecars", "priority": 3, "nowrap": True},
        ],
        empty_text="No captures match this filter.",
    )
    known_site_options = "".join(
        f'<option value="{html.escape(site_id)}">{html.escape(plain_site_label(site_id, vault_dir))}</option>'
        for site_id in sorted({str(row.get("site") or "") for row in all_rows if str(row.get("site") or "")})
    )
    filters = f"""
    <form method="get" action="/captures" data-submit-on-change>
      <label>Site<input name="site" list="captures-known-sites" value="{html.escape(first_query_value(query, "site"))}"></label>
      <datalist id="captures-known-sites">{known_site_options}</datalist>
      <label>Date from<input type="date" name="date_from" value="{html.escape(first_query_value(query, "date_from"))}"></label>
      <label>Date to<input type="date" name="date_to" value="{html.escape(first_query_value(query, "date_to"))}"></label>
      <label><input type="checkbox" name="has_photo" value="1" {"checked" if first_query_value(query, "has_photo") else ""}> Has photo</label>
      <label><input type="checkbox" name="has_audio" value="1" {"checked" if first_query_value(query, "has_audio") else ""}> Has audio</label>
      <label><input type="checkbox" name="has_candidate" value="1" {"checked" if first_query_value(query, "has_candidate") else ""}> Has candidate</label>
      <label><input type="checkbox" name="has_vision_sidecar" value="1" {"checked" if first_query_value(query, "has_vision_sidecar") else ""}> Has vision sidecar</label>
      <button type="submit">Filter</button>
    </form>
    """
    body = f"<header><h1>Captures</h1><p class=\"muted\">Read-only browse of recent field captures.</p></header><div class=\"content-with-rail\"><aside class=\"filter-rail\"><section><h2>Filters</h2>{filters}</section></aside><section><h2>Recent captures</h2>{table}</section></div>"
    return html_page("Captures", body, active_section="captures")


def _missing_sidecar_record(asset: field_photo_vision.FieldPhotoAsset, submitter: dict[str, str]) -> dict[str, object]:
    return {
        "status": "missing",
        "capture_id": asset.capture_id,
        "photo_asset_id": asset.photo_asset_id,
        "photo_id": asset.photo_id,
        "site_id": asset.site_id,
        "submitted_area": asset.area,
        "submitted_phase": asset.phase,
        "captured_at": asset.captured_at,
        "submitter_name": submitter["submitter_name"],
        "submitter_id": submitter["submitter_id"],
        "source_image_path": str(asset.image_path),
        "image_media_url": asset.image_media_url,
        "area_guess": "",
        "description": "No vision description yet.",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": "",
        "model_name": "",
        "model_provider": "",
        "generated_at": "",
        "warnings": [],
        "error": {},
    }


def photo_card_records_for_capture(root: Path, capture_id: str) -> list[dict[str, object]]:
    """Build one photo-vision record per photo asset in a capture, merging
    sidecar fields when available and surfacing 'missing' status when not.
    Orphan sidecars (those whose photo_asset_id doesn't match any discovered
    asset) for this capture are appended so failed/legacy records don't
    silently disappear.
    """
    intake_dir = field_photo_vision.default_intake_dir(root)
    upload_dir = field_photo_vision.default_upload_dir(root)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(root)
    submitter = submitters_by_capture(root).get(capture_id, safe_submitter({}))
    sidecars = load_photo_vision_sidecars(photo_vision_dir)
    sidecars_by_asset = {str(s.get("photo_asset_id") or ""): s for s in sidecars if s.get("photo_asset_id")}

    records: list[dict[str, object]] = []
    seen_asset_ids: set[str] = set()
    try:
        discovered_assets = field_photo_vision.discover_photo_assets(intake_dir, upload_dir, capture_id=capture_id)
    except Exception:  # noqa: BLE001 - capture detail should still render if a best-effort media scan races the filesystem.
        discovered_assets = []
    for asset in discovered_assets:
        seen_asset_ids.add(asset.photo_asset_id)
        sidecar = sidecars_by_asset.get(asset.photo_asset_id)
        if sidecar:
            record = dict(sidecar)
            record.update(
                {
                    "capture_id": record.get("capture_id") or asset.capture_id,
                    "photo_asset_id": asset.photo_asset_id,
                    "site_id": record.get("site_id") or asset.site_id,
                    "submitted_area": record.get("submitted_area") or asset.area,
                    "submitted_phase": record.get("submitted_phase") or asset.phase,
                    "captured_at": record.get("captured_at") or asset.captured_at,
                    "source_image_path": record.get("source_image_path") or str(asset.image_path),
                    "image_media_url": record.get("image_media_url") or asset.image_media_url,
                    "submitter_name": record.get("submitter_name") or submitter["submitter_name"],
                    "submitter_id": record.get("submitter_id") or submitter["submitter_id"],
                }
            )
        else:
            record = _missing_sidecar_record(asset, submitter)
        note = asset.note.strip()
        if note:
            record["note"] = note
        records.append(record)
    for sidecar in sidecars:
        asset_id = str(sidecar.get("photo_asset_id") or "")
        if not asset_id or asset_id in seen_asset_ids:
            continue
        if str(sidecar.get("capture_id") or "") != capture_id:
            continue
        record = dict(sidecar)
        record.setdefault("submitter_name", submitter["submitter_name"])
        record.setdefault("submitter_id", submitter["submitter_id"])
        records.append(record)
    records.sort(key=lambda item: (str(item.get("captured_at") or ""), str(item.get("photo_asset_id") or "")))
    return records


def _is_image_file(path: Path) -> bool:
    try:
        return path.is_file() and (mimetypes.guess_type(path.name)[0] or "").startswith("image/")
    except OSError:
        return False


def _served_media_url(media_url: object, upload_root: Path) -> str:
    url = safe_media_url(media_url)
    if not url:
        relative = str(media_url or "").strip().lstrip("/")
        if relative and ".." not in relative:
            url = safe_media_url(f"/media/{quote(relative, safe='/')}")
    if not url:
        return ""
    try:
        media_path = resolve_media_request(url.removeprefix("/media/"), upload_root)
    except (OSError, UnsafeMediaPath):
        return ""
    return url if _is_image_file(media_path) else ""


def _upload_relative_from_text(value: str) -> Path | None:
    parts = Path(value).parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "uploads" and index + 1 < len(parts):
            return Path(*parts[index + 1 :])
    return None


def _media_url_from_source_path(source_path: object, upload_root: Path) -> str:
    raw_path = str(source_path or "").strip()
    if not raw_path:
        return ""
    try:
        media_path = resolve_media_path(raw_path, upload_root)
    except (OSError, UnsafeMediaPath):
        relative = _upload_relative_from_text(raw_path)
        if relative is None:
            return ""
        try:
            media_path = resolve_media_path(str(relative), upload_root)
        except (OSError, UnsafeMediaPath):
            return ""
    if not _is_image_file(media_path):
        return ""
    try:
        relative_path = media_path.resolve(strict=False).relative_to(upload_root.expanduser().resolve(strict=False))
    except ValueError:
        return ""
    return safe_media_url(f"/media/{quote(relative_path.as_posix(), safe='/')}")


def _media_url_from_capture_file(capture_id: object, filename: object, upload_root: Path) -> str:
    capture = str(capture_id or "").strip()
    name = Path(str(filename or "").strip().replace("\\", "/")).name
    if not capture or not name:
        return ""
    try:
        date_dirs = [path for path in upload_root.glob("*") if path.is_dir()]
    except OSError:
        return ""
    for date_dir in date_dirs:
        media_path = date_dir / capture / name
        if not _is_image_file(media_path):
            continue
        try:
            relative_path = media_path.resolve(strict=False).relative_to(upload_root)
        except ValueError:
            continue
        return safe_media_url(f"/media/{quote(relative_path.as_posix(), safe='/')}")
    return ""


def _filename_from_media_reference(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return Path(unquote(text.removeprefix("/media/")).replace("\\", "/")).name


def resolved_photo_media_url(record: dict[str, object], root: Path | None = None) -> str:
    if root is None:
        return safe_media_url(record.get("image_media_url"))
    upload_root = root.expanduser().resolve(strict=False) / "uploads"
    for value in (record.get("image_media_url"), record.get("photo_id")):
        media_url = _served_media_url(value, upload_root)
        if media_url:
            return media_url
    filename = _filename_from_media_reference(record.get("image_media_url")) or _filename_from_media_reference(record.get("photo_id"))
    media_url = _media_url_from_capture_file(record.get("capture_id"), filename, upload_root)
    if media_url:
        return media_url
    return _media_url_from_source_path(record.get("source_image_path"), upload_root)


def render_photo_preview(record: dict[str, object], root: Path | None = None) -> str:
    media_url = resolved_photo_media_url(record, root)
    if media_url:
        js_arg = html.escape(json.dumps(media_url), quote=True)
        escaped = html.escape(media_url, quote=True)
        return (
            f'<a href="#" onclick="openLb({js_arg});return false" title="View full size" style="cursor:zoom-in;display:block">'
            f'<img src="{escaped}" alt="Field capture photo preview"'
            f' loading="lazy" class="site-gallery-thumb">'
            f"</a>"
        )
    return '<p class="muted">No safe image preview available.</p>'


def _without_empty_values(values: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in values.items()
        if value is not None and (not isinstance(value, str) or bool(value.strip()))
    }


def _vision_status_label(value: object) -> object:
    return "Awaiting analysis." if str(value or "").strip().lower() == "missing" else value


def _vision_description_label(value: object) -> object:
    return "Awaiting analysis." if str(value or "").strip() == "No vision description yet." else value


def _status_slug(value: object) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower()).strip("_")


def _pill(label: str, *, status: str = "") -> str:
    status_class = f" status-{html.escape(_status_slug(status), quote=True)}" if status else ""
    return f'<span class="pill{status_class}">{html.escape(label)}</span>'


def _photo_status(record: dict[str, object]) -> tuple[str, str]:
    status = str(record.get("status") or "").strip().lower()
    if not status or status == "missing":
        return "Awaiting analysis", "pending"
    if status in {"completed", "complete", "processed", "succeeded", "success"}:
        return "Analyzed", "resolved"
    if status in {"failed", "malformed", "error"}:
        return humanize_key(status), "failed"
    return humanize_key(status), status


def _overall_capture_status(photo_records: list[dict[str, object]], *, photo_count: int = 0) -> tuple[str, str]:
    if not photo_records:
        if photo_count:
            return "Awaiting analysis", "pending"
        return "No photos", "no_action_needed"
    statuses = [_photo_status(record)[0].lower() for record in photo_records]
    if any(status in {"failed", "malformed", "error"} for status in statuses):
        return "Needs review", "failed"
    if any(status == "awaiting analysis" for status in statuses):
        return "Awaiting analysis", "pending"
    return "Analyzed", "resolved"


def _photo_description(record: dict[str, object]) -> str:
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    status = str(record.get("status") or "").strip().lower()
    if status in {"failed", "malformed"}:
        message = str(error.get("message") or "").strip()
        return f"Vision analysis did not complete: {message}" if message else "Vision analysis did not complete."
    description = str(_vision_description_label(record.get("description", "")) or "").strip()
    if description and description != "Awaiting analysis.":
        return description
    return "Awaiting analysis." if not status or status == "missing" else "No vision description available."


def _photo_note_html(record: dict[str, object]) -> str:
    note = str(record.get("note") or "").strip()
    if not note:
        return ""
    escaped_note = html.escape(note).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return f'<p class="subline site-gallery-meta"><b>Photo note</b><br>{escaped_note}</p>'


def render_photo_card(record: dict[str, object], root: Path | None = None) -> str:
    title = (
        str(record.get("area_guess") or "").strip()
        or str(record.get("submitted_area") or "").strip()
        or "Photo"
    )
    description = _photo_description(record)
    status_label, status_kind = _photo_status(record)
    meta_parts = [
        html.escape(str(record.get("submitted_phase") or "").strip()),
        render_relative_time(str(record.get("captured_at") or "")),
    ]
    meta = " &middot; ".join(part for part in meta_parts if part)
    meta_html = (
        f'<p class="subline site-gallery-meta">{meta}</p>'
        if meta
        else '<p class="subline site-gallery-meta">&mdash;</p>'
    )
    return f"""
    <article class="site-gallery-card">
      {render_photo_preview(record, root)}
      <span class="site-gallery-card-body">
        <strong>{html.escape(title)}</strong>
        {meta_html}
        {_photo_note_html(record)}
        <p>{html.escape(description)}</p>
        {_pill(status_label, status=status_kind)}
      </span>
    </article>
    """


def render_photo_processing_details(record: dict[str, object]) -> str:
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    values = _without_empty_values(
        {
            "photo_asset_id": record.get("photo_asset_id", ""),
            "submitter": record.get("submitter_name", UNKNOWN_SUBMITTER),
            "submitter_id": record.get("submitter_id", ""),
            "submitted_area": record.get("submitted_area", ""),
            "submitted_phase": record.get("submitted_phase", ""),
            "captured_at": record.get("captured_at", ""),
            "image_media_url": safe_media_url(record.get("image_media_url")) or "",
            "site_context": record.get("site_context_summary", ""),
            "vision_status": _vision_status_label(record.get("status", "")),
            "area_guess": record.get("area_guess", ""),
            "description": _vision_description_label(record.get("description", "")),
            "visible_objects": ", ".join(string_list(record.get("visible_objects"))),
            "possible_conditions": ", ".join(string_list(record.get("possible_conditions"))),
            "possible_issues": ", ".join(string_list(record.get("possible_issues"))),
            "warnings": ", ".join(significant_warnings(record.get("warnings"))),
            "model_name": record.get("model_name", ""),
            "confidence": record.get("confidence", ""),
            "error_type": error.get("type", ""),
            "error_message": error.get("message", ""),
            "can_retry": error.get("can_retry", ""),
        }
    )
    title = (
        str(record.get("area_guess") or "").strip()
        or str(record.get("submitted_area") or "").strip()
        or str(record.get("photo_asset_id") or "").strip()
        or "Photo"
    )
    return f"""
    <article>
      <h4>{html.escape(title)}</h4>
      {render_kv(values)}
    </article>
    """


def render_photo_processing_section(photo_records: list[dict[str, object]]) -> str:
    if not photo_records:
        return '<section><h3>Photo vision details</h3><p class="muted">No photos discovered for this capture.</p></section>'
    return f'<section><h3>Photo vision details</h3>{"".join(render_photo_processing_details(record) for record in photo_records)}</section>'


def _capture_meta_chips(detail_record: dict[str, object], photo_records: list[dict[str, object]]) -> str:
    photo_count = int(detail_record.get("photo_count") or 0)
    photo_label = f"{photo_count} photo" if photo_count == 1 else f"{photo_count} photos"
    captured_at = str(detail_record.get("captured_at") or "").strip()
    date_label = render_relative_time(captured_at) or html.escape(captured_at or "Date unknown")
    status_label, status_kind = _overall_capture_status(photo_records, photo_count=photo_count)
    return (
        '<p class="subline">'
        f'{_pill(photo_label)}'
        f'<span class="pill">{date_label}</span>'
        f'{_pill(status_label, status=status_kind)}'
        "</p>"
    )


def _details_block(title: str, body: str, *, count: int | None = None) -> str:
    count_html = ""
    if count is not None:
        count_html = (
            '<span class="details-count">'
            f'{html.escape(str(count))} {html.escape("record" if count == 1 else "records")}'
            "</span>"
        )
    return (
        '<details class="detail-block">'
        f'<summary><span>{html.escape(title)}</span>{count_html}</summary>'
        f'<div class="detail-block-body">{body}</div>'
        "</details>"
    )


def _friendly_extraction_summary(
    candidates: list[dict[str, object]],
    drafts_by_candidate: dict[str, list[dict[str, object]]],
) -> str:
    if not candidates:
        return '<section><h3>Extraction summary</h3><p>No issues flagged - routine QC.</p></section>'

    candidate_count = len(candidates)
    draft_count = sum(len(drafts_by_candidate.get(str(candidate.get("candidate_id") or ""), [])) for candidate in candidates)
    candidate_word = "follow-up" if candidate_count == 1 else "follow-ups"
    if draft_count:
        state_counts: dict[str, int] = {}
        for candidate in candidates:
            for draft in drafts_by_candidate.get(str(candidate.get("candidate_id") or ""), []):
                state = humanize_key(draft.get("queue_state") or "not_staged").lower()
                state_counts[state] = state_counts.get(state, 0) + 1
        states = ", ".join(f"{count} {state}" for state, count in sorted(state_counts.items()))
        tail = f"{draft_count} draft{'s' if draft_count != 1 else ''} {states}"
    else:
        statuses = sorted(
            {
                humanize_key(candidate.get("status") or "in_review").lower()
                for candidate in candidates
            }
        )
        tail = ", ".join(statuses) or "in review"
    return (
        '<section><h3>Extraction summary</h3>'
        f'<p>Extracted: {candidate_count} {candidate_word} proposed &rarr; {html.escape(tail)}.</p>'
        "</section>"
    )


def _candidate_links(candidates: list[dict[str, object]]) -> str:
    return "".join(
        f'<li><a href="/candidates?candidate_id={quote(str(item.get("candidate_id") or ""))}">{html.escape(str(item.get("summary") or item.get("candidate_id") or ""))}</a></li>'
        for item in candidates
    ) or "<li>No candidates.</li>"


def _draft_links(candidates: list[dict[str, object]], drafts: dict[str, list[dict[str, object]]]) -> str:
    links = []
    for candidate in candidates:
        for draft in drafts.get(str(candidate.get("candidate_id") or ""), []):
            links.append(f'<li><a href="/drafts?draft_id={quote(str(draft.get("draft_id") or ""))}">{html.escape(str(draft.get("draft_id") or ""))}</a> {html.escape(humanize_key(draft.get("queue_state") or ""))}</li>')
    return "".join(links) or "<li>No drafts.</li>"


def _other_uploads_section(root: Path, upload_dir: Path | None) -> str:
    other_uploads = ""
    if upload_dir:
        for item in sorted(upload_dir.glob("*")):
            if not item.is_file():
                continue
            if (mimetypes.guess_type(item.name)[0] or "").startswith("image/"):
                continue
            media_url = f"/media/{item.relative_to(root / 'uploads')}"
            other_uploads += f'<li><a href="{html.escape(media_url)}">{html.escape(item.name)}</a></li>'
    return f"<section><h3>Other uploads</h3><ul>{other_uploads or '<li>No other uploads.</li>'}</ul></section>"


def render_voice_processing_section(root: Path, audio_assets: list[audio_transcription.FieldAudioAsset]) -> str:
    if not audio_assets:
        return '<section><h3>Voice note processing</h3><p class="muted">No voice note.</p></section>'

    transcript_dir = audio_transcription.default_transcript_dir(root)
    cards = []
    for asset in audio_assets:
        transcript_path = audio_transcription.transcript_path_for(transcript_dir, asset.audio_asset_id)
        transcript, _error = read_json_artifact(transcript_path)
        details = render_kv(
            {
                "audio_asset_id": asset.audio_asset_id,
                "filename": asset.audio_filename,
                "transcript_status": str(transcript.get("status") or "pending") if transcript else "pending",
            }
        )
        cards.append(
            f"""
            <article>
              <h4>{html.escape(asset.audio_filename or asset.audio_asset_id)}</h4>
              {details}
            </article>
            """
        )
    return f'<section><h3>Voice note processing</h3>{"".join(cards)}</section>'


def _processing_details_section(
    root: Path,
    upload_dir: Path | None,
    detail_record: dict[str, object],
    photo_records: list[dict[str, object]],
    audio_assets: list[audio_transcription.FieldAudioAsset],
    candidates: list[dict[str, object]],
    drafts: dict[str, list[dict[str, object]]],
) -> str:
    capture_details = record_section(
        "Capture details",
        detail_record,
        (
            "capture_id",
            "site",
            "area",
            "submitter",
            "captured_at",
            "vision_status",
            "photo_count",
            "candidate_count",
            "sidecar_count",
        ),
    )
    candidate_links = _candidate_links(candidates)
    draft_links = _draft_links(candidates, drafts)
    body = f"""
    {capture_details}
    {render_photo_processing_section(photo_records)}
    {_other_uploads_section(root, upload_dir)}
    {render_voice_processing_section(root, audio_assets)}
    {render_semantic_results_section(root, audio_assets)}
    <section><h3>Action candidates</h3><ul>{candidate_links}</ul></section>
    <section><h3>Drafts and queue state</h3><ul>{draft_links}</ul></section>
    """
    count = int(detail_record.get("sidecar_count") or 0) + len(candidates)
    return _details_block("Processing details", body, count=count)


def capture_detail_record(
    root: Path,
    capture_id: str,
    upload_dir: Path | None,
    candidates: list[dict[str, object]],
    photo_records: list[dict[str, object]],
) -> dict[str, object]:
    record = intake_records(root).get(capture_id, {})
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    body = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    submitter = safe_submitter(metadata, body)
    counts = media_counts(upload_dir) if upload_dir else {"files": 0, "photos": 0, "audio": 0}
    vision_statuses = sorted(
        {
            str(photo.get("status") or "").strip()
            for photo in photo_records
            if str(photo.get("status") or "").strip()
        }
    )
    return {
        "capture_id": capture_id,
        "site": str(metadata.get("site_id") or body.get("site_id") or ""),
        "area": str(body.get("area") or body.get("qc_category") or metadata.get("area") or metadata.get("qc_category") or ""),
        "submitter": submitter["submitter_name"],
        "captured_at": str(body.get("captured_at") or metadata.get("captured_at") or (upload_dir.parent.name if upload_dir else "")),
        "vision_status": vision_statuses[0] if len(vision_statuses) == 1 else vision_statuses,
        "photo_count": counts["photos"],
        "candidate_count": len(candidates),
        "sidecar_count": len(sidecar_map(root).get(capture_id, [])),
    }


def audio_assets_for_capture(root: Path, capture_id: str) -> list[audio_transcription.FieldAudioAsset]:
    return [
        asset
        for asset in audio_transcription.discover_audio_assets(
            audio_transcription.default_intake_dir(root),
            audio_transcription.default_upload_dir(root),
        )
        if asset.upload_id == capture_id or Path(asset.upload_id).name == capture_id
    ]


def _plain_text_block(value: object, *, empty_text: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return f'<p class="muted">{html.escape(empty_text)}</p>' if empty_text else ""
    return f"<pre>{html.escape(text)}</pre>"


def _transcript_status_text(transcript: dict[str, object] | None) -> str:
    if not transcript:
        return "Transcript pending."
    status = str(transcript.get("status") or "pending").strip() or "pending"
    return f"Transcript {humanize_key(status)}."


def render_voice_transcript_section(root: Path, audio_assets: list[audio_transcription.FieldAudioAsset]) -> str:
    if not audio_assets:
        return '<section><h3>Voice note & transcript</h3><p class="muted">No voice note.</p></section>'

    transcript_dir = audio_transcription.default_transcript_dir(root)
    cards = []
    for asset in audio_assets:
        transcript_path = audio_transcription.transcript_path_for(transcript_dir, asset.audio_asset_id)
        transcript, _error = read_json_artifact(transcript_path)
        raw_text = str(transcript.get("raw_text") or "").strip() if transcript else ""
        transcript_html = _plain_text_block(raw_text) if raw_text else f'<p class="muted">{html.escape(_transcript_status_text(transcript))}</p>'
        transcript_status = str(transcript.get("status") or "pending") if transcript else "pending"
        status_html = f'<p class="subline">{html.escape(_transcript_status_text({"status": transcript_status}))}</p>' if raw_text else ""
        cards.append(
            f"""
            <article>
              <h3>{html.escape(asset.audio_filename or asset.audio_asset_id)}</h3>
              {status_html}
              {audio_player(asset.audio_media_url)}
              {transcript_html}
            </article>
            """
        )
    return f'<section><h3>Voice note & transcript</h3>{"".join(cards)}</section>'


def _render_semantic_actions(actions: object) -> object:
    if not isinstance(actions, list):
        return []
    rendered: list[object] = []
    for action in actions:
        if isinstance(action, dict):
            rendered.append({str(key): value for key, value in action.items()})
        else:
            rendered.append(action)
    return rendered


def render_semantic_results_section(root: Path, audio_assets: list[audio_transcription.FieldAudioAsset]) -> str:
    if not audio_assets:
        return '<section><h3>Semantic results</h3><p class="muted">No voice note.</p></section>'

    semantic_dir = audio_semantics.default_semantic_dir(root)
    cards = []
    for asset in audio_assets:
        semantic_path = audio_semantics.semantic_path_for(semantic_dir, asset.audio_asset_id)
        semantic, _error = read_json_artifact(semantic_path)
        if not semantic:
            body = '<p class="muted">Semantic pending.</p>'
        else:
            body = render_kv(
                {
                    "semantic_status": semantic.get("status", ""),
                    "operational_summary": semantic.get("operational_summary", ""),
                    "cleaned_internal_note": semantic.get("cleaned_internal_note", ""),
                    "client_safe_note": semantic.get("client_safe_note", ""),
                    "extracted_actions": _render_semantic_actions(semantic.get("extracted_actions")),
                    "issue_detected": semantic.get("issue_detected", ""),
                    "issue_type": semantic.get("issue_type", ""),
                    "urgency": semantic.get("urgency", ""),
                    "visit_proposed": semantic.get("visit_proposed", ""),
                    "visit_type": semantic.get("visit_type", ""),
                    "suggested_tags": semantic.get("suggested_tags") if isinstance(semantic.get("suggested_tags"), list) else [],
                }
            )
        cards.append(
            f"""
            <article>
              <h3>{html.escape(asset.audio_filename or asset.audio_asset_id)}</h3>
              {body}
            </article>
            """
        )
    return f'<section><h3>Semantic results</h3>{"".join(cards)}</section>'


def render_detail(ctx: object, capture_id: str) -> str:
    root = ctx.runtime_root
    upload_dir = next((path for path in upload_capture_dirs(root) if path.name == capture_id), None)
    candidates = candidate_map(root).get(capture_id, [])
    drafts = draft_map(root)

    photo_records = photo_card_records_for_capture(root, capture_id)
    photos_html = (
        f'<div class="site-gallery">{"".join(render_photo_card(record, root) for record in photo_records)}</div>'
        if photo_records
        else '<p class="zero-state">No photos in this capture.</p>'
    )
    audio_assets = audio_assets_for_capture(root, capture_id)
    voice_transcript_html = render_voice_transcript_section(root, audio_assets)
    detail_record = capture_detail_record(root, capture_id, upload_dir, candidates, photo_records)
    vault_dir = getattr(getattr(ctx, "config", None), "vault_dir", root)
    site_label = html.unescape(plain_site_label(str(detail_record.get("site") or ""), vault_dir))
    raw_site = str(detail_record.get("site") or "")
    if raw_site and site_label.endswith(f"({raw_site})"):
        site_label = site_label[: -len(f"({raw_site})")].strip()
    submitter_label = str(detail_record.get("submitter") or UNKNOWN_SUBMITTER)
    header_parts = [part for part in (site_label, submitter_label) if part]
    header_title = " &mdash; ".join(html.escape(part) for part in header_parts) or "Capture Detail"
    processing_details_html = _processing_details_section(root, upload_dir, detail_record, photo_records, audio_assets, candidates, drafts)
    extraction_summary_html = _friendly_extraction_summary(candidates, drafts)
    body = f"""
    <header><h1>{header_title}</h1><p class="muted"><code>{render_short_id(capture_id)}</code></p>{_capture_meta_chips(detail_record, photo_records)}</header>
    {render_back_link("/captures", "Back to Captures")}
    <section><h3>Photos</h3>{photos_html}</section>
    {voice_transcript_html}
    {extraction_summary_html}
    {processing_details_html}
    """
    return html_page("Capture Detail", body, active_section="captures")


def render(ctx: object) -> str:
    capture_id = first_query_value(getattr(ctx, "query", {}), "capture_id").strip()
    if capture_id:
        return render_detail(ctx, capture_id)
    return render_list(ctx)
