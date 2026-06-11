from __future__ import annotations

import html
import json
import logging
import mimetypes
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from field_capture import approved_job_drafts
from field_capture import action_candidates as field_action_candidates
from field_capture import candidate_staging
from field_capture import client_notifications as field_client_notifications
from event_pipeline import couchdb_config
from event_pipeline.sites import SITES
from ops_dashboard import audit
from ops_dashboard.common import (
    apply_pending_candidate_counts,
    candidate_capture_sort_value,
    default_actor,
    first_filter_value,
    first_query_value,
    is_advisory_warning,
    pending_candidate_counts_by_capture,
    photo_vision_by_capture,
    read_json_artifact,
    render_capture_candidate_signal,
    render_fields,
    render_job_summary,
    render_kv,
    render_list,
    render_relative_time,
    render_thumb,
    capture_thumbnails,
    safe_media_url,
    safe_submitter,
    significant_warnings,
    string_list,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page
from processing_core.action_candidates import (
    RESOLUTION_CLIENT_NOTIFIED,
    RESOLUTION_DISMISSED,
    RESOLUTION_OPEN,
    RESOLUTION_RESOLVED,
    patch_candidate_resolution,
)
from queue_spec import validate_job


UNKNOWN_SUBMITTER = "Unknown submitter"
LOGGER = logging.getLogger(__name__)

DISMISS_REASON_COMPLETION = "completion"

RESOLUTION_DISPLAY = {
    RESOLUTION_OPEN: ("open", "Open"),
    RESOLUTION_CLIENT_NOTIFIED: ("notified", "Client Notified"),
    RESOLUTION_RESOLVED: ("resolved", "Resolved"),
    RESOLUTION_DISMISSED: ("dismissed", "Dismissed"),
    "": ("pending", "Pending"),
}


def render(request_ctx: object) -> str:
    runtime_root = getattr(request_ctx, "runtime_root", Path("."))
    query = getattr(request_ctx, "query", {})
    return render_field_capture_review(runtime_root, query)


def handle_review_post(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    action = getattr(request_ctx, "action", "")
    return _handle_review_post(action, request_ctx, body)


def handle_client_informed(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_client_informed_post(request_ctx, body)


def handle_resolve(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_resolve_post(request_ctx, body)


def handle_completion_dismiss_post(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_completion_dismiss(request_ctx, body)


def handle_archive(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_archive_post(request_ctx, body, archived=True)


def handle_unarchive(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_archive_post(request_ctx, body, archived=False)


def handle_resubmit(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_resubmit_post(request_ctx, body)


def handle_fix(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_fix_post(request_ctx, body)


def is_audio_file(path: Path) -> bool:
    mime = mimetypes.guess_type(path.name)[0] or ""
    return mime.startswith("audio/") or path.suffix.lower() in {".webm", ".m4a", ".mp3", ".wav", ".aac"}


def candidate_counts(candidate_dir: Path, runtime_root: Path) -> dict[str, int]:
    report = field_action_candidates.list_action_candidates_report(candidate_dir, runtime_root=runtime_root)
    counts = report["counts"]
    return {status: int(counts.get(status, 0)) for status in ("pending_review", "approved", "rejected", "failed")}


def candidate_detail(
    path: Path,
    payload: dict[str, object],
    runtime_root: Path | None = None,
    vision_items: list[dict[str, object]] | None = None,
    notification: dict[str, object] | None = None,
) -> dict[str, object]:
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    metadata = payload.get("channel_metadata") if isinstance(payload.get("channel_metadata"), dict) else {}
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    proposed_jobs = approved_job_drafts.proposed_queue_jobs(payload, runtime_root=runtime_root)
    first_job = proposed_jobs[0] if proposed_jobs else ("", {}, "")
    proposed_job_type, proposed_payload, proposed_error = first_job
    return {
        "candidate_id": str(payload.get("candidate_id") or ""),
        "status": str(payload.get("status") or ""),
        "site_id": str(metadata.get("site_id") or provenance.get("site_id") or payload.get("site_id") or ""),
        "area": str(metadata.get("area") or ""),
        "visit_proposed": bool(metadata.get("visit_proposed") is True),
        "visit_type": str(metadata.get("visit_type") or ""),
        "capture_id": str(metadata.get("upload_id") or payload.get("capture_id") or ""),
        "captured_at": str(metadata.get("captured_at") or payload.get("created_at") or ""),
        "submitter_name": str(metadata.get("submitter_name") or ""),
        "submitter_id": str(metadata.get("submitter_id") or metadata.get("person_id") or ""),
        "audio_asset_id": str(provenance.get("audio_asset_id") or ""),
        "summary": str(payload.get("summary") or ""),
        "rationale": str(payload.get("rationale") or ""),
        "source_text": str(payload.get("source_text") or ""),
        "source_context": str(payload.get("source_context") or ""),
        "semantic_artifact_path": str(provenance.get("semantic_artifact_path") or ""),
        "source_transcript_path": str(provenance.get("source_transcript_path") or ""),
        "artifact_path": str(path),
        "reviewer": str(payload.get("reviewer") or ""),
        "reviewed_at": str(payload.get("reviewed_at") or ""),
        "review_rationale": str(payload.get("review_rationale") or ""),
        "review_history": payload.get("review_history") if isinstance(payload.get("review_history"), list) else [],
        "vision_items": vision_items or [],
        "client_notification": notification or {},
        "resolution": resolution,
        "archived": bool(payload.get("archived") is True),
        "archived_at": str(payload.get("archived_at") or ""),
        "archived_by": str(payload.get("archived_by") or ""),
        "proposed_job_type": str(proposed_job_type or ""),
        "proposed_payload": proposed_payload if isinstance(proposed_payload, dict) else {},
        "proposed_job_error": str(proposed_error or ""),
        "_rev": str(payload.get("_rev") or ""),
        "proposed_jobs": [
            {"job_type": str(job_type or ""), "payload": payload if isinstance(payload, dict) else {}, "error": str(error or "")}
            for job_type, payload, error in proposed_jobs
        ],
    }


def review_candidates(candidate_dir: Path, status: str | None, runtime_root: Path) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    vision_by_capture = photo_vision_by_capture(runtime_root)
    submitters = submitters_by_capture(runtime_root)
    notification_dir = field_client_notifications.default_notification_dir(runtime_root)
    for path, payload in field_action_candidates.couchdb_candidate_payloads(status=status):
        if payload.get("type") != "action_candidate_review":
            continue
        metadata = payload.get("channel_metadata") if isinstance(payload.get("channel_metadata"), dict) else {}
        capture_id = str(metadata.get("upload_id") or payload.get("capture_id") or "")
        candidate_id = str(payload.get("candidate_id") or "")
        notification = field_client_notifications.load_notification(notification_dir, candidate_id)
        candidate = candidate_detail(path, payload, runtime_root, vision_by_capture.get(capture_id, []), notification)
        submitter = submitters.get(capture_id, safe_submitter({}))
        if not candidate["submitter_name"]:
            candidate["submitter_name"] = submitter["submitter_name"]
        if not candidate["submitter_id"]:
            candidate["submitter_id"] = submitter["submitter_id"]
        candidates.append(candidate)
    candidates.sort(key=lambda item: (str(item["candidate_id"]), str(item["artifact_path"])))
    return candidates


def render_flash(query: dict[str, list[str]]) -> str:
    message = first_query_value(query, "message")
    error = first_query_value(query, "error")
    if error:
        return f'<section class="error"><strong>Error:</strong> {html.escape(error)}</section>'
    if message:
        return f'<section class="success">{html.escape(message)}</section>'
    return ""


def render_filter_links(selected_status: str) -> str:
    links = []
    tabs = [
        ("pending", "/field-capture/review?status=pending_review", selected_status == "pending_review"),
        ("open", "/field-capture/review?status=approved&resolution=open", selected_status == "approved"),
        ("client notified", "/field-capture/review?status=approved&resolution=client_notified", False),
        ("resolved", "/field-capture/review?status=approved&resolution=resolved", False),
        ("dismissed", "/field-capture/review?status=approved&resolution=dismissed", False),
        ("all", "/field-capture/review?status=all", selected_status == "all"),
    ]
    for label, href, active in tabs:
        prefix = "<strong>" if active else ""
        suffix = "</strong>" if active else ""
        links.append(f'{prefix}<a href="{html.escape(href)}">{html.escape(label)}</a>{suffix}')
    return " | ".join(links)


def candidate_resolution(candidate: dict[str, object]) -> dict[str, object]:
    return candidate.get("resolution") if isinstance(candidate.get("resolution"), dict) else {}


def resolution_status(candidate: dict[str, object]) -> str:
    return str(candidate_resolution(candidate).get("status") or "")


def resolution_filter_status(candidate: dict[str, object]) -> str:
    return resolution_status(candidate) or RESOLUTION_OPEN


def render_resolution_pill(status: str) -> str:
    css_class, label = RESOLUTION_DISPLAY.get(status, RESOLUTION_DISPLAY[""])
    return f'<span class="pill resolution-{css_class}">{html.escape(label)}</span>'


def render_resolution_counts(candidates: list[dict[str, object]]) -> str:
    counts = {
        RESOLUTION_OPEN: 0,
        RESOLUTION_CLIENT_NOTIFIED: 0,
        RESOLUTION_RESOLVED: 0,
        RESOLUTION_DISMISSED: 0,
    }
    for candidate in candidates:
        status = resolution_filter_status(candidate)
        if status in counts:
            counts[status] += 1
    return f"""
      <p class="muted">
        <span class="pill resolution-open">Open: {counts[RESOLUTION_OPEN]}</span>
        <span class="pill resolution-notified">Notified: {counts[RESOLUTION_CLIENT_NOTIFIED]}</span>
        <span class="pill resolution-resolved">Resolved: {counts[RESOLUTION_RESOLVED]}</span>
        <span class="pill resolution-dismissed">Dismissed: {counts[RESOLUTION_DISMISSED]}</span>
      </p>
    """


def render_candidate_vision(vision_items: object) -> str:
    if not isinstance(vision_items, list) or not vision_items:
        return '<h4>Vision</h4><p class="muted">No vision description yet.</p>'
    cards = []
    for i, item in enumerate(vision_items):
        if not isinstance(item, dict):
            continue
        label = str(item.get("area_guess") or item.get("photo_asset_id") or f"Photo {i + 1}")
        img_url = safe_media_url(str(item.get("image_media_url") or ""))
        thumb_html = _render_thumb_strip([img_url]) if img_url else ""
        details = render_fields(
            {
                "status": item.get("status", ""),
                "photo_asset_id": item.get("photo_asset_id", ""),
                "area_guess": item.get("area_guess", ""),
                "description": item.get("description", ""),
                "visible_objects": ", ".join(string_list(item.get("visible_objects"))),
                "possible_conditions": ", ".join(string_list(item.get("possible_conditions"))),
                "possible_issues": ", ".join(string_list(item.get("possible_issues"))),
                "warnings": ", ".join(string_list(item.get("warnings"))),
                "model_name": item.get("model_name", ""),
                "confidence": item.get("confidence", ""),
            }
        )
        cards.append(f"<details><summary>{html.escape(label)}</summary>{thumb_html}{details}</details>")
    empty = '<p class="muted">No vision description yet.</p>'
    return f"<h4>Vision</h4>{''.join(cards) or empty}"


def read_candidate_context_text(path_value: object, *, json_pretty: bool = False) -> str:
    path = Path(str(path_value or ""))
    if not str(path) or not path.exists() or not path.is_file():
        return ""
    payload, error = read_json_artifact(path)
    if json_pretty and payload is not None:
        return json.dumps(payload, indent=2, sort_keys=True)
    if payload is not None:
        return str(payload.get("raw_text") or payload.get("text") or payload.get("transcript") or json.dumps(payload, indent=2, sort_keys=True))
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return error


def render_candidate_context(candidate: dict[str, object]) -> str:
    transcript = read_candidate_context_text(candidate.get("source_transcript_path"))
    semantic = read_candidate_context_text(candidate.get("semantic_artifact_path"), json_pretty=True)
    vision_items = candidate.get("vision_items") if isinstance(candidate.get("vision_items"), list) else []
    vision = json.dumps(vision_items, indent=2, sort_keys=True) if vision_items else "No photo vision sidecar for this capture."
    return f"""
      <h4>Raw transcript</h4>
      <pre>{html.escape(transcript or "No transcript text found.")}</pre>
      <h4>Semantic artifact</h4>
      <pre>{html.escape(semantic or "No semantic artifact found.")}</pre>
      <h4>Photo vision sidecar</h4>
      <pre>{html.escape(vision)}</pre>
    """


def render_resolve_form(candidate_id: str) -> str:
    escaped_candidate_id = html.escape(candidate_id)
    return f"""
        <form method="post" action="/field-capture/review/resolve" class="review-action-form">
          <input type="hidden" name="candidate_id" value="{escaped_candidate_id}">
          <label>Resolved by <input name="resolved_by" value="{html.escape(default_actor())}" required></label>
          <label>Note <textarea name="resolved_note" placeholder="What was done?"></textarea></label>
          <p><button type="submit">Mark Resolved</button></p>
        </form>
    """


def render_client_notify_form(candidate_id: str) -> str:
    escaped_candidate_id = html.escape(candidate_id)
    return f"""
        <form class="review-action-form" method="post" action="/field-capture/review/client-informed">
          <p class="action-identity">
            <strong>Mark Client Informed</strong>
            <code>{escaped_candidate_id}</code><br>
            Communication happened; this does not mean resolved.
          </p>
          <input type="hidden" name="candidate_id" value="{escaped_candidate_id}">
          <label>Method</label>
          <select name="method" required>
            <option value="email">email</option>
            <option value="phone">phone</option>
            <option value="in_person">in person</option>
            <option value="text">text</option>
            <option value="other">other</option>
          </select>
          <label>By</label>
          <input name="by" value="{html.escape(default_actor())}" required>
          <label>Note</label>
          <textarea name="note" placeholder="Emailed client with photo/context."></textarea>
          <p><button type="submit">Mark Client Informed</button></p>
        </form>
    """


def render_resolution_progression(candidate: dict[str, object]) -> str:
    if candidate.get("archived") is True:
        return ""
    status = resolution_status(candidate)
    res = candidate_resolution(candidate)
    candidate_id = str(candidate["candidate_id"])
    client_notification = candidate.get("client_notification") if isinstance(candidate.get("client_notification"), dict) else {}
    client_informed = bool(client_notification.get("client_informed"))
    client_status = "Client Informed" if client_informed else "Client not yet informed"
    client_method = str(client_notification.get("client_informed_method") or "")
    client_by = str(client_notification.get("client_informed_by") or "")
    client_at = str(client_notification.get("client_informed_at") or "")
    client_note = str(client_notification.get("client_informed_note") or "")
    client_bits = [client_status]
    if client_method:
        client_bits.append(f"method {client_method.replace('_', ' ')}")
    if client_by:
        client_bits.append(f"by {client_by}")
    if client_at:
        client_bits.append(client_at)
    client_details = render_fields(
        {
            "client_informed": client_informed,
            "client_informed_method": client_method,
            "client_informed_by": client_by,
            "client_informed_at": client_at,
            "client_informed_note": client_note,
        }
    )
    if status == RESOLUTION_RESOLVED:
        resolved_by = str(res.get("resolved_by") or "")
        resolved_at = render_relative_time(res.get("resolved_at"))
        resolved_note = str(res.get("resolved_note") or "")
        return f"""
        <h4>Resolution</h4>
        <p>{render_resolution_pill(RESOLUTION_RESOLVED)}
           {html.escape(resolved_by)} · {resolved_at} · {html.escape(resolved_note)}</p>
        """
    if status == RESOLUTION_DISMISSED:
        dismissed_by = str(res.get("dismissed_by") or "")
        dismissed_at = render_relative_time(res.get("dismissed_at"))
        return f"""
        <h4>Resolution</h4>
        <p>{render_resolution_pill(RESOLUTION_DISMISSED)}
           {html.escape(dismissed_by)} · {dismissed_at}</p>
        """
    if candidate["status"] != "approved":
        return ""
    if status == RESOLUTION_CLIENT_NOTIFIED:
        return f"""
        <h4>Resolution</h4>
        <p><span class="pill resolution-notified">{html.escape(' | '.join(client_bits))}</span></p>
        {client_details}
        {render_resolve_form(candidate_id)}
        """
    return f"""
        <h4>Resolution</h4>
        {render_client_notify_form(candidate_id)}
        {render_resolve_form(candidate_id)}
    """


def render_action_icon(kind: str) -> str:
    if kind == "approve":
        return (
            '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M20 6 9 17l-5-5"></path></svg>'
        )
    if kind == "resubmit":
        return (
            '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M21 12a9 9 0 1 1-3-6.7"></path><path d="M21 3v6h-6"></path></svg>'
        )
    if kind == "restore":
        return (
            '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M3 12a9 9 0 0 1 15.6-6"></path><path d="M3 3v6h6"></path>'
            '<path d="M21 12a9 9 0 0 1-15.6 6"></path></svg>'
        )
    if kind == "archive":
        return (
            '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M3 6h18"></path><path d="M8 6V4h8v2"></path>'
            '<path d="M19 6l-1 14H6L5 6"></path></svg>'
        )
    return (
        '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16" fill="none" '
        'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg>'
    )


def render_proposed_job_item(job_type: str, payload: dict[str, object], error: str) -> str:
    if error:
        return f"""
        <div class="proposed-job-item proposed-job-blocked">
          <p><strong>Cannot prepare a queue job yet.</strong></p>
          <p>{html.escape(error)}</p>
        </div>
        """
    if not job_type or not payload:
        return """
        <div class="proposed-job-item proposed-job-blocked">
          <p><strong>No queue mutation is proposed for this candidate.</strong></p>
        </div>
        """
    if job_type == "set_entity_status":
        details_payload = {
            "entity_type": payload.get("entity_type"),
            "entity_id": payload.get("entity_id"),
            "new_state": payload.get("status"),
        }
    else:
        details_payload = payload
    details = render_fields(details_payload)
    return f"""
    <div class="proposed-job-item">
      <p><strong>{render_job_summary(job_type, payload)}</strong></p>
      {details}
    </div>
    """


def render_proposed_job(candidate: dict[str, object]) -> str:
    raw_jobs = candidate.get("proposed_jobs") if isinstance(candidate.get("proposed_jobs"), list) else []
    jobs = [job for job in raw_jobs if isinstance(job, dict)]
    if not jobs:
        jobs = [{"job_type": candidate.get("proposed_job_type"), "payload": candidate.get("proposed_payload"), "error": candidate.get("proposed_job_error")}]
    items = [
        render_proposed_job_item(
            str(job.get("job_type") or ""),
            job.get("payload") if isinstance(job.get("payload"), dict) else {},
            str(job.get("error") or ""),
        )
        for job in jobs
    ]
    count_text = f"{len(items)} proposed changes" if len(items) != 1 else "Proposed change"
    return f"""
    <section class="proposed-job">
      <p class="muted">{html.escape(count_text)}</p>
      {''.join(items)}
    </section>
    """


def render_evidence_summary(candidate: dict[str, object]) -> str:
    source_text = str(candidate.get("source_text") or "").strip()
    source_context = str(candidate.get("source_context") or "").strip()
    rationale = str(candidate.get("rationale") or "").strip()
    pieces = []
    if source_text:
        pieces.append(f"<blockquote>{html.escape(source_text)}</blockquote>")
    elif source_context:
        pieces.append(f"<blockquote>{html.escape(source_context)}</blockquote>")
    if rationale:
        pieces.append(f"<p>{html.escape(rationale)}</p>")
    if not pieces:
        pieces.append('<p class="muted">No evidence text was captured.</p>')
    return f"""
    <section class="decision-evidence">
      <p class="muted">Why</p>
      {''.join(pieces)}
    </section>
    """


def candidate_proposed_error(candidate: dict[str, object]) -> str:
    proposed_jobs = candidate.get("proposed_jobs") if isinstance(candidate.get("proposed_jobs"), list) else []
    proposed_error = str(candidate.get("proposed_job_error") or "")
    if proposed_jobs:
        proposed_error = next((str(job.get("error") or "") for job in proposed_jobs if isinstance(job, dict) and job.get("error")), "")
    return proposed_error


def render_candidate_fix_form(candidate: dict[str, object], proposed_error: str) -> str:
    candidate_id = html.escape(str(candidate["candidate_id"]))
    rev = html.escape(str(candidate.get("_rev") or ""), quote=True)
    current_site_id = str(candidate.get("site_id") or "")
    site_options = render_site_id_options(current_site_id)
    visit_checked = " checked" if candidate.get("visit_proposed") is True else ""
    return f"""
      <form class="review-action-form approve-form" method="post" action="/field-capture/review/fix">
        <p class="action-identity">
          <strong>Fix &amp; resubmit</strong>
          <code>{candidate_id}</code><br>
          Correct the proposed job payload, then re-queue it directly.
        </p>
        <p class="error"><strong>Current error:</strong> {html.escape(proposed_error)}</p>
        <input type="hidden" name="candidate_id" value="{candidate_id}">
        <input type="hidden" name="_rev" value="{rev}">
        <input type="hidden" name="reviewer" value="{html.escape(default_actor())}">
        <label>Site
          <select name="site_id" required>
            {site_options}
          </select>
        </label>
        <label>Area <input name="area" value="{html.escape(str(candidate.get('area') or ''), quote=True)}"></label>
        <input type="hidden" name="visit_proposed_present" value="1">
        <label><input type="checkbox" name="visit_proposed" value="1"{visit_checked}> Visit proposed</label>
        <label>Visit type <input name="visit_type" value="{html.escape(str(candidate.get('visit_type') or ''), quote=True)}"></label>
        <label>Summary <input name="summary" value="{html.escape(str(candidate.get('summary') or ''), quote=True)}"></label>
        <p><button class="approve icon-button" type="submit" aria-label="Fix and resubmit this candidate">{render_action_icon("resubmit")} <span>Fix &amp; resubmit</span></button></p>
      </form>
    """


def render_site_id_options(current_site_id: str) -> str:
    current = str(current_site_id or "").strip()
    options = ['<option value="">Select site</option>']
    rows = sorted(
        (site for site in SITES if str(site.get("site_id") or "").strip()),
        key=lambda site: (str(site.get("canonical") or site.get("name") or ""), str(site.get("site_id") or "")),
    )
    for site in rows:
        site_id = str(site.get("site_id") or "").strip()
        name = str(site.get("name") or site.get("canonical") or site_id)
        selected = " selected" if site_id == current else ""
        options.append(
            f'<option value="{html.escape(site_id, quote=True)}"{selected}>{html.escape(name)} ({html.escape(site_id)})</option>'
        )
    return "\n".join(options)


def render_resubmit_archive_actions(candidate: dict[str, object]) -> str:
    raw_candidate_id = str(candidate["candidate_id"])
    candidate_id = html.escape(raw_candidate_id)
    rev = html.escape(str(candidate.get("_rev") or ""), quote=True)
    proposed_error = candidate_proposed_error(candidate)
    resubmit_disabled = " disabled" if proposed_error else ""
    resubmit_hint = "Resubmit and stage this proposed job" if not proposed_error else "Cannot resubmit until the proposed job is valid"
    error_hint = f'<p class="muted">{html.escape(resubmit_hint)}</p>' if proposed_error else ""
    fix_form = render_candidate_fix_form(candidate, proposed_error) if proposed_error else ""
    return f"""
    <div class="actions decision-actions">
      <form class="review-action-form approve-form" method="post" action="/field-capture/review/resubmit">
        <p class="action-identity">
          <strong>Resubmit</strong>
          <code>{candidate_id}</code><br>
          Re-queue the proposed job directly.
        </p>
        <input type="hidden" name="candidate_id" value="{candidate_id}">
        <input type="hidden" name="_rev" value="{rev}">
        <input type="hidden" name="reviewer" value="{html.escape(default_actor())}">
        {error_hint}
        <p><button class="approve icon-button" type="submit" aria-label="{html.escape(resubmit_hint)}"{resubmit_disabled}>{render_action_icon("resubmit")} <span>Resubmit</span></button></p>
      </form>
      <form class="review-action-form reject-form" method="post" action="/field-capture/review/archive">
        <p class="action-identity">
          <strong>Delete</strong>
          <code>{candidate_id}</code><br>
          Soft-archive this candidate.
        </p>
        <input type="hidden" name="candidate_id" value="{candidate_id}">
        <input type="hidden" name="_rev" value="{rev}">
        <input type="hidden" name="reviewer" value="{html.escape(default_actor())}">
        <p><button class="reject icon-button" type="submit" aria-label="Soft-archive this candidate">{render_action_icon("archive")} <span>Delete</span></button></p>
      </form>
      {fix_form}
    </div>
    """


def render_restore_action(candidate: dict[str, object]) -> str:
    candidate_id = html.escape(str(candidate["candidate_id"]))
    rev = html.escape(str(candidate.get("_rev") or ""), quote=True)
    return f"""
    <div class="actions decision-actions">
      <form class="review-action-form approve-form" method="post" action="/field-capture/review/unarchive">
        <p class="action-identity">
          <strong>Restore</strong>
          <code>{candidate_id}</code><br>
          Return this candidate to its review bucket.
        </p>
        <input type="hidden" name="candidate_id" value="{candidate_id}">
        <input type="hidden" name="_rev" value="{rev}">
        <input type="hidden" name="reviewer" value="{html.escape(default_actor())}">
        <p><button class="approve icon-button" type="submit" aria-label="Restore this candidate">{render_action_icon("restore")} <span>Restore</span></button></p>
      </form>
    </div>
    """


def render_candidate_card(candidate: dict[str, object], thumb_urls: list[str] | None = None) -> str:
    review_history = candidate.get("review_history") if isinstance(candidate.get("review_history"), list) else []
    history_text = json.dumps(review_history, indent=2, sort_keys=True) if review_history else "No review history."
    raw_summary = str(candidate["summary"]) or str(candidate["candidate_id"])
    raw_rationale = str(candidate["rationale"]) or "No machine rationale."
    summary = html.escape(raw_summary)
    rationale = html.escape(raw_rationale)
    thumb_strip = _render_thumb_strip(thumb_urls or [])
    actions = ""
    if candidate.get("archived") is True:
        actions = render_restore_action(candidate)
    elif candidate["status"] == "pending_review":
        raw_candidate_id = str(candidate["candidate_id"])
        candidate_id = html.escape(raw_candidate_id)
        proposed_error = candidate_proposed_error(candidate)
        approve_disabled = " disabled" if proposed_error else ""
        approve_hint = "Approve and stage this job" if not proposed_error else "Cannot approve until the proposed job is valid"
        actions = f"""
        <div class="actions decision-actions">
          <form class="review-action-form approve-form" method="post" action="/field-capture/review/approve">
            <p class="action-identity">
              <strong>Approve</strong>
              <code>{candidate_id}</code><br>
              Stage this job for processing.
            </p>
            <input type="hidden" name="candidate_id" value="{candidate_id}">
            <input type="hidden" name="_rev" value="{html.escape(str(candidate.get('_rev') or ''), quote=True)}">
            <label>Reviewer</label>
            <input name="reviewer" value="{html.escape(default_actor())}" required>
            <label>Note</label>
            <textarea name="rationale" rows="2"></textarea>
            <p><button class="approve icon-button" type="submit" aria-label="{html.escape(approve_hint)}"{approve_disabled}>{render_action_icon("approve")} <span>Approve</span></button></p>
          </form>
          <form class="review-action-form reject-form" method="post" action="/field-capture/review/reject">
            <p class="action-identity">
              <strong>Deny</strong>
              <code>{candidate_id}</code><br>
              Leave the data unchanged.
            </p>
            <input type="hidden" name="candidate_id" value="{candidate_id}">
            <input type="hidden" name="_rev" value="{html.escape(str(candidate.get('_rev') or ''), quote=True)}">
            <label>Reviewer</label>
            <input name="reviewer" value="{html.escape(default_actor())}" required>
            <label>Note</label>
            <textarea name="rationale" rows="2"></textarea>
            <p><button class="reject icon-button" type="submit" aria-label="Deny this proposed job">{render_action_icon("deny")} <span>Deny</span></button></p>
          </form>
        </div>
        """
    elif candidate["status"] in {"failed", "rejected"}:
        actions = render_resubmit_archive_actions(candidate)
    operator_details = render_fields(
        {
            "status": candidate["status"],
            "site_id": candidate["site_id"],
            "area": candidate["area"],
            "capture_id": candidate["capture_id"],
            "captured_at": candidate.get("captured_at", ""),
            "submitter": candidate["submitter_name"],
            "source_text": candidate["source_text"],
            "source_context": candidate["source_context"],
            "reviewer": candidate["reviewer"],
            "reviewed_at": candidate["reviewed_at"],
            "review_rationale": candidate["review_rationale"],
            "archived": candidate.get("archived"),
            "archived_at": candidate.get("archived_at", ""),
            "archived_by": candidate.get("archived_by", ""),
        }
    )
    internals = render_fields(
        {
            "candidate_id": candidate["candidate_id"],
            "submitter_id": candidate["submitter_id"],
            "audio_asset_id": candidate["audio_asset_id"],
            "semantic_artifact_path": candidate["semantic_artifact_path"],
            "source_transcript_path": candidate["source_transcript_path"],
            "candidate_artifact_path": candidate["artifact_path"],
        }
    )
    return f"""
    <article>
      {render_resolution_pill(resolution_status(candidate))}
      {render_proposed_job(candidate)}
      {render_evidence_summary(candidate)}
      {actions}
      {thumb_strip}
      {render_resolution_progression(candidate)}
      <details>
        <summary>Review details</summary>
        <section class="machine-interpretation">
          <p class="muted">Machine summary</p>
          <p><strong>{summary}</strong></p>
          <p>{rationale}</p>
        </section>
        {operator_details}
        {render_candidate_vision(candidate.get("vision_items"))}
        <h4>Transcript and semantics</h4>
        {render_candidate_context(candidate)}
        <h4>Reviewer History</h4>
        <pre>{html.escape(history_text)}</pre>
      </details>
      <details class="raw-json">
        <summary>Internals</summary>
        {internals}
      </details>
    </article>
    """


def candidate_filter_options(candidates: list[dict[str, object]], key: str) -> list[str]:
    return sorted({str(candidate.get(key) or "") for candidate in candidates if str(candidate.get(key) or "")})


def query_values(query: dict[str, list[str]], key: str) -> set[str]:
    return {str(value).strip() for value in query.get(key, []) if str(value).strip()}


def capture_has_media(runtime_root: Path, capture_id: str, *, media_kind: str) -> bool:
    upload_root = runtime_root / "uploads"
    if not capture_id or not upload_root.exists():
        return False
    for capture_dir in upload_root.glob(f"*/{capture_id}"):
        if not capture_dir.is_dir():
            continue
        for item in capture_dir.glob("*"):
            if not item.is_file():
                continue
            mime = mimetypes.guess_type(item.name)[0] or ""
            if media_kind == "photo" and mime.startswith("image/"):
                return True
            if media_kind == "audio" and is_audio_file(item):
                return True
    return False


def capture_thumbnail(runtime_root: Path, capture_id: str) -> str:
    urls = capture_thumbnails(runtime_root, capture_id)
    return urls[0] if urls else ""


def _render_thumb_strip(urls: list[str], *, size: int = 100) -> str:
    if not urls:
        return ""
    thumbs = [
        f'<a href="#" onclick="openLb({html.escape(json.dumps(url), quote=True)});return false"'
        f' title="View full size" style="cursor:zoom-in">'
        f'<img class="candidate-thumb" src="{html.escape(url, quote=True)}"'
        f' width="{size}" height="{size}" style="object-fit:cover" alt="photo"></a>'
        for url in urls
    ]
    return f'<div class="thumb-strip">{"".join(thumbs)}</div>'


def candidate_has_warning(candidate: dict[str, object]) -> bool:
    vision_items = candidate.get("vision_items") if isinstance(candidate.get("vision_items"), list) else []
    return any(significant_warnings(item.get("warnings") if isinstance(item, dict) else []) for item in vision_items)


def filter_candidates(candidates: list[dict[str, object]], runtime_root: Path, query: dict[str, list[str]]) -> list[dict[str, object]]:
    candidate_id = first_query_value(query, "candidate_id").strip()
    resolutions = query_values(query, "resolution")
    sites = query_values(query, "site")
    submitters = query_values(query, "submitter")
    date_from = first_query_value(query, "date_from").strip()
    date_to = first_query_value(query, "date_to").strip()
    area = first_query_value(query, "area").strip().lower()
    has_photo = first_query_value(query, "has_photo").lower() in {"1", "true", "yes", "on"}
    has_audio = first_query_value(query, "has_audio").lower() in {"1", "true", "yes", "on"}
    has_warning = first_query_value(query, "has_warning").lower() in {"1", "true", "yes", "on"}
    filtered = candidates
    if resolutions:
        filtered = [candidate for candidate in filtered if resolution_filter_status(candidate) in resolutions]
    if candidate_id:
        filtered = [candidate for candidate in filtered if str(candidate.get("candidate_id") or "") == candidate_id]
    if sites:
        filtered = [candidate for candidate in filtered if str(candidate.get("site_id") or "") in sites]
    if submitters:
        filtered = [candidate for candidate in filtered if str(candidate.get("submitter_name") or UNKNOWN_SUBMITTER) in submitters]
    if area:
        filtered = [candidate for candidate in filtered if area in str(candidate.get("area") or "").lower()]
    if date_from:
        filtered = [candidate for candidate in filtered if str(candidate.get("captured_at") or candidate.get("capture_id") or "") >= date_from]
    if date_to:
        filtered = [candidate for candidate in filtered if str(candidate.get("captured_at") or candidate.get("capture_id") or "") <= date_to + "T99"]
    if has_photo:
        filtered = [candidate for candidate in filtered if capture_has_media(runtime_root, str(candidate.get("capture_id") or ""), media_kind="photo")]
    if has_audio:
        filtered = [candidate for candidate in filtered if capture_has_media(runtime_root, str(candidate.get("capture_id") or ""), media_kind="audio")]
    if has_warning:
        filtered = [candidate for candidate in filtered if candidate_has_warning(candidate)]
    return filtered


def render_multi_select(name: str, label: str, options: list[str], selected: set[str]) -> str:
    choices = []
    for option in options:
        checked = " checked" if option in selected else ""
        choices.append(f'<label><input type="checkbox" name="{html.escape(name)}" value="{html.escape(option)}"{checked}> {html.escape(option)}</label>')
    body = "".join(choices) if choices else '<p class="muted">No options</p>'
    return f"<fieldset><legend>{html.escape(label)}</legend>{body}</fieldset>"


def render_candidate_filters(query: dict[str, list[str]], all_candidates: list[dict[str, object]]) -> str:
    selected_status = first_filter_value(query, "status") or "approved"
    statuses = ["pending_review", "approved", "failed", "rejected", "archived", "all"]
    status_controls = "".join(
        f'<label><input type="radio" name="status" value="{html.escape(status)}"{" checked" if status == selected_status else ""}> {html.escape(status.replace("_", " "))}</label>'
        for status in statuses
    )
    resolution_controls = ""
    if selected_status == "approved":
        selected_resolutions = query_values(query, "resolution") or {RESOLUTION_OPEN, RESOLUTION_CLIENT_NOTIFIED}
        resolution_options = [
            (RESOLUTION_OPEN, "Open"),
            (RESOLUTION_CLIENT_NOTIFIED, "Client Notified"),
            (RESOLUTION_RESOLVED, "Resolved"),
            (RESOLUTION_DISMISSED, "Dismissed"),
        ]
        resolution_controls = "<fieldset><legend>Resolution</legend>" + "".join(
            f'<label><input type="checkbox" name="resolution" value="{html.escape(value)}"{" checked" if value in selected_resolutions else ""}> {html.escape(label)}</label>'
            for value, label in resolution_options
        ) + "</fieldset>"
    return f"""
    <form method="get" action="/candidates" data-submit-on-change>
      <fieldset><legend>Status</legend>{status_controls}</fieldset>
      {resolution_controls}
      {render_multi_select("site", "Site", candidate_filter_options(all_candidates, "site_id"), query_values(query, "site"))}
      {render_multi_select("submitter", "Submitter", candidate_filter_options(all_candidates, "submitter_name"), query_values(query, "submitter"))}
      <label>Date from<input type="date" name="date_from" value="{html.escape(first_query_value(query, "date_from"))}"></label>
      <label>Date to<input type="date" name="date_to" value="{html.escape(first_query_value(query, "date_to"))}"></label>
      <label>Area contains<input name="area" value="{html.escape(first_query_value(query, "area"))}"></label>
      <label><input type="checkbox" name="has_photo" value="true" {"checked" if first_query_value(query, "has_photo") else ""}> Has photo</label>
      <label><input type="checkbox" name="has_audio" value="true" {"checked" if first_query_value(query, "has_audio") else ""}> Has audio</label>
      <label><input type="checkbox" name="has_warning" value="true" {"checked" if first_query_value(query, "has_warning") else ""}> Has photo-vision warning</label>
      <p><button type="submit">Filter</button></p>
    </form>
    """


def render_candidate_groups(candidates: list[dict[str, object]], runtime_root: Path) -> str:
    if not candidates:
        return "<p>No candidates match this filter.</p>"
    groups: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        groups.setdefault(str(candidate.get("capture_id") or "unknown"), []).append(candidate)
    rendered = []
    for capture_id, group in groups.items():
        first = group[0]
        pending_capture_count = max((int(candidate.get("capture_candidate_count") or 0) for candidate in group), default=0)
        capture_signal = render_capture_candidate_signal(pending_capture_count)
        vision_items = first.get("vision_items") if isinstance(first.get("vision_items"), list) else []
        vision_summary = ""
        if vision_items and isinstance(vision_items[0], dict):
            vision_summary = " ".join(str(vision_items[0].get(key) or "") for key in ("area_guess", "description")).strip()
        thumb_urls = capture_thumbnails(runtime_root, capture_id)
        thumb_strip = _render_thumb_strip(thumb_urls)
        rendered.append(
            f"""<section class="candidate-group" id="capture-{html.escape(capture_id)}">
        <div class="candidate-group-header">
          <div>
            <h2>{html.escape(capture_id)}</h2>
            <p class="muted">Site {html.escape(str(first.get("site_id") or ""))} | {html.escape(str(first.get("submitter_name") or UNKNOWN_SUBMITTER))} | {html.escape(str(first.get("captured_at") or ""))}</p>
            {capture_signal}
            <p>{html.escape(vision_summary[:220])}</p>
          </div>
          {thumb_strip}
        </div>
        {''.join(render_candidate_card(candidate, thumb_urls) for candidate in group)}
      </section>"""
        )
    return "".join(rendered)


def render_field_capture_review(runtime_root: Path, query: dict[str, list[str]]) -> str:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    explicit_status = bool(first_filter_value(query, "status"))
    selected_status = first_filter_value(query, "status") or "approved"
    if selected_status == "all":
        status_filter = None
    elif selected_status in field_action_candidates.REVIEW_STATUSES or selected_status == "archived":
        status_filter = selected_status
    else:
        selected_status = "approved"
        status_filter = selected_status
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_resolved)
    counts = candidate_counts(candidate_dir, runtime_resolved)
    all_candidates = review_candidates(candidate_dir, None, runtime_resolved)
    if selected_status == "archived":
        all_candidates = [*all_candidates, *review_candidates(candidate_dir, "archived", runtime_resolved)]
    pending_counts = pending_candidate_counts_by_capture(all_candidates)
    approved_candidates = [candidate for candidate in all_candidates if candidate.get("status") == "approved"]
    candidates = review_candidates(candidate_dir, status_filter, runtime_resolved)
    effective_query = dict(query)
    if selected_status == "approved" and "resolution" not in effective_query:
        effective_query["resolution"] = [RESOLUTION_OPEN, RESOLUTION_CLIENT_NOTIFIED]
    candidates = filter_candidates(candidates, runtime_resolved, effective_query)
    if not explicit_status and selected_status == "approved" and not candidates and counts.get("pending_review", 0) > 0:
        selected_status = "pending_review"
        status_filter = selected_status
        fallback_query = dict(query)
        fallback_query["status"] = [selected_status]
        fallback_query.pop("resolution", None)
        effective_query = fallback_query
        candidates = filter_candidates(review_candidates(candidate_dir, status_filter, runtime_resolved), runtime_resolved, effective_query)
    apply_pending_candidate_counts(candidates, pending_counts)
    candidates.sort(key=candidate_capture_sort_value, reverse=True)
    cards = render_candidate_groups(candidates, runtime_resolved)
    body = f"""
    <header>
      <p><a href="/">BTQ Ops Dashboard</a></p>
      <h1>Captures</h1>
      <p class="muted">Runtime root: <code>{html.escape(str(runtime_resolved))}</code></p>
      <p class="muted">Private localhost/Tailscale use only. Actions on this page track issue lifecycle state; vault mutation remains separate.</p>
    </header>
    {render_flash(query)}
    <section class="notice">
      <h2>Review Counts</h2>
      {render_kv(counts)}
      {render_resolution_counts(approved_candidates)}
      <p>{render_filter_links(selected_status)}</p>
      <p class="muted">Draft generation, approved draft staging, queue processing, transcription, semantic processing, VPS sync, and vault mutation remain CLI-only/separate operations.</p>
      <p class="muted"><a href="/field-photos">Browse photo vision sidecars →</a></p>
    </section>
    <div class="content-with-rail">
      <aside class="filter-rail">
        <section>
          <h2>Filters</h2>
          {render_candidate_filters(effective_query, all_candidates)}
        </section>
      </aside>
      <div>
        <section>
          <h2>{html.escape(selected_status.replace("_", " ").title())} Candidates</h2>
          <p class="muted">Showing {len(candidates)} candidate records grouped by capture.</p>
        </section>
        {cards}
      </div>
    </div>
    """
    return html_page("Captures", body, active_section="candidates")


def redirect_to_review(message: str = "", error: str = "", status: str = "pending_review") -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    query = {"status": status}
    if error:
        query["error"] = error
    elif message:
        query["message"] = message
    location = f"/field-capture/review?{urlencode(query)}"
    body = f'<a href="{html.escape(location)}">Return to Field Capture Review</a>'.encode("utf-8")
    return HTTPStatus.SEE_OTHER, "text/html; charset=utf-8", body, {"Location": location}


def redirect_to_inbox(message: str = "", error: str = "") -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    query = {}
    if error:
        query["error"] = error
    elif message:
        query["message"] = message
    location = f"/?{urlencode(query)}" if query else "/"
    body = f'<a href="{html.escape(location)}">Return to Captures</a>'.encode("utf-8")
    return HTTPStatus.SEE_OTHER, "text/html; charset=utf-8", body, {"Location": location}


def latest_draft_id_for_candidate(runtime_root: Path, candidate_id: str) -> str:
    draft_ids = latest_draft_ids_for_candidate(runtime_root, candidate_id)
    return draft_ids[-1] if draft_ids else ""


def latest_draft_ids_for_candidate(runtime_root: Path, candidate_id: str) -> list[str]:
    return candidate_staging.latest_draft_ids_for_candidate(runtime_root, candidate_id)


def stage_candidate_job_after_approval(runtime_root: Path, candidate_id: str) -> str:
    return candidate_staging.stage_candidate_job_after_approval(runtime_root, candidate_id)


def _handle_review_post(action: str, ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    reviewer = first_query_value(form, "reviewer").strip()
    rationale = first_query_value(form, "rationale").strip()
    expected_rev = first_query_value(form, "_rev").strip() or None
    route = f"/field-capture/review/{action}"
    runtime_resolved = ctx.runtime_root
    if not candidate_id:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: candidate_id is required",
            },
        )
        return redirect_to_inbox(error="candidate_id is required")
    if not reviewer:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: reviewer is required",
            },
        )
        return redirect_to_inbox(error="reviewer is required")

    try:
        cdb_config = couchdb_config.from_env()
        result = field_action_candidates.apply_candidate_review(
            cdb_config,
            couchdb_config.field_captures_database(),
            candidate_id=candidate_id,
            action=action,
            reviewer=reviewer,
            expected_rev=expected_rev,
            rationale=rationale,
        )
        if result.error:
            raise field_action_candidates.CandidateReviewError(result.error)
    except (field_action_candidates.CandidateReviewError, couchdb_config.CouchDBConfigError) as exc:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": f"failed: {exc}",
            },
        )
        return redirect_to_inbox(error=str(exc))
    if result.already_decided:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": f"already_decided: {result.candidate_id} status={result.status}",
            },
        )
        return redirect_to_inbox(error="candidate was already decided")
    audit.append_audit(
        runtime_resolved,
        {
            "route": route,
            "actor": "localhost",
            "payload": ctx.form_payload(form),
            "result_summary": f"success: {result.candidate_id} marked {result.status}",
        },
    )
    message = "approved" if action == "approve" else "denied"
    return redirect_to_inbox(message=message)


def valid_proposed_jobs(candidate: dict[str, object], runtime_root: Path) -> bool:
    return proposed_jobs_error(candidate, runtime_root) == ""


def proposed_jobs_error(candidate: dict[str, object], runtime_root: Path) -> str:
    proposed_jobs = approved_job_drafts.proposed_queue_jobs(candidate, runtime_root=runtime_root)
    if not proposed_jobs:
        return "candidate has no proposed queue job"
    for proposed_job_type, proposed_payload, proposed_error in proposed_jobs:
        if proposed_error or not proposed_job_type or not isinstance(proposed_payload, dict) or not proposed_payload:
            return str(proposed_error or "candidate has an invalid proposed queue job")
        if not validate_job({"job_type": proposed_job_type, "payload": proposed_payload}):
            return "candidate has an invalid proposed queue job"
    return ""


def _fix_patch_fields(form: dict[str, list[str]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key in ("site_id", "area", "visit_type", "summary", "rationale", "source_text", "confidence"):
        if key in form:
            fields[key] = first_query_value(form, key).strip()
    if "visit_proposed_present" in form or "visit_proposed" in form:
        values = [str(value or "").strip().lower() for value in form.get("visit_proposed", [])]
        fields["visit_proposed"] = any(value in {"1", "true", "yes", "on"} for value in values)
    return fields


def _handle_fix_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    reviewer = first_query_value(form, "reviewer").strip() or default_actor()
    expected_rev = first_query_value(form, "_rev").strip() or None
    route = "/field-capture/review/fix"
    runtime_resolved = ctx.runtime_root
    redirect_status = "failed"
    if not candidate_id:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": "failed: candidate_id is required"})
        return redirect_to_review(error="candidate_id is required", status=redirect_status)
    try:
        cdb_config = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        doc = field_action_candidates.get_action_candidate(cdb_config, db, candidate_id)
        if doc is None:
            raise field_action_candidates.CandidateReviewError(f"candidate not found: {candidate_id}")
        prior_status = str(doc.get("status") or "")
        redirect_status = prior_status if prior_status in {"failed", "rejected"} else "failed"
        if prior_status not in {"failed", "rejected"}:
            raise field_action_candidates.CandidateReviewError(f"candidate status cannot be fixed: {prior_status}")
        if doc.get("archived") is True:
            raise field_action_candidates.CandidateReviewError("archived candidate must be restored before fixing")
        fields = _fix_patch_fields(form)
        field_action_candidates.patch_action_candidate_fields(
            cdb_config,
            db,
            candidate_id,
            fields=fields,
            expected_rev=expected_rev,
        )
        refetched = field_action_candidates.get_action_candidate(cdb_config, db, candidate_id)
        if refetched is None:
            raise field_action_candidates.CandidateReviewError(f"candidate not found after patch: {candidate_id}")
        patched_rev = str(refetched.get("_rev") or "")
        patched_candidate = field_action_candidates.couchdb_action_candidate_to_review_payload(refetched)
        validation_error = proposed_jobs_error(patched_candidate, runtime_resolved)
        if validation_error:
            audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"saved_patch_invalid: {candidate_id} {validation_error}"})
            return redirect_to_review(error=validation_error, status=redirect_status)
        stage_summary = stage_candidate_job_after_approval(runtime_resolved, candidate_id)
        field_action_candidates.set_action_candidate_status(
            cdb_config,
            db,
            candidate_id,
            status="approved",
            reviewed_by=reviewer,
            rationale="Fixed payload and resubmitted directly from review triage.",
            expected_rev=patched_rev or None,
            expected_prior_statuses={"failed", "rejected"},
            reason="fix_resubmit",
        )
    except field_action_candidates.AlreadyDecided:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"conflict: {candidate_id}"})
        return redirect_to_review(error="candidate changed before this fix could be saved. Reload and try again.", status=redirect_status)
    except field_action_candidates.CandidateReviewError as exc:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"failed: {exc}"})
        return redirect_to_review(error=str(exc), status=redirect_status)
    except (field_action_candidates.CouchDBCandidateWriterError, couchdb_config.CouchDBConfigError) as exc:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"failed: {exc}"})
        return redirect_to_review(error=str(exc), status=redirect_status)
    audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"success: {candidate_id} fixed_and_resubmitted {stage_summary}"})
    return redirect_to_review(message="fixed and resubmitted", status="approved")


