"""Independent verifier tests for the "My accounts" home panel (543).

The uncommitted change under test (ops_dashboard/sections/home.py) does two
things to the homepage `render(ctx)`:

  (a) relabels the collapsible site directory from "Site directory · N" to
      "All sites · N" (same DOM id `site-directory`, same storage key
      `btq-home-sites-collapsed`).
  (b) adds a "My accounts · M" collapsible panel (`_render_my_accounts_panel`)
      built from `event_pipeline.context_resolver.operator_context_snapshot`,
      filtered by `event_pipeline.visit_coverage._normalize_account(...)["active"]`,
      restricted to sites present in the directory's `site_records`, linked to
      `/sites/<id>`, with a scope line naming the operator.

These tests are authored by the INDEPENDENT VERIFIER. Sandbox identities only
(SANDBOX/S2/S3/S4/S9 site ids, "Sandy Sandbox" operator) -- no real B&T data,
no live CouchDB (the repo-root conftest.py hermetic guard blocks real :5984
connections regardless).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import context_resolver, site_operational_calendar, visit_coverage
from ops_dashboard.sections import home


# --------------------------------------------------------------------------- #
# Shared render() harness (mirrors test_home_redesign.py / test_home_coverage_panel_428a.py)
# --------------------------------------------------------------------------- #

_ZERO_COVERAGE_REPORT: dict = {
    "weekly": {"count": 0, "target": 4, "remaining": 4, "completed": []},
    "overdue": [],
    "gaps": [],
}


def _by_type_row(site_id: str, name: str, account: str = "Acme", *, active: bool = True) -> dict:
    doc = {"type": "location", "job": site_id, "location": name, "account": account}
    if not active:
        doc["active"] = False
    return {"doc": doc}


_DIRECTORY_ROWS = [
    _by_type_row("SANDBOX", "Sandbox Site", "Sandbox Co"),
    _by_type_row("S2", "Site Two"),
    _by_type_row("S3", "Site Three"),
]


def _views(by_type_rows: list[dict] | None = None) -> dict[str, list[dict]]:
    return {
        "by_type": list(_DIRECTORY_ROWS) if by_type_rows is None else by_type_rows,
        "locations_all": [],
        "employees_by_site": [],
        "opportunities_by_site_status": [],
        "visits_by_site_date": [],
    }


def _install_home(
    monkeypatch: pytest.MonkeyPatch,
    *,
    view_rows: dict[str, list[dict]],
    snapshot: dict | None = None,
    snapshot_raises: bool = False,
    coverage_report_fn=None,
    operator_id: str = "op-sandy",
) -> SimpleNamespace:
    def fake_query_view(_base, _auth, _db, _ddoc, view, **_kwargs):
        return view_rows.get(view, [])

    monkeypatch.setattr(home._inbox_mod, "console_counts", lambda _ctx: {})
    monkeypatch.setattr(home, "query_view", fake_query_view)
    monkeypatch.setattr(home, "_voice_memo_employees_lookup", lambda: [])
    monkeypatch.setattr(home._field_photos_mod, "latest_photo_cards", lambda _rt, limit: ("", False))
    monkeypatch.setattr(
        home.couchdb_config,
        "from_env",
        lambda: SimpleNamespace(base_url="http://x", auth_header=lambda: {}, timeout=10),
    )
    monkeypatch.setattr(home.couchdb_config, "vault_database", lambda: "db")
    monkeypatch.setattr(
        home, "coverage_report", coverage_report_fn or (lambda _operator: dict(_ZERO_COVERAGE_REPORT))
    )
    monkeypatch.setattr(home, "current_operator_identity", lambda: operator_id)

    if snapshot_raises:
        def boom(_operator):
            raise RuntimeError("resolver backend down")

        monkeypatch.setattr(home, "operator_context_snapshot", boom)
    else:
        monkeypatch.setattr(home, "operator_context_snapshot", lambda _operator: snapshot)

    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


def _extract_panel(body: str, dom_id: str) -> str:
    """Slice out one <details id="dom_id">...</details> block (script excluded)."""
    start = body.index(f'id="{dom_id}"')
    end = body.index("</details>", start)
    return body[start:end]


# --------------------------------------------------------------------------- #
# Contract #1: active-filtered, directory-restricted "My accounts" list.
# --------------------------------------------------------------------------- #

def _contract1_snapshot() -> dict:
    return {
        "operator": "Sandy Sandbox",
        "accounts": [
            {"site_id": "SANDBOX", "site_name": "Sandbox Site", "status": "active"},
            {"site_id": "S2", "site_name": "Site Two", "status": "active"},
            {"site_id": "S9", "site_name": "Site Nine", "status": "active"},  # not in directory
            {"site_id": "S8", "site_name": "Site Eight", "status": "inactive"},  # inactive
        ],
    }


def test_my_accounts_active_and_directory_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot=_contract1_snapshot())
    body = home.render(ctx)

    assert "All sites · 3" in body
    assert "My accounts · 2" in body

    panel = _extract_panel(body, "my-accounts")
    assert '<a href="/sites/SANDBOX">' in panel
    assert '<a href="/sites/S2">' in panel
    assert "Sandy Sandbox" in panel  # scope line names the operator

    # S3 is in the directory but absent from the snapshot -> never appears here.
    assert "S3" not in panel and "Site Three" not in panel
    # S9 is active but not in the directory -> excluded.
    assert "S9" not in panel and "Site Nine" not in panel
    # S8 is in the snapshot but inactive -> excluded.
    assert "S8" not in panel and "Site Eight" not in panel


# --------------------------------------------------------------------------- #
# Contract #2: unresolved-operator resolutions render a visible message; the
# site directory still renders normally.
# --------------------------------------------------------------------------- #

def test_not_found_resolution_shows_message_and_directory_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "operator": "nobody",
        "accounts": [],
        "resolution": {"kind": "not_found", "query": "nobody"},
    }
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot=snapshot)
    body = home.render(ctx)

    assert "All sites · 3" in body
    assert "My accounts · 0" in body
    panel = _extract_panel(body, "my-accounts")
    assert "could not resolve operator" in panel.lower()


def test_ambiguous_resolution_says_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "operator": "sandy",
        "accounts": [],
        "resolution": {"kind": "ambiguous", "query": "sandy"},
    }
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot=snapshot)
    body = home.render(ctx)

    assert "My accounts · 0" in body
    panel = _extract_panel(body, "my-accounts")
    assert "ambiguous" in panel.lower()


# --------------------------------------------------------------------------- #
# Contract #3: operator_context_snapshot raising degrades gracefully; the
# coverage panel (a separate, successful fetch) is unaffected.
# --------------------------------------------------------------------------- #

def test_resolver_exception_degrades_page_coverage_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot_raises=True)
    body = home.render(ctx)

    assert isinstance(body, str) and body
    panel = _extract_panel(body, "my-accounts")
    assert "unavailable" in panel.lower()
    # The homepage must not 500 mid-render; the rest of the shell is present.
    assert "All sites · 3" in body
    # coverage_report succeeded independently -> its panel renders normally,
    # not the "Coverage unavailable." degrade path.
    assert "Visit/QC Coverage" in body
    assert "Coverage unavailable." not in body


# --------------------------------------------------------------------------- #
# Contract #4: render() calls coverage_report and operator_context_snapshot
# with the SAME current_operator_id() value.
# --------------------------------------------------------------------------- #

def test_render_uses_same_operator_id_for_coverage_and_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def spy_coverage_report(operator):
        calls["coverage"] = operator
        return dict(_ZERO_COVERAGE_REPORT)

    def spy_snapshot(operator):
        calls["resolver"] = operator
        return _contract1_snapshot()

    ctx = _install_home(
        monkeypatch,
        view_rows=_views(),
        coverage_report_fn=spy_coverage_report,
        operator_id="op-sandy-99",
    )
    monkeypatch.setattr(home, "operator_context_snapshot", spy_snapshot)

    home.render(ctx)

    assert calls["coverage"] == "op-sandy-99"
    assert calls["resolver"] == "op-sandy-99"
    assert calls["coverage"] == calls["resolver"]


# --------------------------------------------------------------------------- #
# Contract #5: a snapshot gaining a directory site updates the count/listing;
# nothing else about the directory changes.
# --------------------------------------------------------------------------- #

def test_snapshot_gaining_site_increases_count_directory_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = {
        "operator": "Sandy Sandbox",
        "accounts": [
            {"site_id": "SANDBOX", "site_name": "Sandbox Site", "status": "active"},
            {"site_id": "S2", "site_name": "Site Two", "status": "active"},
            {"site_id": "S3", "site_name": "Site Three", "status": "active"},
        ],
    }
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot=snapshot)
    body = home.render(ctx)

    assert "My accounts · 3" in body
    assert "All sites · 3" in body  # directory itself is unchanged
    panel = _extract_panel(body, "my-accounts")
    assert '<a href="/sites/S3">' in panel


# --------------------------------------------------------------------------- #
# Contract #6: DOM id / storage key stability + distinctness.
# --------------------------------------------------------------------------- #

def test_dom_ids_and_storage_keys_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home(monkeypatch, view_rows=_views(), snapshot=_contract1_snapshot())
    body = home.render(ctx)

    assert '<details class="home-collapsible" id="site-directory">' in body
    assert "btq-home-sites-collapsed" in body

    assert '<details class="home-collapsible" id="my-accounts">' in body
    assert "btq-home-my-accounts-collapsed" in body

    # Distinct DOM id and storage key -- the two panels do not collide/clobber
    # each other's collapsed/expanded persistence.
    assert "my-accounts" != "site-directory"
    assert "btq-home-my-accounts-collapsed" != "btq-home-sites-collapsed"


# --------------------------------------------------------------------------- #
# Contract #8: no membership is inferred from the directory. Dedicated
# assertion + the fixture used for the MUTATION CHECKS below (a directory
# site absent from the snapshot [S4], and a snapshot account present in the
# directory but inactive [S3], alongside an active-but-out-of-directory
# account [S9]).
# --------------------------------------------------------------------------- #

_MUTATION_SITE_RECORDS = {
    "SANDBOX": SimpleNamespace(name="Sandbox Site"),
    "S2": SimpleNamespace(name="Site Two"),
    "S3": SimpleNamespace(name="Site Three"),
    "S4": SimpleNamespace(name="Site Four"),
}

_MUTATION_SNAPSHOT = {
    "operator": "Sandy Sandbox",
    "accounts": [
        {"site_id": "SANDBOX", "site_name": "Sandbox Site", "status": "active"},
        {"site_id": "S2", "site_name": "Site Two", "status": "active"},
        {"site_id": "S3", "site_name": "Site Three", "status": "inactive"},  # in directory, inactive
        {"site_id": "S9", "site_name": "Site Nine", "status": "active"},  # active, not in directory
        # S4 is in the directory (_MUTATION_SITE_RECORDS) but absent from the
        # snapshot entirely -- must never appear (this is the dedicated
        # "no membership inferred from directory" case).
    ],
}


def test_directory_membership_alone_does_not_grant_my_accounts() -> None:
    html = home._render_my_accounts_panel(_MUTATION_SNAPSHOT, _MUTATION_SITE_RECORDS, "op-sandy")
    assert "My accounts · 2" in html
    assert '<a href="/sites/SANDBOX">' in html
    assert '<a href="/sites/S2">' in html
    # S4: present in the directory, absent from the snapshot -> must not appear.
    assert "Site Four" not in html
    # S3: present in both, but the snapshot marks it inactive -> excluded.
    assert "Site Three" not in html
    # S9: active in the snapshot, but not in the directory -> excluded.
    assert "Site Nine" not in html


# --------------------------------------------------------------------------- #
# Contract #7: consumer agreement -- home's active site-id set matches
# visit_coverage's, built from the SAME operator_context_snapshot.
# --------------------------------------------------------------------------- #

def _location_doc(site_id: str, name: str, account: str = "Acme") -> dict:
    return {"_id": f"location_{site_id}", "type": "location", "job": site_id, "location": name, "account": account}


def _employee_doc(operator_doc_id: str, site_ids: list[str], name: str = "Sandy Sandbox") -> dict:
    return {"_id": operator_doc_id, "type": "employee", "name": name, "site_ids": site_ids, "status": "active"}


def test_consumer_agreement_home_matches_coverage_active_site_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    operator_doc_id = "employee_sandy"
    site_docs = [
        _location_doc("SANDBOX", "Sandbox Site"),
        _location_doc("S2", "Site Two"),
    ]
    person = _employee_doc(operator_doc_id, ["SANDBOX", "S2"])

    # Single source of truth: one call to operator_context_snapshot, injected
    # via its own `docs=` test seam (no live CouchDB).
    snapshot = context_resolver.operator_context_snapshot(
        operator_doc_id, docs={"accounts": site_docs, "people": [person]}
    )
    assert "resolution" not in snapshot
    assert {a["job_number"] for a in snapshot["accounts"]} == {"SANDBOX", "S2"}

    # --- home leg -----------------------------------------------------------
    by_type_rows = [{"doc": d} for d in site_docs]
    ctx = _install_home(
        monkeypatch,
        view_rows=_views(by_type_rows),
        snapshot=snapshot,
        operator_id=operator_doc_id,
    )
    body = home.render(ctx)
    panel = _extract_panel(body, "my-accounts")
    home_site_ids = set(re.findall(r'/sites/([A-Za-z0-9_]+)"', panel))

    # --- visit_coverage leg (accounts= / snapshot= injection seams) --------
    coverage = visit_coverage.account_coverage(
        operator_doc_id,
        accounts=snapshot["accounts"],
        visits=[],
        visit_gaps=[],
        snapshot=snapshot,
    )
    coverage_site_ids = {row["site_id"] for row in coverage["accounts"]}

    assert home_site_ids == {"SANDBOX", "S2"}
    assert coverage_site_ids == {"SANDBOX", "S2"}
    assert home_site_ids == coverage_site_ids

    # --- site_operational_calendar leg --------------------------------------
    # Full three-way equality (home vs. coverage vs. calendar's own active-
    # SITE scope) is NOT reachable from this snapshot alone: calendar's
    # `_active_scoped_locations` filters on the injected LOCATION doc's own
    # `active`/`status` field (a location-level notion), separate from
    # `visit_coverage._normalize_account`'s ACCOUNT-shape active status that
    # home/coverage use. Proving true parity there would require live
    # location docs (with `operational_calendars`) whose active/status field
    # is asserted to always mirror the account doc it was derived from --
    # that invariant lives outside this snapshot contract and would need a
    # real (or much larger synthetic) CouchDB fixture to exercise honestly.
    #
    # What IS provable hermetically: site_operational_calendar consumes the
    # exact SAME resolved snapshot object (same accounts) that home and
    # coverage used above, when handed it directly via its public `snapshot=`
    # seam on `site_calendar_report` / its `_resolve_snapshot` helper -- i.e.
    # calendar is single-sourced from the identical operator-scope snapshot,
    # not a second, independently-resolved one.
    resolved_for_calendar = site_operational_calendar._resolve_snapshot(
        operator_doc_id, snapshot=snapshot, docs=None, config=None
    )
    assert resolved_for_calendar is snapshot
    assert {a["job_number"] for a in resolved_for_calendar["accounts"]} == coverage_site_ids
