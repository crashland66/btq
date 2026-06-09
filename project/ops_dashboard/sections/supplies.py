from __future__ import annotations

import html
from urllib.parse import quote

from ops_dashboard.common import default_actor, first_query_value, handle_mark_transition_post, humanize_key, render_back_link, render_kv, render_site_label, render_status_transition, render_table, slugify_status, write_mark_job
from ops_dashboard.layout import html_page
from site_supplies import discover_site_supplies, supply_as_export

SUPPLY_STATUS_OPTIONS = ("", "open", "ordered", "delivered", "stocked", "no_action_needed")
SUPPLY_TRANSITIONS = {
    "mark-ordered": {
        "label": "Mark ordered",
        "job_type": "mark_supply_ordered",
        "target_status": "ordered",
        "source_statuses": {"open"},
        "fields": ("ordered_at", "ordered_by", "ordered_note"),
        "post_route": "/supplies/mark-ordered",
        "confirm_route": "/supplies/mark-ordered-confirm",
    },
    "mark-delivered": {
        "label": "Mark delivered",
        "job_type": "mark_supply_delivered",
        "target_status": "delivered",
        "source_statuses": {"ordered"},
        "fields": ("delivered_at", "delivered_by", "delivered_note"),
        "post_route": "/supplies/mark-delivered",
        "confirm_route": "/supplies/mark-delivered-confirm",
    },
    "mark-stocked": {
        "label": "Mark stocked",
        "job_type": "mark_supply_stocked",
        "target_status": "stocked",
        "source_statuses": {"delivered"},
        "fields": ("stocked_at", "stocked_by", "stocked_note"),
        "post_route": "/supplies/mark-stocked",
        "confirm_route": "/supplies/mark-stocked-confirm",
    },
    "mark-no-action-needed": {
        "label": "Mark no action needed",
        "job_type": "mark_supply_no_action_needed",
        "target_status": "no_action_needed",
        "source_statuses": {"open", "ordered", "delivered"},
        "fields": ("no_action_needed_at", "no_action_needed_by", "no_action_needed_note"),
        "post_route": "/supplies/mark-no-action-needed",
        "confirm_route": "/supplies/mark-no-action-needed-confirm",
    },
}
SUPPLY_CONFIRM_ROUTES = {str(config["confirm_route"]): name for name, config in SUPPLY_TRANSITIONS.items()}


