from __future__ import annotations

import html
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from btq_vault.projector import _esc
from field_capture.review_dashboard import review_dashboard_report
from field_capture.review_maintenance import review_maintenance_status_report
from ops_dashboard.common import (
    DEFAULT_LOG_LINES,
    count_files,
    latest_json_artifacts,
    latest_uploads,
    photo_vision_status,
    humanize_key,
    render_count_badge,
    render_issue_list,
    render_kv,
    render_list,
    submitters_by_capture,
    tail_lines,
    voice_memo_intake_state,
    voice_memo_status,
)
from ops_dashboard.layout import html_page
from queue_processor import health as queue_health
from queue_processor import metrics
from site_issues import discover_site_issues, issue_as_export

HEALTH_LABELS = {
    "runtime_root_exists": "Runtime Root",
    "queue_count": "Queue Depth",
    "processed_count": "Processed",
    "failed_count": "Failed",
    "field_capture_intake_count": "FC Intakes",
    "uploads_count": "Uploads",
    "reviews_count": "Reviews",
    "disk_usage": "Disk",
    "runtime_size": "Runtime Size",
    "runtime_files": "Runtime Files",
}

# A runaway file count is an early scaling-failure signal; flag it amber/red.
RUNTIME_FILES_WARN = 100_000
RUNTIME_FILES_DANGER = 250_000


def warning_lines(lines: list[str], limit: int = 10) -> list[str]:
    matches = [line for line in lines if "ERROR" in line or "WARN" in line or "failed" in line.lower()]
    return matches[-limit:]


def disk_usage(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False, "used_bytes": 0, "free_bytes": 0, "total_bytes": 0}
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"exists": True, "used_bytes": 0, "free_bytes": 0, "total_bytes": 0}
    return {"exists": True, "used_bytes": usage.used, "free_bytes": usage.free, "total_bytes": usage.total}


_FOOTPRINT_CACHE_TTL_SECONDS = 300.0
_footprint_cache: dict[str, tuple[float, dict[str, int]]] = {}
_footprint_lock = threading.Lock()


def runtime_footprint(runtime_root: Path) -> dict[str, int]:
    """Total bytes and file count under runtime_root.

    Cached for a few minutes — the runtime tree can hold hundreds of
    thousands of files, so walking it on every dashboard request would be
    far too expensive.
    """
    key = str(runtime_root)

    def _fresh() -> dict[str, int] | None:
        cached = _footprint_cache.get(key)
        if cached is None or time.monotonic() - cached[0] >= _FOOTPRINT_CACHE_TTL_SECONDS:
            return None
        return cached[1]

    hit = _fresh()
    if hit is not None:
        return hit
    with _footprint_lock:
        hit = _fresh()
        if hit is not None:
            return hit
        total_bytes = 0
        file_count = 0
        if runtime_root.exists():
            for root, _dirs, files in os.walk(runtime_root, onerror=lambda _exc: None):
                for name in files:
                    file_count += 1
                    try:
                        total_bytes += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        pass
        result = {"total_bytes": total_bytes, "file_count": file_count}
        _footprint_cache[key] = (time.monotonic(), result)
        return result


def _format_gb(value: object) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    return f"{amount / 1_000_000_000:.1f}"


def render_disk_usage(value: object) -> str:
    if not isinstance(value, dict) or not value.get("exists"):
        return "unavailable"
    total = float(value.get("total_bytes") or 0)
    used = float(value.get("used_bytes") or 0)
    pct = 0.0 if total <= 0 else min(100.0, max(0.0, used / total * 100))
    text = f"{_format_gb(used)} GB used / {_format_gb(total)} GB total ({pct:.0f}%)"
    return f"""<div class="disk-usage">
        <span>{html.escape(text)}</span>
        <span class="disk-bar" aria-hidden="true"><span style="width: {pct:.0f}%"></span></span>
      </div>"""


def render_runtime_files(value: object) -> str:
    try:
        count = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        count = 0
    if count >= RUNTIME_FILES_DANGER:
        kind = "danger"
    elif count >= RUNTIME_FILES_WARN:
        kind = "pending"
    else:
        kind = "neutral"
    return str(render_count_badge(count, kind=kind))


