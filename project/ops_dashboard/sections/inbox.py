from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from field_capture import action_candidates as field_action_candidates
from field_capture.review_status import review_status_report
from ops_dashboard.common import (
    UNKNOWN_SUBMITTER,
    apply_pending_candidate_counts,
    candidate_capture_sort_value,
    latest_uploads,
    load_photo_vision_sidecars,
    pending_candidate_counts_by_capture,
    read_json_artifact,
    render_capture_candidate_signal,
    render_table,
    render_relative_time,
    resolve_site_label,
    significant_warnings,
    submitters_by_capture,
)
from ops_dashboard.layout import html_page
from .health_pipeline import pipeline_status
from site_equipment import discover_site_equipment, priority_sort
from site_issues import discover_site_issues
from site_supplies import discover_site_supplies, urgency_sort

INBOX_SHAPE_CAPTURE = "capture"
INBOX_SHAPE_STRUCTURED = "structured"
INBOX_SHAPE_GAP = "gap"


def render(request_ctx: object) -> str:
    return render_inbox(request_ctx)


def age_seconds(path: Path) -> int:
    try:
        return max(0, int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
    except OSError:
        return 0


def age_seconds_from_timestamp(value: object) -> int:
    timestamp = str(value or "").strip()
    if not timestamp:
        return 0
    try:
        created_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0, int(datetime.now(timezone.utc).timestamp() - created_at.timestamp()))


def review_candidates(
    candidate_dir: Path,
    status: str | None,
    runtime_root: Path,
    *,
    submitters: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, object]]:
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    field_action_candidates.resolve_within_root(candidate_root, runtime_root.expanduser().resolve(strict=False))
    candidates: list[dict[str, object]] = []
    if submitters is None:
        submitters = submitters_by_capture(runtime_root)
    for path, payload in field_action_candidates.iter_candidate_artifacts(candidate_root):
        if payload.get("type") != "action_candidate_review":
            continue
        if status is not None and payload.get("status") != status:
            continue
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        metadata = payload.get("channel_metadata") if isinstance(payload.get("channel_metadata"), dict) else {}
        capture_id = str(metadata.get("upload_id") or "")
        submitter = submitters.get(capture_id, {})
        candidates.append(
            {
                "candidate_id": str(payload.get("candidate_id") or ""),
                "status": str(payload.get("status") or ""),
                "site_id": str(metadata.get("site_id") or provenance.get("site_id") or ""),
                "area": str(metadata.get("area") or ""),
                "capture_id": capture_id,
                "captured_at": str(metadata.get("captured_at") or payload.get("created_at") or ""),
                "submitter_name": str(metadata.get("submitter_name") or submitter.get("submitter_name") or ""),
                "summary": str(payload.get("summary") or ""),
                "source_text": str(payload.get("source_text") or ""),
                "source_context": str(payload.get("source_context") or ""),
                "source_transcript_path": str(provenance.get("source_transcript_path") or ""),
                "artifact_path": str(path),
                "visit_proposed": bool(metadata.get("visit_proposed") or payload.get("visit_proposed") or False),
            }
        )
    candidates.sort(key=lambda item: (str(item["candidate_id"]), str(item["artifact_path"])))
    return candidates


def candidate_inbox_row(candidate: dict[str, object], artifact_path: str = "") -> dict[str, object]:
    path = Path(artifact_path or str(candidate.get("artifact_path") or ""))
    metadata = candidate.get("channel_metadata") if isinstance(candidate.get("channel_metadata"), dict) else {}
    note_text = str(candidate.get("source_text") or "").strip()
    action_label = str(candidate.get("summary") or "").strip()
    display_summary = note_text or action_label
    # Keep the home grid scannable; full note is available through deep_link.
    if len(display_summary) > 200:
        display_summary = display_summary[:197].rstrip() + "..."
    return {
        "site": str(candidate.get("site_id") or ""),
        "area": str(candidate.get("area") or ""),
        "submitter": str(candidate.get("submitter_name") or UNKNOWN_SUBMITTER),
        "summary": display_summary,
        "age_seconds": age_seconds(path) if str(path) else 0,
        "deep_link": f"/candidates?candidate_id={quote(str(candidate.get('candidate_id') or ''))}",
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "capture_candidate_count": int(candidate.get("capture_candidate_count") or 0),
        "capture_signal": int(candidate.get("capture_candidate_count") or 0),
        "has_note": bool(candidate.get("source_transcript_path")),
        "visit_proposed": bool(metadata.get("visit_proposed") or candidate.get("visit_proposed") or False),
    }