def _handle_archive_post(ctx: object, body: bytes, *, archived: bool) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    reviewer = first_query_value(form, "reviewer").strip() or default_actor()
    expected_rev = first_query_value(form, "_rev").strip() or None
    route = "/field-capture/review/archive" if archived else "/field-capture/review/unarchive"
    runtime_resolved = ctx.runtime_root
    redirect_status = "archived"
    if not candidate_id:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": "failed: candidate_id is required"})
        return redirect_to_review(error="candidate_id is required", status=redirect_status)
    try:
        cdb_config = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        doc = field_action_candidates.get_action_candidate(cdb_config, db, candidate_id)
        if doc is None:
            raise field_action_candidates.CandidateReviewError(f"candidate not found: {candidate_id}")
        prior_status = str(doc.get("status") or "pending_review")
        if archived:
            redirect_status = prior_status if prior_status in field_action_candidates.REVIEW_STATUSES else "pending_review"
        current_rev = str(doc.get("_rev") or "")
        if expected_rev is not None and expected_rev != current_rev:
            raise field_action_candidates.AlreadyDecided(f"candidate _rev changed: {candidate_id}")
        field_action_candidates.set_action_candidate_archived(
            cdb_config,
            db,
            candidate_id,
            archived=archived,
            by=reviewer,
            expected_rev=expected_rev,
        )
    except field_action_candidates.AlreadyDecided:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"conflict: {candidate_id}"})
        return redirect_to_review(error="candidate changed before this action could be saved. Reload and try again.", status=redirect_status)
    except (field_action_candidates.CandidateReviewError, field_action_candidates.CouchDBCandidateWriterError, couchdb_config.CouchDBConfigError) as exc:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"failed: {exc}"})
        return redirect_to_review(error=str(exc), status=redirect_status)
    result = "archived" if archived else "restored"
    audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"success: {candidate_id} {result}"})
    return redirect_to_review(message=result, status=redirect_status)


