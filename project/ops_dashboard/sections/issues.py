from __future__ import annotations

import html

from ops_dashboard.common import render_issue_list, render_kv
from ops_dashboard.layout import html_page
from site_issues import discover_site_issues, issue_as_export

ISSUE_SORT_OPTIONS = ("", "site", "recency")


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


def render(request_ctx: object) -> str:
    return render_field_capture_issues(request_ctx)
