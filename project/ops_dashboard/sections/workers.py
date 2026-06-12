"""/workers — per-worker last-capture roster.

A completeness lens, not a performance metric: each row is a worker and the most
recent field capture that has reached the server, sorted stalest-first so anyone
who has gone quiet relative to their schedule rises to the top. "Last capture" is
the device submit time (captured_at) — the claimed work time, the column to
compare against a shift. "Received" is when it actually reached the server; the
gap is sync latency (large on iOS, where there is no background sync).
"""
from __future__ import annotations

import html
import json
import urllib.parse as urlparse
import urllib.request as urlrequest
from datetime import datetime, timezone

from event_pipeline import couchdb_config
from ops_dashboard.common import render_table
from ops_dashboard.layout import html_page

STALE_DAYS = 3


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fetch_captures() -> list[dict]:
    base = couchdb_config.base_url()
    headers = {"Accept": "application/json", **dict(couchdb_config.from_env().auth_header())}
    database = couchdb_config.field_captures_database()
    timeout = max(couchdb_config.timeout(), 30.0)
    url = f"{base}/{urlparse.quote(database, safe='')}/_all_docs?include_docs=true"
    req = urlrequest.Request(url, headers=headers)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    captures = []
    for row in data.get("rows", []):
        doc = row.get("doc")
        if isinstance(doc, dict) and doc.get("type") == "field_capture":
            captures.append(doc)
    return captures


def _aggregate(captures: list[dict]) -> list[dict]:
    by_person: dict[str, dict] = {}
    for doc in captures:
        person_id = str(doc.get("person_id") or "")
        name = str(doc.get("person_name") or "").strip() or person_id or "(unknown)"
        key = person_id or name
        entry = by_person.setdefault(
            key, {"name": name, "count": 0, "last_cap": None, "last_rcv": None}
        )
        entry["count"] += 1
        if entry["name"] == "(unknown)" and name != "(unknown)":
            entry["name"] = name
        cap_dt = _parse_dt(doc.get("captured_at"))
        if cap_dt and (entry["last_cap"] is None or cap_dt > entry["last_cap"]):
            entry["last_cap"] = cap_dt
        rcv_dt = _parse_dt(doc.get("created_at"))
        if rcv_dt and (entry["last_rcv"] is None or rcv_dt > entry["last_rcv"]):
            entry["last_rcv"] = rcv_dt
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(by_person.values(), key=lambda e: e["last_cap"] or floor)


def _humanize_age(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days}d ago"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours}h ago"
    return f"{int(seconds // 60)}m ago"


def _fmt_local(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "—"


def _age_cell(_value: object, item: dict) -> str:
    text = html.escape(str(item.get("age") or ""))
    if item.get("stale"):
        return f'<span class="pill" style="border-color:var(--pending);color:var(--pending)">{text}</span>'
    return text


def render(ctx: object) -> str:  # noqa: ARG001 — section contract
    try:
        captures = _fetch_captures()
    except couchdb_config.CouchDBConfigError as exc:
        body = f'<p class="zero-state">CouchDB is not configured: {html.escape(str(exc))}</p>'
        return html_page("Worker captures", body, active_section="workers")
    except Exception as exc:  # noqa: BLE001 — surface any read failure inline
        body = f'<p class="zero-state">Could not read {html.escape(couchdb_config.field_captures_database())}: {html.escape(str(exc))}</p>'
        return html_page("Worker captures", body, active_section="workers")

    now = datetime.now(timezone.utc)
    entries = _aggregate(captures)
    rows = []
    for e in entries:
        stale = e["last_cap"] is None or (now - e["last_cap"]).total_seconds() >= STALE_DAYS * 86400
        age = _humanize_age((now - e["last_cap"]).total_seconds()) if e["last_cap"] else "never"
        rows.append(
            {
                "name": e["name"],
                "last_capture": _fmt_local(e["last_cap"]),
                "age": age,
                "received": _fmt_local(e["last_rcv"]),
                "count": e["count"],
                "stale": stale,
            }
        )

    table = render_table(
        rows,
        [
            {"key": "name", "label": "Worker"},
            {"key": "last_capture", "label": "Last capture", "nowrap": True},
            {"key": "age", "label": "Age", "format": _age_cell, "nowrap": True},
            {"key": "received", "label": "Received", "priority": 2, "nowrap": True},
            {"key": "count", "label": "Captures"},
        ],
        empty_text="No captures recorded yet.",
    )
    stale_n = sum(1 for r in rows if r["stale"])
    intro = (
        f'<p class="muted">{len(rows)} worker(s); {stale_n} quiet for &ge;{STALE_DAYS}d (highlighted). '
        '"Last capture" is the device submit time — compare it to the schedule. '
        '"Received" is when it reached the server; the gap is sync latency. '
        "This reflects only captures that have synced — a stale row can mean undrained device sync, "
        "not a missed visit.</p>"
    )
    return html_page("Worker captures", intro + table, active_section="workers")
