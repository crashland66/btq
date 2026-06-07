"""Regression tests for `btq setup-couchdb` orchestration (run_setup)."""
from __future__ import annotations

import sys

from event_pipeline.couchdb import (
    migrate_sites,
    push_design_doc,
    setup_command,
    setup_databases,
)


def _stub_connection(monkeypatch):
    monkeypatch.setattr(setup_databases, "couchdb_url", lambda: "http://couch.example")
    monkeypatch.setattr(setup_databases, "auth_headers", lambda: {})
    monkeypatch.setattr(setup_databases, "verify_connection", lambda url, headers: "3.5.1")
    monkeypatch.setattr(
        setup_databases,
        "create_required_databases",
        lambda url, headers: {"btq_vault": "exists"},
    )
    monkeypatch.setattr(
        setup_databases,
        "provision_vault_indexes",
        lambda url, headers: {"idx-type-status": "exists"},
    )
    monkeypatch.setattr(
        setup_databases,
        "provision_photo_vision_database",
        lambda url, headers: {"database": "exists", "indexes": {"idx-generated": "exists"}},
    )


def test_run_setup_design_step_passes_empty_argv(monkeypatch):
    # Pollute sys.argv exactly as a real `btq setup-couchdb` invocation does.
    monkeypatch.setattr(sys, "argv", ["btq", "setup-couchdb"])
    _stub_connection(monkeypatch)

    captured: dict[str, object] = {}

    def fake_push(argv=None):
        captured["argv"] = argv
        print("vault: design document already up to date.")
        return 0

    monkeypatch.setattr(push_design_doc, "main", fake_push)

    rc = setup_command.run_setup(skip_migrate=True)

    assert rc == 0
    # The design step must pass an explicit empty argv, NOT fall back to sys.argv
    # (which would make push_design_doc's argparse abort on "setup-couchdb").
    assert captured["argv"] == []


def test_run_setup_does_not_crash_when_sys_argv_polluted(monkeypatch):
    # Exercise the REAL push_design_doc.main with a polluted sys.argv, stubbing only
    # its network side effects. Before the fix this raised SystemExit(2)
    # ("unrecognized arguments: setup-couchdb").
    monkeypatch.setattr(sys, "argv", ["btq", "setup-couchdb"])
    _stub_connection(monkeypatch)
    monkeypatch.setattr(push_design_doc, "push_design_doc", lambda name: "already up to date")
    monkeypatch.setattr(push_design_doc, "seed_system_defaults_if_absent", lambda: "already exists")
    migrated: dict[str, bool] = {}

    def fake_migrate():
        migrated["ran"] = True
        print("created: 0 []")
        return 0

    monkeypatch.setattr(migrate_sites, "main", fake_migrate)

    rc = setup_command.run_setup()

    assert rc == 0
    assert migrated["ran"] is True


def test_run_setup_provisions_vault_indexes(monkeypatch):
    # The operator path `btq setup-couchdb` must actually create the btq_vault
    # Mango indexes — they live in setup_databases.provision_vault_indexes(), which
    # this command (not setup_databases.main()) is responsible for invoking.
    monkeypatch.setattr(sys, "argv", ["btq", "setup-couchdb"])
    monkeypatch.setattr(setup_databases, "couchdb_url", lambda: "http://couch.example")
    monkeypatch.setattr(setup_databases, "auth_headers", lambda: {})
    monkeypatch.setattr(setup_databases, "verify_connection", lambda url, headers: "3.5.1")
    monkeypatch.setattr(
        setup_databases,
        "create_required_databases",
        lambda url, headers: {"btq_vault": "exists"},
    )
    provisioned: dict[str, bool] = {}

    def fake_provision(url, headers):
        provisioned["ran"] = True
        return {"idx-type-status": "created"}

    monkeypatch.setattr(setup_databases, "provision_vault_indexes", fake_provision)
    monkeypatch.setattr(
        setup_databases,
        "provision_photo_vision_database",
        lambda url, headers: {"database": "exists", "indexes": {"idx-generated": "exists"}},
    )
    monkeypatch.setattr(push_design_doc, "main", lambda argv=None: 0)

    rc = setup_command.run_setup(skip_migrate=True)

    assert rc == 0
    assert provisioned["ran"] is True


def test_run_setup_provisions_photo_vision_indexes(monkeypatch):
    # The operator path `btq setup-couchdb` must actually create the
    # btq_photo_vision database and Mango indexes. They live in
    # setup_databases.provision_photo_vision_database(), which this command
    # (not setup_databases.main()) is responsible for invoking.
    monkeypatch.setattr(sys, "argv", ["btq", "setup-couchdb"])
    monkeypatch.setattr(setup_databases, "couchdb_url", lambda: "http://couch.example")
    monkeypatch.setattr(setup_databases, "auth_headers", lambda: {})
    monkeypatch.setattr(setup_databases, "verify_connection", lambda url, headers: "3.5.1")
    monkeypatch.setattr(
        setup_databases,
        "create_required_databases",
        lambda url, headers: {"btq_vault": "exists"},
    )
    monkeypatch.setattr(
        setup_databases,
        "provision_vault_indexes",
        lambda url, headers: {"idx-type-status": "exists"},
    )
    provisioned: dict[str, bool] = {}

    def fake_provision(url, headers):
        provisioned["ran"] = True
        return {"database": "created", "indexes": {"idx-generated": "created"}}

    monkeypatch.setattr(setup_databases, "provision_photo_vision_database", fake_provision)
    monkeypatch.setattr(push_design_doc, "main", lambda argv=None: 0)

    rc = setup_command.run_setup(skip_migrate=True)

    assert rc == 0
    assert provisioned["ran"] is True
