from __future__ import annotations

import html
from copy import copy
from pathlib import Path
from urllib.parse import urlencode

import ops_dashboard.sections.equipment as equipment
import ops_dashboard.sections.inbox as inbox
import ops_dashboard.sections.issues as issues
import ops_dashboard.sections.supplies as supplies
import ops_dashboard.sections.swipe as swipe


CONSOLE_TABS = (
    ("review", "Review"),
    ("issues", "Issues"),
    ("supplies", "Supplies"),
    ("equipment", "Equipment"),
)


def selected_tab(query: dict[str, list[str]]) -> str:
    values = query.get("tab") if isinstance(query, dict) else None
    tab = str(values[0] if values else "review").strip().lower()
    return tab if tab in {key for key, _label in CONSOLE_TABS} else "review"


def render_tab_bar(active_tab: str, counts: dict[str, int], query: dict[str, list[str]] | None = None) -> str:
    links = []
    for tab, label in CONSOLE_TABS:
        current = tab == active_tab
        current_attr = ' aria-current="page" aria-selected="true"' if current else ' aria-selected="false"'
        href = console_tab_href(tab, query)
        links.append(
            f'<a class="console-tab{" is-active" if current else ""}" href="{html.escape(href)}" '
            f'role="tab"{current_attr}>'
            f'<span>{html.escape(label)}</span>'
            f'<span class="console-tab-badge">{html.escape(str(int(counts.get(tab, 0))))}</span>'
            "</a>"
        )
    return f'<nav class="console-tabs" role="tablist" aria-label="Operational console">{"".join(links)}</nav>'


def console_tab_href(tab: str, query: dict[str, list[str]] | None = None) -> str:
    params: dict[str, str] = {"tab": tab}
    query = query if isinstance(query, dict) else {}
    site_id = first_query_value(query, "site_id")
    sort = first_query_value(query, "sort")
    status = first_query_value(query, "status")
    if site_id:
        params["site_id"] = site_id
    if sort:
        params["sort"] = sort
    if tab in {"supplies", "equipment"}:
        params["status"] = status or "open"
    return "/?" + urlencode(params)


def _review_panel(ctx: object) -> str:
    runtime_root = getattr(ctx, "runtime_root", Path("."))
    review_counts = inbox.console_review_queue_counts(ctx)
    payload = swipe.swipe_payload(runtime_root, counts=review_counts)
    return swipe.render_body(ctx, payload=payload)


def _state_panel(ctx: object, tab: str) -> str:
    panel_ctx = state_panel_context(ctx, tab)
    if tab == "issues":
        return issues.render_field_capture_issues_body(panel_ctx, embedded=True)
    if tab == "supplies":
        return supplies.render_field_capture_supplies_body(panel_ctx, embedded=True)
    if tab == "equipment":
        return equipment.render_field_capture_equipment_body(panel_ctx, embedded=True)
    return ""


def state_panel_context(ctx: object, tab: str) -> object:
    panel_ctx = copy(ctx)
    query = {key: list(values) for key, values in getattr(ctx, "query", {}).items()}
    query["tab"] = [tab]
    if tab in {"supplies", "equipment"} and not first_query_value(query, "status"):
        query["status"] = ["open"]
    panel_ctx.query = query
    return panel_ctx


def first_query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def render_console(ctx: object) -> str:
    query = getattr(ctx, "query", {})
    active_tab = selected_tab(query)
    counts = inbox.console_counts(ctx)
    if active_tab == "review":
        panel = _review_panel(ctx)
    else:
        panel = _state_panel(ctx, active_tab)
    return (
        '<section class="ops-console" aria-label="Operational console">'
        f"{render_tab_bar(active_tab, counts, query)}"
        f'<div class="console-panel" role="tabpanel" data-console-tab="{html.escape(active_tab)}">{panel}</div>'
        "</section>"
    )
