from __future__ import annotations

import html
from pathlib import Path

import ops_dashboard.sections.inbox as inbox
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


def render_tab_bar(active_tab: str, counts: dict[str, int]) -> str:
    links = []
    for tab, label in CONSOLE_TABS:
        current = tab == active_tab
        current_attr = ' aria-current="page" aria-selected="true"' if current else ' aria-selected="false"'
        links.append(
            f'<a class="console-tab{" is-active" if current else ""}" href="/?tab={html.escape(tab)}" '
            f'role="tab"{current_attr}>'
            f'<span>{html.escape(label)}</span>'
            f'<span class="console-tab-badge">{html.escape(str(int(counts.get(tab, 0))))}</span>'
            "</a>"
        )
    return f'<nav class="console-tabs" role="tablist" aria-label="Operational console">{"".join(links)}</nav>'


def _review_panel(ctx: object) -> str:
    runtime_root = getattr(ctx, "runtime_root", Path("."))
    review_counts = inbox.console_review_queue_counts(ctx)
    payload = swipe.swipe_payload(runtime_root, counts=review_counts)
    return swipe.render_body(ctx, payload=payload)


def _state_panel(ctx: object, tab: str) -> str:
    card = inbox.console_cards(ctx)[tab]
    return inbox._render_primary_card(card, ctx.config.vault_dir)


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
        f"{render_tab_bar(active_tab, counts)}"
        f'<div class="console-panel" role="tabpanel" data-console-tab="{html.escape(active_tab)}">{panel}</div>'
        "</section>"
    )

