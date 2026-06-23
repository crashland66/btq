"""Independent-verifier tests for the `scripts/btq-context` read-only CLI.

All fixtures here are SYNTHETIC / ANONYMIZED. No real B&T / Interfuse / PHN
account names, no real people, no real job numbers, phones, or emails appear in
this file. Every doc is injected via the CLI's `docs=` / `now=` test seam (which
`main()` threads straight into `context_resolver`), so nothing touches a live
CouchDB and no write/enqueue path is exercised.

Invented identities used throughout:
  Accounts:  "Acme Widgets - North" (4001), "Beta Tools - South" (4002)
  People:    "Pat Vendor", "Dana Direct", "Alex Assigned"
  Operator:  "Gregory Stoltz" (resolved from "Greg")
  Job numbers: 4001 / 4002 / 4003 (fake)

The CLI script has no `.py` extension; it is loaded by file path via importlib
and driven through `main(argv, docs=..., now=...)`, capturing stdout + the int
return code.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Load the extension-less CLI script by file path
# --------------------------------------------------------------------------- #
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "btq-context"


def _load_cli():
    # The script has no .py extension, so give importlib an explicit source
    # loader (spec_from_file_location alone yields loader=None for it).
    loader = importlib.machinery.SourceFileLoader("btq_context_cli", str(_SCRIPT_PATH))
    spec = importlib.util.spec_from_loader("btq_context_cli", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return _load_cli()


def _run(cli, argv, *, docs=None, now=None):
    """Drive main(argv) with injected synthetic docs; return (rc, stdout_text)."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv, docs=docs, now=now)
    return rc, buf.getvalue()


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #
def _acme_site() -> dict:
    return {
        "_id": "site_4001",
        "_rev": "3-acme",
        "type": "site",
        "site_id": "4001",
        "account": "Acme Widgets - North",
        "aliases": ["AWN"],
        "status": "active",
        "manager": "Gregory Stoltz",
    }


def _beta_site() -> dict:
    return {
        "_id": "site_4002",
        "_rev": "1-beta",
        "type": "site",
        "site_id": "4002",
        "account": "Beta Tools - South",
        "aliases": ["Beta S"],
        "active": True,
        "manager": "Gregory Stoltz",
    }


def _operator() -> dict:
    return {
        "_id": "emp_op",
        "_rev": "1",
        "type": "employee",
        "name": "Gregory Stoltz",
        "first": "Gregory",
        "last": "Stoltz",
        "role": "Area Manager",
        "site_ids": ["4001", "4002"],
    }


def _direct_report() -> dict:
    return {
        "_id": "emp_direct",
        "_rev": "1",
        "type": "employee",
        "name": "Dana Direct",
        "first": "Dana",
        "last": "Direct",
        "role": "Cleaner",
        "site_ids": ["9999"],  # not one of the operator's sites -> relationship = direct
        "manager": "Gregory Stoltz",
    }


def _assigned_report() -> dict:
    return {
        "_id": "emp_assigned",
        "_rev": "1",
        "type": "employee",
        "name": "Alex Assigned",
        "first": "Alex",
        "last": "Assigned",
        "role": "Cleaner",
        "site_ids": ["4001"],  # on an operator site, different manager -> relationship = assigned
        "manager": "Some Other Lead",
    }


def _all_people() -> list[dict]:
    return [_operator(), _direct_report(), _assigned_report()]


def _snapshot_docs() -> dict:
    return {"accounts": [_acme_site(), _beta_site()], "people": _all_people()}


# --------------------------------------------------------------------------- #
# JSON helper assertions
# --------------------------------------------------------------------------- #
def _assert_sorted_json(text: str):
    """Parse stdout as JSON, assert keys are emitted sorted + round-trips."""
    payload = json.loads(text)  # valid JSON or raises

    def keys_sorted(obj):
        if isinstance(obj, dict):
            keys = list(obj.keys())
            assert keys == sorted(keys), f"keys not sorted: {keys}"
            for v in obj.values():
                keys_sorted(v)
        elif isinstance(obj, list):
            for item in obj:
                keys_sorted(item)

    keys_sorted(payload)
    # round-trip: re-serialize with sort_keys and ensure stable
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        json.loads(text), sort_keys=True
    )
    return payload


