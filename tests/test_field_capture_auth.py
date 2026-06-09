from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

import pytest

import field_capture.auth as auth_module
from field_capture.auth import TokenStore, authorize_token, ensure_token_columns, token_hash, utc_now
from field_capture.server import create_token
from vault_errors import NotFoundError


class FakeEmployeeStore:
    def __init__(self, docs: dict[str, dict[str, object]] | None = None, *, error: Exception | None = None) -> None:
        self.docs = docs or {}
        self.error = error

    def get_optional(self, doc_id: str) -> dict[str, object] | None:
        if self.error is not None:
            raise self.error
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None else None

    def find_employee_docs_by_person_id(self, person_id: str) -> list[dict[str, object]]:
        if self.error is not None:
            raise self.error
        return [
            dict(doc)
            for doc in self.docs.values()
            if doc.get("type") == "employee" and str(doc.get("person_id") or "") == person_id
        ]

    def find_employee_docs(self) -> list[dict[str, object]]:
        if self.error is not None:
            raise self.error
        return [dict(doc) for doc in self.docs.values() if doc.get("type") == "employee"]


class FakeSiteRegistry:
    def __init__(self, sites: dict[str, str], *, list_error: Exception | None = None, resolve_error: Exception | None = None) -> None:
        self.sites = dict(sites)
        self.list_error = list_error
        self.resolve_error = resolve_error

    def resolve_canonical(self, site_id: str) -> str | None:
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.sites.get(str(site_id))

    def list_sites(self) -> list[dict[str, str]]:
        if self.list_error is not None:
            raise self.list_error
        return [{"site_id": site_id, "canonical": canonical} for site_id, canonical in self.sites.items()]


def employee_doc(
    *,
    person_id: str = "jordan-avery",
    name: str = "Jordan Avery",
    site_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "person_id": person_id,
        "name": name,
        "status": "active",
        "site_ids": site_ids or ["7050"],
        "vault_path": f"People/{person_id}.md",
    }


def patch_canonical(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docs: dict[str, dict[str, object]] | None = None,
    sites: dict[str, str] | None = None,
    store_error: Exception | None = None,
    registry: FakeSiteRegistry | None = None,
) -> tuple[FakeEmployeeStore, FakeSiteRegistry]:
    store = FakeEmployeeStore(docs, error=store_error)
    site_registry = registry or FakeSiteRegistry(sites or {"7050": "Summit Wire"})
    monkeypatch.setattr(auth_module.CouchDBEntityStore, "from_env", classmethod(lambda cls: store))
    monkeypatch.setattr(auth_module, "CouchDBSiteRegistry", lambda: site_registry)
    return store, site_registry


def test_authorize_token_valid_builds_session_from_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    patch_canonical(monkeypatch, docs={"employee_jordan-avery": employee_doc()}, sites={"7050": "Summit Wire"})
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", label="Jordan iPhone")

    session = authorize_token(store, vault_root, created.token_value)

    assert session is not None
    assert str(session.person.person_id) == "jordan-avery"
    assert session.person.note_path == Path("employee_jordan-avery")
    assert [str(site.site_id) for site in session.sites] == ["7050"]
    assert store.get_token(created.record.token_id).last_used_at is not None


