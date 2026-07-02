from __future__ import annotations

import html
from datetime import datetime, timezone

from ops_dashboard.common import first_query_value, render_table
from ops_dashboard.layout import html_page
from .employees import _display_name, _string, load_employees


CLIENT_FIELDS = [
    "_id",
    "capture_id",
    "person_id",
    "person_name",
    "captured_at",
    "exported_at",
    "created_at",
    "client",
]


def _timestamp_text(doc: dict[str, object]) -> str:
    return _string(doc.get("captured_at")) or _string(doc.get("exported_at")) or _string(doc.get("created_at"))


def _timestamp_sort_value(value: str) -> tuple[int, str]:
    raw = _string(value)
    if not raw:
        return 0, ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 1, raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return 2, parsed.astimezone(timezone.utc).isoformat()


def latest_client_by_person(capture_docs: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for doc in capture_docs:
        if not isinstance(doc, dict):
            continue
        client = doc.get("client")
        if not isinstance(client, dict) or not client:
            continue
        person_id = _string(doc.get("person_id"))
        person_name = _string(doc.get("person_name"))
        person_key = person_id or person_name
        if not person_key:
            continue
        last_seen = _timestamp_text(doc)
        row = {
            "person_id": person_id,
            "person_name": person_name,
            "last_seen": last_seen,
            "capture_id": _string(doc.get("capture_id")) or _string(doc.get("_id")),
            "kind": _string(client.get("kind")) or "other",
            "platform": _string(client.get("platform")) or "unknown",
            "os_version": _string(client.get("os_version")),
            "app_version": _string(client.get("app_version")),
            "device_model": _string(client.get("device_model")),
            "user_agent": _string(client.get("user_agent")),
        }
        existing = latest.get(person_key)
        if existing is None or _timestamp_sort_value(last_seen) >= _timestamp_sort_value(_string(existing.get("last_seen"))):
            latest[person_key] = row
    return latest


def load_capture_client_docs() -> list[dict[str, object]]:
    from ops_dashboard import common

    return common._query_field_capture_docs_all(CLIENT_FIELDS, limit=1000)


def _person_id(doc: dict[str, object]) -> str:
    return _string(doc.get("person_id")) or _string(doc.get("_id")).removeprefix("employee_").removeprefix("person_")


def _empty(value: object) -> str:
    text = _string(value)
    return html.escape(text) if text else '<span class="muted">&mdash;</span>'


def _client_cell(value: object, row: dict[str, object]) -> str:
    kind = _string(value)
    label = html.escape(kind.title()) if kind else "&mdash;"
    pill_class = "success" if kind == "native" else "warning" if kind == "pwa" else ""
    pill = f'<span class="pill {pill_class}">{label}</span>' if pill_class else f'<span class="pill">{label}</span>'
    model = _string(row.get("device_model"))
    return f"{pill}<p class=\"subline\">{html.escape(model)}</p>" if model else pill


def _person_cell(value: object, row: dict[str, object]) -> str:
    name = _string(value) or _string(row.get("person_id"))
    person_id = _string(row.get("person_id"))
    id_html = f'<br><code class="muted">{html.escape(person_id)}</code>' if person_id and person_id != name else ""
    return f'<span class="person-name">{html.escape(name) if name else "&mdash;"}</span>{id_html}'


def _last_seen_cell(value: object, row: dict[str, object]) -> str:
    rendered = _empty(value)
    capture_id = _string(row.get("capture_id"))
    if not capture_id:
        return rendered
    return f'{rendered}<p class="subline"><code>{html.escape(capture_id)}</code></p>'


def _rows(query: dict[str, list[str]]) -> tuple[list[dict[str, object]], str]:
    error_html = ""
    try:
        employee_docs = load_employees()
    except Exception as exc:  # noqa: BLE001
        employee_docs = []
        error_html = f'<section class="error"><p>{html.escape(str(exc))}</p></section>'

    rollup = latest_client_by_person(load_capture_client_docs())
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for doc in employee_docs:
        person_id = _person_id(doc)
        if not person_id:
            continue
        latest = rollup.get(person_id) or {}
        seen.add(person_id)
        rows.append(
            {
                "person_id": person_id,
                "person_name": _display_name(doc),
                "kind": _string(latest.get("kind")),
                "platform": _string(latest.get("platform")),
                "os_version": _string(latest.get("os_version")),
                "app_version": _string(latest.get("app_version")),
                "device_model": _string(latest.get("device_model")),
                "last_seen": _string(latest.get("last_seen")),
                "capture_id": _string(latest.get("capture_id")),
                "user_agent": _string(latest.get("user_agent")),
            }
        )

    for key, latest in rollup.items():
        if _string(latest.get("person_id")) in seen or key in seen:
            continue
        rows.append(latest | {"person_name": _string(latest.get("person_name")) or key})

    kind = first_query_value(query, "kind") or "all"
    platform = first_query_value(query, "platform") or "all"
    name_contains = first_query_value(query, "name_contains").strip().lower()
    if kind != "all":
        rows = [row for row in rows if _string(row.get("kind")) == kind]
    if platform != "all":
        rows = [row for row in rows if _string(row.get("platform")) == platform]
    if name_contains:
        rows = [
            row
            for row in rows
            if name_contains in _string(row.get("person_name")).lower() or name_contains in _string(row.get("person_id")).lower()
        ]

    sort = first_query_value(query, "sort") or "name"
    if sort == "last_seen":
        rows.sort(key=lambda row: _timestamp_sort_value(_string(row.get("last_seen"))), reverse=True)
    elif sort == "kind":
        rows.sort(key=lambda row: (_string(row.get("kind")) or "zz", _string(row.get("person_name")).lower()))
    elif sort == "platform":
        rows.sort(key=lambda row: (_string(row.get("platform")) or "zz", _string(row.get("person_name")).lower()))
    else:
        rows.sort(key=lambda row: _string(row.get("person_name")).lower())
    return rows, error_html


def _radio(name: str, current: str, values: list[str]) -> str:
    return "".join(
        f'<label><input type="radio" name="{html.escape(name)}" value="{html.escape(value)}" '
        f'{"checked" if current == value else ""}> {html.escape("All" if value == "all" else value.title())}</label>'
        for value in values
    )


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    rows, error_html = _rows(query)
    kind = first_query_value(query, "kind") or "all"
    platform = first_query_value(query, "platform") or "all"
    sort = first_query_value(query, "sort") or "name"
    table = render_table(
        rows,
        [
            {"key": "person_name", "label": "Person", "format": _person_cell, "nowrap": True},
            {"key": "kind", "label": "Client", "format": _client_cell, "nowrap": True},
            {"key": "platform", "label": "Platform", "format": lambda value, _row: _empty(value), "nowrap": True},
            {"key": "os_version", "label": "OS", "format": lambda value, _row: _empty(value), "priority": 2},
            {"key": "app_version", "label": "App", "format": lambda value, _row: _empty(value), "priority": 2, "nowrap": True},
            {"key": "last_seen", "label": "Last Seen", "format": _last_seen_cell, "nowrap": True},
            {"key": "user_agent", "label": "User Agent", "format": lambda value, _row: _empty(value), "priority": 3},
        ],
        empty_text="No people match this client filter.",
    )
    filters = f"""
      <form method="get" action="/clients" data-submit-on-change>
        <fieldset class="filter-columns"><legend>Client</legend>{_radio('kind', kind, ['all', 'native', 'pwa', 'other'])}</fieldset>
        <fieldset class="filter-columns"><legend>Platform</legend>{_radio('platform', platform, ['all', 'ios', 'android', 'web', 'unknown'])}</fieldset>
        <label>Name contains <input name="name_contains" value="{html.escape(first_query_value(query, 'name_contains'))}"></label>
        <label>Sort <select name="sort">
          <option value="name" {"selected" if sort == "name" else ""}>Name</option>
          <option value="kind" {"selected" if sort == "kind" else ""}>Client</option>
          <option value="platform" {"selected" if sort == "platform" else ""}>Platform</option>
          <option value="last_seen" {"selected" if sort == "last_seen" else ""}>Last seen</option>
        </select></label>
        <button>Apply</button>
      </form>
    """
    body = (
        '<header><h1>Clients / Devices</h1></header>'
        f'{error_html}<div class="content-with-rail"><aside class="filter-rail"><section><h2>Filters</h2>{filters}</section></aside>'
        f'<section><h2>Latest by person</h2>{table}</section></div>'
    )
    return html_page("Clients / Devices", body, active_section="employees")
