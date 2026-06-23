"""Independent-verifier tests for the CouchDB context resolver core.

All fixtures here are SYNTHETIC / ANONYMIZED. No real B&T account names, no real
people, no real job numbers, phones, or emails appear in this file. Every doc is
injected via the `docs=` test seam so nothing touches a live CouchDB.

Invented identities used throughout:
  Accounts: "Acme Widgets - North", "Beta Tools - South", "Gamma Logistics"
  People:   "Pat Vendor", "Pat Supplier", "Dana Direct", "Alex Assigned", ...
  Operators: "Gregory Stoltz" (resolved from "Greg"), "Dana Manager"
  Job numbers: 4001 / 4002 / 4003 / 8888 / 9999 (fake)
"""
from __future__ import annotations

import datetime as dt

import pytest

from event_pipeline import context_resolver as cr


# --------------------------------------------------------------------------- #
# Synthetic fixtures
# --------------------------------------------------------------------------- #
def _bare_site() -> dict:
    """A thin site doc keyed by bare <id> (no `account`/`aliases`)."""
    return {
        "_id": "4001",
        "_rev": "1-bare",
        "type": "site",
        "site_id": "4001",
        "name": "Acme North",
    }


def _rich_site() -> dict:
    """A rich site doc keyed by `site_<id>` for the SAME site_id."""
    return {
        "_id": "site_4001",
        "_rev": "3-rich",
        "type": "site",
        "site_id": "4001",
        "account": "Acme Widgets - North",
        "aliases": ["Acme N", "AWN"],
        "status": "active",
        "manager": "Gregory Stoltz",
    }


def _beta_site() -> dict:
    return {
        "_id": "site_4002",
        "_rev": "1",
        "type": "site",
        "site_id": "4002",
        "account": "Beta Tools - South",
        "aliases": ["Beta S"],
        "active": True,
    }


def _gamma_site() -> dict:
    return {
        "_id": "site_4003",
        "_rev": "1",
        "type": "site",
        "site_id": "4003",
        "account": "Gamma Logistics",
        "aliases": ["Gamma Depot"],
    }


def _people() -> list[dict]:
    return [
        {
            "_id": "emp_100",
            "_rev": "1",
            "type": "employee",
            "name": "Pat Vendor",
            "first": "Pat",
            "last": "Vendor",
            "ehub_id": "E100",
            "role": "Cleaner",
            "site_ids": ["4001"],
            "manager": "Gregory Stoltz",
        },
        {
            "_id": "emp_101",
            "_rev": "1",
            "type": "employee",
            "name": "Pat Supplier",
            "first": "Pat",
            "last": "Supplier",
            "ehub_id": "E101",
            "role": "Cleaner",
            "site_ids": ["4002"],
            "manager": "Dana Lead",
        },
    ]


# --------------------------------------------------------------------------- #
# Account resolution
# --------------------------------------------------------------------------- #
def test_account_by_site_id():
    res = cr.resolve_account("4002", docs={"accounts": [_beta_site()]})
    assert res["kind"] == "account"
    assert res["job_number"] == "4002"
    assert res["canonical_name"] == "Beta Tools - South"


def test_account_by_canonical_name():
    res = cr.resolve_account("Beta Tools - South", docs={"accounts": [_beta_site()]})
    assert res["kind"] == "account"
    assert res["job_number"] == "4002"


def test_account_by_alias_entry():
    res = cr.resolve_account("AWN", docs={"accounts": [_rich_site()]})
    assert res["kind"] == "account"
    assert res["canonical_name"] == "Acme Widgets - North"


def test_account_by_normalized_variant_case_and_whitespace():
    res = cr.resolve_account("  acme   WIDGETS - north ", docs={"accounts": [_rich_site()]})
    assert res["kind"] == "account"
    assert res["canonical_name"] == "Acme Widgets - North"


def test_account_two_doc_form_dedupe_richer_wins():
    # bare <id> + site_<id> for the same site_id -> ONE result, richer fields win.
    res = cr.resolve_account("4001", docs={"accounts": [_bare_site(), _rich_site()]})
    assert res["kind"] == "account"  # not ambiguous
    assert res["canonical_name"] == "Acme Widgets - North"  # rich, not "Acme North"
    assert res["aliases"]  # rich aliases present
    assert res["source"]["doc_id"] == "site_4001"  # richer doc is the source
    assert res["source"]["rev"] == "3-rich"


