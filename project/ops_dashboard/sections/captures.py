from __future__ import annotations

import html
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from field_capture import approved_job_drafts, audio_semantics, audio_transcription
from field_capture import photo_vision as field_photo_vision
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
    return sorted([path for path in uploads.glob("*/*") if path.is_dir()], key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)


def media_counts(path: Path) -> dict[str, int]:
    files = [item for item in path.glob("*") if item.is_file()]
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
    body = f"<header><h1>Captures</h1><p class=\"muted\">Read-only capture browse. Coming in prompt sections for sites/tokens/system remain separate.</p></header><div class=\"content-with-rail\"><aside class=\"filter-rail\"><section><h2>Filters</h2>{filters}</section></aside><section><h2>Recent captures</h2>{table}</section></div>"
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
    for asset in field_photo_vision.discover_photo_assets(intake_dir, upload_dir, capture_id=capture_id):
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


def render_photo_preview(record: dict[str, object]) -> str:
    media_url = safe_media_url(record.get("image_media_url"))
    if media_url:
        js_arg = html.escape(json.dumps(media_url), quote=True)
        escaped = html.escape(media_url, quote=True)
        return (
            f'<div><a href="#" onclick="openLb({js_arg});return false" title="View full size" style="cursor:zoom-in">'
            f'<img src="{escaped}" alt="Field capture photo preview"'
            f' style="max-width:220px;max-height:180px;object-fit:contain;border:1px solid #d9e2ec;border-radius:6px;background:#f8fafc;">'
            f'</a></div>'
        )
    if record.get("source_image_path"):
        return '<p class="muted">Image path available locally.</p>'
    return '<p class="muted">No safe image preview available.</p>'


def render_photo_card(record: dict[str, object]) -> str:
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    failure = ""
    if record.get("status") in {"failed", "malformed"}:
        failure = render_kv(
            {
                "error_type": error.get("type", ""),
                "error_message": error.get("message", ""),
                "can_retry": error.get("can_retry", ""),
            }
        )
    details = render_kv(
        {
            "photo_asset_id": record.get("photo_asset_id", ""),
            "submitter": record.get("submitter_name", UNKNOWN_SUBMITTER),
            "submitter_id": record.get("submitter_id", ""),
            "submitted_area": record.get("submitted_area", ""),
            "submitted_phase": record.get("submitted_phase", ""),
            "captured_at": record.get("captured_at", ""),
            "image_media_url": safe_media_url(record.get("image_media_url")) or "",
            "site_context": record.get("site_context_summary", ""),
            "vision_status": record.get("status", ""),
            "area_guess": record.get("area_guess", ""),
            "description": record.get("description", ""),
            "visible_objects": ", ".join(string_list(record.get("visible_objects"))),
            "possible_conditions": ", ".join(string_list(record.get("possible_conditions"))),
            "possible_issues": ", ".join(string_list(record.get("possible_issues"))),
            "warnings": ", ".join(significant_warnings(record.get("warnings"))),
            "model_name": record.get("model_name", ""),
            "confidence": record.get("confidence", ""),
        }
    )
    title = str(record.get("area_guess") or record.get("status") or record.get("photo_asset_id") or "Photo")
    return f"""
    <article>
      <h3>{html.escape(title)}</h3>
      {render_photo_preview(record)}
      {details}
      {failure}
    </article>
    """


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
              <h3>{html.escape(asset.audio_filename or asset.audio_asset_id)}</h3>
              {audio_player(asset.audio_media_url)}
              {details}
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
    photos_html = "".join(render_photo_card(record) for record in photo_records) or "<p>No photos in this capture.</p>"
    audio_assets = audio_assets_for_capture(root, capture_id)
    voice_transcript_html = render_voice_transcript_section(root, audio_assets)
    semantic_results_html = render_semantic_results_section(root, audio_assets)
    capture_details = record_section(
        "Capture details",
        capture_detail_record(root, capture_id, upload_dir, candidates, photo_records),
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

    other_uploads = ""
    if upload_dir:
        for item in sorted(upload_dir.glob("*")):
            if not item.is_file():
                continue
            if (mimetypes.guess_type(item.name)[0] or "").startswith("image/"):
                continue
            media_url = f"/media/{item.relative_to(root / 'uploads')}"
            other_uploads += f'<li><a href="{html.escape(media_url)}">{html.escape(item.name)}</a></li>'

    candidate_links = "".join(f'<li><a href="/candidates?candidate_id={quote(str(item.get("candidate_id") or ""))}">{html.escape(str(item.get("summary") or item.get("candidate_id") or ""))}</a></li>' for item in candidates) or "<li>No candidates.</li>"
    draft_links = []
    for candidate in candidates:
        for draft in drafts.get(str(candidate.get("candidate_id") or ""), []):
            draft_links.append(f'<li><a href="/drafts?draft_id={quote(str(draft.get("draft_id") or ""))}">{html.escape(str(draft.get("draft_id") or ""))}</a> {html.escape(humanize_key(draft.get("queue_state") or ""))}</li>')
    body = f"""
    <header><h1>Capture Detail</h1><p><code>{render_short_id(capture_id)}</code></p></header>
    {render_back_link("/captures", "Back to Captures")}
    {capture_details}
    <section><h3>Photos</h3>{photos_html}</section>
    <section><h3>Other uploads</h3><ul>{other_uploads or '<li>No other uploads.</li>'}</ul></section>
    {voice_transcript_html}
    {semantic_results_html}
    <section><h3>Action candidates</h3><ul>{candidate_links}</ul></section>
    <section><h3>Drafts and queue state</h3><ul>{''.join(draft_links) or '<li>No drafts.</li>'}</ul></section>
    """
    return html_page("Capture Detail", body, active_section="captures")


def render(ctx: object) -> str:
    capture_id = first_query_value(getattr(ctx, "query", {}), "capture_id").strip()
    if capture_id:
        return render_detail(ctx, capture_id)
    return render_list(ctx)
