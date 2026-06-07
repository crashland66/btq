from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from field_capture import sync_token


def sample_row(token_id: str = "fct_test", *, revoked: int = 0) -> dict[str, object]:
    return {
        "token_id": token_id,
        "token_hash": f"hash_{token_id}",
        "person_id": "per_alice",
        "created_at": "2026-05-16T00:00:00Z",
        "expires_at": None,
        "revoked": revoked,
        "label": "Alice phone",
        "last_used_at": None,
        "can_submit": 1,
        "can_view_site": 1,
        "token_type": "capture",
        "site_ids": '["*"]',
    }


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def run_payload(monkeypatch: pytest.MonkeyPatch, db_path: Path, payload: dict[str, object]) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return sync_token.main(["--db", str(db_path)])


def test_sync_token_ensures_schema_on_fresh_db(tmp_path: Path) -> None:
    with connect(tmp_path / "tokens.sqlite3") as connection:
        sync_token.ensure_schema(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(field_capture_tokens)").fetchall()}

    assert set(sync_token.SYNC_COLUMNS).issubset(columns)


def test_sync_token_upsert_inserts_new_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "tokens.sqlite3"

    assert run_payload(monkeypatch, db_path, {"action": "upsert", "row": sample_row()}) == 0

    with connect(db_path) as connection:
        row = connection.execute("SELECT * FROM field_capture_tokens WHERE token_id = ?", ("fct_test",)).fetchone()

    assert row is not None
    assert row["token_hash"] == "hash_fct_test"
    assert row["site_ids"] == '["*"]'


def test_sync_token_upsert_replaces_existing_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "tokens.sqlite3"

    assert run_payload(monkeypatch, db_path, {"action": "upsert", "row": sample_row(revoked=0)}) == 0
    assert run_payload(monkeypatch, db_path, {"action": "upsert", "row": sample_row(revoked=1)}) == 0

    with connect(db_path) as connection:
        row = connection.execute("SELECT revoked FROM field_capture_tokens WHERE token_id = ?", ("fct_test",)).fetchone()

    assert row["revoked"] == 1


def test_sync_token_revoke_sets_revoked_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    with connect(db_path) as connection:
        sync_token.ensure_schema(connection)
        sync_token.upsert_row(connection, sample_row(revoked=0))

    assert run_payload(monkeypatch, db_path, {"action": "revoke", "token_id": "fct_test"}) == 0

    with connect(db_path) as connection:
        row = connection.execute("SELECT revoked FROM field_capture_tokens WHERE token_id = ?", ("fct_test",)).fetchone()

    assert row["revoked"] == 1


def test_sync_token_backfill_walks_source_db_and_upserts_all(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    with connect(source) as connection:
        sync_token.ensure_schema(connection)
        sync_token.upsert_row(connection, sample_row("fct_one"))
        sync_token.upsert_row(connection, sample_row("fct_two"))

    with connect(target) as connection:
        sync_token.ensure_schema(connection)
        sync_token.backfill_from(connection, source)
        rows = connection.execute("SELECT token_id FROM field_capture_tokens ORDER BY token_id").fetchall()

    assert [row["token_id"] for row in rows] == ["fct_one", "fct_two"]


def test_sync_token_unknown_action_returns_error_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "nope"})))

    exit_code = sync_token.main(["--db", str(tmp_path / "tokens.sqlite3")])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["ok"] is False
    assert "unknown action" in payload["error"]


def test_sync_token_payload_omits_token_value_column() -> None:
    assert "token_value" not in sync_token.SYNC_COLUMNS
