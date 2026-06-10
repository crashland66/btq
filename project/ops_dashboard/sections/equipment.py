from __future__ import annotations

import html
from urllib.parse import quote

from ops_dashboard.common import default_actor, first_query_value, handle_edit_record_fields_post, handle_mark_transition_post, humanize_key, render_back_link, render_kv, render_record_edit_form, render_site_label, render_status_transition, render_table, slugify_status, write_mark_job
from ops_dashboard.layout import html_page
from site_equipment import discover_site_equipment, equipment_as_export

EQUIPMENT_STATUS_OPTIONS = ("", "open", "approved", "ordered", "provided", "denied", "no_action_needed")
EQUIPMENT_EDIT_FIELDS = ("site_id", "equipment_name", "reason", "priority", "notes")
EQUIPMENT_EDIT_SELECT_OPTIONS = {"priority": ("low", "normal", "high", "urgent")}
EQUIPMENT_TRANSITIONS = {
    "mark-approved": {
        "label": "Mark approved",
        "job_type": "mark_equipment_approved",
        "target_status": "approved",
        "source_statuses": {"open"},
        "fields": ("approved_at", "approved_by", "approval_note"),
        "post_route": "/equipment/mark-approved",
        "confirm_route": "/equipment/mark-approved-confirm",
    },
    "mark-denied": {
        "label": "Mark denied",
        "job_type": "mark_equipment_denied",
        "target_status": "denied",
        "source_statuses": {"open", "approved"},
        "fields": ("denied_at", "denied_by", "denial_note"),
        "post_route": "/equipment/mark-denied",
        "confirm_route": "/equipment/mark-denied-confirm",
    },
    "mark-ordered": {
        "label": "Mark ordered",
        "job_type": "mark_equipment_ordered",
        "target_status": "ordered",
        "source_statuses": {"approved"},
        "fields": ("ordered_at", "ordered_by", "ordered_note"),
        "post_route": "/equipment/mark-ordered",
        "confirm_route": "/equipment/mark-ordered-confirm",
    },
    "mark-provided": {
        "label": "Mark provided",
        "job_type": "mark_equipment_provided",
        "target_status": "provided",
        "source_statuses": {"ordered"},
        "fields": ("provided_at", "provided_by", "provided_note"),
        "post_route": "/equipment/mark-provided",
        "confirm_route": "/equipment/mark-provided-confirm",
    },
    "mark-no-action-needed": {
        "label": "Mark no action needed",
        "job_type": "mark_equipment_no_action_needed",
        "target_status": "no_action_needed",
        "source_statuses": {"open", "approved", "ordered"},
        "fields": ("no_action_needed_at", "no_action_needed_by", "no_action_needed_note"),
        "post_route": "/equipment/mark-no-action-needed",
        "confirm_route": "/equipment/mark-no-action-needed-confirm",
    },
}
EQUIPMENT_CONFIRM_ROUTES = {str(config["confirm_route"]): name for name, config in EQUIPMENT_TRANSITIONS.items()}


def render_field_capture_equipment(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    site_id = query_value(query, "site_id") or None
    status = query_value(query, "status") or None
    title = build_title(site_id, status)
    return html_page(
        title,
        render_field_capture_equipment_body(ctx),
        active_section="equipment",
    )


def render_field_capture_equipment_body(ctx: object, *, embedded: bool = False) -> str:
    query = getattr(ctx, "query", {})
    config = ctx.config
    site_id = query_value(query, "site_id") or None
    status = query_value(query, "status") or None
    sort = query_value(query, "sort")
    archived = query_value(query, "archived") == "1"
    report = discover_site_equipment(config.vault_dir, site_id=site_id, status=status, include_archived=archived, archived_only=archived)
    exported = sort_equipment(equipment_from_report(report), sort)
    title = f"Archived {build_title(site_id, status)}" if archived else build_title(site_id, status)
    action = "/" if embedded else "/equipment"
    tab = "equipment" if embedded else None
    return f"""
    <header>
      <h1>{html.escape(title)}</h1>
      <p class="muted">Read-only structured equipment request records from the vault.</p>
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
      <h2>Equipment</h2>
      {render_equipment_list(exported)}
    </section>
"""


def build_title(site_id: str | None, status: str | None) -> str:
    parts = ["Equipment Requests"]
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
    action: str = "/equipment",
    tab: str | None = None,
    sort: str = "",
    archived: bool = False,
) -> str:
    options = []
    counts = counts if isinstance(counts, dict) else {}
    status_counts = counts.get("by_status") if isinstance(counts.get("by_status"), dict) else counts
    for value in EQUIPMENT_STATUS_OPTIONS:
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
    for value, label in (("", "Default order"), ("priority", "Priority"), ("site", "Site"), ("recency", "Most recent")):
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


