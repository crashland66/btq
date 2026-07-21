from __future__ import annotations

import html
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from event_pipeline import couchdb_config
from field_capture import action_candidates as field_action_candidates
from field_capture import job_draft_review
from field_capture.review_status import review_status_report
from ops_dashboard.common import (
    UNKNOWN_SUBMITTER,
    apply_pending_candidate_counts,
    candidate_capture_sort_value,
    default_actor,
    latest_uploads,
    load_photo_vision_sidecars,
    pending_candidate_counts_by_capture,
    query_couchdb_find,
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
from . import site_orders

INBOX_SHAPE_CAPTURE = "capture"
INBOX_SHAPE_STRUCTURED = "structured"
INBOX_SHAPE_GAP = "gap"
FAILED_CAPTURE_COUNT_PAGE_SIZE = 5000
FAILED_CAPTURE_COUNT_PAGE_CAP = 20
FAILED_CAPTURE_COUNT_CACHE_TTL_SECONDS = 60.0
FAILED_CAPTURE_ROW_LIMIT = 5
FAILED_CAPTURE_REASON_PLACEHOLDER = "reason not recorded"
_failed_capture_count_cache: dict[str, tuple[float, int | None]] = {}
_failed_capture_count_lock = threading.Lock()


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
    candidates: list[dict[str, object]] = []
    if submitters is None:
        submitters = submitters_by_capture(runtime_root)
    for path, payload in job_draft_review.couchdb_job_draft_payloads(review_status=status):
        if payload.get("type") != "job_draft_review":
            continue
        capture_id = str(payload.get("source_capture_id") or "")
        submitter = submitters.get(capture_id, {})
        candidates.append(
            {
                "candidate_id": str(payload.get("draft_id") or ""),
                "draft_id": str(payload.get("draft_id") or ""),
                "status": str(payload.get("review_status") or ""),
                "review_status": str(payload.get("review_status") or ""),
                "site_id": str(payload.get("site_id") or ""),
                "area": "",
                "capture_id": capture_id,
                "captured_at": str(payload.get("created_at") or ""),
                "submitter_name": str(payload.get("submitter_name") or submitter.get("submitter_name") or ""),
                "summary": str(payload.get("message") or ""),
                "source_text": str(payload.get("message") or ""),
                "source_context": "",
                "source_transcript_path": str(payload.get("source_capture_id") or ""),
                "artifact_path": str(path),
                "visit_proposed": False,
                "job_type": str(payload.get("job_type") or ""),
                # _rev + group_id ride along so the inbox can render the grouped
                # approve-set form (the endpoint pairs _rev to draft_id by position).
                "_rev": str(payload.get("_rev") or ""),
                "group_id": str(payload.get("group_id") or ""),
            }
        )
    candidates.sort(key=lambda item: (str(item["candidate_id"]), str(item["artifact_path"])))
    return candidates


def candidate_inbox_row(candidate: dict[str, object], artifact_path: str = "") -> dict[str, object]:
    path = Path(artifact_path or str(candidate.get("artifact_path") or ""))
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
        "deep_link": f"/candidates?draft_id={quote(str(candidate.get('draft_id') or candidate.get('candidate_id') or ''))}",
        "candidate_id": str(candidate.get("candidate_id") or ""),
        "draft_id": str(candidate.get("draft_id") or ""),
        "capture_candidate_count": int(candidate.get("capture_candidate_count") or 0),
        "capture_signal": int(candidate.get("capture_candidate_count") or 0),
        "has_note": bool(candidate.get("source_transcript_path")),
        "visit_proposed": bool(candidate.get("visit_proposed") or False),
    }


def candidate_group_key(candidate: dict[str, object]) -> str:
    """Drafts split out of one capture share group_id/capture_id."""
    return (
        str(candidate.get("group_id") or "").strip()
        or str(candidate.get("capture_id") or "").strip()
        or str(candidate.get("draft_id") or candidate.get("candidate_id") or "").strip()
    )