def _handle_resubmit_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    reviewer = first_query_value(form, "reviewer").strip() or default_actor()
    expected_rev = first_query_value(form, "_rev").strip() or None
    route = "/field-capture/review/resubmit"
    runtime_resolved = ctx.runtime_root
    redirect_status = "failed"
    if not candidate_id:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": "failed: candidate_id is required"})
        return redirect_to_review(error="candidate_id is required", status=redirect_status)
    try:
        cdb_config = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        doc = field_action_candidates.get_action_candidate(cdb_config, db, candidate_id)
        if doc is None:
            raise field_action_candidates.CandidateReviewError(f"candidate not found: {candidate_id}")
        prior_status = str(doc.get("status") or "")
        redirect_status = prior_status if prior_status in {"failed", "rejected"} else "failed"
        if prior_status not in {"failed", "rejected"}:
            raise field_action_candidates.CandidateReviewError(f"candidate status cannot be resubmitted: {prior_status}")
        if doc.get("archived") is True:
            raise field_action_candidates.CandidateReviewError("archived candidate must be restored before resubmitting")
        current_rev = str(doc.get("_rev") or "")
        if expected_rev is not None and expected_rev != current_rev:
            raise field_action_candidates.AlreadyDecided(f"candidate _rev changed: {candidate_id}")
        candidate = field_action_candidates.couchdb_action_candidate_to_review_payload(doc)
        if not valid_proposed_jobs(candidate, runtime_resolved):
            raise field_action_candidates.CandidateReviewError("Can't resubmit — the proposed job isn't valid yet. Editing/fixing the payload is coming soon.")
        stage_summary = stage_candidate_job_after_approval(runtime_resolved, candidate_id)
        field_action_candidates.set_action_candidate_status(
            cdb_config,
            db,
            candidate_id,
            status="approved",
            reviewed_by=reviewer,
            rationale="Resubmitted directly from review triage.",
            expected_rev=expected_rev,
            expected_prior_statuses={"failed", "rejected"},
            reason="resubmit",
        )
    except field_action_candidates.AlreadyDecided:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"conflict: {candidate_id}"})
        return redirect_to_review(error="candidate changed before this action could be saved. Reload and try again.", status=redirect_status)
    except field_action_candidates.CandidateReviewError as exc:
        message = str(exc)
        if message != "Can't resubmit — the proposed job isn't valid yet. Editing/fixing the payload is coming soon.":
            message = f"Can't resubmit — {message}"
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"failed: {message}"})
        return redirect_to_review(error=message, status=redirect_status)
    except (field_action_candidates.CouchDBCandidateWriterError, couchdb_config.CouchDBConfigError) as exc:
        audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"failed: {exc}"})
        return redirect_to_review(error=str(exc), status=redirect_status)
    audit.append_audit(runtime_resolved, {"route": route, "actor": "localhost", "payload": ctx.form_payload(form), "result_summary": f"success: {candidate_id} resubmitted {stage_summary}"})
    return redirect_to_review(message="resubmitted", status="approved")


