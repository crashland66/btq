from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from field_capture import approved_job_drafts
from field_capture import draft_staging
from ops_dashboard.common import first_query_value, humanize_key, read_json_artifact, render_back_link, render_collapsible_json, render_job_summary, render_kv, render_short_id, render_table, resolve_site_label
from ops_dashboard.layout import html_page

def candidate_dir(root: Path) -> Path:
    return approved_job_drafts.default_candidate_dir(root)


def draft_dir(root: Path) -> Path:
    return approved_job_drafts.default_draft_dir(root)


def proposed_site(draft: dict[str, object]) -> str:
    payload = draft.get("proposed_payload") if isinstance(draft.get("proposed_payload"), dict) else {}
    return str(payload.get("site_id") or payload.get("site") or "")


def load_drafts(root: Path, *, include_payload: bool = False, include_source: bool = False) -> list[dict[str, object]]:
    drafts = []
    root_dir = draft_dir(root)
    if not root_dir.exists():
        return []
    for path in sorted(root_dir.glob("*.json")):
        payload, _error = read_json_artifact(path)
        if not payload:
            continue
        proposed_payload = payload.get("proposed_payload") if isinstance(payload.get("proposed_payload"), dict) else {}
        draft_id = str(payload.get("draft_id") or path.stem)
        staging_payload, _ = read_json_artifact(root / "reviews" / "staging" / "field_capture" / f"{draft_id}.json")
        item = {
            "draft_id": draft_id,
            "candidate_id": str(payload.get("candidate_id") or ""),
            "status": "approved_draft" if payload.get("status") == "approved_draft" else str(payload.get("status") or ""),
            "proposed_job_type": str(payload.get("proposed_job_type") or ""),
            "proposed_payload": proposed_payload if include_payload else {},
            "summary_preview": str(payload.get("source_context") or payload.get("rationale") or ""),
            "source_context": str(payload.get("source_context") or "") if include_source else "",
            "artifact_path": str(path),
            "queue_state": str(staging_payload.get("status") if staging_payload else "not_staged"),
            "provenance": payload.get("provenance") if include_source and isinstance(payload.get("provenance"), dict) else {},
        }
        drafts.append(item)
    return drafts


def filter_drafts(items: list[dict[str, object]], query: dict[str, list[str]]) -> list[dict[str, object]]:
    status = first_query_value(query, "status").strip()
    sites = {value for value in query.get("site", []) if value}
    job_types = {value for value in query.get("job_type", []) if value}
    if status and status != "all":
        items = [item for item in items if str(item.get("status") or "") == status]
    if sites:
        items = [item for item in items if proposed_site(item) in sites]
    if job_types:
        items = [item for item in items if str(item.get("proposed_job_type") or "") in job_types]
    return items


def render_filters(items: list[dict[str, object]], query: dict[str, list[str]], vault_dir: Path) -> str:
    status = first_query_value(query, "status") or "all"
    statuses = ["all", "approved_draft", "failed_draft"]
    sites = sorted({proposed_site(item) for item in items if proposed_site(item)})
    job_types = sorted({str(item.get("proposed_job_type") or "") for item in items if item.get("proposed_job_type")})
    status_counts = {item: sum(1 for draft in items if str(draft.get("status") or "") == item) for item in statuses if item != "all"}
    status_counts["all"] = len(items)
    site_counts = {item: sum(1 for draft in items if proposed_site(draft) == item) for item in sites}
    job_type_counts = {item: sum(1 for draft in items if str(draft.get("proposed_job_type") or "") == item) for item in job_types}
    status_controls = "".join(f'<label><input type="radio" name="status" value="{html.escape(item)}"{" checked" if item == status else ""}> {html.escape("All" if item == "all" else humanize_key(item))} ({status_counts.get(item, 0)})</label>' for item in statuses)
    site_controls = "".join(f'<label><input type="checkbox" name="site" value="{html.escape(item)}"{" checked" if item in query.get("site", []) else ""}> {resolve_site_label(item, vault_dir)} ({site_counts.get(item, 0)})</label>' for item in sites) or '<p class="muted">No sites</p>'
    job_controls = "".join(f'<label><input type="checkbox" name="job_type" value="{html.escape(item)}"{" checked" if item in query.get("job_type", []) else ""}> {html.escape(humanize_key(item))} ({job_type_counts.get(item, 0)})</label>' for item in job_types) or '<p class="muted">No job types</p>'
    return f"""
    <form method="get" action="/drafts" data-submit-on-change>
      <fieldset><legend>Status</legend>{status_controls}</fieldset>
      <fieldset><legend>Site</legend>{site_controls}</fieldset>
      <fieldset><legend>Job type</legend>{job_controls}</fieldset>
      <p><button type="submit">Filter</button></p>
    </form>
    """


