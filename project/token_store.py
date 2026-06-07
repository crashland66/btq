"""SQLite-backed token store for person access tokens.

This module is intentionally dependency-free (standard library only) so it can
be imported by surfaces that do not carry the full vault toolchain — notably
the voice memo intake server, which deploys as a self-contained tree. The
vault-aware helpers that turn a token into an authorized session live in
``field_capture/auth.py``, which re-exports everything here for compatibility.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


UNIVERSAL_SITE_SCOPE = "*"
ALLOWED_ROLES = ("cleaner", "site_admin")
DEFAULT_ROLE = "cleaner"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def token_hash(token_value: str) -> str:
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    token_hash: str
    person_id: str
    created_at: str
    expires_at: str | None
    revoked: bool
    label: str
    last_used_at: str | None
    can_submit: bool
    can_view_site: bool
    role: str = DEFAULT_ROLE
    token_type: str = "capture"
    site_ids: tuple[str, ...] = ()
    token_value: str | None = None


@dataclass(frozen=True)
class CreatedToken:
    record: TokenRecord
    token_value: str


class TokenStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve(strict=False)

    def ensure_parent_dir(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        self.ensure_parent_dir()
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
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
            ensure_token_columns(connection)

    def create_token(
        self,
        person_id: str,
        label: str = "",
        expires_at: datetime | None = None,
        *,
        can_submit: bool = True,
        can_view_site: bool = True,
        role: str = DEFAULT_ROLE,
        token_type: str = "capture",
        site_ids: Iterable[str] | None = None,
    ) -> CreatedToken:
        if role not in ALLOWED_ROLES:
            raise ValueError(f"Unknown token role: {role}")
        self.initialize()
        token_id = f"fct_{secrets.token_urlsafe(10)}"
        token_value = f"fc_{secrets.token_urlsafe(32)}"
        created_at = isoformat(utc_now())
        assert created_at is not None
        normalized_site_ids = normalize_site_ids(site_ids or [])
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO field_capture_tokens
                  (token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at,
                   can_submit, can_view_site, role, token_type, site_ids, token_value)
                VALUES (?, ?, ?, ?, ?, 0, ?, NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    token_hash(token_value),
                    person_id,
                    created_at,
                    isoformat(expires_at),
                    label,
                    1 if can_submit else 0,
                    1 if can_view_site else 0,
                    role,
                    token_type,
                    json.dumps(list(normalized_site_ids)),
                    token_value,
                ),
            )
        record = self.get_token(token_id)
        if record is None:
            raise RuntimeError("Created token could not be read back")
        return CreatedToken(record=record, token_value=token_value)

    def get_token(self, token_id: str) -> TokenRecord | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM field_capture_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        return row_to_record(row) if row is not None else None

    def list_tokens(self) -> list[TokenRecord]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM field_capture_tokens
                ORDER BY revoked ASC, created_at DESC, token_id ASC
                """
            ).fetchall()
        return [row_to_record(row) for row in rows]

    def revoke_token(self, token_id: str) -> bool:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE field_capture_tokens SET revoked = 1 WHERE token_id = ? AND revoked = 0",
                (token_id,),
            )
            return cursor.rowcount > 0

    def set_token_value(self, token_id: str, raw_value: str) -> bool:
        """Populate token_value only when raw_value matches the stored hash."""
        self.initialize()
        raw_value = raw_value.strip()
        if not raw_value:
            return False
        expected_hash = token_hash(raw_value)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT token_hash FROM field_capture_tokens WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["token_hash"]) != expected_hash:
                return False
            connection.execute(
                "UPDATE field_capture_tokens SET token_value = ? WHERE token_id = ?",
                (raw_value, token_id),
            )
        return True

    def authenticate(self, token_value: str, *, now: datetime | None = None) -> TokenRecord | None:
        self.initialize()
        hashed = token_hash(token_value.strip())
        current_time = utc_now() if now is None else now.astimezone(timezone.utc)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM field_capture_tokens WHERE token_hash = ?",
                (hashed,),
            ).fetchone()
            if row is None:
                return None
            record = row_to_record(row)
            if record.revoked:
                return None
            expires_at = parse_timestamp(record.expires_at)
            if expires_at is not None and expires_at <= current_time:
                return None
            connection.execute(
                "UPDATE field_capture_tokens SET last_used_at = ? WHERE token_id = ?",
                (isoformat(current_time), record.token_id),
            )
        updated = self.get_token(record.token_id)
        return updated


def row_to_record(row: sqlite3.Row) -> TokenRecord:
    keys = row.keys()
    return TokenRecord(
        token_id=str(row["token_id"]),
        token_hash=str(row["token_hash"]),
        person_id=str(row["person_id"]),
        created_at=str(row["created_at"]),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
        revoked=bool(row["revoked"]),
        label=str(row["label"] or ""),
        last_used_at=None if row["last_used_at"] is None else str(row["last_used_at"]),
        can_submit=bool(row["can_submit"]),
        can_view_site=bool(row["can_view_site"]),
        role=DEFAULT_ROLE if "role" not in keys or row["role"] is None else str(row["role"] or DEFAULT_ROLE),
        token_type=str(row["token_type"] or "capture"),
        site_ids=parse_site_ids(row["site_ids"]),
        token_value=None if "token_value" not in keys or row["token_value"] is None else str(row["token_value"]),
    )


def ensure_token_columns(connection: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(field_capture_tokens)").fetchall()}
    if "can_submit" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN can_submit INTEGER NOT NULL DEFAULT 1")
    if "can_view_site" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN can_view_site INTEGER NOT NULL DEFAULT 1")
    if "role" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN role TEXT NOT NULL DEFAULT 'cleaner'")
    if "token_type" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN token_type TEXT NOT NULL DEFAULT 'capture'")
    if "site_ids" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN site_ids TEXT NOT NULL DEFAULT '[]'")
    if "token_value" not in columns:
        connection.execute("ALTER TABLE field_capture_tokens ADD COLUMN token_value TEXT")


def parse_site_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return normalize_site_ids(parsed)


def normalize_site_ids(site_ids: Iterable[object]) -> tuple[str, ...]:
    normalized = {str(site_id).strip() for site_id in site_ids if str(site_id).strip()}
    if UNIVERSAL_SITE_SCOPE in normalized:
        return (UNIVERSAL_SITE_SCOPE,)
    return tuple(sorted(normalized))


def token_records_as_rows(records: Iterable[TokenRecord]) -> list[str]:
    rows = ["token_id | person_id | type | role | can_submit | can_view_site | site_ids | revoked | expires_at | last_used_at | label"]
    for record in records:
        rows.append(
            " | ".join(
                [
                    record.token_id,
                    record.person_id,
                    record.token_type,
                    record.role,
                    "yes" if record.can_submit else "no",
                    "yes" if record.can_view_site else "no",
                    ",".join(record.site_ids),
                    "yes" if record.revoked else "no",
                    record.expires_at or "",
                    record.last_used_at or "",
                    record.label,
                ]
            )
        )
    return rows
