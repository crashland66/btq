from __future__ import annotations

import html
import json
import logging
import mimetypes
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from field_capture import action_candidates as field_action_candidates
from field_capture import client_notifications as field_client_notifications
from field_capture import job_draft_review
from event_pipeline import couchdb_config
from ops_dashboard import audit
from ops_dashboard.common import (
    apply_pending_candidate_counts,
    candidate_capture_sort_value,
    default_actor,
    first_filter_value,
    first_query_value,
    human_source_details_by_capture,
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


def handle_edit(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_edit_post(request_ctx, body)


def handle_approve_set(request_ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return _handle_approve_set_post(request_ctx, body)


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


def is_audio_file(path: Path) -> bool:
    mime = mimetypes.guess_type(path.name)[0] or ""
    return mime.startswith("audio/") or path.suffix.lower() in {".webm", ".m4a", ".mp3", ".wav", ".aac"}


def candidate_counts(candidate_dir: Path, runtime_root: Path) -> dict[str, int]:
    counts = {"pending_approval": 0, "approved": 0, "rejected": 0}
    for _path, payload in job_draft_review.couchdb_job_draft_payloads():
        status = str(payload.get("review_status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def candidate_detail(
    path: Path,
    payload: dict[str, object],
    runtime_root: Path | None = None,
    vision_items: list[dict[str, object]] | None = None,
    notification: dict[str, object] | None = None,
    human_source: dict[str, str] | None = None,
) -> dict[str, object]:
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    draft_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    proposed_job_type = str(payload.get("job_type") or "")
    proposed_error = str(payload.get("validation_error") or "")
    if not proposed_error and not validate_job({"job_type": proposed_job_type, "payload": draft_payload}):
        proposed_error = "job_draft fails queue_spec validation"
    capture_id = str(payload.get("source_capture_id") or "")
    if human_source is None and runtime_root is not None and capture_id:
        human_source = human_source_details_by_capture(runtime_root).get(capture_id, {})
    human_source = human_source if isinstance(human_source, dict) else {}
    resolved_source_text = str(human_source.get("source_text") or "").strip()
    cleaned_internal_note = str(human_source.get("cleaned_internal_note") or "").strip()
    return {
        "candidate_id": str(payload.get("draft_id") or ""),
        "draft_id": str(payload.get("draft_id") or ""),
        "status": str(payload.get("review_status") or ""),
        "review_status": str(payload.get("review_status") or ""),
        "site_id": str(payload.get("site_id") or ""),
        "area": "",
        "visit_proposed": False,
        "visit_type": "",
        "capture_id": capture_id,
        "captured_at": str(payload.get("created_at") or ""),
        "submitter_name": str(payload.get("submitter_name") or ""),
        "submitter_id": "",
        "audio_asset_id": "",
        "summary": str(payload.get("message") or ""),
        "rationale": "",
        "source_text": resolved_source_text,
        "source_context": cleaned_internal_note if cleaned_internal_note != resolved_source_text else "",
        "source_is_human": bool(resolved_source_text),
        "source_missing_message": "" if resolved_source_text else "No human source found for originating capture.",
        "semantic_artifact_path": str(human_source.get("semantic_artifact_path") or ""),
        "source_transcript_path": str(human_source.get("source_transcript_path") or ""),
        "artifact_path": str(path),
        "reviewer": str(payload.get("reviewed_by") or ""),
        "reviewed_at": str(payload.get("reviewed_at") or ""),
        "review_rationale": str(payload.get("review_rationale") or ""),
        "review_history": [],
        "vision_items": vision_items or [],
        "client_notification": notification or {},
        "resolution": resolution,
        "archived": False,
        "archived_at": "",
        "archived_by": "",
        "proposed_job_type": str(proposed_job_type or ""),
        "proposed_payload": draft_payload,
        "proposed_job_error": str(proposed_error or ""),
        "_rev": str(payload.get("_rev") or ""),
        "proposed_jobs": [
            {"job_type": proposed_job_type, "payload": draft_payload, "error": str(proposed_error or "")}
        ],
    }


def review_candidates(candidate_dir: Path, status: str | None, runtime_root: Path) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    vision_by_capture = photo_vision_by_capture(runtime_root)
    human_sources = human_source_details_by_capture(runtime_root)
    submitters = submitters_by_capture(runtime_root)
    for path, payload in job_draft_review.couchdb_job_draft_payloads(review_status=status):
        if payload.get("type") != "job_draft_review":
            continue
        capture_id = str(payload.get("source_capture_id") or "")
        notification: dict[str, object] = {}
        candidate = candidate_detail(
            path,
            payload,
            runtime_root,
            vision_by_capture.get(capture_id, []),
            notification,
            human_sources.get(capture_id, {}),
        )
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
        ("pending", "/field-capture/review?status=pending_approval", selected_status == "pending_approval"),
        ("open", "/field-capture/review?status=approved&resolution=open", selected_status == "approved"),
        ("rejected", "/field-capture/review?status=rejected", selected_status == "rejected"),
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


def render_source_pill(candidate: dict[str, object]) -> str:
    if not candidate.get("source_is_human"):
        return ""
    return '<span class="pill source-human">source: human</span>'


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
    source_is_human = bool(candidate.get("source_is_human"))
    pieces = []
    if source_text:
        pieces.append(f"<blockquote>{html.escape(source_text)}</blockquote>")
    elif source_context:
        pieces.append(f"<blockquote>{html.escape(source_context)}</blockquote>")
    elif not source_is_human:
        missing_message = str(
            candidate.get("source_missing_message") or "No human source found for originating capture."
        )
        pieces.append(
            f'<p class="muted">{html.escape(missing_message)}</p>'
        )
    if rationale:
        pieces.append(f"<p>{html.escape(rationale)}</p>")
    if not pieces:
        pieces.append('<p class="muted">No human source found for originating capture.</p>')
    label = "Reported by cleaner" if source_is_human else "Cleaner report"
    return f"""
    <section class="decision-evidence">
      <p class="muted">{html.escape(label)}</p>
      {''.join(pieces)}
    </section>
    """


def candidate_proposed_error(candidate: dict[str, object]) -> str:
    proposed_jobs = candidate.get("proposed_jobs") if isinstance(candidate.get("proposed_jobs"), list) else []
    proposed_error = str(candidate.get("proposed_job_error") or "")
    if proposed_jobs:
        proposed_error = next((str(job.get("error") or "") for job in proposed_jobs if isinstance(job, dict) and job.get("error")), "")
    return proposed_error


def render_candidate_edit_form(candidate: dict[str, object]) -> str:
    raw_draft_id = str(candidate.get("draft_id") or candidate.get("candidate_id") or "")
    draft_id = html.escape(raw_draft_id)
    rev = html.escape(str(candidate.get("_rev") or ""), quote=True)
    payload = candidate.get("proposed_payload") if isinstance(candidate.get("proposed_payload"), dict) else {}
    payload_text = json.dumps(payload, indent=2, sort_keys=True)
    return f"""
    <details class="review-action-form edit-form">
      <summary>Edit payload</summary>
      <form method="post" action="/field-capture/review/edit">
        <p class="action-identity">
          <strong>Edit</strong>
          <code>{draft_id}</code><br>
          Update this draft payload, then revalidate it.
        </p>
        <input type="hidden" value="{draft_id}" name="draft_id">
        <input type="hidden" name="_rev" value="{rev}">
        <label>Reviewer</label>
        <input name="reviewer" value="{html.escape(default_actor())}" required>
        <label>Payload JSON</label>
        <textarea name="payload" rows="10" spellcheck="false" required>{html.escape(payload_text)}</textarea>
        <p><button class="icon-button" type="submit" aria-label="Save edited draft payload">Save payload</button></p>
      </form>
    </details>
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
    actions = ""
    if candidate["status"] == "pending_approval":
        raw_draft_id = str(candidate.get("draft_id") or candidate["candidate_id"])
        draft_id = html.escape(raw_draft_id)
        proposed_error = candidate_proposed_error(candidate)
        approve_disabled = " disabled" if proposed_error else ""
        approve_hint = "Approve and stage this job" if not proposed_error else "Cannot approve until the proposed job is valid"
        actions = f"""
        <div class="actions decision-actions">
          <form class="review-action-form approve-form" method="post" action="/field-capture/review/approve">
            <p class="action-identity">
              <strong>Approve</strong>
              <code>{draft_id}</code><br>
              Stage this job for processing.
            </p>
            <input type="hidden" name="draft_id" value="{draft_id}">
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
              <code>{draft_id}</code><br>
              Leave the data unchanged.
            </p>
            <input type="hidden" name="draft_id" value="{draft_id}">
            <input type="hidden" name="_rev" value="{html.escape(str(candidate.get('_rev') or ''), quote=True)}">
            <label>Reviewer</label>
            <input name="reviewer" value="{html.escape(default_actor())}" required>
            <label>Note</label>
            <textarea name="rationale" rows="2"></textarea>
            <p><button class="reject icon-button" type="submit" aria-label="Deny this proposed job">{render_action_icon("deny")} <span>Deny</span></button></p>
          </form>
          {render_candidate_edit_form(candidate)}
        </div>
        """
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
            "draft_id": candidate.get("draft_id", ""),
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
      <p class="candidate-action"><strong>{summary}</strong> {render_source_pill(candidate)}</p>
      {actions}
      <details>
        <summary>Review details</summary>
        {render_proposed_job(candidate)}
        {render_evidence_summary(candidate)}
        <section class="machine-interpretation">
          <p class="muted">Machine rationale</p>
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
        f' width="{size}" height="{size}" loading="lazy" decoding="async"'
        f' style="object-fit:cover" alt="photo"></a>'
        for url in urls
    ]
    return f'<div class="thumb-strip">{"".join(thumbs)}</div>'


def candidate_has_warning(candidate: dict[str, object]) -> bool:
    vision_items = candidate.get("vision_items") if isinstance(candidate.get("vision_items"), list) else []
    return any(significant_warnings(item.get("warnings") if isinstance(item, dict) else []) for item in vision_items)


def filter_candidates(candidates: list[dict[str, object]], runtime_root: Path, query: dict[str, list[str]]) -> list[dict[str, object]]:
    candidate_id = first_query_value(query, "candidate_id").strip() or first_query_value(query, "draft_id").strip()
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
        filtered = [
            candidate
            for candidate in filtered
            if str(candidate.get("candidate_id") or "") == candidate_id
            or str(candidate.get("draft_id") or "") == candidate_id
        ]
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
    selected_status = first_filter_value(query, "status") or "pending_approval"
    statuses = ["pending_approval", "approved", "rejected", "all"]
    status_controls = "".join(
        f'<label><input type="radio" name="status" value="{html.escape(status)}"{" checked" if status == selected_status else ""}> {html.escape(status.replace("_", " "))}</label>'
        for status in statuses
    )
    resolution_controls = ""
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
        # The capture IS the message + photos; the cards below are its proposed
        # actions. Lead with what the worker said, then who/site, then thumbnails.
        raw_message = (
            str(first.get("source_text") or "").strip()
            or str(first.get("source_context") or "").strip()
        )
        if raw_message:
            message_html = html.escape(raw_message)
        else:
            message_html = '<span class="muted">No human source found for originating capture.</span>'
        who = html.escape(str(first.get("submitter_name") or UNKNOWN_SUBMITTER))
        site = html.escape(str(first.get("site_id") or ""))
        when = str(first.get("captured_at") or "").strip()
        meta_html = f"{who} · Site {site}" + (f" · {html.escape(when)}" if when else "")
        vision_note = (
            f'<p class="muted candidate-vision-note">{html.escape(vision_summary[:220])}</p>'
            if vision_summary
            else ""
        )
        cards_html = "".join(render_candidate_card(candidate, thumb_urls) for candidate in group)
        rendered.append(
            f"""<section class="candidate-group" id="capture-{html.escape(capture_id)}">
        <div class="candidate-group-header">
          <div>
            <p class="candidate-message">{message_html}</p>
            <p class="candidate-meta muted">{meta_html}</p>
            {capture_signal}
            {vision_note}
          </div>
          {thumb_strip}
        </div>
        {cards_html}
      </section>"""
        )
    return "".join(rendered)


def render_field_capture_review(runtime_root: Path, query: dict[str, list[str]]) -> str:
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    explicit_status = bool(first_filter_value(query, "status"))
    selected_status = first_filter_value(query, "status") or "pending_approval"
    if selected_status == "all":
        status_filter = None
    elif selected_status in {"pending_approval", "approved", "rejected"}:
        status_filter = selected_status
    else:
        selected_status = "pending_approval"
        status_filter = selected_status
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_resolved)
    counts = candidate_counts(candidate_dir, runtime_resolved)
    all_candidates = review_candidates(candidate_dir, None, runtime_resolved)
    pending_counts = pending_candidate_counts_by_capture(all_candidates)
    candidates = review_candidates(candidate_dir, status_filter, runtime_resolved)
    effective_query = dict(query)
    candidates = filter_candidates(candidates, runtime_resolved, effective_query)
    apply_pending_candidate_counts(candidates, pending_counts)
    candidates.sort(key=candidate_capture_sort_value, reverse=True)
    cards = render_candidate_groups(candidates, runtime_resolved)
    body = f"""
    <header>
      <p><a href="/">BTQ Ops Dashboard</a></p>
      <h1>Captures</h1>
      <p class="muted">Runtime root: <code>{html.escape(str(runtime_resolved))}</code></p>
      <p class="muted">Private localhost/Tailscale use only. Actions on this page review job drafts; vault mutation remains separate.</p>
    </header>
    {render_flash(query)}
    <section class="notice">
      <h2>Review Counts</h2>
      {render_kv(counts)}
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
          <h2>{html.escape(selected_status.replace("_", " ").title())} Drafts</h2>
          <p class="muted">Showing {len(candidates)} job draft records grouped by capture.</p>
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


def _handle_review_post(action: str, ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    draft_id = first_query_value(form, "draft_id").strip()
    reviewer = first_query_value(form, "reviewer").strip()
    rationale = first_query_value(form, "rationale").strip()
    expected_rev = first_query_value(form, "_rev").strip() or None
    route = f"/field-capture/review/{action}"
    runtime_resolved = ctx.runtime_root
    # Operator scope is provided by the ops-dashboard binding to localhost only;
    # reviewer is audit attribution, not authentication.
    if not draft_id:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": "failed: draft_id is required",
            },
        )
        return redirect_to_inbox(error="draft_id is required")
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
        result = job_draft_review.apply_job_draft_review(
            cdb_config,
            couchdb_config.field_captures_database(),
            draft_id,
            action,
            reviewer,
            expected_rev=expected_rev,
            rationale=rationale,
        )
        if result.error:
            raise RuntimeError(result.error)
    except (RuntimeError, couchdb_config.CouchDBConfigError) as exc:
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
                "result_summary": f"already_decided: {result.draft_id} review_status={result.review_status}",
            },
        )
        return redirect_to_inbox(error="already decided by another reviewer")
    audit.append_audit(
        runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": f"success: {result.draft_id} marked {result.review_status}",
            },
        )
    message = "approved" if action == "approve" else "denied"
    return redirect_to_inbox(message=message)