def candidate_group_line(candidate: dict[str, object]) -> dict[str, object]:
    """One checklist line inside a grouped approve-set card."""
    row = candidate_inbox_row(candidate)
    summary = str(candidate.get("summary") or candidate.get("source_text") or "").strip()
    if len(summary) > 140:
        summary = summary[:137].rstrip() + "..."
    return {
        "draft_id": str(candidate.get("draft_id") or candidate.get("candidate_id") or ""),
        "_rev": str(candidate.get("_rev") or ""),
        "job_type": str(candidate.get("job_type") or ""),
        "summary": summary,
        "deep_link": str(row.get("deep_link") or ""),
    }


def group_pending_candidates(
    candidates: list[dict[str, object]], limit: int = 5
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split pending drafts into single rows (rendered as today) and grouped cards.

    Order of the incoming list is preserved; a capture takes the position of its
    first draft. Returns (rows, groups) capped at `limit` entries combined.
    """
    order: list[str] = []
    buckets: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        key = candidate_group_key(candidate)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(candidate)

    rows: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    entries = 0
    for key in order:
        if entries >= limit:
            break
        members = buckets[key]
        if len(members) == 1:
            # Regression-sensitive: a single-draft capture renders exactly as before.
            rows.append(candidate_inbox_row(members[0]))
        else:
            first = members[0]
            groups.append(
                {
                    "group_id": key,
                    "capture_id": str(first.get("capture_id") or ""),
                    "site": str(first.get("site_id") or ""),
                    "submitter": str(first.get("submitter_name") or UNKNOWN_SUBMITTER),
                    "age_seconds": max(
                        age_seconds(Path(str(member.get("artifact_path") or ""))) if str(member.get("artifact_path") or "") else 0
                        for member in members
                    ),
                    "draft_count": len(members),
                    "lines": [candidate_group_line(member) for member in members],
                }
            )
        entries += 1
    return rows, groups


def _render_group_line(line: dict[str, object]) -> str:
    draft_id = html.escape(str(line.get("draft_id") or ""))
    deep_link = html.escape(str(line.get("deep_link") or ""))
    job_type = html.escape(str(line.get("job_type") or "") or "(unknown job)")
    summary = html.escape(str(line.get("summary") or ""))
    open_link = f'<a class="inbox-group-open" href="{deep_link}">Open</a>' if deep_link else ""
    # draft_id + _rev are hidden inputs so both always post, in document order —
    # /field-capture/review/approve-set pairs revs[index] to draft_ids[index].
    return f"""<li class="inbox-group-item">
        <input type="hidden" name="draft_id" value="{draft_id}">
        <input type="hidden" name="_rev" value="{html.escape(str(line.get('_rev') or ''))}">
        <label class="inbox-group-label">
          <input type="checkbox" name="checked" value="{draft_id}" checked>
          <span class="inbox-group-text"><strong>{job_type}</strong><span class="muted">{summary}</span></span>
        </label>
        {open_link}
      </li>"""


def render_capture_group_card(group: dict[str, object], vault_root: Path) -> str:
    lines = group.get("lines") if isinstance(group.get("lines"), list) else []
    items = "".join(_render_group_line(line) for line in lines if isinstance(line, dict))
    count = int(group.get("draft_count") or len(lines))
    site_label = resolve_site_label(group.get("site"), vault_root)
    submitter = html.escape(str(group.get("submitter") or UNKNOWN_SUBMITTER))
    age = render_relative_time(group.get("age_seconds"))
    return f"""<form class="inbox-group" method="post" action="/field-capture/review/approve-set" data-group-id="{html.escape(str(group.get('group_id') or ''))}">
      <input type="hidden" name="reviewer" value="{html.escape(default_actor())}">
      <p class="inbox-group-head">{site_label} <span class="muted">{submitter} · {count} drafts from one capture · {age}</span></p>
      <ul class="inbox-group-list">{items}</ul>
      <p class="inbox-group-actions"><button type="submit">Approve set</button> <span class="muted">Unchecked drafts are denied.</span></p>
    </form>"""


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


def _query_failed_capture_count(cfg: object, db: str) -> int:
    total = 0
    bookmark: object = None
    for _ in range(FAILED_CAPTURE_COUNT_PAGE_CAP):
        mango: dict[str, object] = {
            "selector": {
                "type": "field_capture",
                "processing_state": "failed",
            },
            "fields": ["_id"],
            "limit": FAILED_CAPTURE_COUNT_PAGE_SIZE,
        }
        if bookmark:
            mango["bookmark"] = bookmark
        response = query_couchdb_find(cfg, db, mango)
        docs = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(docs, list):
            raise RuntimeError("CouchDB _find returned no docs list")
        total += len([doc for doc in docs if isinstance(doc, dict)])
        bookmark = response.get("bookmark")
        if len(docs) < FAILED_CAPTURE_COUNT_PAGE_SIZE or not bookmark:
            break
    return total


def failed_capture_count() -> int | None:
    try:
        cfg = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        cache_key = f"{cfg.base_url.rstrip('/')}/{db}"
    except Exception:  # noqa: BLE001 - count is best-effort for home.
        cfg = None
        db = ""
        cache_key = "unavailable"
    now = time.monotonic()
    with _failed_capture_count_lock:
        cached = _failed_capture_count_cache.get(cache_key)
        if cached is not None and now - cached[0] < FAILED_CAPTURE_COUNT_CACHE_TTL_SECONDS:
            return cached[1]
    try:
        if cfg is None:
            raise RuntimeError("CouchDB config unavailable")
        count = _query_failed_capture_count(cfg, db)
    except Exception:  # noqa: BLE001 - home/inbox should not 500 on CouchDB trouble.
        count = None
    with _failed_capture_count_lock:
        _failed_capture_count_cache[cache_key] = (time.monotonic(), count)
    return count


def _failed_capture_photo_count(doc: dict[str, object]) -> int:
    photos = doc.get("photos")
    if isinstance(photos, list):
        return len(photos)
    for key in ("photo_count", "image_count"):
        try:
            return max(0, int(doc.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return 0


def failed_capture_rows(limit: int = FAILED_CAPTURE_ROW_LIMIT) -> tuple[int | None, list[dict[str, object]]]:
    count = failed_capture_count()
    if count is None:
        return None, []
    try:
        cfg = couchdb_config.from_env()
        db = couchdb_config.field_captures_database()
        response = query_couchdb_find(
            cfg,
            db,
            {
                "selector": {
                    "type": "field_capture",
                    "processing_state": "failed",
                },
                "fields": [
                    "_id",
                    "capture_id",
                    "site_id",
                    "target_id",
                    "target_type",
                    "captured_at",
                    "created_at",
                    "exported_at",
                    "error_reason",
                    "photos",
                    "photo_count",
                    "image_count",
                ],
                "limit": max(limit, 1) * 10,
            },
        )
        docs = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(docs, list):
            return count, []
    except Exception:  # noqa: BLE001 - the card is advisory only.
        return count, []

    rows: list[dict[str, object]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
        site = str(doc.get("site_id") or "").strip()
        if not site and str(doc.get("target_type") or "").strip() == "location":
            site = str(doc.get("target_id") or "").strip()
        reason = str(doc.get("error_reason") or "").strip() or FAILED_CAPTURE_REASON_PLACEHOLDER
        photo_count = _failed_capture_photo_count(doc)
        summary = f"{reason} ({photo_count} photo{'s' if photo_count != 1 else ''})"
        rows.append(
            {
                "site": site,
                "area": "",
                "submitter": "",
                "summary": summary,
                "age_seconds": str(doc.get("captured_at") or doc.get("created_at") or doc.get("exported_at") or ""),
                "deep_link": f"/captures?capture_id={quote(capture_id)}" if capture_id else "/captures",
            }
        )
    rows.sort(key=lambda item: str(item.get("age_seconds") or ""), reverse=True)
    return count, rows[:limit]


def unknown_capture_rows(ctx: object, limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    import os

    # CouchDB is canonical. Never scan the iCloud-synced vault from this daemon —
    # a background launchd process scanning it (dataless materialization) blocks
    # indefinitely. (Sourcing unknown captures from CouchDB is a follow-up.)
    if os.environ.get("BTQ_COUCHDB_URL", "").strip():
        return 0, []
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


def open_site_issues_rows(limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_issues()
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


def open_supply_needs_rows(limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_supplies(status="open")
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


def open_equipment_requests_rows(limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    report = discover_site_equipment(status="open")
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


def dashboard_work_item_rows(limit: int = 5) -> tuple[int, list[dict[str, object]]]:
    return site_orders.dashboard_work_items(limit=limit)


def console_review_queue_counts(ctx: object) -> dict[str, int]:
    cached = getattr(ctx, "_btq_console_review_queue_counts", None)
    if isinstance(cached, dict):
        return cached
    from . import swipe

    runtime_root = getattr(ctx, "runtime_root", Path("."))
    counts = swipe.queue_counts(runtime_root)
    setattr(ctx, "_btq_console_review_queue_counts", counts)
    return counts


def console_cards(ctx: object) -> dict[str, dict[str, object]]:
    cached = getattr(ctx, "_btq_console_cards", None)
    if isinstance(cached, dict):
        return cached
    issues_count, issues_rows = open_site_issues_rows()
    supplies_count, supplies_rows = open_supply_needs_rows()
    equipment_count, equipment_rows = open_equipment_requests_rows()
    cards = {
        "issues": {
            "id": "open_site_issues",
            "title": "Open site issues",
            "count": issues_count,
            "top": issues_rows,
            "see_all": "/field-capture/issues",
            "shape": INBOX_SHAPE_STRUCTURED,
        },
        "supplies": {
            "id": "open_supply_needs",
            "title": "Open supply needs",
            "count": supplies_count,
            "top": supplies_rows,
            "see_all": "/supplies?status=open",
            "shape": INBOX_SHAPE_STRUCTURED,
        },
        "equipment": {
            "id": "open_equipment_requests",
            "title": "Open equipment requests",
            "count": equipment_count,
            "top": equipment_rows,
            "see_all": "/equipment?status=open",
            "shape": INBOX_SHAPE_STRUCTURED,
        },
    }
    setattr(ctx, "_btq_console_cards", cards)
    return cards


def console_counts(ctx: object) -> dict[str, int]:
    cached = getattr(ctx, "_btq_console_counts", None)
    if isinstance(cached, dict):
        return cached
    review_counts = console_review_queue_counts(ctx)
    cards = console_cards(ctx)
    counts = {
        "review": int(review_counts.get("pending_approval", 0)),
        "issues": int(cards["issues"].get("count") or 0),
        "supplies": int(cards["supplies"].get("count") or 0),
        "equipment": int(cards["equipment"].get("count") or 0),
    }
    capture_failures = failed_capture_count()
    if capture_failures is not None:
        counts["failed_captures"] = capture_failures
    setattr(ctx, "_btq_console_counts", counts)
    return counts


def inbox_cards(ctx: object) -> list[dict[str, object]]:
    runtime_resolved = ctx.runtime_root
    candidate_dir = field_action_candidates.default_candidate_dir(runtime_resolved)
    # Walk the intake and candidate dirs once per render and reuse the results
    # across cards. Each walk is ~thousands of stat() calls on the runtime; the
    # earlier version did them 3x per inbox load.
    submitters = submitters_by_capture(runtime_resolved)
    all_candidates = review_candidates(candidate_dir, None, runtime_resolved, submitters=submitters)
    pending = [c for c in all_candidates if str(c.get("review_status") or c.get("status") or "") == "pending_approval"]
    apply_pending_candidate_counts(pending, pending_candidate_counts_by_capture(pending))
    pending.sort(key=candidate_capture_sort_value, reverse=True)
    note_bearing = pending
    no_note: list[dict[str, object]] = []
    status_report = review_status_report(runtime_root=runtime_resolved)
    gaps = status_report.get("lineage_gaps") if isinstance(status_report.get("lineage_gaps"), list) else []
    missing_draft = [gap for gap in gaps if isinstance(gap, dict) and gap.get("type") == "approved_candidate_missing_draft"]
    unstaged_drafts = [gap for gap in gaps if isinstance(gap, dict) and gap.get("type") == "approved_draft_missing_staging_status"]
    failed_count, failed_rows = failed_job_rows(runtime_resolved)
    failed_sidecar_count, failed_sidecar_top = failed_photo_sidecar_rows(runtime_resolved)
    failed_capture_count_value, failed_capture_top = failed_capture_rows()
    unknown_count, unknown_rows = unknown_capture_rows(ctx)
    uploaded_count, uploaded_rows = uploads_without_candidate_rows(
        runtime_resolved, submitters=submitters, all_candidates=all_candidates
    )
    console_state_cards = console_cards(ctx)
    dashboard_work_count, dashboard_work_rows = dashboard_work_item_rows()
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
    note_bearing_rows, note_bearing_groups = group_pending_candidates(note_bearing)
    cards = [
        {"id": "captures_with_note", "title": "Job drafts needing review", "count": len(note_bearing), "top": note_bearing_rows, "groups": note_bearing_groups, "see_all": "/candidates?status=pending_approval", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "pending_candidates", "title": "Pending drafts without capture context", "count": len(no_note), "top": [candidate_inbox_row(item) for item in no_note[:5]], "see_all": "/candidates?status=pending_approval", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "approved_missing_draft", "title": "Approved candidates missing a draft", "count": len(missing_draft), "top": missing_rows, "see_all": "/drafts", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "approved_drafts_not_staged", "title": "Approved drafts not yet staged", "count": len(unstaged_drafts), "top": draft_rows, "see_all": "/drafts", "shape": INBOX_SHAPE_GAP},
        {"id": "failed_queue_jobs", "title": "Failed queue jobs", "count": failed_count, "top": failed_rows, "see_all": "/failed", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "failed_captures", "title": "Failed captures", "count": int(failed_capture_count_value or 0), "top": failed_capture_top, "see_all": "/failed", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "failed_photo_vision_sidecars", "title": "Failed photo vision sidecars", "count": failed_sidecar_count, "top": failed_sidecar_top, "see_all": "/failed", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "unknown_captures", "title": "Unknown captures", "count": unknown_count, "top": unknown_rows, "see_all": "/captures", "shape": INBOX_SHAPE_CAPTURE},
        {"id": "uploaded_without_candidate", "title": "Recently uploaded with no candidate yet", "count": uploaded_count, "top": uploaded_rows, "see_all": "/captures", "shape": INBOX_SHAPE_CAPTURE},
        console_state_cards["issues"],
        console_state_cards["supplies"],
        console_state_cards["equipment"],
        {"id": "dashboard_work_items", "title": "Dashboard work items", "count": dashboard_work_count, "top": dashboard_work_rows, "see_all": "/site-orders", "shape": INBOX_SHAPE_GAP},
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
    rows = rows if isinstance(rows, list) else []
    groups = card.get("groups") if isinstance(card, dict) else []
    groups = [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []
    grouped_html = "".join(render_capture_group_card(group, vault_root) for group in groups)
    # With groups present the table only carries the single-draft captures, so an
    # empty table isn't "nothing waiting" — drop it rather than contradict the card.
    table = (
        render_table(rows, inbox_columns(card.get("shape"), vault_root), empty_text="Nothing waiting")
        if rows or not groups
        else ""
    )
    count = int(card.get("count") or 0)
    bucket = count_bucket(count)
    return f"""<article class="inbox-card" data-card-id="{html.escape(str(card.get('id') or ''))}">
      <h2>{html.escape(str(card.get('title') or ''))}</h2>
      <p class="count" data-count-bucket="{bucket}">{html.escape(str(count))}</p>
      {grouped_html}
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
    stat_ids = ["captures_with_note", "pending_candidates", "failed_queue_jobs", "failed_captures", "unknown_captures", "open_site_issues"]
    primary_ids = ["captures_with_note", "pending_candidates", "failed_queue_jobs", "failed_captures", "unknown_captures", "open_site_issues"]
    summary_specs = [
        ("approved_missing_draft", "Missing drafts"),
        ("approved_drafts_not_staged", "Unstaged drafts"),
        ("failed_photo_vision_sidecars", "Vision sidecars"),
        ("uploaded_without_candidate", "Uploads w/o candidate"),
        ("open_supply_needs", "Supply"),
        ("open_equipment_requests", "Equipment"),
        ("dashboard_work_items", "Dashboard"),
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