def test_revoked_token_is_rejected(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")
    assert store.revoke_token(created.record.token_id)

    assert authorize_token(store, vault_root, created.token_value) is None


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", expires_at=utc_now() - timedelta(minutes=1))

    assert authorize_token(store, vault_root, created.token_value) is None


def test_multi_site_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc(site_ids=["7050", "7060"])},
        sites={"7050": "Summit Wire", "7060": "Apex Powdered Metals"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")

    session = authorize_token(store, vault_root, created.token_value)

    assert session is not None
    assert [site.canonical_name for site in session.sites] == ["Apex Powdered Metals", "Summit Wire"]


def test_authorize_token_explicit_site_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc(site_ids=["7050", "7060"])},
        sites={"7050": "Summit Wire", "7060": "Apex Powdered Metals"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token(
        "jordan-avery",
        label="Client viewer",
        can_submit=False,
        can_view_site=True,
        token_type="client_viewer",
        site_ids=["7050", "missing"],
    )

    session = authorize_token(store, vault_root, created.token_value)

    assert session is not None
    assert session.record.token_type == "client_viewer"
    assert session.record.can_submit is False
    assert session.record.can_view_site is True
    assert session.record.site_ids == ("7050", "missing")
    assert [str(site.site_id) for site in session.sites] == ["7050"]


def test_authorize_token_universal_scope_returns_all_sites_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc()},
        sites={"7050": "Summit Wire", "7060": "Apex Powdered Metals", "7071": "Glenwood"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", label="Jordan admin", token_type="admin_viewer", site_ids=["*"])

    session = authorize_token(store, vault_root, created.token_value)

    assert session is not None
    assert session.record.site_ids == ("*",)
    assert [str(site.site_id) for site in session.sites] == ["7060", "7071", "7050"]


def test_authorize_token_invalid_token_returns_none(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    assert authorize_token(store, tmp_path / "vault", "fc_missing") is None


def test_authorize_token_missing_employee_doc_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(monkeypatch, docs={}, sites={"7050": "Summit Wire"})
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")

    assert authorize_token(store, tmp_path / "vault", created.token_value) is None


def test_load_person_from_canonical_resolves_by_person_id_field_when_doc_id_differs() -> None:
    store = FakeEmployeeStore(
        {
            "employee_per_EXAMPLE0000000000000000000": {
                "_id": "employee_per_EXAMPLE0000000000000000000",
                "type": "employee",
                "person_id": "keller-bruce",
                "name": "Bruce Keller",
                "status": "active",
                "site_ids": ["7050"],
                "vault_path": "People/Keller, Bruce.md",
            }
        }
    )

    person = auth_module.load_person_from_canonical(store, "keller-bruce")

    assert str(person.person_id) == "keller-bruce"
    assert person.canonical_name == "Bruce Keller"
    assert [str(site_id) for site_id in person.site_ids] == ["7050"]


def test_auth_fallback_matches_first_last_token_against_legacy_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(
        monkeypatch,
        docs={
            "employee_legacy_anthony": {
                "_id": "employee_legacy_anthony",
                "type": "employee",
                "first": "Frank",
                "last": "Russo",
                "name": "Frank Russo",
                "status": "active",
                "site_ids": ["7050"],
                "vault_path": "People/Russo, Frank.md",
            }
        },
        sites={"7050": "Summit Wire"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("frank-russo")

    session = authorize_token(store, tmp_path / "vault", created.token_value)

    assert session is not None
    assert str(session.person.person_id) == "frank-russo"
    assert session.person.canonical_name == "Frank Russo"


def test_auth_fallback_still_matches_last_first_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(
        monkeypatch,
        docs={
            "employee_legacy_anthony": {
                "_id": "employee_legacy_anthony",
                "type": "employee",
                "first": "Frank",
                "last": "Russo",
                "name": "Frank Russo",
                "status": "active",
                "site_ids": ["7050"],
                "vault_path": "People/Russo, Frank.md",
            }
        },
        sites={"7050": "Summit Wire"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("russo-frank")

    session = authorize_token(store, tmp_path / "vault", created.token_value)

    assert session is not None
    assert str(session.person.person_id) == "russo-frank"
    assert session.person.canonical_name == "Frank Russo"


def test_auth_direct_person_id_and_id_still_match() -> None:
    person_id_store = FakeEmployeeStore(
        {
            "employee_legacy_jordan": {
                "_id": "employee_legacy_jordan",
                "type": "employee",
                "person_id": "jordan-avery",
                "name": "Jordan Avery",
                "site_ids": ["7050"],
            }
        }
    )
    id_store = FakeEmployeeStore(
        {
            "employee_legacy_jordan": {
                "_id": "employee_jordan-avery",
                "type": "employee",
                "name": "Jordan Avery",
                "site_ids": ["7050"],
            }
        }
    )

    person_id_match = auth_module.load_person_from_canonical(person_id_store, "jordan-avery")
    id_match = auth_module.load_person_from_canonical(id_store, "jordan-avery")

    assert str(person_id_match.person_id) == "jordan-avery"
    assert str(id_match.person_id) == "jordan-avery"


def test_auth_does_not_match_unrelated_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(
        monkeypatch,
        docs={
            "employee_legacy_brandon": {
                "_id": "employee_legacy_brandon",
                "type": "employee",
                "first": "Kevin",
                "last": "Barnes",
                "name": "Kevin Barnes",
                "status": "active",
                "site_ids": ["7050"],
                "vault_path": "People/Barnes, Kevin.md",
            }
        },
        sites={"7050": "Summit Wire"},
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("frank-russo")

    assert authorize_token(store, tmp_path / "vault", created.token_value) is None


def test_load_person_from_canonical_raises_for_truly_absent_person() -> None:
    with pytest.raises(NotFoundError):
        auth_module.load_person_from_canonical(FakeEmployeeStore({}), "absent-worker")


def test_authorize_token_store_unreachable_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(monkeypatch, store_error=RuntimeError("store down"), sites={"7050": "Summit Wire"})
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", site_ids=["*"])

    assert authorize_token(store, tmp_path / "vault", created.token_value) is None


def test_read_helper_logs_then_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    patch_canonical(monkeypatch, store_error=RuntimeError("store down"), sites={"7050": "Summit Wire"})
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", site_ids=["7050"])

    assert authorize_token(store, tmp_path / "vault", created.token_value) is None
    assert "field-capture auth lookup failed person_id=jordan-avery" in caplog.text
    assert "store down" in caplog.text


def test_auth_site_resolution_logs_then_falls_back(caplog) -> None:
    person = auth_module.load_person_from_canonical(FakeEmployeeStore({"employee_jordan-avery": employee_doc()}), "jordan-avery")
    registry = FakeSiteRegistry({"7050": "Summit Wire"}, resolve_error=RuntimeError("registry down"))

    assert auth_module.allowed_sites_for_person(registry, person, ["7050"]) == ()
    assert "site lookup failed during field-capture auth person_id=jordan-avery site_id=7050" in caplog.text


def test_authorize_token_universal_scope_registry_failure_denies_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_canonical(
        monkeypatch,
        docs={"employee_jordan-avery": employee_doc()},
        registry=FakeSiteRegistry({"7050": "Summit Wire"}, list_error=RuntimeError("registry down")),
    )
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery", site_ids=["*"])

    assert authorize_token(store, tmp_path / "vault", created.token_value) is None


def test_site_membership_is_resolved_dynamically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    docs = {"employee_jordan-avery": employee_doc(site_ids=["7050"])}
    patch_canonical(monkeypatch, docs=docs, sites={"7050": "Summit Wire", "7060": "Apex Powdered Metals"})
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")

    first_session = authorize_token(store, vault_root, created.token_value)
    docs["employee_jordan-avery"] = employee_doc(site_ids=["7050", "7060"])
    second_session = authorize_token(store, vault_root, created.token_value)

    assert first_session is not None
    assert second_session is not None
    assert [str(site.site_id) for site in first_session.sites] == ["7050"]
    assert [str(site.site_id) for site in second_session.sites] == ["7060", "7050"]


def test_token_creation_flow_persists_hash_and_prints_raw_token_once(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    db_path = tmp_path / "tokens.sqlite3"
    patch_canonical(monkeypatch, docs={"employee_jordan-avery": employee_doc()}, sites={"7050": "Summit Wire"})

    result = create_token(
        Namespace(
            db=db_path,
            vault_root=vault_root,
            person="jordan-avery",
            label="Jordan iPhone",
            expires_at=None,
            viewer_only=False,
            token_type=None,
            site_id=None,
            all_sites=False,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "token_id: fct_" in output
    assert "person_id: jordan-avery" in output
    assert "label: Jordan iPhone" in output
    raw_token = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("token: "))
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT token_hash, person_id, label FROM field_capture_tokens").fetchone()
    assert row[0] != raw_token
    assert row[1] == "jordan-avery"
    assert row[2] == "Jordan iPhone"


def test_token_creation_accepts_canonical_person_id_from_doc(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_root = tmp_path / "vault"
    db_path = tmp_path / "tokens.sqlite3"
    patch_canonical(
        monkeypatch,
        docs={
            "employee_per_EXAMPLE0000000000000000000": employee_doc(
                person_id="per_EXAMPLE0000000000000000000",
                name="Jordan Avery",
                site_ids=["7030"],
            )
        },
        sites={"7030": "Western Gas Transmission"},
    )

    result = create_token(
        Namespace(
            db=db_path,
            vault_root=vault_root,
            person="per_EXAMPLE0000000000000000000",
            label="Jordan iPhone",
            expires_at=None,
            viewer_only=False,
            token_type=None,
            site_id=None,
            all_sites=False,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "person_id: per_EXAMPLE0000000000000000000" in output


def test_token_store_creates_missing_parent_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "missing" / "runtime" / "field_capture_tokens.sqlite3"

    records = TokenStore(db_path).list_tokens()

    assert records == []
    assert db_path.exists()


def test_create_token_persists_raw_value_in_db(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    created = store.create_token("jordan-avery")
    record = store.get_token(created.record.token_id)

    assert record is not None
    assert record.token_value == created.token_value
    assert record.token_value.startswith("fc_")


def test_token_record_defaults_to_cleaner_role(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    created = store.create_token("jordan-avery")
    record = store.get_token(created.record.token_id)

    assert record is not None
    assert record.role == "cleaner"


def test_token_record_accepts_site_admin_role(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    created = store.create_token("jordan-avery", role="site_admin")
    record = store.get_token(created.record.token_id)

    assert record is not None
    assert record.role == "site_admin"


def test_token_create_rejects_unknown_role(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    with pytest.raises(ValueError):
        store.create_token("jordan-avery", role="superuser")


def test_legacy_token_record_has_none_token_value(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    store.initialize()
    created_at = utc_now().isoformat().replace("+00:00", "Z")
    with store.connect() as connection:
        connection.execute(
            """
            INSERT INTO field_capture_tokens
              (token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at,
               can_submit, can_view_site, token_type, site_ids)
            VALUES (?, ?, ?, ?, NULL, 0, ?, NULL, 1, 1, 'capture', '[]')
            """,
            ("fct_legacy", token_hash("fc_legacy"), "jordan-avery", created_at, "Legacy"),
        )

    record = store.get_token("fct_legacy")

    assert record is not None
    assert record.token_value is None


def test_set_token_value_succeeds_when_raw_matches_stored_hash(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (created.record.token_id,))

    ok = store.set_token_value(created.record.token_id, created.token_value)

    record = store.get_token(created.record.token_id)
    assert ok is True
    assert record is not None
    assert record.token_value == created.token_value


def test_set_token_value_rejects_mismatched_raw_value(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")

    ok = store.set_token_value(created.record.token_id, "fc_wrong_value")

    record = store.get_token(created.record.token_id)
    assert ok is False
    assert record is not None
    assert record.token_value == created.token_value


def test_set_token_value_rejects_unknown_token_id(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")

    ok = store.set_token_value("fct_missing", "fc_missing")

    assert ok is False


def test_set_token_value_strips_whitespace_before_hashing(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens.sqlite3")
    created = store.create_token("jordan-avery")
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (created.record.token_id,))

    ok = store.set_token_value(created.record.token_id, f"  {created.token_value}  ")

    record = store.get_token(created.record.token_id)
    assert ok is True
    assert record is not None
    assert record.token_value == created.token_value


def test_ensure_token_columns_adds_token_value(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE field_capture_tokens (
              token_id TEXT PRIMARY KEY,
              token_hash TEXT NOT NULL UNIQUE,
              person_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT,
              revoked INTEGER NOT NULL DEFAULT 0,
              label TEXT NOT NULL DEFAULT '',
              last_used_at TEXT,
              can_submit INTEGER NOT NULL DEFAULT 1,
              can_view_site INTEGER NOT NULL DEFAULT 1,
              token_type TEXT NOT NULL DEFAULT 'capture',
              site_ids TEXT NOT NULL DEFAULT '[]'
            )
            """
        )

    TokenStore(db_path).initialize()

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(field_capture_tokens)").fetchall()}
    assert "token_value" in columns


def test_ensure_token_columns_adds_role_to_existing_db(tmp_path: Path) -> None:
    db_path = tmp_path / "tokens.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE field_capture_tokens (
              token_id TEXT PRIMARY KEY,
              token_hash TEXT NOT NULL UNIQUE,
              person_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT,
              revoked INTEGER NOT NULL DEFAULT 0,
              label TEXT NOT NULL DEFAULT '',
              last_used_at TEXT,
              can_submit INTEGER NOT NULL DEFAULT 1,
              can_view_site INTEGER NOT NULL DEFAULT 1,
              token_type TEXT NOT NULL DEFAULT 'capture',
              site_ids TEXT NOT NULL DEFAULT '[]',
              token_value TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO field_capture_tokens
              (token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at,
               can_submit, can_view_site, token_type, site_ids, token_value)
            VALUES (?, ?, ?, ?, NULL, 0, ?, NULL, 1, 1, 'capture', '[]', NULL)
            """,
            ("fct_legacy", token_hash("fc_legacy"), "jordan-avery", utc_now().isoformat().replace("+00:00", "Z"), "Legacy"),
        )
        connection.row_factory = sqlite3.Row
        ensure_token_columns(connection)
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(field_capture_tokens)").fetchall()}
        role = connection.execute("SELECT role FROM field_capture_tokens WHERE token_id = ?", ("fct_legacy",)).fetchone()["role"]

    assert "role" in columns
    assert role == "cleaner"