def _handle_edit_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    draft_id = first_query_value(form, "draft_id").strip()
    reviewer = first_query_value(form, "reviewer").strip()
    expected_rev = first_query_value(form, "_rev").strip() or None
    payload_text = first_query_value(form, "payload")
    route = "/field-capture/review/edit"
    runtime_resolved = ctx.runtime_root

    def audit_result(result_summary: str) -> None:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": result_summary,
            },
        )

    if not draft_id:
        audit_result("failed: draft_id is required")
        return redirect_to_inbox(error="draft_id is required")
    if not reviewer:
        audit_result("failed: reviewer is required")
        return redirect_to_inbox(error="reviewer is required")
    try:
        new_payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        message = f"malformed payload JSON: {exc.msg}"
        audit_result(f"failed: {message}")
        return redirect_to_inbox(error=message)
    if not isinstance(new_payload, dict):
        message = "payload JSON must be an object"
        audit_result(f"failed: {message}")
        return redirect_to_inbox(error=message)

    try:
        cdb_config = couchdb_config.from_env()
        result = job_draft_review.edit_job_draft_payload(
            cdb_config,
            couchdb_config.field_captures_database(),
            draft_id,
            new_payload,
            editor=reviewer,
            expected_rev=expected_rev,
        )
    except couchdb_config.CouchDBConfigError as exc:
        audit_result(f"failed: {exc}")
        return redirect_to_inbox(error=str(exc))

    if result.already_decided:
        audit_result(f"already_decided: {result.draft_id} review_status={result.review_status}")
        return redirect_to_inbox(error="already decided")
    if result.error:
        audit_result(f"failed: {result.error}")
        return redirect_to_inbox(error=result.error)
    audit_result(f"success: {result.draft_id} payload updated")
    return redirect_to_inbox(message="payload updated")