# --------------------------------------------------------------------------- #
# --account
# --------------------------------------------------------------------------- #
def test_account_resolve_top_level_flag(cli):
    rc, out = _run(cli, ["--account", "4001"], docs={"accounts": [_acme_site()]})
    assert rc == 0
    payload = _assert_sorted_json(out)
    assert payload["kind"] == "account"
    assert payload["job_number"] == "4001"
    assert payload["canonical_name"] == "Acme Widgets - North"


def test_account_resolve_subcommand(cli):
    rc, out = _run(cli, ["resolve", "--account", "AWN"], docs={"accounts": [_acme_site()]})
    assert rc == 0
    payload = json.loads(out)
    assert payload["kind"] == "account"
    assert payload["canonical_name"] == "Acme Widgets - North"


# --------------------------------------------------------------------------- #
# --person
# --------------------------------------------------------------------------- #
def test_person_resolve_top_level_flag(cli):
    rc, out = _run(cli, ["--person", "Dana Direct"], docs={"people": _all_people()})
    assert rc == 0
    payload = _assert_sorted_json(out)
    assert payload["kind"] == "person"
    assert payload["canonical_name"] == "Dana Direct"
    assert payload["role"] == "Cleaner"


# --------------------------------------------------------------------------- #
# --manager --type accounts / employees (list mode)
# --------------------------------------------------------------------------- #
def test_list_accounts(cli):
    rc, out = _run(cli, ["--manager", "Greg", "--type", "accounts"], docs=_snapshot_docs())
    assert rc == 0
    payload = _assert_sorted_json(out)
    assert isinstance(payload, list)
    job_numbers = {a["job_number"] for a in payload}
    assert job_numbers == {"4001", "4002"}
    assert all(a["kind"] == "account" for a in payload)


def test_list_employees_with_relationship_labels(cli):
    rc, out = _run(cli, ["--manager", "Greg", "--type", "employees"], docs=_snapshot_docs())
    assert rc == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    by_name = {p["display_name"]: p for p in payload}
    assert by_name["Dana Direct"]["relationship"] == "direct"
    assert by_name["Alex Assigned"]["relationship"] == "assigned"
    # operator self is excluded
    assert "Gregory Stoltz" not in by_name


# --------------------------------------------------------------------------- #
# --manager (snapshot) + injected now flows through deterministically
# --------------------------------------------------------------------------- #
def test_snapshot_shape_and_injected_now(cli):
    fixed_now = "2026-06-23T12:00:00+00:00"
    rc, out = _run(cli, ["--manager", "Greg"], docs=_snapshot_docs(), now=fixed_now)
    assert rc == 0
    payload = _assert_sorted_json(out)
    assert set(payload) >= {"operator", "accounts", "people", "generated_at"}
    assert payload["operator"] == "Gregory Stoltz"
    assert payload["generated_at"] == fixed_now  # injected now flowed through
    assert {a["job_number"] for a in payload["accounts"]} == {"4001", "4002"}
    rels = {p["display_name"]: p["relationship"] for p in payload["people"]}
    assert rels == {"Dana Direct": "direct", "Alex Assigned": "assigned"}


def test_snapshot_subcommand_matches_top_level(cli):
    fixed_now = "2026-06-23T12:00:00+00:00"
    rc_sub, out_sub = _run(cli, ["snapshot", "--manager", "Greg"], docs=_snapshot_docs(), now=fixed_now)
    rc_top, out_top = _run(cli, ["--manager", "Greg"], docs=_snapshot_docs(), now=fixed_now)
    assert rc_sub == rc_top == 0
    assert json.loads(out_sub) == json.loads(out_top)