def render_field_capture_supplies(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    config = ctx.config
    site_id = str((query.get("site_id") or [""])[0]).strip() or None
    status = str((query.get("status") or [""])[0]).strip() or None
    report = discover_site_supplies(config.vault_dir, site_id=site_id, status=status)
    exported = supplies_from_report(report)
    title = build_title(site_id, status)
    return html_page(
        title,
        f"""
    <header>
      <h1>{html.escape(title)}</h1>
      <p class="muted">Read-only structured supply need records from the vault.</p>
    </header>
    <section>
      <h2>Summary</h2>
      {render_kv(report.get("counts") if isinstance(report.get("counts"), dict) else {})}
    </section>
    <section>
      <h2>Filters</h2>
      {render_filter_form(site_id, status, report.get("counts") if isinstance(report.get("counts"), dict) else {})}
    </section>
    <section>
      <h2>Supplies</h2>
      {render_supply_list(exported)}
    </section>
""",
        active_section="supplies",
    )


def build_title(site_id: str | None, status: str | None) -> str:
    parts = ["Supply Needs"]
    if site_id:
        parts.append(f"for {site_id}")
    if status:
        parts.append(f"with status {status}")
    return " ".join(parts)


def render_filter_form(site_id: str | None, status: str | None, counts: dict[str, object] | None = None) -> str:
    options = []
    counts = counts if isinstance(counts, dict) else {}
    status_counts = counts.get("by_status") if isinstance(counts.get("by_status"), dict) else counts
    for value in SUPPLY_STATUS_OPTIONS:
        selected = ' selected' if (status or "") == value else ""
        label = "Any status" if not value else humanize_key(value)
        count_text = ""
        if value:
            count = status_counts.get(value)
            if isinstance(count, int):
                count_text = f" ({count})"
        options.append(f'<option value="{html.escape(value)}"{selected}>{html.escape(label)}{count_text}</option>')
    return f"""<form method="get" action="/supplies" data-submit-on-change>
        <label for="site_id">Site ID</label>
        <input id="site_id" name="site_id" value="{html.escape(site_id or '')}">
        <label for="status">Status</label>
        <select id="status" name="status">{"".join(options)}</select>
        <p><button type="submit">Apply filters</button></p>
      </form>"""


def render_supply_list(supplies: list[object]) -> str:
    if not supplies:
        return "<p>No supply needs found.</p>"
    rows = [supply for supply in supplies if isinstance(supply, dict)]
    return render_table(
        rows,
        [
            {"key": "item_name", "label": "Item", "format": lambda value, row: f'<a href="/supplies?supply_id={quote(str(row.get("supply_id") or ""))}">{html.escape(str(value or "Supply need"))}</a>'},
            {"key": "status", "label": "Status", "format": lambda value, _row: render_pill(value), "nowrap": True},
            {"key": "urgency", "label": "Urgency", "format": lambda value, _row: render_pill(value), "priority": 2, "nowrap": True},
            {"key": "site_id", "label": "Site", "format": lambda value, row: render_site_label(value, site_name=row.get("site_name")), "priority": 1, "nowrap": True},
            {"key": "quantity_needed", "label": "Quantity", "priority": 2, "nowrap": True},
            {"key": "requested_by", "label": "Requested By", "priority": 2, "nowrap": True},
            {"key": "notes", "label": "Notes", "format": lambda value, _row: html.escape(truncate(str(value or ""))), "priority": 3},
            {"key": "_id", "label": "ID", "format": lambda value, _row: html.escape(str(value or "")), "priority": 3, "nowrap": True},
        ],
        empty_text="No supply needs found.",
    )


def supplies_from_report(report: dict[str, object]) -> list[dict[str, object]]:
    supplies = report.get("supplies") if isinstance(report.get("supplies"), list) else []
    return [supply_as_export(supply, include_path=True) for supply in supplies]


def find_supply(vault_root: Path, supply_id: str) -> dict[str, object] | None:
    report = discover_site_supplies(vault_root)
    for supply in supplies_from_report(report):
        if str(supply.get("supply_id") or "") == supply_id:
            return supply
    return None


def render_supply_detail(ctx: object, supply_id: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    supply = find_supply(config.vault_dir, supply_id)
    flash = render_flash(query)
    if supply is None:
        content = f'<section class="error">Supply not found: {html.escape(supply_id)}</section>'
    else:
        title = str(supply.get("item_name") or "Supply need")
        content = f"""
        <section>
          <h2>{html.escape(title)}</h2>
          <p>{render_pill(supply.get("status"))}</p>
          {render_kv(supply, labels={key: humanize_key(key) for key in supply})}
        </section>
        <section>
          <h2>Lifecycle History</h2>
          {render_lifecycle_history(supply, ("ordered", "delivered", "stocked"))}
        </section>
        {render_action_panel(supply)}
        """
    return html_page("Supply Detail", f"<header><h1>Supply Detail</h1></header>{render_back_link('/supplies', 'Back to Supplies')}{flash}{content}", active_section="supplies")


def render_lifecycle_history(item: dict[str, object], prefixes: tuple[str, ...]) -> str:
    rows = []
    for prefix in prefixes:
        for suffix in ("at", "by", "note"):
            key = f"{prefix}_{suffix}"
            value = str(item.get(key) or "").strip()
            if value:
                rows.append({"event": humanize_key(key), "value": value})
    return render_table(rows, [{"key": "event", "label": "Event"}, {"key": "value", "label": "Value"}], empty_text="No lifecycle history yet.")


def valid_transitions(status: object) -> list[tuple[str, dict[str, object]]]:
    current = str(status or "").strip()
    return [(name, config) for name, config in SUPPLY_TRANSITIONS.items() if current in config["source_statuses"]]


def render_action_panel(supply: dict[str, object]) -> str:
    transitions = valid_transitions(supply.get("status"))
    if not transitions:
        return ""
    supply_id = str(supply.get("supply_id") or "")
    current_status = str(supply.get("status") or "")
    current_pill = f'<p>Currently: <span class="pill status-{slugify_status(current_status)}">{html.escape(current_status)}</span></p>' if current_status else ""
    links = "".join(
        f'<p><a class="button" href="{html.escape(str(config["confirm_route"]))}?supply_id={quote(supply_id)}">{html.escape(str(config["label"]))}</a></p>'
        for _name, config in transitions
    )
    return f"<section><h2>Actions</h2>{current_pill}{links}</section>"


def render_supply_mark_confirm(ctx: object, transition_name: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    supply_id = first_query_value(query, "supply_id").strip()
    supply = find_supply(config.vault_dir, supply_id)
    transition = SUPPLY_TRANSITIONS.get(transition_name)
    if supply is None or transition is None:
        content = f'<section class="error">Supply transition not found: {html.escape(supply_id)}</section>'
    else:
        transition_html = render_status_transition(supply.get("status"), transition["target_status"])
        content = f"""
        <section>
          <h2>{html.escape(str(supply.get("item_name") or "Supply need"))}</h2>
          {transition_html}
          {render_kv({"supply_id": supply.get("supply_id", ""), "site": f"{supply.get('site_id') or ''} {supply.get('site_name') or ''}".strip(), "current_status": humanize_key(supply.get("status", ""))}, labels={"supply_id": "Supply ID", "site": "Site", "current_status": "Current Status"})}
          <form method="post" action="{html.escape(str(transition["post_route"]))}">
            <input type="hidden" name="supply_id" value="{html.escape(supply_id)}">
            <input type="hidden" name="confirm" value="1">
            <label>Actor (your name)</label>
            <input name="actor" type="text" required value="{html.escape(default_actor())}">
            <label>Note (optional)</label>
            <textarea name="note"></textarea>
            <button type="submit">{html.escape(str(transition["label"]))}</button>
          </form>
          <p><a href="/supplies?supply_id={quote(supply_id)}">Cancel</a></p>
        </section>
        """
    return html_page("Confirm Supply Transition", f"<header><h1>Confirm Supply Transition</h1></header>{render_back_link(f'/supplies?supply_id={quote(supply_id)}', 'Back to supply detail')}{content}", active_section="supplies")


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


def lifecycle_text(item: dict[str, object], prefix: str) -> str:
    parts = [str(item.get(f"{prefix}_at") or "").strip(), str(item.get(f"{prefix}_by") or "").strip(), str(item.get(f"{prefix}_note") or "").strip()]
    return " | ".join(part for part in parts if part)


def truncate(text: str, limit: int = 200) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "..."


def render(request_ctx: object) -> str:
    query = getattr(request_ctx, "query", {})
    route_path = getattr(request_ctx, "route_path", "/supplies")
    if route_path in SUPPLY_CONFIRM_ROUTES:
        return render_supply_mark_confirm(request_ctx, SUPPLY_CONFIRM_ROUTES[route_path])
    supply_id = first_query_value(query, "supply_id").strip()
    if supply_id:
        return render_supply_detail(request_ctx, supply_id)
    return render_field_capture_supplies(request_ctx)


def handle_supply_mark_post(ctx: object, body: bytes, *, job_type: str, route: str):
    return handle_mark_transition_post(
        ctx, body,
        job_type=job_type,
        route=route,
        id_field="supply_id",
        redirect_path="/supplies",
    )


def handle_mark_supply_ordered(ctx: object, body: bytes):
    return handle_supply_mark_post(ctx, body, job_type="mark_supply_ordered", route="/supplies/mark-ordered")


def handle_mark_supply_delivered(ctx: object, body: bytes):
    return handle_supply_mark_post(ctx, body, job_type="mark_supply_delivered", route="/supplies/mark-delivered")


def handle_mark_supply_stocked(ctx: object, body: bytes):
    return handle_supply_mark_post(ctx, body, job_type="mark_supply_stocked", route="/supplies/mark-stocked")


def handle_mark_supply_no_action_needed(ctx: object, body: bytes):
    return handle_supply_mark_post(ctx, body, job_type="mark_supply_no_action_needed", route="/supplies/mark-no-action-needed")