def render_equipment_list(equipment: list[object]) -> str:
    if not equipment:
        return "<p>No equipment requests found.</p>"
    rows = [request for request in equipment if isinstance(request, dict)]
    return render_table(
        rows,
        [
            {"key": "equipment_name", "label": "Equipment", "format": lambda value, row: f'<a href="/equipment?equipment_id={quote(str(row.get("equipment_id") or ""))}">{html.escape(str(value or "Equipment request"))}</a>'},
            {"key": "status", "label": "Status", "format": lambda value, _row: render_pill(value), "nowrap": True},
            {"key": "priority", "label": "Priority", "format": lambda value, _row: render_pill(value), "priority": 2, "nowrap": True},
            {"key": "site_id", "label": "Site", "format": lambda value, row: render_site_label(value, site_name=row.get("site_name")), "priority": 1, "nowrap": True},
            {"key": "requested_by", "label": "Requested By", "priority": 2, "nowrap": True},
            {"key": "reason", "label": "Reason", "format": lambda value, _row: html.escape(truncate(str(value or ""))), "priority": 2},
            {"key": "equipment_id", "label": "Actions", "format": lambda _value, row: render_row_actions(row), "priority": 2, "nowrap": True},
            {"key": "_id", "label": "ID", "format": lambda value, _row: html.escape(str(value or "")), "priority": 3, "nowrap": True},
        ],
        empty_text="No equipment requests found.",
    )


def equipment_from_report(report: dict[str, object]) -> list[dict[str, object]]:
    equipment = report.get("equipment") if isinstance(report.get("equipment"), list) else []
    return [equipment_as_export(request, include_path=True) for request in equipment]


def query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def sort_equipment(equipment: list[dict[str, object]], sort: str) -> list[dict[str, object]]:
    if sort == "site":
        return sorted(equipment, key=lambda request: (str(request.get("site_id") or ""), str(request.get("equipment_name") or ""), str(request.get("equipment_id") or "")))
    if sort == "recency":
        return sorted(
            equipment,
            key=lambda request: (
                str(request.get("created_at") or request.get("observed_at") or ""),
                str(request.get("equipment_id") or ""),
            ),
            reverse=True,
        )
    return equipment


def find_equipment(vault_root: Path, equipment_id: str) -> dict[str, object] | None:
    report = discover_site_equipment(vault_root, include_archived=True)
    for request in equipment_from_report(report):
        if str(request.get("equipment_id") or "") == equipment_id:
            return request
    return None