def render_list(ctx: object) -> str:
    root = ctx.runtime_root
    query = getattr(ctx, "query", {})
    vault_dir = ctx.config.vault_dir
    all_drafts = load_drafts(root, include_payload=True)
    drafts = filter_drafts(all_drafts, query)
    table = render_table(
        drafts,
        [
            {"key": "draft_id", "label": "Draft", "format": lambda value, _row: f'<a href="/drafts?draft_id={quote(str(value))}">{render_short_id(value)}</a>', "nowrap": True},
            {"key": "candidate_id", "label": "Candidate", "format": lambda value, _row: render_short_id(value), "priority": 3, "nowrap": True},
            {"key": "status", "label": "Status", "format": lambda value, _row: html.escape(humanize_key(value)), "nowrap": True},
            {"key": "proposed_job_type", "label": "Job", "format": lambda value, _row: html.escape(humanize_key(value)), "priority": 2, "nowrap": True},
            {"key": "site", "label": "Site", "format": lambda _value, row: resolve_site_label(proposed_site(row), vault_dir)},
            {"key": "summary_preview", "label": "Summary", "format": lambda value, row: html.escape(str(value or row.get("source_context") or "")[:180]), "priority": 2},
            {"key": "queue_state", "label": "Queue State", "format": lambda value, _row: html.escape(humanize_key(value or "not_staged")), "priority": 2, "nowrap": True},
        ],
        empty_text="No drafts match this filter.",
    )
    body = f"""
    <header><h1>Drafts</h1><p class="muted">Generate one approved candidate draft and stage one draft at a time. Stage approved candidate drafts to the queue.</p></header>
    <div class="content-with-rail">
      <aside class="filter-rail"><section><h2>Filters</h2>{render_filters(all_drafts, query, vault_dir)}</section></aside>
      <section><h2>Approved Drafts</h2>{table}</section>
    </div>
    """
    return html_page("Drafts", body, active_section="drafts")


def find_draft(root: Path, draft_id: str) -> dict[str, object] | None:
    for draft in load_drafts(root, include_payload=True, include_source=True):
        if str(draft.get("draft_id") or "") == draft_id:
            return draft
    return None


def render_draft_detail(ctx: object, draft_id: str) -> str:
    root = ctx.runtime_root
    draft = find_draft(root, draft_id)
    query = getattr(ctx, "query", {})
    if draft is None:
        content = f"<section class=\"error\">Draft not found: {html.escape(draft_id)}</section>"
    else:
        proposed_type = str(draft.get("proposed_job_type") or "")
        proposed_payload = draft.get("proposed_payload") if isinstance(draft.get("proposed_payload"), dict) else {}
        summary = render_job_summary(proposed_type, proposed_payload)
        collapsible = render_collapsible_json(proposed_payload, summary_html=summary, summary_label="Proposed payload JSON")
        content = f"""
        <section>
          <h2>{html.escape(draft_id)}</h2>
          {render_kv({"candidate_id": draft.get("candidate_id", ""), "status": humanize_key(draft.get("status", "")), "proposed_job_type": humanize_key(draft.get("proposed_job_type", "")), "queue_state": humanize_key(draft.get("queue_state", "")), "artifact_path": draft.get("artifact_path", "")}, labels={"candidate_id": "Candidate ID", "status": "Status", "proposed_job_type": "Proposed Job Type", "queue_state": "Queue State", "artifact_path": "Artifact Path"})}
          {collapsible}
          <p><a class="button" href="/drafts/stage-preview?draft_id={quote(draft_id)}">Stage this draft (dry-run)</a></p>
        </section>
        """
    flash = render_flash(query)
    return html_page("Draft Detail", f"<header><h1>Draft Detail</h1></header>{render_back_link('/drafts', 'Back to Drafts')}{flash}{content}", active_section="drafts")


def render_flash(query: dict[str, list[str]]) -> str:
    error = first_query_value(query, "error")
    message = first_query_value(query, "message")
    if error:
        return f'<section class="error">{html.escape(error)}</section>'
    if message:
        return f'<section class="success">{html.escape(message)}</section>'
    return ""


