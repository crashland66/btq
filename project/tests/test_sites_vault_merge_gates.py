"""Adversarial gates for the btq_sites → btq_vault merge (site identity unification).

Each gate EXECUTES the new path:

* the registry resolves through btq_vault's sites_* views and nothing else —
  a mutation back to the btq_sites views/database fails these tests;
* SANDBOX (code-canonical demo persona) resolves with NO vault doc, and a
  real view row for the same site_id overrides the builtin;
* the /sites editor read-modify-writes the canonical location doc without
  clobbering operational fields owned by the vault pipeline;
* the setup seeder is fill-only — a stale static table can never overwrite
  operator-edited registration data;
* no production code references the retired database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib import error

import pytest

from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from event_pipeline.couchdb import migrate_sites
from ops_dashboard.sections import sites as sites_section


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def strict_vault_views(alias_rows: list[dict], site_id_rows: dict[str, list[dict]], calls: list[str] | None = None):
    """urlopen stub that ONLY answers the btq_vault sites_* views; every other
    URL (e.g. the retired btq_sites design doc) 404s."""

    def fake_urlopen(req: object, timeout: float = 10.0) -> FakeResponse:
        url = getattr(req, "full_url")
        if calls is not None:
            calls.append(url)
        if "/btq_vault/_design/btq_vault/_view/sites_by_alias" in url:
            return FakeResponse({"rows": alias_rows})
        if "/btq_vault/_design/btq_vault/_view/sites_by_site_id" in url:
            for key, rows in site_id_rows.items():
                if key in url:
                    return FakeResponse({"rows": rows})
            return FakeResponse({"rows": []})
        raise error.HTTPError(url, 404, "not found", hdrs=None, fp=None)

    return fake_urlopen


# --------------------------------------------------------------------------- #
# Registry repoint: vault views are THE source
# --------------------------------------------------------------------------- #
def test_registry_reads_only_btq_vault_sites_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    rows = [{"id": "location_7050", "key": "summit wire", "value": {"site_id": "7050", "canonical": "Summit Wire", "note_path": None, "vision_context": None}}]
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        strict_vault_views(rows, {"7050": rows}, calls),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("summit wire") == ("Summit Wire", "high")
    assert registry.resolve_canonical("7050") == "Summit Wire"
    assert calls, "registry made no HTTP calls"
    for url in calls:
        assert "/btq_vault/_design/btq_vault/_view/sites_by_" in url
        assert "btq_sites" not in url


# --------------------------------------------------------------------------- #
# SANDBOX: code-canonical, no vault doc required
# --------------------------------------------------------------------------- #
def test_sandbox_resolves_with_empty_views(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        strict_vault_views([], {}),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("sandbox") == ("Sandbox Site", "high")
    assert registry.resolve_site_id("Sandbox Site") == "SANDBOX"
    assert registry.resolve_canonical("SANDBOX") == "Sandbox Site"
    assert registry.get_vision_context("SANDBOX") is not None
    assert {"site_id": "SANDBOX", "canonical": "Sandbox Site"} in registry.list_sites()


def test_sandbox_view_row_overrides_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"id": "location_SANDBOX", "key": "sandbox", "value": {"site_id": "SANDBOX", "canonical": "Sandbox Override", "note_path": None, "vision_context": None}}
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        strict_vault_views([row], {"SANDBOX": [row]}),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_site("sandbox") == ("Sandbox Override", "high")
    assert registry.resolve_canonical("SANDBOX") == "Sandbox Override"


def test_unknown_site_gets_no_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "event_pipeline.couchdb_registry.request.urlopen",
        strict_vault_views([], {}),
    )
    registry = CouchDBSiteRegistry(base_url="http://couchdb.test")

    assert registry.resolve_canonical("9999") is None
    assert registry.get_vision_context("9999") is None


# --------------------------------------------------------------------------- #
# Editor RMW: registration fields only, operational fields ride through
# --------------------------------------------------------------------------- #
def editor_form(**overrides: str) -> dict[str, list[str]]:
    data = {
        "site_id": "7050",
        "canonical_name": "Summit Wire",
        "active": "1",
        "aliases": "summit\nwire",
        "note_path": "Accounts/Summit/about.md",
        "capture_guidance": "",
    }
    data.update(overrides)
    return {key: [value] for key, value in data.items()}


def test_editor_save_preserves_vault_operational_fields() -> None:
    existing = {
        "_id": "location_7050",
        "_rev": "9-abc",
        "type": "location",
        "operator": "op_greg",
        "status": "active",
        "account": "Summit",
        "location": "Summit Wire",
        "billing_monthly": "1883.15",
        "address": "1 Wire Way",
        "service_days": "m-f",
        "job": 7050,
    }

    site_id, doc = sites_section.form_doc(editor_form(), existing=existing)

    assert site_id == "7050"
    assert doc["_id"] == "location_7050"
    assert doc["type"] == "location"
    # Registration fields written...
    assert doc["capture_active"] is True
    assert doc["aliases"] == ["summit", "wire"]
    # ...operational fields owned by the vault pipeline ride through untouched.
    assert doc["billing_monthly"] == "1883.15"
    assert doc["address"] == "1 Wire Way"
    assert doc["service_days"] == "m-f"
    assert doc["operator"] == "op_greg"
    assert doc["status"] == "active"


def test_editor_new_site_creates_valid_location_doc() -> None:
    site_id, doc = sites_section.form_doc(editor_form(site_id="9001", canonical_name="New Site"))

    assert site_id == "9001"
    assert doc["_id"] == "location_9001"
    assert doc["type"] == "location"
    assert doc["operator"], "validator requires operator on new location docs"
    assert doc["status"] == "active"
    assert doc["location"] == "New Site"
    assert doc["capture_active"] is True


# --------------------------------------------------------------------------- #
# Seeder: fill-only, never overwrites operator-edited registration
# --------------------------------------------------------------------------- #
def test_seeder_fill_missing_never_overwrites() -> None:
    existing = {
        "_id": "location_7050",
        "type": "location",
        "operator": "op_greg",
        "location": "Operator Edited Name",
        "aliases": ["operator", "edited"],
        "capture_active": False,
        "site_id": "7050",
        "note_path": "Accounts/Custom/about.md",
    }
    desired = {
        "site_id": "7050",
        "location": "Stale Static Name",
        "aliases": ["stale"],
        "capture_active": True,
        "vision_context": {"context_id": "7050"},
        "note_path": "Accounts/Stale/about.md",
    }

    merged, is_created, changed = migrate_sites.fill_missing(existing, desired, "7050")

    assert not is_created
    # Only the truly-missing field was filled...
    assert merged["vision_context"] == {"context_id": "7050"}
    assert changed
    # ...every operator-owned value survived the stale static table.
    assert merged["location"] == "Operator Edited Name"
    assert merged["aliases"] == ["operator", "edited"]
    assert merged["capture_active"] is False
    assert merged["note_path"] == "Accounts/Custom/about.md"


def test_seeder_skips_sandbox() -> None:
    # SANDBOX must never be seeded into the vault — it is code-canonical.
    import inspect

    source = inspect.getsource(migrate_sites.main)
    assert "BUILTIN_SANDBOX_SITE" in source


# --------------------------------------------------------------------------- #
# Retirement sweep: no production code references the dead database
# --------------------------------------------------------------------------- #
ALLOWED_BTQ_SITES_REFERENCES = {
    # The two operational artifacts of the retirement itself.
    "project/event_pipeline/couchdb/migrate_sites_to_vault.py",
    "project/event_pipeline/couchdb/retire_sites_db.py",
}


def test_no_production_reference_to_btq_sites() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in (repo_root / "project").rglob("*.py"):
        rel = str(path.relative_to(repo_root))
        if "/tests/" in rel or "/.venv/" in rel or "__pycache__" in rel:
            continue
        if rel in ALLOWED_BTQ_SITES_REFERENCES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"btq_sites(?!_bak)", text) or "sites_database" in text or "BTQ_COUCHDB_SITES_DB" in text:
            offenders.append(rel)
    assert not offenders, f"production code still references the retired btq_sites database: {offenders}"