def render_equipment_detail(ctx: object, equipment_id: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    request = find_equipment(config.vault_dir, equipment_id)
    flash = render_flash(query)
    if request is None:
        content = f'<section class="error">Equipment request not found: {html.escape(equipment_id)}</section>'
    else:
        title = str(request.get("equipment_name") or "Equipment request")
        content = f"""
        <section>
          <h2>{html.escape(title)}</h2>
          <p>{render_pill(request.get("status"))}</p>
          {render_archived_notice(request)}
          {render_kv(request, labels={key: humanize_key(key) for key in request})}
        </section>
        <section>
          <h2>Lifecycle History</h2>
          {render_lifecycle_history(request)}
        </section>
        {render_action_panel(request)}
        {render_edit_panel(request)}
        {render_archive_panel(request)}
        """
    return html_page("Equipment Detail", f"<header><h1>Equipment Detail</h1></header>{render_back_link('/equipment', 'Back to Equipment')}{flash}{content}", active_section="equipment")


def render_lifecycle_history(item: dict[str, object]) -> str:
    keys = (
        "approved_at",
        "approved_by",
        "approval_note",
        "denied_at",
        "denied_by",
        "denial_note",
        "ordered_at",
        "ordered_by",
        "ordered_note",
        "provided_at",
        "provided_by",
        "provided_note",
    )
    rows = []
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            rows.append({"event": humanize_key(key), "value": value})
    return render_table(rows, [{"key": "event", "label": "Event"}, {"key": "value", "label": "Value"}], empty_text="No lifecycle history yet.")


def valid_transitions(status: object) -> list[tuple[str, dict[str, object]]]:
    current = str(status or "").strip()
    return [(name, config) for name, config in EQUIPMENT_TRANSITIONS.items() if current in config["source_statuses"]]


def render_action_panel(request: dict[str, object]) -> str:
    transitions = valid_transitions(request.get("status"))
    equipment_id = str(request.get("equipment_id") or "")
    if not equipment_id or is_archived(request) or not transitions:
        return ""
    current_status = str(request.get("status") or "")
    current_pill = f'<p>Currently: <span class="pill status-{slugify_status(current_status)}">{html.escape(current_status)}</span></p>' if current_status else ""
    links = "".join(
        f'<p><a class="button" href="{html.escape(str(config["confirm_route"]))}?equipment_id={quote(equipment_id)}">{html.escape(str(config["label"]))}</a></p>'
        for _name, config in transitions
    )
    return f"<section><h2>Actions</h2>{current_pill}{links}</section>"


def render_archive_panel(request: dict[str, object]) -> str:
    if not str(request.get("equipment_id") or ""):
        return ""
    return f"<section><h2>Archive</h2>{render_archive_control(request)}</section>"


def render_edit_panel(request: dict[str, object]) -> str:
    if not str(request.get("equipment_id") or ""):
        return ""
    form = render_record_edit_form(
        request,
        record_type="equipment_request",
        id_field="equipment_id",
        post_route="/equipment/edit",
        fields=EQUIPMENT_EDIT_FIELDS,
        select_options=EQUIPMENT_EDIT_SELECT_OPTIONS,
    )
    return f"<section><h2>Edit</h2>{form}</section>"


def render_row_actions(request: dict[str, object]) -> str:
    equipment_id = str(request.get("equipment_id") or "")
    if is_archived(request):
        return render_restore_form(equipment_id)
    links = [
        f'<a href="{html.escape(str(config["confirm_route"]))}?equipment_id={quote(equipment_id)}">{html.escape(str(config["label"]))}</a>'
        for _name, config in valid_transitions(request.get("status"))
    ]
    return " ".join(links)


def is_archived(request: dict[str, object]) -> bool:
    return request.get("archived") is True or str(request.get("archived") or "").strip().lower() == "true"


def render_archived_notice(request: dict[str, object]) -> str:
    if not is_archived(request):
        return ""
    archived_at = str(request.get("archived_at") or "").strip()
    archived_by = str(request.get("archived_by") or "").strip()
    detail = " ".join(part for part in (archived_at, f"by {archived_by}" if archived_by else "") if part)
    return f'<p class="muted">Archived{": " + html.escape(detail) if detail else ""}</p>'


def render_archive_control(request: dict[str, object]) -> str:
    equipment_id = str(request.get("equipment_id") or "")
    archived = is_archived(request)
    route = "/equipment/restore" if archived else "/equipment/archive"
    label = "Restore" if archived else "Archive (Delete)"
    return f"""
      <form method="post" action="{route}">
        <input type="hidden" name="equipment_id" value="{html.escape(equipment_id)}">
        <input type="hidden" name="confirm" value="1">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <label>Note (optional)</label>
        <textarea name="note"></textarea>
        <button type="submit">{label}</button>
      </form>
    """


def render_restore_form(equipment_id: str) -> str:
    return f"""<form method="post" action="/equipment/restore">
        <input type="hidden" name="equipment_id" value="{html.escape(equipment_id)}">
        <input type="hidden" name="actor" value="{html.escape(default_actor())}">
        <input type="hidden" name="confirm" value="1">
        <button type="submit">Restore</button>
      </form>"""


def render_equipment_mark_confirm(ctx: object, transition_name: str) -> str:
    config = ctx.config
    query = getattr(ctx, "query", {})
    equipment_id = first_query_value(query, "equipment_id").strip()
    request = find_equipment(config.vault_dir, equipment_id)
    transition = EQUIPMENT_TRANSITIONS.get(transition_name)
    if request is None or transition is None:
        content = f'<section class="error">Equipment transition not found: {html.escape(equipment_id)}</section>'
    else:
        transition_html = render_status_transition(request.get("status"), transition["target_status"])
        content = f"""
        <section>
          <h2>{html.escape(str(request.get("equipment_name") or "Equipment request"))}</h2>
          {transition_html}
          {render_kv({"equipment_id": request.get("equipment_id", ""), "site": f"{request.get('site_id') or ''} {request.get('site_name') or ''}".strip(), "current_status": humanize_key(request.get("status", ""))}, labels={"equipment_id": "Equipment ID", "site": "Site", "current_status": "Current Status"})}
          <form method="post" action="{html.escape(str(transition["post_route"]))}">
            <input type="hidden" name="equipment_id" value="{html.escape(equipment_id)}">
            <input type="hidden" name="confirm" value="1">
            <label>Actor (your name)</label>
            <input name="actor" type="text" required value="{html.escape(default_actor())}">
            <label>Note (optional)</label>
            <textarea name="note"></textarea>
            <button type="submit">{html.escape(str(transition["label"]))}</button>
          </form>
          <p><a href="/equipment?equipment_id={quote(equipment_id)}">Cancel</a></p>
        </section>
        """
    return html_page("Confirm Equipment Transition", f"<header><h1>Confirm Equipment Transition</h1></header>{render_back_link(f'/equipment?equipment_id={quote(equipment_id)}', 'Back to equipment detail')}{content}", active_section="equipment")


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


def lifecycle_text(item: dict[str, object], prefix: str, note_key: str) -> str:
    parts = [str(item.get(f"{prefix}_at") or "").strip(), str(item.get(f"{prefix}_by") or "").strip(), str(item.get(note_key) or "").strip()]
    return " | ".join(part for part in parts if part)


def truncate(text: str, limit: int = 200) -> str:
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "..."


def render(request_ctx: object) -> str:
    query = getattr(request_ctx, "query", {})
    route_path = getattr(request_ctx, "route_path", "/equipment")
    if route_path in EQUIPMENT_CONFIRM_ROUTES:
        return render_equipment_mark_confirm(request_ctx, EQUIPMENT_CONFIRM_ROUTES[route_path])
    equipment_id = first_query_value(query, "equipment_id").strip()
    if equipment_id:
        return render_equipment_detail(request_ctx, equipment_id)
    return render_field_capture_equipment(request_ctx)


def handle_equipment_mark_post(ctx: object, body: bytes, *, job_type: str, route: str):
    return handle_mark_transition_post(
        ctx, body,
        job_type=job_type,
        route=route,
        id_field="equipment_id",
        redirect_path="/equipment",
    )


def handle_mark_equipment_approved(ctx: object, body: bytes):
    return handle_equipment_mark_post(ctx, body, job_type="mark_equipment_approved", route="/equipment/mark-approved")


def handle_mark_equipment_denied(ctx: object, body: bytes):
    return handle_equipment_mark_post(ctx, body, job_type="mark_equipment_denied", route="/equipment/mark-denied")


def handle_mark_equipment_ordered(ctx: object, body: bytes):
    return handle_equipment_mark_post(ctx, body, job_type="mark_equipment_ordered", route="/equipment/mark-ordered")


def handle_mark_equipment_provided(ctx: object, body: bytes):
    return handle_equipment_mark_post(ctx, body, job_type="mark_equipment_provided", route="/equipment/mark-provided")


def handle_mark_equipment_no_action_needed(ctx: object, body: bytes):
    return handle_equipment_mark_post(ctx, body, job_type="mark_equipment_no_action_needed", route="/equipment/mark-no-action-needed")


def handle_equipment_archive(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_archived",
        route="/equipment/archive",
        id_field="equipment_id",
        redirect_path="/equipment",
        payload_id_key="record_id",
        extra_payload={"record_type": "equipment_request"},
    )


def handle_equipment_restore(ctx: object, body: bytes):
    return handle_mark_transition_post(
        ctx, body,
        job_type="mark_record_unarchived",
        route="/equipment/restore",
        id_field="equipment_id",
        redirect_path="/equipment",
        payload_id_key="record_id",
        extra_payload={"record_type": "equipment_request"},
    )


def handle_equipment_edit(ctx: object, body: bytes):
    return handle_edit_record_fields_post(
        ctx,
        body,
        route="/equipment/edit",
        record_type="equipment_request",
        id_field="equipment_id",
        redirect_path="/equipment",
        fields=EQUIPMENT_EDIT_FIELDS,
    )
