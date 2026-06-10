from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import quote

from ops_dashboard.common import default_actor, first_query_value, handle_mark_transition_post, humanize_key, render_back_link, render_issue_list, render_kv, render_status_transition, render_table, slugify_status
from ops_dashboard.layout import html_page
from site_issues import discover_site_issues, issue_as_export

ISSUE_SORT_OPTIONS = ("", "site", "recency")
ISSUE_TRANSITIONS = {
    "mark-monitoring": {
        "label": "Mark monitoring",
        "job_type": "mark_issue_monitoring",
        "target_status": "monitoring",
        "source_statuses": {"open"},
        "fields": ("monitoring_at", "monitoring_by", "monitoring_note"),
        "post_route": "/field-capture/issues/mark-monitoring",
        "confirm_route": "/field-capture/issues/mark-monitoring-confirm",
    },
    "mark-resolved": {
        "label": "Mark resolved",
        "job_type": "mark_issue_resolved",
        "target_status": "resolved",
        "source_statuses": {"open", "monitoring"},
        "fields": ("resolved_at", "resolved_by", "resolved_note"),
        "post_route": "/field-capture/issues/mark-resolved",
        "confirm_route": "/field-capture/issues/mark-resolved-confirm",
    },
    "reopen": {
        "label": "Reopen",
        "job_type": "mark_issue_open",
        "target_status": "open",
        "source_statuses": {"monitoring", "resolved"},
        "fields": ("open_at", "open_by", "open_note"),
        "post_route": "/field-capture/issues/reopen",
        "confirm_route": "/field-capture/issues/reopen-confirm",
    },
}
ISSUE_CONFIRM_ROUTES = {str(config["confirm_route"]): name for name, config in ISSUE_TRANSITIONS.items()}


def render_field_capture_issues(request_ctx: object) -> str:
    query = getattr(request_ctx, "query", {})
    site_id = query_value(query, "site_id") or None
    title = build_title(site_id)
    return html_page(
        title,
        render_field_capture_issues_body(request_ctx),
        active_section="issues",
    )


def render_field_capture_issues_body(request_ctx: object, *, embedded: bool = False) -> str:
    query = getattr(request_ctx, "query", {})
    config = request_ctx.config
    site_id = query_value(query, "site_id") or None
    sort = query_value(query, "sort")
    report = discover_site_issues(config.vault_dir, site_id=site_id)
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    exported = sort_issues([issue_as_export(issue, include_path=True) for issue in issues], sort)
    title = build_title(site_id)
    action = "/" if embedded else "/field-capture/issues"
    tab = "issues" if embedded else None
    return f"""
    <header>
      <h1>{html.escape(title)}</h1>
      <p class="muted">Read-only structured site issue records from the vault.</p>
    </header>
    <section>
      <h2>Summary</h2>
      {render_kv(report.get("counts") if isinstance(report.get("counts"), dict) else {})}
    </section>
    <section>
      <h2>Filters</h2>
      {render_filter_form(site_id, sort, action=action, tab=tab)}
    </section>
    <section>
      <h2>Issues</h2>
      {render_issue_list(exported, "No site issues found.")}
    </section>
"""


def build_title(site_id: str | None) -> str:
    return f"Site Issues for {site_id}" if site_id else "Site Issues"


def query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def render_filter_form(site_id: str | None, sort: str, *, action: str, tab: str | None = None) -> str:
    tab_input = f'<input type="hidden" name="tab" value="{html.escape(tab)}">' if tab else ""
    sort_options = []
    for value in ISSUE_SORT_OPTIONS:
        selected = ' selected' if (sort or "") == value else ""
        label = {"": "Default order", "site": "Site", "recency": "Most recent"}.get(value, value)
        sort_options.append(f'<option value="{html.escape(value)}"{selected}>{html.escape(label)}</option>')
    return f"""<form method="get" action="{html.escape(action)}" data-submit-on-change>
        {tab_input}
        <label for="site_id">Site ID</label>
        <input id="site_id" name="site_id" value="{html.escape(site_id or '')}">
        <label for="sort">Sort</label>
        <select id="sort" name="sort">{"".join(sort_options)}</select>
        <p><button type="submit">Apply filters</button></p>
      </form>"""


def sort_issues(issues: list[dict[str, object]], sort: str) -> list[dict[str, object]]:
    if sort == "site":
        return sorted(issues, key=lambda issue: (str(issue.get("site_id") or ""), str(issue.get("title") or ""), str(issue.get("issue_id") or "")))
    if sort == "recency":
        return sorted(
            issues,
            key=lambda issue: (
                str(issue.get("updated_at") or issue.get("created_at") or issue.get("observed_at") or ""),
                str(issue.get("issue_id") or ""),
            ),
            reverse=True,
        )
    return issues


def issues_from_report(report: dict[str, object]) -> list[dict[str, object]]:
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    return [issue_as_export(issue, include_path=True) for issue in issues]


def find_issue(vault_root: Path, issue_id: str) -> dict[str, object] | None:
    report = discover_site_issues(vault_root)
    for issue in issues_from_report(report):
        if str(issue.get("issue_id") or "") == issue_id:
            return issue
    return None