def render_voice_memo_intake(intake: object) -> str:
    if not isinstance(intake, dict) or not intake.get("available"):
        reason = str(intake.get("reason") or "") if isinstance(intake, dict) else ""
        suffix = f" ({html.escape(reason)})" if reason else ""
        return f'<p class="muted">Intake state unavailable{suffix}.</p>'
    counts = intake.get("counts") if isinstance(intake.get("counts"), dict) else {}
    kinds = {"pending": "pending", "claimed": "pending", "intake_done": "neutral", "intake_failed": "danger", "other": "neutral"}
    labels = {"pending": "Pending", "claimed": "Claimed", "intake_done": "Intake Done", "intake_failed": "Intake Failed", "other": "Other"}
    rows = []
    for key in ("pending", "claimed", "intake_done", "intake_failed", "other"):
        if key == "other" and not counts.get("other"):
            continue
        badge = render_count_badge(counts.get(key, 0), kind=kinds[key])
        rows.append(f"<tr><th>{html.escape(labels[key])}</th><td>{badge}</td></tr>")
    return '<table class="kv-table">' + "".join(rows) + "</table>"


def render_health_value(key: str, value: object) -> str:
    if key == "runtime_root_exists":
        return "✅" if bool(value) else "❌"
    if key == "disk_usage":
        return render_disk_usage(value)
    if key == "runtime_size":
        return f"{_format_gb(value)} GB"
    if key == "runtime_files":
        return render_runtime_files(value)
    if key == "failed_count":
        return f'<a href="/failed">{render_count_badge(value, kind="danger")}</a>'
    if key.endswith("_count"):
        return str(render_count_badge(value, kind="neutral"))
    return html.escape(str(value))


def render_health_panel(health: dict[str, object]) -> str:
    rows = []
    for key, label in HEALTH_LABELS.items():
        rows.append(f"<tr><th>{html.escape(label)}</th><td>{render_health_value(key, health.get(key, ''))}</td></tr>")
    return '<table class="kv-table runtime-health">' + "".join(rows) + "</table>"


