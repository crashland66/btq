"""`token audit` flags tokens whose person_id no longer resolves to an employee.

The SQLite token store is separate from canonical CouchDB, so a person_id
rename/merge (e.g. the ULID -> lastname_firstname migration) can leave a token
pointing at a person that no longer exists. The audit catches that drift before
it surfaces as a runtime `invalid_token`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from field_capture import server
from token_store import TokenStore
from vault_errors import NotFoundError


def _seed_tokens(tmp_path: Path) -> Path:
    db_path = tmp_path / "tokens.sqlite3"
    store = TokenStore(db_path)
    store.create_token("stoltz_gregory", label="Operator", token_type="admin_viewer")
    store.create_token("per_01OLD_ULID", label="ops-dashboard-import", token_type="import")
    revoked = store.create_token("ghost_person", label="Revoked stale")
    store.revoke_token(revoked.record.token_id)
    return db_path


class _FakeStore:
    pass


def _patch_canonical(monkeypatch, live_person_ids: set[str]) -> None:
    monkeypatch.setattr(server.CouchDBEntityStore, "from_env", lambda: _FakeStore())

    def fake_resolve(store: object, person_id: str) -> object:
        if person_id in live_person_ids:
            return object()
        raise NotFoundError(person_id)

    monkeypatch.setattr(server, "load_person_from_canonical", fake_resolve)


def _ns(db: Path, include_revoked: bool = False) -> argparse.Namespace:
    return argparse.Namespace(db=db, include_revoked=include_revoked)


def test_audit_flags_stale_active_token(tmp_path, monkeypatch, capsys):
    db = _seed_tokens(tmp_path)
    _patch_canonical(monkeypatch, live_person_ids={"stoltz_gregory"})

    rc = server.audit_tokens(_ns(db))
    out = capsys.readouterr().out

    assert rc == 1  # non-zero so a cron/CI gate can alarm
    assert "MISSING | " in out
    assert "per_01OLD_ULID" in out
    assert "ok | " in out and "stoltz_gregory" in out
    # revoked tokens are excluded by default, even when their person is missing.
    assert "Revoked stale" not in out
    assert "ghost_person" not in out


def test_audit_clean_when_all_persons_resolve(tmp_path, monkeypatch, capsys):
    db = _seed_tokens(tmp_path)
    _patch_canonical(monkeypatch, live_person_ids={"stoltz_gregory", "per_01OLD_ULID"})

    rc = server.audit_tokens(_ns(db))
    out = capsys.readouterr().out

    assert rc == 0
    assert "MISSING" not in out


def test_audit_include_revoked_checks_revoked_tokens(tmp_path, monkeypatch, capsys):
    db = _seed_tokens(tmp_path)
    _patch_canonical(monkeypatch, live_person_ids={"stoltz_gregory", "per_01OLD_ULID"})

    rc = server.audit_tokens(_ns(db, include_revoked=True))
    out = capsys.readouterr().out

    # The revoked token's person (ghost_person) is missing, so it is now flagged.
    assert rc == 1
    assert "ghost_person" in out
    assert "MISSING | " in out
