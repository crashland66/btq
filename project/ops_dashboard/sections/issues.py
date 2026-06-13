from __future__ import annotations

import html
from urllib.parse import quote

from ops_dashboard.common import default_actor, first_query_value, handle_edit_record_fields_post, handle_mark_transition_post, humanize_key, render_back_link, render_issue_list, render_kv, render_record_edit_form, render_status_transition, render_table, slugify_status
from ops_dashboard.layout import html_page
from site_issues import discover_site_issues, issue_as_export

ISSUE_SORT_OPTIONS = ("", "site", "recency")
ISSUE_EDIT_FIELDS = ("site_id", "title", "summary", "priority", "category", "resolution_trigger")
ISSUE_EDIT_SELECT_OPTIONS = {
    "priority": ("low", "normal", "high", "urgent"),
    "category": ("maintenance", "supply", "access", "staffing", "quality", "safety", "client_request", "other"),
}
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
    site_id = query_value(query, "site_id") or None
    sort = query_value(query, "sort")
    archived = query_value(query, "archived") == "1"
    report = discover_site_issues(site_id=site_id, include_archived=archived, archived_only=archived)
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    exported = sort_issues([issue_as_export(issue, include_path=True) for issue in issues], sort)
    title = f"Archived {build_title(site_id)}" if archived else build_title(site_id)
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
      {render_filter_form(site_id, sort, archived=archived, action=action, tab=tab)}
    </section>
    <section>
      <h2>Issues</h2>
      {render_archived_issue_list(exported) if archived else render_issue_list(exported, "No site issues found.")}
    </section>
"""


def build_title(site_id: str | None) -> str:
    return f"Site Issues for {site_id}" if site_id else "Site Issues"


def query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def render_filter_form(site_id: str | None, sort: str, *, archived: bool = False, action: str, tab: str | None = None) -> str:
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
        <label><input type="checkbox" name="archived" value="1"{' checked' if archived else ''}> Archived</label>
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


def find_issue(issue_id: str) -> dict[str, object] | None:
    report = discover_site_issues(include_archived=True)
    for issue in issues_from_report(report):
        if str(issue.get("issue_id") or "") == issue_id:
            return issue
    return None


def render_issue_detail(ctx: object, issue_id: str) -> str:
    query = getattr(ctx, "query", {})
    issue = find_issue(issue_id)
    flash = render_flash(query)
    if issue is None:
        content = f'<section class="error">Issue not found: {html.escape(issue_id)}</section>'
    else:
        title = str(issue.get("title") or "Site issue")
        content = f"""
        <section>
          <h2>{html.escape(title)}</h2>
          <p>{render_pill(issue.get("status"))}</p>
          {render_archived_notice(issue)}
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
        {render_edit_panel(issue)}
        {render_archive_panel(issue)}
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
    issue_id = str(issue.get("issue_id") or "")
    if not issue_id or is_archived(issue) or not transitions:
        return ""
    current_status = str(issue.get("status") or "")
    current_pill = f'<p>Currently: <span class="pill status-{slugify_status(current_status)}">{html.escape(current_status)}</span></p>' if current_status else ""
    links = "".join(
        f'<p><a class="button" href="{html.escape(str(config["confirm_route"]))}?issue_id={quote(issue_id)}">{html.escape(str(config["label"]))}</a></p>'
        for _name, config in transitions
    )
    return f"<section><h2>Actions</h2>{current_pill}{links}</section>"


def render_archive_panel(issue: dict[str, object]) -> str:
    if not str(issue.get("issue_id") or ""):
        return ""
    return f"<section><h2>Archive</h2>{render_archive_control(issue)}</section>"


def render_edit_panel(issue: dict[str, object]) -> str:
    if not str(issue.get("issue_id") or ""):
        return ""
    form = render_record_edit_form(
        issue,
        record_type="site_issue",
        id_field="issue_id",
        post_route="/field-capture/issues/edit",
        fields=ISSUE_EDIT_FIELDS,
        select_options=ISSUE_EDIT_SELECT_OPTIONS,
    )
    return f"<section><h2>Edit</h2>{form}</section>"


def is_archived(issue: dict[str, object]) -> bool:
    return issue.get("archived") is True or str(issue.get("archived") or "").strip().lower() == "true"


def render_archived_notice(issue: dict[str, object]) -> str:
    if not is_archived(issue):
        return ""
    archived_at = str(issue.get("archived_at") or "").strip()
    archived_by = str(issue.get("archived_by") or "").strip()
    detail = " ".join(part for part in (archived_at, f"by {archived_by}" if archived_by else "") if part)
    return f'<p class="muted">Archived{": " + html.escape(detail) if detail else ""}</p>'


def render_archive_control(issue: dict[str, object]) -> str:
    issue_id = str(issue.get("issue_id") or "")
    archived = is_archived(issue)
    route = "/field-capture/issues/restore" if archived else "/field-capture/issues/archive"
    label = "Restore" if archived else "Archive (Delete)"
    return f"""
      <form method="post" action="{route}">
        <input type="hidden" name="issue_id" value="{html.escape(issue_id)}">
        <input type="hidden" name="confirm" value="1">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <label>Note (optional)</label>
        <textarea name="note"></textarea>
        <button type="submit">{label}</button>
      </form>
    """


def render_archived_issue_list(issues: list[dict[str, object]]) -> str:
    return render_table(
        issues,
        [
            {"key": "title", "label": "Issue", "format": lambda value, row: f'<a href="/field-capture/issues?issue_id={quote(str(row.get("issue_id") or ""))}">{html.escape(str(value or "Site issue"))}</a>'},
            {"key": "status", "label": "Status", "format": lambda value, _row: render_pill(value), "nowrap": True},
            {"key": "site_id", "label": "Site", "nowrap": True},
            {"key": "archived_at", "label": "Archived At", "nowrap": True},
            {"key": "issue_id", "label": "Actions", "format": lambda _value, row: render_restore_form(str(row.get("issue_id") or "")), "nowrap": True},
        ],
        empty_text="No archived site issues found.",
    )


def render_restore_form(issue_id: str) -> str:
    return f"""<form method="post" action="/field-capture/issues/restore">
        <input type="hidden" name="issue_id" value="{html.escape(issue_id)}">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <input type="hidden" name="confirm" value="1">
        <button type="submit">Restore</button>
      </form>"""


def render_issue_mark_confirm(ctx: object, transition_name: str) -> str:
    query = getattr(ctx, "query", {})
    issue_id = first_query_value(query, "issue_id").strip()
    issue = find_issue(issue_id)
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


def handle_issue_archive(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_archived",
        route="/field-capture/issues/archive",
        id_field="issue_id",
        redirect_path="/field-capture/issues",
        payload_id_key="record_id",
        extra_payload={"record_type": "site_issue"},
    )


def handle_issue_restore(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_unarchived",
        route="/field-capture/issues/restore",
        id_field="issue_id",
        redirect_path="/field-capture/issues",
        payload_id_key="record_id",
        extra_payload={"record_type": "site_issue"},
    )


def handle_issue_edit(ctx: object, body: bytes):
    return handle_edit_record_fields_post(
        ctx,
        body,
        route="/field-capture/issues/edit",
        record_type="site_issue",
        id_field="issue_id",
        redirect_path="/field-capture/issues",
        fields=ISSUE_EDIT_FIELDS,
    )
