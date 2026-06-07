from __future__ import annotations

"""Sync individual field-capture token rows from the Pro into a remote sqlite DB.

Companion to project/field_capture/auth.py -- this module is the *ongoing-sync*
counterpart of the one-shot bootstrap in prod_auth_data.py. It reads a JSON
payload from stdin and applies it to the SQLite DB pointed at by --db.

Designed to be invoked over SSH by the Pro's ops dashboard after a successful
create_token or revoke_token:

    ssh deploy@vps.example.com \
        'cd /srv/btq/apps/field-capture && python3 -m field_capture.sync_token \
         --db /srv/btq/data/field_capture_tokens.sqlite3' < payload.json

Or in backfill mode (reads from a source SQLite, walks all tokens, pushes
each to the target via stdin-less direct connection):

    python3 -m field_capture.sync_token --db <target> --backfill-from <source>

Schema columns kept in sync (token_value is intentionally excluded -- the VPS
only needs the hash for authentication; the raw value is Pro-side support
data per ai-methodology:projects/btq/61_token_admin_supportability/).
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


SYNC_COLUMNS = (
    "token_id",
    "token_hash",
    "person_id",
    "created_at",
    "expires_at",
    "revoked",
    "label",
    "last_used_at",
    "can_submit",
    "can_view_site",
    "token_type",
    "site_ids",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS field_capture_tokens (
          token_id TEXT PRIMARY KEY,
          token_hash TEXT NOT NULL UNIQUE,
          person_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          expires_at TEXT,
          revoked INTEGER NOT NULL DEFAULT 0,
          label TEXT NOT NULL DEFAULT '',
          last_used_at TEXT
        )
        """
    )
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(field_capture_tokens)").fetchall()}
    if "can_submit" not in columns:
        conn.execute("ALTER TABLE field_capture_tokens ADD COLUMN can_submit INTEGER NOT NULL DEFAULT 1")
    if "can_view_site" not in columns:
        conn.execute("ALTER TABLE field_capture_tokens ADD COLUMN can_view_site INTEGER NOT NULL DEFAULT 1")
    if "token_type" not in columns:
        conn.execute("ALTER TABLE field_capture_tokens ADD COLUMN token_type TEXT NOT NULL DEFAULT 'capture'")
    if "site_ids" not in columns:
        conn.execute("ALTER TABLE field_capture_tokens ADD COLUMN site_ids TEXT NOT NULL DEFAULT '[]'")


def upsert_row(conn: sqlite3.Connection, row: dict[str, object]) -> None:
    values = [row.get(column) for column in SYNC_COLUMNS]
    placeholders = ", ".join("?" for _ in SYNC_COLUMNS)
    columns = ", ".join(SYNC_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO field_capture_tokens ({columns}) VALUES ({placeholders})",
        values,
    )


def revoke(conn: sqlite3.Connection, token_id: str) -> None:
    conn.execute("UPDATE field_capture_tokens SET revoked = 1 WHERE token_id = ?", (token_id,))


def backfill_from(target_conn: sqlite3.Connection, source_db_path: Path) -> None:
    source_conn = sqlite3.connect(source_db_path)
    source_conn.row_factory = sqlite3.Row
    try:
        rows = source_conn.execute(f"SELECT {', '.join(SYNC_COLUMNS)} FROM field_capture_tokens").fetchall()
        for row in rows:
            upsert_row(target_conn, {column: row[column] for column in SYNC_COLUMNS})
    finally:
        source_conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--backfill-from")
    args = parser.parse_args(argv)

    try:
        with sqlite3.connect(Path(args.db)) as conn:
            ensure_schema(conn)
            if args.backfill_from:
                backfill_from(conn, Path(args.backfill_from))
            else:
                payload = json.load(sys.stdin)
                action = str(payload.get("action") or "")
                if action == "upsert":
                    row = payload.get("row")
                    if not isinstance(row, dict):
                        raise ValueError("upsert payload requires row object")
                    upsert_row(conn, row)
                elif action == "revoke":
                    token_id = str(payload.get("token_id") or "").strip()
                    if not token_id:
                        raise ValueError("revoke payload requires token_id")
                    revoke(conn, token_id)
                else:
                    raise ValueError(f"unknown action: {action}")
        print(json.dumps({"ok": True}))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
