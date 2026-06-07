from __future__ import annotations

import html
import mimetypes
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from ops_dashboard import audit
from ops_dashboard.common import first_query_value, load_photo_vision_sidecars, read_json_artifact, render_back_link, render_collapsible_json, render_job_summary, render_relative_time, render_short_id, render_table, resolve_site_label
from ops_dashboard.layout import html_page
from processing_core.artifacts import resolve_within_root, write_json_object


PHOTO_ASSET_RE = re.compile(r"^fcp_[A-Za-z0-9_-]+$")


def age_seconds(path: Path) -> int:
    try:
        return max(0, int(datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
    except OSError:
        return 0


def failed_job_id(path: Path, payload: dict[str, object]) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(payload.get("job_id") or payload.get("computed_job_id") or metadata.get("draft_id") or path.stem)


def job_site(payload: dict[str, object]) -> str:
    job_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(payload.get("site_id") or job_payload.get("site_id") or metadata.get("site_id") or "")


def error_excerpt(path: Path, root: Path) -> str:
    # Single-job lookup kept for callers like render_job_detail. The list
    # view uses error_excerpts_for() below to avoid scanning queue_watch.log
    # N times per page load.
    return error_excerpts_for([path], root).get(path, "see logs")


_EXCERPTS_CACHE_TTL_SECONDS = 5.0
# cache[(root, frozenset(paths))] = (cached_at, log_mtime, log_size, result)
_EXCERPTS_CACHE: dict[tuple[str, frozenset[str]], tuple[float, float, int, dict[Path, str]]] = {}
_EXCERPTS_CACHE_LOCK = threading.Lock()


def error_excerpts_for(paths: list[Path], root: Path) -> dict[Path, str]:
    """Return {path: short error excerpt} for many failed-job paths in one pass.

    Reads queue_watch.log once and scans line-by-line, matching against all
    pending job tokens at once. The previous per-path implementation loaded
    the entire log into memory and ran .splitlines() per failed job, which
    on a multi-hundred-MB log allocated tens of GB transiently and tripped
    the dashboard's RSS watchdog.

    Results are cached for a few seconds keyed by (root, paths, log
    mtime+size) so repeated /failed renders within the cache window don't
    re-scan the ~900 MB queue_watch.log on every request.
    """
    log_path = root / "logs" / "queue_watch.log"
    try:
        log_stat = log_path.stat()
        log_mtime = log_stat.st_mtime
        log_size = log_stat.st_size
    except OSError:
        log_mtime = 0.0
        log_size = 0
    cache_key = (str(root), frozenset(str(p) for p in paths))

    def _cached_if_fresh() -> dict[Path, str] | None:
        cached = _EXCERPTS_CACHE.get(cache_key)
        if cached is None:
            return None
        cached_at, cached_mtime, cached_size, cached_result = cached
        if cached_mtime != log_mtime or cached_size != log_size:
            return None
        if time.monotonic() - cached_at >= _EXCERPTS_CACHE_TTL_SECONDS:
            return None
        return cached_result

    fresh = _cached_if_fresh()
    if fresh is not None:
        return fresh

    with _EXCERPTS_CACHE_LOCK:
        fresh = _cached_if_fresh()
        if fresh is not None:
            return fresh
        result: dict[Path, str] = {}
        pending: list[Path] = []
        for path in paths:
            sibling = path.with_suffix(".error.txt")
            if sibling.exists():
                try:
                    result[path] = sibling.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0][:220]
                    continue
                except (OSError, IndexError):
                    pass
            pending.append(path)
        if pending:
            token_to_path: dict[str, Path] = {}
            for path in pending:
                token_to_path[path.stem] = path
                token_to_path[path.name] = path
            last_match: dict[Path, str] = {}
            if log_path.exists():
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            for token, owner in token_to_path.items():
                                if token in line:
                                    last_match[owner] = line.strip()
                                    break
                except OSError:
                    pass
            for path in pending:
                match = last_match.get(path, "")
                result[path] = (match[:220] if match else "see logs")
        _EXCERPTS_CACHE[cache_key] = (time.monotonic(), log_mtime, log_size, result)
        return result


def failed_jobs(root: Path) -> list[dict[str, object]]:
    rows = []
    failed_dir = root / "failed"
    if not failed_dir.exists():
        return []
    # Intentionally exclude failed/quarantine/ — quarantined jobs are
    # operator-acknowledged dead ends (e.g., source media gone). They stay
    # on disk for forensics but shouldn't pollute the "needs attention" list.
    paths = list(failed_dir.glob("*.json"))
    sorted_paths = sorted(paths, key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    excerpts = error_excerpts_for(sorted_paths, root)
    for path in sorted_paths:
        payload, error = read_json_artifact(path)
        payload = payload or {"error": error}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        draft_id = str(metadata.get("draft_id") or payload.get("draft_id") or "")
        rows.append({"path": path, "payload": payload, "job_id": failed_job_id(path, payload), "draft_id": draft_id, "site": job_site(payload), "error": excerpts.get(path, "see logs"), "age_seconds": age_seconds(path)})
    return rows


def sidecars(root: Path) -> list[dict[str, object]]:
    sidecar_dir = root / "field_capture" / "photo_vision"
    if not sidecar_dir.exists():
        return []
    # load_photo_vision_sidecars is mtime-cached and returns summaries (no
    # full payload). The full payload is read on-demand in
    # render_sidecar_detail for the single sidecar being viewed.
    summaries = load_photo_vision_sidecars(sidecar_dir)
    out: list[dict[str, object]] = []
    for summary in summaries:
        if summary.get("status") != "failed":
            continue
        path = Path(str(summary.get("path") or ""))
        err = summary.get("error") if isinstance(summary.get("error"), dict) else {}
        out.append({
            "path": path,
            "photo_asset_id": str(summary.get("photo_asset_id") or path.stem),
            "site": str(summary.get("site_id") or ""),
            "area": str(summary.get("submitted_area") or summary.get("area_guess") or ""),
            "error_type": str(err.get("type") or "failed"),
            "error_message": str(err.get("message") or "failed"),
            "age_seconds": age_seconds(path),
        })
    return out


def render_job_detail(root: Path, job_id: str) -> str:
    job = next((item for item in failed_jobs(root) if item["job_id"] == job_id), None)
    if not job:
        return f'<section class="error">Failed job not found: {html.escape(job_id)}</section>'
    draft_link = f'<p><a href="/drafts?draft_id={quote(str(job["draft_id"]))}">Open matching draft</a></p>' if job.get("draft_id") else ""
    job_payload = job["payload"] if isinstance(job["payload"], dict) else {}
    job_type = str(job_payload.get("job_type") or "")
    inner_payload = job_payload.get("payload") if isinstance(job_payload.get("payload"), dict) else {}
    collapsible = render_collapsible_json(job_payload, summary_html=render_job_summary(job_type, inner_payload))
    return f"""
    {render_back_link("/failed", "Back to Failed")}
    <section>
      <h2>{html.escape(job_id)}</h2>
      <p>{html.escape(str(job.get("error") or ""))}</p>
      {draft_link}
      <p><a href="/runtime-file?path={quote(str(Path(job["path"]).relative_to(root)))}">Download JSON</a></p>
      {collapsible}
    </section>
    """


def render_sidecar_detail(sidecar_id: str, rows: list[dict[str, object]]) -> str:
    item = next((row for row in rows if row["photo_asset_id"] == sidecar_id), None)
    if not item:
        return f'<section class="error">Sidecar not found: {html.escape(sidecar_id)}</section>'
    # The list view uses a mtime-cached summary without the full payload to
    # stay fast across 2k+ sidecars. Read the full file here for the one
    # sidecar being rendered.
    payload, _error = read_json_artifact(Path(item["path"]))
    payload = payload if isinstance(payload, dict) else {}
    status = html.escape(str(payload.get("status") or "failed"))
    asset = render_short_id(payload.get("photo_asset_id") or sidecar_id)
    area = html.escape(str(payload.get("area_guess") or payload.get("submitted_area") or ""))
    summary = f"Photo vision sidecar — {status}"
    if asset:
        summary = f"{summary}: {asset}"
    if area:
        summary = f"{summary} ({area})"
    collapsible = render_collapsible_json(payload, summary_html=summary)
    return f"""
    {render_back_link("/failed", "Back to Failed")}
    <section>
      <h2>{html.escape(sidecar_id)}</h2>
      <form method="post" action="/failed/retry-sidecar">
        <input type="hidden" name="photo_asset_id" value="{html.escape(sidecar_id)}">
        <button type="submit">Retry this sidecar</button>
      </form>
      {collapsible}
    </section>
    """


def render_sidecar_retry_form(photo_asset_id: object) -> str:
    asset_id = str(photo_asset_id or "")
    return f"""<form class="inline-form" method="post" action="/failed/retry-sidecar">
        <input type="hidden" name="photo_asset_id" value="{html.escape(asset_id, quote=True)}">
        <button type="submit">Retry</button>
      </form>"""


def render(ctx: object) -> str:
    root = ctx.runtime_root
    query = getattr(ctx, "query", {})
    job_id = first_query_value(query, "job_id").strip()
    sidecar_id = first_query_value(query, "sidecar_id").strip()
    message = first_query_value(query, "message")
    flash = f'<section class="success">{html.escape(message)}</section>' if message else ""
    if job_id:
        return html_page("Failed Job", f"<header><h1>Failed Job</h1></header>{render_job_detail(root, job_id)}", active_section="failed")
    sidecar_rows = sidecars(root)
    if sidecar_id:
        return html_page("Failed Sidecar", f"<header><h1>Failed Sidecar</h1></header>{flash}{render_sidecar_detail(sidecar_id, sidecar_rows)}", active_section="failed")
    vault_dir = ctx.config.vault_dir
    failed_job_rows = failed_jobs(root)
    jobs_html = render_table(
        failed_job_rows,
        [
            {"key": "job_id", "label": "Job ID", "format": lambda value, _row: f'<a href="/failed?job_id={quote(str(value))}">{render_short_id(value)}</a>'},
            {"key": "payload", "label": "Type", "format": lambda value, _row: html.escape(str(value.get("job_type") or "")) if isinstance(value, dict) else ""},
            {"key": "site", "label": "Site", "format": lambda value, _row: resolve_site_label(value, vault_dir)},
            {"key": "error", "label": "Error"},
            {"key": "age_seconds", "label": "Age", "format": lambda value, _row: render_relative_time(value)},
        ],
        empty_text="No failed queue jobs.",
    )
    sidecars_html = render_table(
        sidecar_rows,
        [
            {"key": "photo_asset_id", "label": "Sidecar", "format": lambda value, _row: f'<a href="/failed?sidecar_id={quote(str(value))}">{render_short_id(value)}</a>'},
            {"key": "site", "label": "Site", "format": lambda value, _row: resolve_site_label(value, vault_dir)},
            {"key": "area", "label": "Area"},
            {"key": "error_type", "label": "Type"},
            {"key": "error_message", "label": "Message"},
            {"key": "photo_asset_id", "label": "Retry", "format": lambda value, _row: render_sidecar_retry_form(value)},
        ],
        empty_text="No failed photo-vision sidecars.",
    )
    body = f"""
    <header><h1>Failed</h1><p class="muted">Inspect failed queue jobs and queue a single photo-vision retry intent. Coming in prompt sections for sites/tokens/system remain separate.</p></header>
    <section><h2>Failed queue jobs</h2>{jobs_html}</section>
    <section><h2>Failed photo-vision sidecars</h2>{sidecars_html}</section>
    """
    return html_page("Failed", body, active_section="failed")


def handle_retry_sidecar_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    asset_id = first_query_value(form, "photo_asset_id").strip()
    if not PHOTO_ASSET_RE.match(asset_id):
        audit.append_audit(root, {"route": "/failed/retry-sidecar", "actor": "localhost", "payload": {"photo_asset_id": asset_id}, "result_summary": "failed: invalid_photo_asset_id"})
        return 303, "text/html; charset=utf-8", b"invalid", {"Location": "/failed?error=invalid_photo_asset_id"}
    path = root / "reviews" / "photo_vision_retries" / f"{asset_id}.json"
    payload = {"photo_asset_id": asset_id, "requested_at": datetime.now(timezone.utc).isoformat(), "actor": "localhost"}
    write_json_object(path, payload)
    audit.append_audit(root, {"route": "/failed/retry-sidecar", "actor": "localhost", "payload": {"photo_asset_id": asset_id}, "result_summary": "success: retry_queued"})
    location = f"/failed?sidecar_id={quote(asset_id)}&message=retry_queued"
    return 303, "text/html; charset=utf-8", b"queued", {"Location": location}


def handle_runtime_file(ctx: object):
    root = ctx.runtime_root
    relpath = first_query_value(getattr(ctx, "query", {}), "path").strip()
    if not relpath or ".." in Path(relpath).parts or Path(relpath).is_absolute():
        return 404, "application/json; charset=utf-8", b'{"error": "not_found"}', {}
    try:
        path = (root / relpath).resolve(strict=False)
        resolve_within_root(path, root)
    except ValueError:
        return 404, "application/json; charset=utf-8", b'{"error": "not_found"}', {}
    if not path.is_file():
        return 404, "application/json; charset=utf-8", b'{"error": "not_found"}', {}
    content_type = "application/json; charset=utf-8" if path.suffix == ".json" else (mimetypes.guess_type(path.name)[0] or "text/plain") + "; charset=utf-8"
    return 200, content_type, path.read_bytes(), {}