def _handle_client_informed_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    method = first_query_value(form, "method").strip()
    informed_by = first_query_value(form, "by").strip()
    note = first_query_value(form, "note").strip()
    runtime_resolved = ctx.runtime_root
    route = "/field-capture/review/client-informed"
    if not candidate_id:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: candidate_id is required",
            },
        )
        return redirect_to_review(error="candidate_id is required", status="approved")
    if method not in field_client_notifications.METHODS:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: client informed method is required",
            },
        )
        return redirect_to_review(error="client informed method is required", status="approved")
    if not informed_by:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: client informed by is required",
            },
        )
        return redirect_to_review(error="client informed by is required", status="approved")
    try:
        payload = field_client_notifications.mark_client_informed(
            candidate_id=candidate_id,
            method=method,
            informed_by=informed_by,
            note=note,
            runtime_root=runtime_resolved,
            candidate_dir=field_action_candidates.default_candidate_dir(runtime_resolved),
            notification_dir=field_client_notifications.default_notification_dir(runtime_resolved),
        )
    except field_client_notifications.ClientNotificationError as exc:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": f"failed: {exc}",
            },
        )
        return redirect_to_review(error=str(exc), status="approved")
    now = str(payload.get("client_informed_at") or "")
    try:
        patch_candidate_resolution(
            field_action_candidates.default_candidate_dir(runtime_resolved),
            candidate_id,
            resolution_status=RESOLUTION_CLIENT_NOTIFIED,
            by=informed_by,
            note=note,
            at=now,
            runtime_root=runtime_resolved,
        )
    except Exception as exc:
        LOGGER.warning("could not patch candidate resolution after client notification: %s", exc)
    audit.append_audit(
        runtime_resolved,
        {
            "route": route,
            "actor": "localhost",
            "payload": ctx.form_payload(form),
            "result_summary": f"success: {payload['candidate_id']} marked client informed",
        },
    )
    return redirect_to_review(message=f"{payload['candidate_id']} marked client informed", status="approved")


