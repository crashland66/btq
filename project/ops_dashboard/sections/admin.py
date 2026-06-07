from __future__ import annotations

from ops_dashboard.layout import html_page


def render_admin(query: dict[str, list[str]] | None = None) -> str:
    body = """
    <header><h1>Admin</h1></header>
    <section>
      <h2>System Health</h2>
      <a href="/health">Health</a>
      <p class="muted">Pipeline health, queue depth, failed jobs, and sidecar status</p>
    </section>
    <section>
      <h2>Operations</h2>
      <a href="/captures">Captures</a>
      <p class="muted">Browse field captures: photos, voice, transcript, and semantic results</p>
      <a href="/candidates">Action Candidates</a>
      <p class="muted">Review pending action candidates: approve or deny</p>
      <a href="/drafts">Drafts &amp; Queue</a>
      <p class="muted">Approved drafts and their queue state</p>
      <a href="/issues">Site Issues</a>
      <p class="muted">Logged site issues by site and status</p>
      <a href="/supplies">Supplies</a>
      <p class="muted">Supply needs and orders: open, mark ordered, mark delivered</p>
    </section>
    <section>
      <h2>Configuration</h2>
      <a href="/sites">Sites</a>
      <p class="muted">Site registry, capture guidance, and display categories</p>
      <a href="/employees">Employees</a>
      <p class="muted">Employee directory, status, assignments, and contact links</p>
      <a href="/tokens">Tokens</a>
      <p class="muted">Field-capture tokens: issue, revoke, and inspect</p>
      <a href="/system">System</a>
      <p class="muted">System-wide defaults: vision context, categories, and capture advice</p>
    </section>
    <section>
      <h2>Media</h2>
      <a href="/field-photos">Field Photos</a>
      <p class="muted">Browse field photo captures in cards with vision context</p>
      <a href="/photos">Photo Vision Sidecars</a>
      <p class="muted">Search photo-vision sidecars by severity, site, and date</p>
      <a href="/audio">Audio Processing</a>
      <p class="muted">Review uploaded audio, transcription status, semantic cleanup, and voice memo intake</p>
      <a href="/batch-images">Batch Image Import</a>
      <p class="muted">Import WhatsApp fallback photos as one field capture</p>
    </section>
    """
    return html_page("Admin", body, active_section="admin")


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    return render_admin(query)
