from __future__ import annotations

import html

from ops_dashboard.common import render_issue_list, render_kv
from ops_dashboard.layout import html_page
from site_issues import discover_site_issues, issue_as_export


def render_field_capture_issues(request_ctx: object) -> str:
    query = getattr(request_ctx, "query", {})
    config = request_ctx.config
    site_id = str((query.get("site_id") or [""])[0]).strip() or None
    report = discover_site_issues(config.vault_dir, site_id=site_id)
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    exported = [issue_as_export(issue, include_path=True) for issue in issues]
    title = f"Site Issues for {site_id}" if site_id else "Site Issues"
    return html_page(
        title,
        f"""
    <header>
      <h1>{html.escape(title)}</h1>
      <p class="muted">Read-only structured site issue records from the vault.</p>
    </header>
    <section>
      <h2>Summary</h2>
      {render_kv(report.get("counts") if isinstance(report.get("counts"), dict) else {})}
    </section>
    <section>
      <h2>Issues</h2>
      {render_issue_list(exported, "No site issues found.")}
    </section>
""",
        active_section="issues",
    )


def render(request_ctx: object) -> str:
    return render_field_capture_issues(request_ctx)
