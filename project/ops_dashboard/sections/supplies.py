from __future__ import annotations

import html
from urllib.parse import quote

from ops_dashboard.common import default_actor, first_query_value, handle_edit_record_fields_post, handle_mark_transition_post, humanize_key, render_back_link, render_kv, render_record_edit_form, render_site_label, render_status_transition, render_table, slugify_status, write_mark_job
from ops_dashboard.layout import html_page
from site_supplies import discover_site_supplies, supply_as_export

SUPPLY_STATUS_OPTIONS = ("", "open", "ordered", "delivered", "stocked", "no_action_needed")
SUPPLY_EDIT_FIELDS = ("site_id", "item_name", "quantity_needed", "urgency", "notes")
SUPPLY_EDIT_SELECT_OPTIONS = {"urgency": ("low", "normal", "high", "critical")}
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
    site_id = query_value(query, "site_id") or None
    status = query_value(query, "status") or None
    title = build_title(site_id, status)
    return html_page(
        title,
        render_field_capture_supplies_body(ctx),
        active_section="supplies",
    )


def render_field_capture_supplies_body(ctx: object, *, embedded: bool = False) -> str:
    query = getattr(ctx, "query", {})
    site_id = query_value(query, "site_id") or None
    status = query_value(query, "status") or None
    sort = query_value(query, "sort")
    archived = query_value(query, "archived") == "1"
    report = discover_site_supplies(site_id=site_id, status=status, include_archived=archived, archived_only=archived)
    exported = sort_supplies(supplies_from_report(report), sort)
    title = f"Archived {build_title(site_id, status)}" if archived else build_title(site_id, status)
    action = "/" if embedded else "/supplies"
    tab = "supplies" if embedded else None
    return f"""
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
      {render_filter_form(site_id, status, report.get("counts") if isinstance(report.get("counts"), dict) else {}, action=action, tab=tab, sort=sort, archived=archived)}
    </section>
    <section>
      <h2>Supplies</h2>
      {render_supply_list(exported, list_filters=list_filters(site_id=site_id, status=status, sort=sort, archived=archived))}
    </section>
"""


def build_title(site_id: str | None, status: str | None) -> str:
    parts = ["Supply Needs"]
    if site_id:
        parts.append(f"for {site_id}")
    if status:
        parts.append(f"with status {status}")
    return " ".join(parts)


def render_filter_form(
    site_id: str | None,
    status: str | None,
    counts: dict[str, object] | None = None,
    *,
    action: str = "/supplies",
    tab: str | None = None,
    sort: str = "",
    archived: bool = False,
) -> str:
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
    tab_input = f'<input type="hidden" name="tab" value="{html.escape(tab)}">' if tab else ""
    sort_options = []
    for value, label in (("", "Default order"), ("urgency", "Urgency"), ("site", "Site"), ("recency", "Most recent")):
        selected = ' selected' if (sort or "") == value else ""
        sort_options.append(f'<option value="{html.escape(value)}"{selected}>{html.escape(label)}</option>')
    return f"""<form method="get" action="{html.escape(action)}" data-submit-on-change>
        {tab_input}
        <label for="site_id">Site ID</label>
        <input id="site_id" name="site_id" value="{html.escape(site_id or '')}">
        <label for="status">Status</label>
        <select id="status" name="status">{"".join(options)}</select>
        <label for="sort">Sort</label>
        <select id="sort" name="sort">{"".join(sort_options)}</select>
        <label><input type="checkbox" name="archived" value="1"{' checked' if archived else ''}> Archived</label>
        <p><button type="submit">Apply filters</button></p>
      </form>"""


def list_filters(*, site_id: str | None, status: str | None, sort: str, archived: bool) -> dict[str, str]:
    return {
        "status": status or "",
        "site_id": site_id or "",
        "sort": sort,
        "archived": "1" if archived else "",
    }


def render_list_return_inputs(filters: dict[str, str] | None) -> str:
    if filters is None:
        return ""
    inputs = ['<input type="hidden" name="return" value="list">']
    for key in ("status", "site_id", "sort", "archived"):
        value = str(filters.get(key) or "").strip()
        if value:
            inputs.append(f'<input type="hidden" name="{key}" value="{html.escape(value, quote=True)}">')
    return "\n        ".join(inputs)