# --------------------------------------------------------------------------- #
# Ambiguous -> exit 2 + candidates JSON still printed
# --------------------------------------------------------------------------- #
def test_account_ambiguous_exit_2_with_candidates(cli):
    a1 = {"_id": "site_4001", "_rev": "1", "type": "site", "site_id": "4001",
          "account": "Acme Widgets - North", "aliases": ["North Plant"]}
    a2 = {"_id": "site_4002", "_rev": "1", "type": "site", "site_id": "4002",
          "account": "Beta Tools - North", "aliases": ["North Depot"]}
    rc, out = _run(cli, ["--account", "north"], docs={"accounts": [a1, a2]})
    assert rc == 2
    payload = _assert_sorted_json(out)
    assert payload["kind"] == "ambiguous"
    assert {c["job_number"] for c in payload["candidates"]} == {"4001", "4002"}


def test_person_ambiguous_exit_2_with_candidates(cli):
    p1 = {"_id": "emp_1", "_rev": "1", "type": "employee", "name": "Pat Vendor",
          "first": "Pat", "last": "Vendor", "site_ids": []}
    p2 = {"_id": "emp_2", "_rev": "1", "type": "employee", "name": "Pat Supplier",
          "first": "Pat", "last": "Supplier", "site_ids": []}
    rc, out = _run(cli, ["--person", "Pat"], docs={"people": [p1, p2]})
    assert rc == 2
    payload = json.loads(out)
    assert payload["kind"] == "ambiguous"
    assert len(payload["candidates"]) == 2


# --------------------------------------------------------------------------- #
# not_found -> exit 2 + not_found JSON
# --------------------------------------------------------------------------- #
def test_account_not_found_exit_2(cli):
    rc, out = _run(cli, ["--account", "zzz totally unknown"], docs={"accounts": [_acme_site()]})
    assert rc == 2
    payload = json.loads(out)
    assert payload["kind"] == "not_found"


def test_person_not_found_exit_2(cli):
    rc, out = _run(cli, ["--person", "zzz nobody here"], docs={"people": _all_people()})
    assert rc == 2
    payload = json.loads(out)
    assert payload["kind"] == "not_found"


# --------------------------------------------------------------------------- #
# Format: table -> rc 0 and NON-JSON text
# --------------------------------------------------------------------------- #
def test_account_table_format_non_json(cli):
    rc, out = _run(cli, ["--account", "4001", "--format", "table"], docs={"accounts": [_acme_site()]})
    assert rc == 0
    assert "Acme Widgets - North" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)  # table is human text, not JSON


def test_snapshot_table_format_non_json(cli):
    rc, out = _run(cli, ["--manager", "Greg", "--format", "table"], docs=_snapshot_docs(),
                   now="2026-06-23T12:00:00+00:00")
    assert rc == 0
    assert "Operator: Gregory Stoltz" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_ambiguous_table_still_json_and_exit_2(cli):
    # Unresolved short-circuits to JSON candidates even under --format table.
    a1 = {"_id": "site_4001", "_rev": "1", "type": "site", "site_id": "4001",
          "account": "Acme Widgets - North", "aliases": ["North Plant"]}
    a2 = {"_id": "site_4002", "_rev": "1", "type": "site", "site_id": "4002",
          "account": "Beta Tools - North", "aliases": ["North Depot"]}
    rc, out = _run(cli, ["--account", "north", "--format", "table"], docs={"accounts": [a1, a2]})
    assert rc == 2
    payload = json.loads(out)  # candidates emitted as JSON, not a table
    assert payload["kind"] == "ambiguous"


# --------------------------------------------------------------------------- #
# main(argv) is import-testable and returns the int exit code
# --------------------------------------------------------------------------- #
def test_main_returns_int(cli):
    rc, _ = _run(cli, ["--account", "4001"], docs={"accounts": [_acme_site()]})
    assert isinstance(rc, int)


def test_bad_invocation_returns_nonzero(cli):
    # No selector flags at all -> ValueError path -> rc 2 (not a crash).
    rc, _out = _run(cli, [])
    assert rc == 2