def test_account_dedupe_order_independent():
    # Order should not change which doc wins.
    res = cr.resolve_account("4001", docs={"accounts": [_rich_site(), _bare_site()]})
    assert res["kind"] == "account"
    assert res["source"]["doc_id"] == "site_4001"


def test_account_ambiguous_returns_candidates_no_guess():
    a1 = {
        "_id": "site_4001", "_rev": "1", "type": "site", "site_id": "4001",
        "account": "Acme Widgets - North", "aliases": ["North Plant"],
    }
    a2 = {
        "_id": "site_4002", "_rev": "1", "type": "site", "site_id": "4002",
        "account": "Beta Tools - North", "aliases": ["North Depot"],
    }
    res = cr.resolve_account("north", docs={"accounts": [a1, a2]})
    assert res["kind"] == "ambiguous"
    job_numbers = {c["job_number"] for c in res["candidates"]}
    assert job_numbers == {"4001", "4002"}  # both surfaced, no silent pick


def test_account_not_found():
    res = cr.resolve_account("zzz totally unknown", docs={"accounts": [_rich_site()]})
    assert res["kind"] == "not_found"
    assert res.get("job_number") is None


def test_account_source_provenance_present():
    res = cr.resolve_account("4001", docs={"accounts": [_rich_site()]})
    src = res["source"]
    assert set(src) >= {"database", "doc_id", "rev"}
    assert src["doc_id"] == "site_4001"
    assert src["rev"] == "3-rich"
    assert src["database"]  # non-empty database label


# --------------------------------------------------------------------------- #
# Person resolution
# --------------------------------------------------------------------------- #
def test_person_by_canonical_name():
    res = cr.resolve_person("Pat Vendor", docs={"people": _people()})
    assert res["kind"] == "person"
    assert res["canonical_name"] == "Pat Vendor"
    assert res["role"] == "Cleaner"
    assert res["assigned_job_numbers"] == ["4001"]
    assert res["manager"] == "Gregory Stoltz"


def test_person_by_display_first_last():
    res = cr.resolve_person("Pat Vendor", docs={"people": _people()})
    assert res["display_name"] == "Pat Vendor"


def test_person_by_display_last_comma_first():
    res = cr.resolve_person("Vendor, Pat", docs={"people": _people()})
    assert res["kind"] == "person"
    assert res["canonical_name"] == "Pat Vendor"


def test_person_by_ehub_id():
    res = cr.resolve_person("E101", docs={"people": _people()})
    assert res["kind"] == "person"
    assert res["canonical_name"] == "Pat Supplier"


def test_person_by_doc_id():
    res = cr.resolve_person("emp_100", docs={"people": _people()})
    assert res["kind"] == "person"
    assert res["canonical_name"] == "Pat Vendor"


def test_person_ambiguous_same_first_name():
    res = cr.resolve_person("Pat", docs={"people": _people()})
    assert res["kind"] == "ambiguous"
    names = {c["canonical_name"] for c in res["candidates"]}
    assert names == {"Pat Vendor", "Pat Supplier"}


def test_person_not_found():
    res = cr.resolve_person("Nobody Here", docs={"people": _people()})
    assert res["kind"] == "not_found"


def test_person_source_provenance_present():
    res = cr.resolve_person("emp_100", docs={"people": _people()})
    src = res["source"]
    assert set(src) >= {"database", "doc_id", "rev"}
    assert src["doc_id"] == "emp_100"
    assert src["rev"] == "1"