def render_supply_list(supplies: list[object], *, list_filters: dict[str, str] | None = None) -> str:
    rows = [supply for supply in supplies if isinstance(supply, dict)]
    if not rows:
        return "<p>No supply needs found.</p>"
    cards = []
    for supply in rows:
        supply_id = str(supply.get("supply_id") or "")
        title = html.escape(str(supply.get("item_name") or "Supply need"))
        pills = "".join((render_pill(supply.get("status")), render_pill(supply.get("urgency"))))
        quantity = str(supply.get("quantity_needed") or "").strip()
        requested_by = str(supply.get("requested_by") or "").strip()
        record_id = str(supply.get("_id") or "").strip()
        notes = truncate(str(supply.get("notes") or ""))
        meta_lines = [
            f'<div><span style="color:var(--muted)">Site:</span> {render_site_label(supply.get("site_id"), site_name=supply.get("site_name"))}</div>',
        ]
        if quantity:
            meta_lines.append(f'<div><span style="color:var(--muted)">Quantity:</span> {html.escape(quantity)}</div>')
        if requested_by:
            meta_lines.append(f'<div><span style="color:var(--muted)">Requested by:</span> {html.escape(requested_by)}</div>')
        if record_id:
            meta_lines.append(f'<div><span style="color:var(--muted)">ID:</span> {html.escape(record_id)}</div>')
        notes_html = f'<p style="margin:10px 0 0">{html.escape(notes)}</p>' if notes else ""
        archived_notice = render_archived_notice(supply)
        actions = render_row_actions(supply, list_filters=list_filters)
        actions_footer = f'<footer style="margin-top:12px">{actions}</footer>' if actions else ""
        cards.append(
            f"""<article style="border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:14px">
  <h3 style="margin:0 0 8px"><a href="/supplies?supply_id={quote(supply_id)}">{title}</a></h3>
  <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">{pills}</div>
  <div style="display:grid;gap:4px">{"".join(meta_lines)}</div>
  {notes_html}
  {archived_notice}
  {actions_footer}
</article>"""
        )
    return f'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px;margin-top:12px">{"".join(cards)}</div>'


def supplies_from_report(report: dict[str, object]) -> list[dict[str, object]]:
    supplies = report.get("supplies") if isinstance(report.get("supplies"), list) else []
    return [supply_as_export(supply, include_path=True) for supply in supplies]


def query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def sort_supplies(supplies: list[dict[str, object]], sort: str) -> list[dict[str, object]]:
    if sort == "site":
        return sorted(supplies, key=lambda supply: (str(supply.get("site_id") or ""), str(supply.get("item_name") or ""), str(supply.get("supply_id") or "")))
    if sort == "recency":
        return sorted(
            supplies,
            key=lambda supply: (
                str(supply.get("created_at") or supply.get("observed_at") or ""),
                str(supply.get("supply_id") or ""),
            ),
            reverse=True,
        )
    return supplies


def find_supply(supply_id: str) -> dict[str, object] | None:
    report = discover_site_supplies(include_archived=True)
    for supply in supplies_from_report(report):
        if str(supply.get("supply_id") or "") == supply_id:
            return supply
    return None


