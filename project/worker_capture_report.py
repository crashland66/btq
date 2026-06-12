#!/usr/bin/env python3
"""Worker capture report — who last posted a capture, and when.

Reads every field-capture document from CouchDB, groups by worker, and prints
each worker's most recent capture, sorted stalest-first so anyone who has gone
quiet relative to their schedule rises to the top. This is a completeness lens,
not a performance metric: a stale row means "no evidence has reached the server
for this worker lately" — which on iOS can be undrained sync rather than a
missed visit, so read it against what you know from eHub.

Timestamps:
  * "Last capture" is `captured_at` — the device clock when the worker hit
    submit (the claimed work time). It is the right column to compare to a shift.
  * `--verbose` also shows `created_at` — when the capture actually reached the
    server. The gap between the two is the sync latency (large on iOS).

Optionally pass --tokens-db to also list workers who hold a capture token but
have NEVER posted a capture (they won't otherwise appear).

Usage:
  python3 -m worker_capture_report                       # all workers who've captured
  python3 -m worker_capture_report --stale-days 3        # flag >3 days quiet
  python3 -m worker_capture_report --tokens-db /srv/btq/data/field_capture_tokens.sqlite3
  python3 -m worker_capture_report --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from event_pipeline import couchdb_config


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO timestamp (offset-aware or 'Z') into an aware datetime."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _fetch_captures(cfg: couchdb_config.CouchDBConfig, db_name: str) -> list[dict]:
    url = f"{cfg.base_url}/{urllib.parse.quote(db_name, safe='')}/_all_docs?include_docs=true"
    req = urllib.request.Request(
        url, headers={**cfg.auth_header(), "Accept": "application/json"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=max(cfg.timeout, 30.0)) as resp:
        data = json.loads(resp.read())
    captures = []
    for row in data.get("rows", []):
        doc = row.get("doc")
        if isinstance(doc, dict) and doc.get("type") == "field_capture":
            captures.append(doc)
    return captures


def build_report(captures: list[dict]) -> dict[str, dict]:
    by_person: dict[str, dict] = {}
    for doc in captures:
        person_id = str(doc.get("person_id") or "")
        name = str(doc.get("person_name") or "").strip() or person_id or "(unknown)"
        key = person_id or name
        entry = by_person.setdefault(
            key, {"name": name, "count": 0, "last_cap": None, "last_cap_raw": "", "last_rcv": None}
        )
        entry["count"] += 1
        if not entry["name"] or entry["name"] == "(unknown)":
            entry["name"] = name
        cap_dt = _parse_dt(doc.get("captured_at"))
        if cap_dt and (entry["last_cap"] is None or cap_dt > entry["last_cap"]):
            entry["last_cap"] = cap_dt
            entry["last_cap_raw"] = str(doc.get("captured_at") or "")
        rcv_dt = _parse_dt(doc.get("created_at"))
        if rcv_dt and (entry["last_rcv"] is None or rcv_dt > entry["last_rcv"]):
            entry["last_rcv"] = rcv_dt
    return by_person


def add_never_captured(by_person: dict[str, dict], tokens_db: str) -> None:
    """Add capture-token holders who have no captures yet, as 'never'."""
    conn = sqlite3.connect(tokens_db)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT person_id, label, revoked, token_type FROM field_capture_tokens"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        if row["revoked"]:
            continue
        if (row["token_type"] or "capture") != "capture":
            continue  # skip admin/viewer tokens
        person_id = str(row["person_id"] or "")
        if not person_id or person_id in by_person:
            continue
        by_person[person_id] = {
            "name": (row["label"] or person_id).strip(),
            "count": 0,
            "last_cap": None,
            "last_cap_raw": "",
            "last_rcv": None,
        }


def _humanize_age(delta_seconds: float) -> str:
    days = int(delta_seconds // 86400)
    if days >= 1:
        return f"{days}d ago"
    hours = int(delta_seconds // 3600)
    if hours >= 1:
        return f"{hours}h ago"
    minutes = int(delta_seconds // 60)
    return f"{minutes}m ago"


def render_table(by_person: dict[str, dict], now: datetime, stale_days: int, verbose: bool) -> str:
    # Stalest first; never-captured (no last_cap) sort to the very top.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    entries = sorted(by_person.values(), key=lambda e: e["last_cap"] or floor)
    lines = []
    header = f"{'Worker':<26} {'Last capture':<17} {'Age':<10}"
    if verbose:
        header += f" {'Received':<17}"
    header += f" {'#':>5}"
    lines.append(header)
    lines.append("-" * len(header))
    for e in entries:
        if e["last_cap"]:
            local = e["last_cap"].astimezone()
            last = local.strftime("%Y-%m-%d %H:%M")
            age_sec = (now - e["last_cap"]).total_seconds()
            age = _humanize_age(age_sec)
            stale = age_sec >= stale_days * 86400
        else:
            last, age, stale = "—", "never", True
        flag = "  <-- check" if stale else ""
        row = f"{e['name'][:26]:<26} {last:<17} {age:<10}"
        if verbose:
            rcv = e["last_rcv"].astimezone().strftime("%Y-%m-%d %H:%M") if e["last_rcv"] else "—"
            row += f" {rcv:<17}"
        row += f" {e['count']:>5}{flag}"
        lines.append(row)
    lines.append("")
    lines.append(
        f"{len(entries)} worker(s); stale threshold {stale_days}d. "
        "'Last capture' = device submit time (captured_at); compare to shift."
    )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Per-worker last-capture report.")
    parser.add_argument("--stale-days", type=int, default=2, help="flag workers quiet >= this many days")
    parser.add_argument("--tokens-db", type=str, default=None, help="sqlite token DB to include never-captured workers")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--verbose", action="store_true", help="also show server receive time (created_at)")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = couchdb_config.from_env()
    db_name = couchdb_config.field_captures_database()
    try:
        captures = _fetch_captures(cfg, db_name)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"error: could not read CouchDB {db_name}: {exc}", file=sys.stderr)
        return 1
    by_person = build_report(captures)
    if args.tokens_db:
        try:
            add_never_captured(by_person, args.tokens_db)
        except sqlite3.Error as exc:
            print(f"warning: could not read tokens DB: {exc}", file=sys.stderr)
    now = datetime.now(timezone.utc)
    if args.json:
        out = [
            {
                "person": e["name"],
                "last_captured_at": e["last_cap_raw"] or None,
                "last_received_at": e["last_rcv"].isoformat() if e["last_rcv"] else None,
                "captures": e["count"],
                "stale": e["last_cap"] is None or (now - e["last_cap"]).total_seconds() >= args.stale_days * 86400,
            }
            for e in sorted(by_person.values(), key=lambda e: e["last_cap"] or datetime.min.replace(tzinfo=timezone.utc))
        ]
        print(json.dumps(out, indent=2))
    else:
        print(render_table(by_person, now, args.stale_days, args.verbose))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