def latest_health_alert(runtime_root: Path) -> dict[str, object] | None:
    path = queue_health.alert_paths(runtime_root)["latest"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "critical":
        return None
    return payload


def render_latest_health_alert(alert: object) -> str:
    if not isinstance(alert, dict):
        return ""
    critical = alert.get("critical")
    if not isinstance(critical, list) or not critical:
        return ""
    items = "".join(f"<li>{html.escape(str(item))}</li>" for item in critical)
    created_at = html.escape(str(alert.get("created_at") or "unknown"))
    alert_id = html.escape(str(alert.get("alert_id") or "unknown"))
    return f"""
      <section class="notice error">
        <h3>Latest Critical Health Alert</h3>
        <p class="muted">Created at: {created_at} · Alert ID: <code>{alert_id}</code></p>
        <ul>{items}</ul>
      </section>
    """


def _metric_number(sample: dict[str, object], key: str) -> float:
    try:
        return float(sample.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _metric_int(sample: dict[str, object], key: str) -> int:
    return int(_metric_number(sample, key))


def _sparkline(values: list[float]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    low = min(values)
    high = max(values)
    if high <= low:
        return blocks[0] * len(values)
    scale = len(blocks) - 1
    return "".join(blocks[round((value - low) / (high - low) * scale)] for value in values)


def _format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _latency_map(sample: dict[str, object]) -> dict[str, object]:
    latency = sample.get("latency")
    return latency if isinstance(latency, dict) else {}


def _latency_number(sample: dict[str, object], stage: str, key: str) -> float:
    stage_payload = _latency_map(sample).get(stage)
    if not isinstance(stage_payload, dict):
        return 0.0
    try:
        return float(stage_payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_ms(value: float) -> str:
    return str(int(round(value)))


def render_pipeline_trend(runtime_root: Path) -> str:
    samples = metrics.load_recent(metrics.metrics_path_for(runtime_root), window_hours=24)
    if not samples:
        return '<div class="muted">No samples yet — metrics begin after the first sampling cycle.</div>'

    last = samples[-1]
    queue_values = [_metric_number(sample, "queue_depth") for sample in samples]
    error_values = [_metric_number(sample, "error_rate") for sample in samples]
    rows = [
        ("Current Queue Depth", _metric_int(last, "queue_depth")),
        ("Queue Depth 24h Min / Max / Last", f"{int(min(queue_values))} / {int(max(queue_values))} / {_metric_int(last, 'queue_depth')}"),
        ("Rolling 24h Error Rate", _format_percent(_metric_number(last, "error_rate"))),
        ("Queue Depth Trend", _sparkline(queue_values)),
        ("Error Rate Trend", _sparkline(error_values)),
    ]
    latest_latency = _latency_map(last)
    if latest_latency:
        for stage in sorted(str(key) for key in latest_latency):
            p50 = _latency_number(last, stage, "p50_ms")
            max_ms = _latency_number(last, stage, "max_ms")
            trend = _sparkline([_latency_number(sample, stage, "p50_ms") for sample in samples])
            rows.append((f"{stage} latency p50 / max (ms)", f"{_format_ms(p50)} / {_format_ms(max_ms)} {trend}"))
    else:
        rows.append(("Latency p50 / max (ms)", "—"))
    return '<table class="kv-table pipeline-trend">' + "".join(
        f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>" for label, value in rows
    ) + "</table>"


def count_kind(key: str) -> str:
    if key.startswith(("failed", "malformed")) or key in {"failed_unresolved"}:
        return "danger"
    if key.startswith(("pending", "backlog", "warning")) or key in {"finding_count", "current"}:
        return "pending"
    return "neutral"


def badge_counts(mapping: dict[str, object], keys: tuple[str, ...]) -> dict[str, object]:
    return {
        key: render_count_badge(mapping.get(key, 0), kind=count_kind(key))
        for key in keys
    }


def labels_for(keys: tuple[str, ...], overrides: dict[str, str] | None = None) -> dict[str, str]:
    labels = {key: humanize_key(key) for key in keys}
    labels.update(overrides or {})
    return labels


def humanized_dict(mapping: object) -> dict[str, object]:
    if not isinstance(mapping, dict):
        return {}
    return {humanize_key(key): value for key, value in mapping.items()}


def build_status(ctx: object, *, log_lines: int = DEFAULT_LOG_LINES) -> dict[str, object]:
    config = ctx.config
    runtime_resolved = ctx.runtime_root
    now = datetime.now(timezone.utc).isoformat()
    capture_submitters = submitters_by_capture(runtime_resolved)
    paths = {
        "queue": runtime_resolved / "queue",
        "processed": runtime_resolved / "processed",
        "failed": runtime_resolved / "failed",
        "field_capture_intake": runtime_resolved / "field_capture" / "intake",
        "uploads": runtime_resolved / "uploads",
        "reviews": runtime_resolved / "reviews",
        "transcripts": runtime_resolved / "field_capture" / "audio_transcripts",
        "semantics": runtime_resolved / "field_capture" / "audio_semantics",
        "photo_vision": runtime_resolved / "field_capture" / "photo_vision",
        "logs": runtime_resolved / "logs",
    }
    review_dashboard = review_dashboard_report(runtime_root=runtime_resolved)
    maintenance = review_maintenance_status_report(runtime_root=runtime_resolved)
    site_issue_report = discover_site_issues(config.vault_dir)
    site_issues = site_issue_report.get("issues") if isinstance(site_issue_report.get("issues"), list) else []
    logs = {
        "field_capture_pipeline_watch": tail_lines(paths["logs"] / "field_capture_pipeline_watch.log", log_lines),
        "field_capture_audio_transcription": tail_lines(paths["logs"] / "field_capture_audio_transcription.log", log_lines),
        "field_capture_audio_semantics": tail_lines(paths["logs"] / "field_capture_audio_semantics.log", log_lines),
        "queue_watch": tail_lines(paths["logs"] / "queue_watch.log", log_lines),
    }
    recent_warnings: list[str] = []
    for lines in logs.values():
        recent_warnings.extend(warning_lines(lines))
    footprint = runtime_footprint(runtime_resolved)
    audio_inbox = getattr(config, "audio_inbox_dir", None)
    audio_inbox_dir = Path(audio_inbox).expanduser() if audio_inbox else None
    return {
        "generated_at": now,
        "runtime_root": str(runtime_resolved),
        "health": {
            "runtime_root_exists": runtime_resolved.exists(),
            "disk_usage": disk_usage(runtime_resolved),
            "runtime_size": footprint["total_bytes"],
            "runtime_files": footprint["file_count"],
            "queue_count": count_files(paths["queue"], "*.json"),
            "processed_count": count_files(paths["processed"], "*.json", recursive=True),
            # Non-recursive so failed/quarantine/ doesn't bloat the count;
            # those are operator-acknowledged dead ends not "needs attention".
            "failed_count": count_files(paths["failed"], "*.json", recursive=False),
            "field_capture_intake_count": count_files(paths["field_capture_intake"], "*.json"),
            "uploads_count": count_files(paths["uploads"], recursive=True),
            "reviews_count": count_files(paths["reviews"], "*.json", recursive=True),
        },
        "latest_health_alert": latest_health_alert(runtime_resolved),
        "field_capture": {
            "latest_uploads": latest_uploads(paths["uploads"], capture_submitters),
            "intake_records": latest_json_artifacts(paths["field_capture_intake"]),
            "transcript_count": count_files(paths["transcripts"], "*.json"),
            "semantic_count": count_files(paths["semantics"], "*.json"),
            "latest_semantics": latest_json_artifacts(paths["semantics"]),
            "photo_vision": photo_vision_status(runtime_resolved),
        },
        "voice_memo": voice_memo_status(runtime_resolved, audio_inbox_dir),
        "voice_memo_intake": voice_memo_intake_state(),
        "review": {
            "dashboard": review_dashboard,
            "pending_candidates": review_dashboard["candidate_summary"]["pending_review"],
            "approved_candidates": review_dashboard["candidate_summary"]["approved"],
            "approved_drafts": review_dashboard["draft_summary"]["approved_draft"],
            "queue_state_counts": review_dashboard["draft_summary"]["queue_state_counts"],
            "failed_unresolved": review_dashboard["draft_summary"]["failed_unresolved"],
            "failed_replayed_successfully": review_dashboard["draft_summary"]["failed_replayed_successfully"],
            "failed_archive_total": review_dashboard["draft_summary"]["failed_archive_total"],
            "suggested_next_command": review_dashboard["suggested_next_command"],
        },
        "maintenance": {
            "finding_count": len(maintenance["findings"]),
            "counts": maintenance["counts"],
            "disk_usage": maintenance["disk_usage"],
            "findings": maintenance["findings"][:10],
        },
        "site_issues": {
            "counts": site_issue_report.get("counts") if isinstance(site_issue_report.get("counts"), dict) else {},
            "warnings": site_issue_report.get("warnings") if isinstance(site_issue_report.get("warnings"), list) else [],
            "recent": [issue_as_export(issue, include_path=True) for issue in site_issues[:10]],
        },
        "logs": {
            "tails": logs,
            "recent_warnings": recent_warnings[-20:],
        },
    }


def render_photo_vision_severity(severity_counts: object) -> str:
    if not isinstance(severity_counts, dict):
        return ""
    kinds = {"ok": "neutral", "degraded": "pending", "unusable": "danger"}
    rows = []
    for key in ("ok", "degraded", "unusable"):
        count = severity_counts.get(key, 0)
        badge = render_count_badge(count, kind=kinds[key])
        label = f'<a href="/photos?severity={html.escape(key)}">{html.escape(key.capitalize())}</a>'
        rows.append(f"<tr><th>{label}</th><td>{badge}</td></tr>")
    return '<table class="kv-table">' + "".join(rows) + "</table>"


def render_dashboard(status: dict[str, object]) -> str:
    health = status["health"]
    latest_alert = status.get("latest_health_alert")
    field_capture = status["field_capture"]
    review = status["review"]
    maintenance = status["maintenance"]
    site_issues = status["site_issues"]
    log_warnings = status["logs"]["recent_warnings"]
    photo_vision = field_capture["photo_vision"]
    voice_memo = status["voice_memo"]
    voice_memo_intake = status["voice_memo_intake"]
    return html_page(
        "BTQ Ops Dashboard",
        f"""
    <header>
      <h1>BTQ Ops Dashboard</h1>
      <p class="muted">Runtime root: <code>{html.escape(status["runtime_root"])}</code></p>
      <p class="muted">Generated at: {html.escape(status["generated_at"])}</p>
    </header>
    <section>
      <h2>Runtime Health</h2>
      {render_latest_health_alert(latest_alert)}
      {render_health_panel(health)}
      <h3>Pipeline trend</h3>
      {render_pipeline_trend(Path(str(status["runtime_root"])))}
    </section>
    <section>
      <h2>Field Capture</h2>
      <div><h3>Latest Uploads</h3>{render_list(field_capture["latest_uploads"], "No uploads found.")}</div>
      <div><h3>Intake Records</h3>{render_list(field_capture["intake_records"], "No intake records found.")}</div>
      {render_kv(badge_counts(field_capture, ("transcript_count", "semantic_count")), labels=labels_for(("transcript_count", "semantic_count"), {"transcript_count": "Transcripts", "semantic_count": "Semantics"}))}
      <h3>Photo Vision</h3>
      {render_kv(badge_counts(photo_vision, ("image_count", "sidecar_count", "completed_count", "failed_count", "skipped_count", "malformed_count", "backlog_count", "warning_count")), labels=labels_for(("image_count", "sidecar_count", "completed_count", "failed_count", "skipped_count", "malformed_count", "backlog_count", "warning_count"), {"image_count": "Images", "sidecar_count": "Sidecars", "completed_count": "Completed", "failed_count": "Failed", "skipped_count": "Skipped", "malformed_count": "Malformed", "backlog_count": "Backlog", "warning_count": "Warnings"}))}
      {render_photo_vision_severity(photo_vision.get("severity_counts") or {})}
      <p><a href="/captures?has_vision_sidecar=1">Browse captures with photo-vision sidecars</a> | <a href="/photos">Search photo-vision sidecars</a></p>
      <h3>Recent Vision Warnings/Failures</h3>
      {render_list(photo_vision["recent_warnings"], "No recent photo vision warnings/failures found.")}
    </section>
    <section>
      <h2>Voice Memo</h2>
      {render_kv(badge_counts(voice_memo, ("intake_count", "queued_count", "note_count", "failed_count")), labels=labels_for(("intake_count", "queued_count", "note_count", "failed_count"), {"intake_count": "Intake (awaiting transcription)", "queued_count": "Queued", "note_count": "Notes Written", "failed_count": "Failed"}))}
      <h3>Intake (CouchDB)</h3>
      {render_voice_memo_intake(voice_memo_intake)}
      <h3>Recent Voice Memo Notes</h3>
      {render_list(voice_memo["recent_notes"], "No voice memo notes yet.")}
    </section>
    <section>
      <h2>Review Workflow</h2>
      {render_kv({"pending_candidates": render_count_badge(review["pending_candidates"], kind="pending"), "approved_candidates": render_count_badge(review["approved_candidates"], kind="neutral"), "approved_drafts": render_count_badge(review["approved_drafts"], kind="neutral"), "queue_state_counts": humanized_dict(review["queue_state_counts"]), "failed_unresolved": render_count_badge(review["failed_unresolved"], kind="danger"), "failed_replayed_successfully": render_count_badge(review["failed_replayed_successfully"], kind="neutral"), "failed_archive_total": render_count_badge(review["failed_archive_total"], kind="danger"), "suggested_next_command": review["suggested_next_command"]}, labels={"pending_candidates": "Pending Candidates", "approved_candidates": "Approved Candidates", "approved_drafts": "Approved Drafts", "queue_state_counts": "Queue State Counts", "failed_unresolved": "Failed Unresolved", "failed_replayed_successfully": "Failed Replayed Successfully", "failed_archive_total": "Failed Archive Total", "suggested_next_command": "Suggested Next Command"})}
      <p><a href="/field-capture/review">Open Field Capture Review</a></p>
      <p class="muted">Review actions mark one candidate approved or rejected only. Draft generation, staging, queue processing, and vault writes remain separate.</p>
    </section>
    <section>
      <h2>Maintenance</h2>
      {render_kv({"finding_count": render_count_badge(maintenance["finding_count"], kind="pending"), "counts": humanized_dict(maintenance["counts"]), "disk_usage": maintenance["disk_usage"]}, labels={"finding_count": "Findings", "counts": "Counts", "disk_usage": "Disk Usage"})}
    </section>
    <section>
      <h2>Open Site Issues</h2>
      {render_kv({key: render_count_badge(value, kind=count_kind(str(key))) if isinstance(value, int) else value for key, value in site_issues["counts"].items()}, labels={str(key): humanize_key(key) for key in site_issues["counts"]})}
      <p><a href="/field-capture/issues">Open Site Issues</a></p>
      {render_issue_list(site_issues["recent"], "No site issues found.")}
    </section>
    <section>
      <h2>Logs</h2>
      <h3>Recent Warnings/Errors</h3>
      <pre>{html.escape(chr(10).join(log_warnings[-20:]) or "No recent warnings/errors found.")}</pre>
    </section>
""",
        refresh=True,
        active_section="health",
    )


def render(request_ctx: object) -> str:
    return render_dashboard(build_status(request_ctx))