def failed_job_rows(runtime_root: Path, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    failed_dir = runtime_root / "failed"
    # Non-recursive: failed/quarantine/ holds operator-acknowledged dead ends
    # and shouldn't pad the inbox "needs attention" count.
    paths = sorted(failed_dir.glob("*.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True) if failed_dir.exists() else []
    rows: list[dict[str, object]] = []
    for path in paths[:limit]:
        payload, _error = read_json_artifact(path)
        payload = payload or {}
        error_path = path.with_suffix(".error.txt")
        error_excerpt = "see logs"
        if error_path.exists():
            try:
                error_excerpt = error_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:180] or "see logs"
            except OSError:
                error_excerpt = "see logs"
        job_id = str(payload.get("job_id") or payload.get("computed_job_id") or path.stem)
        job_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        rows.append(
            {
                "job_type": str(payload.get("job_type") or ""),
                "site": str(payload.get("site_id") or job_payload.get("site_id") or ""),
                "error": error_excerpt,
                "age_seconds": age_seconds(path),
                "deep_link": f"/failed?job_id={quote(job_id)}",
            }
        )
    return len(paths), rows


def failed_photo_sidecar_rows(runtime_root: Path, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    failed_sidecars = [item for item in load_photo_vision_sidecars(runtime_root / "field_capture" / "photo_vision") if item.get("status") == "failed"]
    rows = []
    for item in failed_sidecars[:limit]:
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        path = Path(str(item.get("path") or ""))
        rows.append(
            {
                "site": str(item.get("site_id") or ""),
                "area": str(item.get("submitted_area") or item.get("area_guess") or ""),
                "error": str(error.get("message") or ", ".join(significant_warnings(item.get("warnings"))) or "failed"),
                "age_seconds": age_seconds(path) if str(path) else 0,
                "deep_link": f"/failed?sidecar_id={quote(str(item.get('photo_asset_id') or ''))}",
            }
        )
    return len(failed_sidecars), rows


def unknown_capture_rows(ctx: object, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    vault_dir = ctx.config.vault_dir
    journal_dir = vault_dir / "Journal"
    paths = sorted(journal_dir.glob("*-unknown.md"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True) if journal_dir.exists() else []
    rows = [
        {
            "site": "unknown",
            "area": "",
            "submitter": UNKNOWN_SUBMITTER,
            "summary": f"Unknown capture - {path.name.removesuffix('-unknown.md')}",
            "age_seconds": age_seconds(path),
            "deep_link": "/captures",
        }
        for path in paths[:limit]
    ]
    return len(paths), rows


def uploads_without_candidate_rows(
    runtime_root: Path,
    limit: int = 5,
    *,
    submitters: dict[str, dict[str, str]] | None = None,
    all_candidates: list[dict[str, object]] | None = None,
) -> tuple[int, list[dict[str, object]]]:
    if submitters is None:
        submitters = submitters_by_capture(runtime_root)
    if all_candidates is None:
        all_candidates = review_candidates(
            field_action_candidates.default_candidate_dir(runtime_root),
            None,
            runtime_root,
            submitters=submitters,
        )
    candidate_capture_ids = {str(candidate.get("capture_id") or "") for candidate in all_candidates}
    uploads = latest_uploads(runtime_root / "uploads", submitters, limit=1000)
    missing = [upload for upload in uploads if str(upload.get("capture_id") or "") not in candidate_capture_ids]
    rows = []
    intake_payloads = {}
    for path in (runtime_root / "field_capture" / "intake").glob("*.json") if (runtime_root / "field_capture" / "intake").exists() else []:
        payload, _error = read_json_artifact(path)
        if isinstance(payload, dict):
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            capture_id = str(metadata.get("capture_id") or body.get("capture_id") or "")
            if capture_id:
                intake_payloads[capture_id] = (metadata, body)
    for upload in missing[:limit]:
        capture_id = str(upload.get("capture_id") or "")
        metadata, body = intake_payloads.get(capture_id, ({}, {}))
        rows.append(
            {
                "site": str(metadata.get("site_id") or body.get("site_id") or ""),
                "area": str(body.get("area") or ""),
                "submitter": str(upload.get("submitter_name") or UNKNOWN_SUBMITTER),
                "summary": f"{upload.get('file_count', 0)} files; {upload.get('image_count', 0)} photos; {upload.get('audio_count', 0)} audio",
                "age_seconds": 0,
                "deep_link": f"/captures?capture_id={quote(capture_id)}",
            }
        )
    return len(missing), rows


def open_site_issues_rows(vault_root: Path, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_issues(vault_root)
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    open_issues = [issue for issue in issues if getattr(issue, "status", "") == "open"]
    open_issues.sort(
        key=lambda issue: (
            str(getattr(issue, "site_id", "")),
            str(getattr(issue, "created_at", "")),
            str(getattr(issue, "issue_id", "")),
        )
    )
    rows = [
        {
            "site": str(getattr(issue, "site_id", "")),
            "area": "",
            "submitter": str(getattr(issue, "reported_by", "")),
            "summary": str(getattr(issue, "title", "")),
            "age_seconds": age_seconds_from_timestamp(getattr(issue, "created_at", "")),
            "deep_link": f"/field-capture/issues?site_id={quote(str(getattr(issue, 'site_id', '')))}",
        }
        for issue in open_issues[:limit]
    ]
    return len(open_issues), rows


def open_supply_needs_rows(vault_root: Path, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_supplies(vault_root, status="open")
    supplies = report.get("supplies") if isinstance(report.get("supplies"), list) else []
    open_supplies = [supply for supply in supplies if getattr(supply, "status", "") == "open"]
    open_supplies.sort(
        key=lambda supply: (
            urgency_sort(str(getattr(supply, "urgency", ""))),
            str(getattr(supply, "site_id", "")),
            str(getattr(supply, "created_at", "")),
            str(getattr(supply, "supply_id", "")),
        )
    )
    rows = [
        {
            "site": str(getattr(supply, "site_id", "")),
            "area": "",
            "submitter": str(getattr(supply, "requested_by", "")),
            "summary": str(getattr(supply, "item_name", "")),
            "age_seconds": age_seconds_from_timestamp(getattr(supply, "created_at", "")),
            "deep_link": f"/supplies?supply_id={quote(str(getattr(supply, 'supply_id', '')))}",
        }
        for supply in open_supplies[:limit]
    ]
    return len(open_supplies), rows


def open_equipment_requests_rows(vault_root: Path, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_equipment(vault_root, status="open")
    equipment = report.get("equipment") if isinstance(report.get("equipment"), list) else []
    open_equipment = [request for request in equipment if getattr(request, "status", "") == "open"]
    open_equipment.sort(
        key=lambda request: (
            priority_sort(str(getattr(request, "priority", ""))),
            str(getattr(request, "site_id", "")),
            str(getattr(request, "created_at", "")),
            str(getattr(request, "equipment_id", "")),
        )
    )
    rows = [
        {
            "site": str(getattr(request, "site_id", "")),
            "area": "",
            "submitter": str(getattr(request, "requested_by", "")),
            "summary": str(getattr(request, "equipment_name", "")),
            "age_seconds": age_seconds_from_timestamp(getattr(request, "created_at", "")),
            "deep_link": f"/equipment?equipment_id={quote(str(getattr(request, 'equipment_id', '')))}",
        }
        for request in open_equipment[:limit]
    ]
    return len(open_equipment), rows


def inbox_cards(ctx: object) -> list[dict[str, object]]:
    runtime_resolved = ctx.runtime_root
    vault_root = ctx.config.vault_dir
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_resolved)
    # Walk the intake and candidate dirs once per render and reuse the results
    # across cards. Each walk is ~thousands of stat() calls on the runtime; the
    # earlier version did them 3x per inbox load.
    submitters = submitters_by_capture(runtime_resolved)
    all_candidates = review_candidates(candidate_dir, None, runtime_resolved, submitters=submitters)
    pending = [c for c in all_candidates if str(c.get("status") or "") == "pending_review"]
    apply_pending_candidate_counts(pending, pending_candidate_counts_by_capture(pending))
    pending.sort(key=candidate_capture_sort_value, reverse=True)
    note_bearing = [candidate for candidate in pending if candidate_inbox_row(candidate).get("has_note") is True]
    no_note = [candidate for candidate in pending if candidate not in note_bearing]
    status_report = review_status_report(runtime_root=runtime_resolved)
    gaps = status_report.get("lineage_gaps") if isinstance(status_report.get("lineage_gaps"), list) else []
    missing_draft = [gap for gap in gaps if isinstance(gap, dict) and gap.get("type") == "approved_candidate_missing_draft"]
    unstaged_drafts = [gap for gap in gaps if isinstance(gap, dict) and gap.get("type") == "approved_draft_missing_staging_status"]
    failed_count, failed_rows = failed_job_rows(runtime_resolved)
    failed_sidecar_count, failed_sidecar_top = failed_photo_sidecar_rows(runtime_resolved)
    unknown_count, unknown_rows = unknown_capture_rows(ctx)
    uploaded_count, uploaded_rows = uploads_without_candidate_rows(
        runtime_resolved, submitters=submitters, all_candidates=all_candidates
    )
    issues_count, issues_rows = open_site_issues_rows(vault_root)
    supplies_count, supplies_rows = open_supply_needs_rows(vault_root)
    equipment_count, equipment_rows = open_equipment_requests_rows(vault_root)
    pipeline = pipeline_status(runtime_resolved)
    pipeline_summary = pipeline.get("summary") if isinstance(pipeline.get("summary"), dict) else {}
    pipeline_failing = pipeline_summary.get("failing") if isinstance(pipeline_summary.get("failing"), list) else []
    fail_rows = [
        {
            "site": "",
            "area": "",
            "submitter": "",
            "summary": str(failing),
            "age_seconds": 0,
            "deep_link": "/health/pipeline",
        }
        for failing in pipeline_failing[:5]
    ]
    candidate_by_id = {str(item.get("candidate_id") or ""): item for item in all_candidates}
    missing_rows = []
    for gap in missing_draft[:5]:
        candidate = candidate_by_id.get(str(gap.get("candidate_id") or ""))
        if candidate:
            row = candidate_inbox_row(candidate, str(gap.get("artifact_path") or candidate.get("artifact_path") or ""))
            row["deep_link"] = f"/drafts?candidate_id={quote(str(gap.get('candidate_id') or ''))}"
            missing_rows.append(row)
    draft_rows = [
        {
            "site": "",
            "area": "",
            "submitter": "",
            "summary": str(gap.get("draft_id") or ""),
            "age_seconds": age_seconds(Path(str(gap.get("artifact_path") or ""))),
            "deep_link": f"/drafts?draft_id={quote(str(gap.get('draft_id') or ''))}",
        }
        for gap in unstaged_drafts[:5]
        if isinstance(gap, dict)
    ]
    cards = [
        {"id": "captures_with_note", "title": "Captures with a note — needs triage", "count": len(note_bearing), "top": [candidate_inbox_row(item) for item in note_bearing[:5]], "see_all": "/candidates?status=pending_review&has_audio=true", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "pending_candidates", "title": "Pending candidates without a note", "count": len(no_note), "top": [candidate_inbox_row(item) for item in no_note[:5]], "see_all": "/candidates?status=pending_review", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "approved_missing_draft", "title": "Approved candidates missing a draft", "count": len(missing_draft), "top": missing_rows, "see_all": "/drafts", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "approved_drafts_not_staged", "title": "Approved drafts not yet staged", "count": len(unstaged_drafts), "top": draft_rows, "see_all": "/drafts", "shape": INBOX_SHAPE_GAP},
        {"id": "failed_queue_jobs", "title": "Failed queue jobs", "count": failed_count, "top": failed_rows, "see_all": "/failed", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "failed_photo_vision_sidecars", "title": "Failed photo vision sidecars", "count": failed_sidecar_count, "top": failed_sidecar_top, "see_all": "/failed", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "unknown_captures", "title": "Unknown captures", "count": unknown_count, "top": unknown_rows, "see_all": "/captures", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "uploaded_without_candidate", "title": "Recently uploaded with no candidate yet", "count": uploaded_count, "top": uploaded_rows, "see_all": "/captures", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "open_site_issues", "title": "Open site issues", "count": issues_count, "top": issues_rows, "see_all": "/field-capture/issues", "shape": INBOX_SHAPE_STRUCTURED},
        {"id": "open_supply_needs", "title": "Open supply needs", "count": supplies_count, "top": supplies_rows, "see_all": "/supplies?status=open", "shape": INBOX_SHAPE_STRUCTURED},
        {"id": "open_equipment_requests", "title": "Open equipment requests", "count": equipment_count, "top": equipment_rows, "see_all": "/equipment?status=open", "shape": INBOX_SHAPE_STRUCTURED},
        {"id": "pipeline_health", "title": "Pipeline health", "count": 0 if pipeline_summary.get("ok") else len(pipeline_failing), "top": fail_rows, "see_all": "/health/pipeline", "shape": INBOX_SHAPE_GAP},
    ]
    return cards


def inbox_payload(ctx: object) -> dict[str, object]:
    cards = [card for card in inbox_cards(ctx) if card.get("id") != "pipeline_health"]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "cards": cards}


def count_bucket(count: object) -> str:
    value = int(count or 0)
    if value == 0:
        return "empty"
    if value <= 5:
        return "low"
    if value <= 20:
        return "warn"
    return "high"


def inbox_columns(shape: object, vault_root: Path) -> list[dict[str, object]]:
    def site_formatter(value: object, _item: dict[str, object]) -> str:
        return resolve_site_label(value, vault_root)

    def link_formatter(value: object, item: dict[str, object]) -> str:
        summary = html.escape(str(value or ""))
        link = str(item.get("deep_link") or "")
        linked_summary = f'<a href="{html.escape(link)}">{summary}</a>' if link else summary
        signal = render_capture_candidate_signal(item.get("capture_candidate_count"))
        return f"{linked_summary} {signal}".rstrip()

    time_formatter = lambda value, _item: render_relative_time(value)
    summary_column = {"key": "summary", "label": "Summary", "format": link_formatter, "priority": 1}
    age_column = {"key": "age_seconds", "label": "Age", "format": time_formatter, "priority": 1}
    if shape == INBOX_SHAPE_GAP:
        return [summary_column, age_column]
    if shape == INBOX_SHAPE_STRUCTURED:
        return [
            {"key": "site", "label": "Site", "format": site_formatter, "priority": 1},
            {"key": "submitter", "label": "Requested by", "priority": 2},
            summary_column,
            age_column,
        ]
    return [
        {"key": "site", "label": "Site", "format": site_formatter, "priority": 1},
        {"key": "area", "label": "Area", "priority": 2},
        {"key": "submitter", "label": "Submitter", "priority": 2},
        summary_column,
        age_column,
    ]


def _card_by_id(cards: list[dict[str, object]], card_id: str) -> dict[str, object]:
    return next(card for card in cards if card.get("id") == card_id)


def _see_all_html(card: dict[str, object], count: int) -> str:
    if count <= 0:
        return ""
    return f'<p><a href="{html.escape(str(card.get("see_all") or "#"))}">See all</a></p>'


def _render_primary_card(card: dict[str, object], vault_root: Path) -> str:
    rows = card.get("top") if isinstance(card, dict) else []
    table = render_table(rows if isinstance(rows, list) else [], inbox_columns(card.get("shape"), vault_root), empty_text="Nothing waiting")
    count = int(card.get("count") or 0)
    bucket = count_bucket(count)
    return f"""<article class="inbox-card" data-card-id="{html.escape(str(card.get('id') or ''))}">
      <h2>{html.escape(str(card.get('title') or ''))}</h2>
      <p class="count" data-count-bucket="{bucket}">{html.escape(str(count))}</p>
      {table}
      {_see_all_html(card, count)}
    </article>"""


def _render_stat_badge(card: dict[str, object]) -> str:
    count = int(card.get("count") or 0)
    bucket = count_bucket(count)
    label = html.escape(str(card.get("title") or ""))
    inner = f'<strong>{html.escape(str(count))}</strong><span>{label}</span>'
    if count <= 0:
        return f'<span class="stat-badge" data-count-bucket="{bucket}" data-stat-id="{html.escape(str(card.get("id") or ""))}">{inner}</span>'
    return f'<a class="stat-badge" data-count-bucket="{bucket}" data-stat-id="{html.escape(str(card.get("id") or ""))}" href="{html.escape(str(card.get("see_all") or "#"))}">{inner}</a>'


def _render_summary_segment(card: dict[str, object], label: str) -> str:
    count = int(card.get("count") or 0)
    text = f"{label}: {count}"
    if count <= 0:
        return f'<span data-summary-id="{html.escape(str(card.get("id") or ""))}">{html.escape(text)}</span>'
    return f'<a data-summary-id="{html.escape(str(card.get("id") or ""))}" href="{html.escape(str(card.get("see_all") or "#"))}">{html.escape(text)}</a>'


def render_inbox(ctx: object) -> str:
    runtime_root = ctx.runtime_root
    payload = inbox_payload(ctx)
    vault_root = ctx.config.vault_dir
    cards = payload["cards"] if isinstance(payload["cards"], list) else []
    stat_ids = ["captures_with_note", "pending_candidates", "failed_queue_jobs", "unknown_captures", "open_site_issues"]
    primary_ids = ["captures_with_note", "pending_candidates", "failed_queue_jobs", "unknown_captures", "open_site_issues"]
    summary_specs = [
        ("approved_missing_draft", "Missing drafts"),
        ("approved_drafts_not_staged", "Unstaged drafts"),
        ("failed_photo_vision_sidecars", "Vision sidecars"),
        ("uploaded_without_candidate", "Uploads w/o candidate"),
        ("open_supply_needs", "Supply"),
        ("open_equipment_requests", "Equipment"),
    ]
    stat_strip = "".join(_render_stat_badge(_card_by_id(cards, card_id)) for card_id in stat_ids)
    primary_cards = "".join(_render_primary_card(_card_by_id(cards, card_id), vault_root) for card_id in primary_ids)
    summary_segments = []
    for card_id, label in summary_specs:
        if summary_segments:
            summary_segments.append('<span class="summary-separator">·</span>')
        summary_segments.append(_render_summary_segment(_card_by_id(cards, card_id), label))
    body = f"""
    <header>
      <h1>Inbox</h1>
      <p class="muted">What needs operator attention right now. Runtime root: <code>{html.escape(str(runtime_root))}</code></p>
    </header>
    <section class="stat-strip" aria-label="Inbox counts">
      {stat_strip}
    </section>
    <section class="inbox-primary">
      {primary_cards}
    </section>
    <section>
      <h2>Summary</h2>
      <p class="inbox-summary-strip">{"".join(summary_segments)}</p>
    </section>
    """
    return html_page("BTQ Ops Inbox", body, active_section="inbox", refresh=False)