# --------------------------------------------------------------------------- #
# operator_context_snapshot
# --------------------------------------------------------------------------- #
def _snapshot_world() -> dict:
    operator = {
        "_id": "emp_op", "_rev": "1", "type": "employee",
        "name": "Gregory Stoltz", "first": "Gregory", "last": "Stoltz",
        "site_ids": ["4001", "4002"],
    }
    direct = {  # manager == "Greg" (normalizes to operator), site NOT in operator sites
        "_id": "emp_d", "_rev": "1", "type": "employee",
        "name": "Dana Direct", "first": "Dana", "last": "Direct",
        "manager": "Greg", "site_ids": ["9999"],
    }
    assigned = {  # site overlaps operator, manager is someone else
        "_id": "emp_a", "_rev": "1", "type": "employee",
        "name": "Alex Assigned", "first": "Alex", "last": "Assigned",
        "manager": "Some Other Boss", "site_ids": ["4001"],
    }
    both = {  # manager matches AND site overlaps
        "_id": "emp_b", "_rev": "1", "type": "employee",
        "name": "Bo Both", "first": "Bo", "last": "Both",
        "manager": "Gregory Stoltz", "site_ids": ["4002"],
    }
    unrelated = {  # neither manager nor site overlap
        "_id": "emp_n", "_rev": "1", "type": "employee",
        "name": "Noa None", "first": "Noa", "last": "None",
        "manager": "Nobody", "site_ids": ["8888"],
    }
    return {
        "accounts": [_rich_site(), _beta_site(), _gamma_site()],
        "people": [operator, direct, assigned, both, unrelated],
    }


def test_snapshot_accounts_from_operator_site_ids():
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now="2026-06-23T12:00:00+00:00")
    # operator site_ids are 4001 + 4002 -> only those accounts (NOT gamma 4003)
    assert [a["job_number"] for a in snap["accounts"]] == ["4001", "4002"]


def test_snapshot_operator_resolved_from_greg_alias():
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now="now")
    assert snap["operator"] == "Gregory Stoltz"


def test_snapshot_relationship_labels():
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now="now")
    rels = {p["canonical_name"]: p["relationship"] for p in snap["people"]}
    assert rels == {
        "Dana Direct": "direct",       # manager "Greg" -> Gregory Stoltz, no site overlap
        "Alex Assigned": "assigned",   # site overlap only
        "Bo Both": "both",             # manager match + site overlap
    }
    # operator + unrelated person are excluded
    assert "Noa None" not in rels
    assert "Gregory Stoltz" not in rels


def test_snapshot_manager_greg_to_gregory_normalization():
    # The "direct" person's manager is literally "Greg"; it must still map to the
    # operator whose canonical name is "Gregory Stoltz".
    snap = cr.operator_context_snapshot("Gregory Stoltz", docs=_snapshot_world(), now="now")
    dana = next(p for p in snap["people"] if p["canonical_name"] == "Dana Direct")
    assert dana["relationship"] == "direct"


def test_snapshot_operator_param_not_hardcoded():
    # A DIFFERENT operator must yield different accounts/people sets.
    operator2 = {
        "_id": "emp_op2", "_rev": "1", "type": "employee",
        "name": "Dana Manager", "first": "Dana", "last": "Manager",
        "site_ids": ["8888"],
    }
    reportee = {
        "_id": "emp_r", "_rev": "1", "type": "employee",
        "name": "Rene Report", "first": "Rene", "last": "Report",
        "manager": "Dana Manager", "site_ids": ["4001"],  # not op2 site
    }
    world = {
        "accounts": [_rich_site(), _beta_site(), _gamma_site()],
        "people": [operator2, reportee],
    }
    snap = cr.operator_context_snapshot("Dana Manager", docs=world, now="now")
    assert snap["operator"] == "Dana Manager"
    # operator2 owns only site 8888 -> no matching account doc in the world
    assert snap["accounts"] == []
    rels = {p["canonical_name"]: p["relationship"] for p in snap["people"]}
    assert rels == {"Rene Report": "direct"}  # only manager match, no site overlap


def test_snapshot_injected_now_string_flows_to_generated_at():
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now="2099-01-02T03:04:05+00:00")
    assert snap["generated_at"] == "2099-01-02T03:04:05+00:00"


def test_snapshot_injected_now_datetime_flows_to_generated_at():
    when = dt.datetime(2030, 5, 6, 7, 8, 9, tzinfo=dt.timezone.utc)
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now=when)
    assert snap["generated_at"].startswith("2030-05-06T07:08:09")


def test_snapshot_account_source_provenance():
    snap = cr.operator_context_snapshot("Greg", docs=_snapshot_world(), now="now")
    acme = next(a for a in snap["accounts"] if a["job_number"] == "4001")
    assert set(acme["source"]) >= {"database", "doc_id", "rev"}
    assert acme["source"]["doc_id"] == "site_4001"