def _handle_approve_set_post(ctx: object, body: bytes) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    draft_ids = [str(value).strip() for value in form.get("draft_id", []) if str(value).strip()]
    revs = [str(value).strip() for value in form.get("_rev", [])]
    checked = {str(value).strip() for value in form.get("checked", []) if str(value).strip()}
    reviewer = first_query_value(form, "reviewer").strip()
    rationale = first_query_value(form, "rationale").strip()
    route = "/field-capture/review/approve-set"
    runtime_resolved = ctx.runtime_root

    def audit_result(result_summary: str) -> None:
        audit.append_audit(
            runtime_resolved,
            {
                "route": route,
                "actor": "localhost",
                "payload": ctx.form_payload(form),
                "result_summary": result_summary,
            },
        )

    if not draft_ids:
        audit_result("failed: draft_id is required")
        return redirect_to_inbox(error="draft_id is required")
    if not reviewer:
        audit_result("failed: reviewer is required")
        return redirect_to_inbox(error="reviewer is required")

    approved = 0
    rejected = 0
    already_decided = 0
    errors: list[str] = []
    try:
        cdb_config = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        for index, draft_id in enumerate(draft_ids):
            expected_rev = revs[index] if index < len(revs) and revs[index] else None
            action = "approve" if draft_id in checked else "reject"
            result = job_draft_review.apply_job_draft_review(
                cdb_config,
                db,
                draft_id,
                action,
                reviewer,
                expected_rev=expected_rev,
                rationale=rationale,
            )
            if result.already_decided:
                already_decided += 1
            elif result.error:
                errors.append(f"{draft_id}: {result.error}")
            elif action == "approve":
                approved += 1
            else:
                rejected += 1
    except couchdb_config.CouchDBConfigError as exc:
        audit_result(f"failed: {exc}")
        return redirect_to_inbox(error=str(exc))

    summary = f"approved {approved}, rejected {rejected}, {already_decided} already-decided"
    if errors:
        summary = f"{summary}, {len(errors)} error"
        if len(errors) != 1:
            summary += "s"
    audit_result(f"success: {summary}" if not errors else f"partial: {summary}; {'; '.join(errors)}")
    if errors:
        return redirect_to_inbox(error=f"{summary}: {'; '.join(errors)}")
    return redirect_to_inbox(message=summary)


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