def render_issue_detail(ctx: object, issue_id: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    issue = find_issue(config.vault_dir, issue_id)
    flash = render_flash(query)
    if issue is None:
        content = f'<section class="error">Issue not found: {html.escape(issue_id)}</section>'
    else:
        title = str(issue.get("title") or "Site issue")
        content = f"""
        <section>
          <h2>{html.escape(title)}</h2>
          <p>{render_pill(issue.get("status"))}</p>
          <h3>Summary</h3>
          <p>{html.escape(str(issue.get("summary") or ""))}</p>
          {render_kv(issue, labels={key: humanize_key(key) for key in issue})}
        </section>
        <section>
          <h2>Source</h2>
          {render_source_section(issue)}
        </section>
        <section>
          <h2>Lifecycle History</h2>
          {render_lifecycle_history(issue)}
        </section>
        {render_action_panel(issue)}
        """
    return html_page("Issue Detail", f"<header><h1>Issue Detail</h1></header>{render_back_link('/field-capture/issues', 'Back to Issues')}{flash}{content}", active_section="issues")


def render_source_section(issue: dict[str, object]) -> str:
    return render_kv(
        {
            "related_capture_ids": ", ".join(string_list(issue.get("related_capture_ids"))),
            "related_candidate_ids": ", ".join(string_list(issue.get("related_candidate_ids"))),
        },
        labels={"related_capture_ids": "Related Capture IDs", "related_candidate_ids": "Related Candidate IDs"},
    )


def render_lifecycle_history(issue: dict[str, object]) -> str:
    rows = []
    for key in ("monitoring_at", "monitoring_by", "monitoring_note", "resolved_at", "resolved_by", "resolved_note"):
        value = str(issue.get(key) or "").strip()
        if value:
            rows.append({"event": humanize_key(key), "value": value})
    return render_table(rows, [{"key": "event", "label": "Event"}, {"key": "value", "label": "Value"}], empty_text="No lifecycle history yet.")


def valid_transitions(status: object) -> list[tuple[str, dict[str, object]]]:
    current = str(status or "").strip()
    return [(name, config) for name, config in ISSUE_TRANSITIONS.items() if current in config["source_statuses"]]


def render_action_panel(issue: dict[str, object]) -> str:
    transitions = valid_transitions(issue.get("status"))
    if not transitions:
        return ""
    issue_id = str(issue.get("issue_id") or "")
    current_status = str(issue.get("status") or "")
    current_pill = f'<p>Currently: <span class="pill status-{slugify_status(current_status)}">{html.escape(current_status)}</span></p>' if current_status else ""
    links = "".join(
        f'<p><a class="button" href="{html.escape(str(config["confirm_route"]))}?issue_id={quote(issue_id)}">{html.escape(str(config["label"]))}</a></p>'
        for _name, config in transitions
    )
    return f"<section><h2>Actions</h2>{current_pill}{links}</section>"


def render_issue_mark_confirm(ctx: object, transition_name: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    issue_id = first_query_value(query, "issue_id").strip()
    issue = find_issue(config.vault_dir, issue_id)
    transition = ISSUE_TRANSITIONS.get(transition_name)
    if issue is None or transition is None:
        content = f'<section class="error">Issue transition not found: {html.escape(issue_id)}</section>'
    else:
        transition_html = render_status_transition(issue.get("status"), transition["target_status"])
        content = f"""
        <section>
          <h2>{html.escape(str(issue.get("title") or "Site issue"))}</h2>
          {transition_html}
          {render_kv({"issue_id": issue.get("issue_id", ""), "site": f"{issue.get('site_id') or ''} {issue.get('site') or issue.get('site_name') or ''}".strip(), "current_status": humanize_key(issue.get("status", ""))}, labels={"issue_id": "Issue ID", "site": "Site", "current_status": "Current Status"})}
          <form method="post" action="{html.escape(str(transition["post_route"]))}">
            <input type="hidden" name="issue_id" value="{html.escape(issue_id)}">
            <input type="hidden" name="confirm" value="1">
            <label>Actor (your name)</label>
            <input name="actor" type="text" required value="{html.escape(default_actor())}">
            <label>Note (optional)</label>
            <textarea name="note"></textarea>
            <button type="submit">{html.escape(str(transition["label"]))}</button>
          </form>
          <p><a href="/field-capture/issues?issue_id={quote(issue_id)}">Cancel</a></p>
        </section>
        """
    return html_page("Confirm Issue Transition", f"<header><h1>Confirm Issue Transition</h1></header>{render_back_link(f'/field-capture/issues?issue_id={quote(issue_id)}', 'Back to issue detail')}{content}", active_section="issues")


def render_flash(query: dict[str, list[str]]) -> str:
    error = first_query_value(query, "error")
    message = first_query_value(query, "message")
    if error:
        return f'<section class="error">{html.escape(error)}</section>'
    if message:
        return f'<section class="success">{html.escape(message)}</section>'
    return ""


def render_pill(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f'<span class="pill">{html.escape(humanize_key(text))}</span>'


def string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def render(request_ctx: object) -> str:
    query = getattr(request_ctx, "query", {})
    route_path = getattr(request_ctx, "route_path", "/field-capture/issues")
    if route_path in ISSUE_CONFIRM_ROUTES:
        return render_issue_mark_confirm(request_ctx, ISSUE_CONFIRM_ROUTES[route_path])
    issue_id = first_query_value(query, "issue_id").strip()
    if issue_id:
        return render_issue_detail(request_ctx, issue_id)
    return render_field_capture_issues(request_ctx)


def handle_issue_mark_post(ctx: object, body: bytes, *, job_type: str, route: str):
    return handle_mark_transition_post(
        ctx, body,
        job_type=job_type,
        route=route,
        id_field="issue_id",
        redirect_path="/field-capture/issues",
    )


def handle_mark_issue_monitoring(ctx: object, body: bytes):
    return handle_issue_mark_post(ctx, body, job_type="mark_issue_monitoring", route="/field-capture/issues/mark-monitoring")


def handle_mark_issue_resolved(ctx: object, body: bytes):
    return handle_issue_mark_post(ctx, body, job_type="mark_issue_resolved", route="/field-capture/issues/mark-resolved")


def handle_mark_issue_open(ctx: object, body: bytes):
    return handle_issue_mark_post(ctx, body, job_type="mark_issue_open", route="/field-capture/issues/reopen")