def _handle_resolve_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    resolved_by = first_query_value(form, "resolved_by").strip()
    resolved_note = first_query_value(form, "resolved_note").strip()
    runtime_resolved = ctx.runtime_root
    route = "/field-capture/review/resolve"
    if not candidate_id:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: candidate_id is required",
            },
        )
        return redirect_to_review(error="candidate_id is required", status="approved")
    if not resolved_by:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: resolved_by is required",
            },
        )
        return redirect_to_review(error="resolved_by is required", status="approved")
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_resolved)
    try:
        result = patch_candidate_resolution(
            candidate_dir,
            candidate_id,
            resolution_status=RESOLUTION_RESOLVED,
            by=resolved_by,
            note=resolved_note,
            runtime_root=runtime_resolved,
        )
    except Exception as exc:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": f"failed: {exc}",
            },
        )
        return redirect_to_review(error=str(exc), status="approved")
    audit.append_audit(
        runtime_resolved,
        {
            "route": route,
            "actor": "localhost",
            "payload": ctx.form_payload(form),
            "result_summary": f"success: {result['candidate_id']} marked resolved",
        },
    )
    return redirect_to_review(message="resolved", status="approved")


def _handle_completion_dismiss(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    reviewer = first_query_value(form, "reviewer").strip()
    expected_rev = first_query_value(form, "_rev").strip() or None
    runtime_resolved = ctx.runtime_root
    if not candidate_id:
        return redirect_to_inbox(error="candidate_id is required")
    if not reviewer:
        return redirect_to_inbox(error="reviewer is required")
    try:
        cdb_config = couchdb_config.from_env()
        result = field_action_candidates.apply_candidate_review(
            cdb_config,
            couchdb_config.field_captures_database(),
            candidate_id=candidate_id,
            action="reject",
            reviewer=reviewer,
            expected_rev=expected_rev,
            rationale=DISMISS_REASON_COMPLETION,
        )
        if result.error:
            return redirect_to_inbox(error=result.error)
        if result.already_decided:
            return redirect_to_inbox(error="candidate was already decided")
    except couchdb_config.CouchDBConfigError as exc:
        return redirect_to_inbox(error=str(exc))
    return redirect_to_inbox()