def render_supply_detail(ctx: object, supply_id: str) -> str:
    query = getattr(ctx, "query", {})
    supply = find_supply(supply_id)
    flash = render_flash(query)
    if supply is None:
        content = f'<section class="error">Supply not found: {html.escape(supply_id)}</section>'
    else:
        title = str(supply.get("item_name") or "Supply need")
        content = f"""
        <section>
          <h2>{html.escape(title)}</h2>
          <p>{render_pill(supply.get("status"))}</p>
          {render_archived_notice(supply)}
          {render_kv(supply, labels={key: humanize_key(key) for key in supply})}
        </section>
        <section>
          <h2>Lifecycle History</h2>
          {render_lifecycle_history(supply, ("ordered", "delivered", "stocked"))}
        </section>
        {render_action_panel(supply)}
        {render_edit_panel(supply)}
        {render_archive_panel(supply)}
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
    supply_id = str(supply.get("supply_id") or "")
    if not supply_id or is_archived(supply) or not transitions:
        return ""
    current_status = str(supply.get("status") or "")
    current_pill = f'<p>Currently: <span class="pill status-{slugify_status(current_status)}">{html.escape(current_status)}</span></p>' if current_status else ""
    links = "".join(
        f'<p><a class="button" href="{html.escape(str(config["confirm_route"]))}?supply_id={quote(supply_id)}">{html.escape(str(config["label"]))}</a></p>'
        for _name, config in transitions
    )
    return f"<section><h2>Actions</h2>{current_pill}{links}</section>"


def render_archive_panel(supply: dict[str, object]) -> str:
    if not str(supply.get("supply_id") or ""):
        return ""
    return f"<section><h2>Archive</h2>{render_archive_control(supply)}</section>"


def render_edit_panel(supply: dict[str, object]) -> str:
    if not str(supply.get("supply_id") or ""):
        return ""
    form = render_record_edit_form(
        supply,
        record_type="supply_need",
        id_field="supply_id",
        post_route="/supplies/edit",
        fields=SUPPLY_EDIT_FIELDS,
        select_options=SUPPLY_EDIT_SELECT_OPTIONS,
    )
    return f"<section><h2>Edit</h2>{form}</section>"


def render_row_actions(supply: dict[str, object], *, list_filters: dict[str, str] | None = None) -> str:
    supply_id = str(supply.get("supply_id") or "")
    if is_archived(supply):
        return render_restore_form(supply_id)
    list_return_inputs = render_list_return_inputs(list_filters)
    forms = [
        f"""<form method="post" action="{html.escape(str(config["post_route"]))}" style="display:inline">
        <input type="hidden" name="supply_id" value="{html.escape(supply_id)}">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <input type="hidden" name="confirm" value="1">
        {list_return_inputs}
        <button type="submit">{html.escape(str(config["label"]))}</button>
      </form>"""
        for _name, config in valid_transitions(supply.get("status"))
    ]
    if not forms:
        return ""
    return f'<span style="display:inline-flex;flex-wrap:wrap;gap:8px">{"".join(forms)}</span>'


def is_archived(supply: dict[str, object]) -> bool:
    return supply.get("archived") is True or str(supply.get("archived") or "").strip().lower() == "true"


def render_archived_notice(supply: dict[str, object]) -> str:
    if not is_archived(supply):
        return ""
    archived_at = str(supply.get("archived_at") or "").strip()
    archived_by = str(supply.get("archived_by") or "").strip()
    detail = " ".join(part for part in (archived_at, f"by {archived_by}" if archived_by else "") if part)
    return f'<p class="muted">Archived{": " + html.escape(detail) if detail else ""}</p>'


def render_archive_control(supply: dict[str, object]) -> str:
    supply_id = str(supply.get("supply_id") or "")
    archived = is_archived(supply)
    route = "/supplies/restore" if archived else "/supplies/archive"
    label = "Restore" if archived else "Archive (Delete)"
    return f"""
      <form method="post" action="{route}">
        <input type="hidden" name="supply_id" value="{html.escape(supply_id)}">
        <input type="hidden" name="confirm" value="1">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <label>Note (optional)</label>
        <textarea name="note"></textarea>
        <button type="submit">{label}</button>
      </form>
    """


def render_restore_form(supply_id: str) -> str:
    return f"""<form method="post" action="/supplies/restore">
        <input type="hidden" name="supply_id" value="{html.escape(supply_id)}">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <input type="hidden" name="confirm" value="1">
        <button type="submit">Restore</button>
      </form>"""


def render_supply_mark_confirm(ctx: object, transition_name: str) -> str:
    query = getattr(ctx, "query", {})
    supply_id = first_query_value(query, "supply_id").strip()
    supply = find_supply(supply_id)
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


def handle_supply_archive(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_archived",
        route="/supplies/archive",
        id_field="supply_id",
        redirect_path="/supplies",
        payload_id_key="record_id",
        extra_payload={"record_type": "supply_need"},
    )


def handle_supply_restore(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_unarchived",
        route="/supplies/restore",
        id_field="supply_id",
        redirect_path="/supplies",
        payload_id_key="record_id",
        extra_payload={"record_type": "supply_need"},
    )


def handle_supply_edit(ctx: object, body: bytes):
    return handle_edit_record_fields_post(
        ctx,
        body,
        route="/supplies/edit",
        record_type="supply_need",
        id_field="supply_id",
        redirect_path="/supplies",
        fields=SUPPLY_EDIT_FIELDS,
    )