def render_generate_preview(ctx: object, candidate_id: str) -> str:
    root = ctx.runtime_root
    report = approved_job_drafts.approved_job_drafts_report(candidate_dir(root), draft_dir(root), runtime_root=root, dry_run=True, candidate_ids={candidate_id})
    results = report.get("results") if isinstance(report.get("results"), list) else []
    result = (results[0] if results else {})
    status = str(result.get("status") or "")
    status_text = "success" if result.get("success") or status == "would_create" else "blocked"
    summary_html = f"<strong>Generate preview</strong>: {html.escape(status_text)} for candidate {render_short_id(candidate_id)}"
    preview_collapsible = render_collapsible_json(report, summary_html=summary_html, summary_label="Full report")
    body = f"""
    <header><h1>Generate Draft Preview</h1></header>
    {render_back_link("/candidates", "Back to Candidates")}
    {render_flash(getattr(ctx, "query", {}))}
    <section>
      <h2>{html.escape(candidate_id)}</h2>
      {preview_collapsible}
      <form method="post" action="/drafts/generate">
        <input type="hidden" name="candidate_id" value="{html.escape(candidate_id)}">
        <input type="hidden" name="confirm_dryrun" value="1">
        <button type="submit">Generate this draft</button>
      </form>
    </section>
    """
    return html_page("Generate Draft Preview", body, active_section="drafts")


def render_stage_preview(ctx: object) -> str:
    root = ctx.runtime_root
    query = getattr(ctx, "query", {})
    draft_id = first_query_value(query, "draft_id").strip()
    report = draft_staging.stage_field_capture_drafts_report(runtime_root=root, draft_ids={draft_id}, dry_run=True)
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    mode = "dry run" if report.get("dry_run") else "live"
    summary_html = f"<strong>Stage preview</strong>: {html.escape(str(mode))}, {html.escape(str(completed))} success, {html.escape(str(failed))} failures"
    preview_collapsible = render_collapsible_json(report, summary_html=summary_html, summary_label="Full report")
    body = f"""
    <header><h1>Stage Draft Preview</h1></header>
    {render_back_link(f"/drafts?draft_id={quote(draft_id)}", "Back to draft detail")}
    <section>
      <h2>{html.escape(draft_id)}</h2>
      {preview_collapsible}
      <form method="post" action="/drafts/stage">
        <input type="hidden" name="draft_id" value="{html.escape(draft_id)}">
        <input type="hidden" name="confirm_dryrun" value="1">
        <button type="submit">Stage this draft</button>
      </form>
    </section>
    """
    return html_page("Stage Draft Preview", body, active_section="drafts")


def render(ctx: object) -> str:
    route_path = getattr(ctx, "route_path", "/drafts")
    query = getattr(ctx, "query", {})
    if route_path == "/drafts/stage-preview":
        return render_stage_preview(ctx)
    candidate_id = first_query_value(query, "candidate_id").strip()
    if candidate_id and first_query_value(query, "action") == "generate":
        return render_generate_preview(ctx, candidate_id)
    draft_id = first_query_value(query, "draft_id").strip()
    if draft_id:
        return render_draft_detail(ctx, draft_id)
    return render_list(ctx)


def latest_draft_for_candidate(root: Path, candidate_id: str) -> str:
    matches = [draft for draft in load_drafts(root) if str(draft.get("candidate_id") or "") == candidate_id]
    if not matches:
        return ""
    return str(matches[-1].get("draft_id") or "")


def handle_generate_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate_id = first_query_value(form, "candidate_id").strip()
    if first_query_value(form, "confirm_dryrun") != "1":
        ctx.audit("/drafts/generate", ctx.form_payload(form), "failed: confirm_required")
        return ctx.redirect(f"/drafts?candidate_id={quote(candidate_id)}&action=generate&error=confirm_required")
    try:
        counts = approved_job_drafts.create_approved_job_drafts(candidate_dir(root), draft_dir(root), runtime_root=root, candidate_ids={candidate_id})
        draft_id = latest_draft_for_candidate(root, candidate_id)
        summary = f"success: generated draft_id={draft_id} counts={counts}"
        ctx.audit("/drafts/generate", ctx.form_payload(form), summary)
        return ctx.redirect(f"/drafts?draft_id={quote(draft_id)}&message=generated")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/drafts/generate", ctx.form_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/drafts?candidate_id={quote(candidate_id)}&action=generate&error={quote(str(exc))}")


def handle_stage_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    draft_id = first_query_value(form, "draft_id").strip()
    if first_query_value(form, "confirm_dryrun") != "1":
        ctx.audit("/drafts/stage", ctx.form_payload(form), "failed: confirm_required")
        return ctx.redirect(f"/drafts?draft_id={quote(draft_id)}&error=confirm_required")
    try:
        report = draft_staging.stage_field_capture_drafts_report(runtime_root=root, draft_ids={draft_id}, dry_run=False)
        result = (report.get("results") or [{}])[0] if isinstance(report.get("results"), list) else {}
        queue_path = str(result.get("queue_path") or "")
        ctx.audit("/drafts/stage", ctx.form_payload(form), f"success: staged draft_id={draft_id} queue_path={queue_path}")
        return ctx.redirect(f"/drafts?draft_id={quote(draft_id)}&message=staged")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/drafts/stage", ctx.form_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/drafts?draft_id={quote(draft_id)}&error={quote(str(exc))}")
