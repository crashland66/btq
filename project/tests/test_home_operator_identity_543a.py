"""Independent verifier tests for the operator-identity amendment (543a).

This amendment sits on top of the already-verified "My accounts" homepage
change (543, see test_home_my_accounts_543.py). It adds
``btq_vault.entity_types.current_operator_identity()`` -- a *resolver-facing*
identity (matches employee ``_id``/``person_id``/name) distinct from
``current_operator_id()`` (the *document-stamping* id, e.g. ``op_greg``) --
and repoints ``home.render()`` to call it once and hand the SAME value to
both ``coverage_report(...)`` and ``operator_context_snapshot(...)``.

Covers:
  1. ``current_operator_identity()``'s env-resolution contract in isolation.
  2. ``home.render()`` derives identity from ``current_operator_identity()``
     (not ``current_operator_id()``) and passes the same value to both
     consumers.
  3. The "My accounts" scope/could-not-resolve line names the resolved
     operator, or -- when unresolved -- the identity string that was looked
     up (not whatever the resolver happens to echo back).

Sandbox identities only; no live CouchDB (the repo-root conftest.py hermetic
guard blocks real :5984 connections regardless).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from btq_vault import entity_types
from ops_dashboard.sections import home


def _clear_operator_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_OPERATOR_IDENTITY", raising=False)
    monkeypatch.delenv("BTQ_OPERATOR_ID", raising=False)


# --------------------------------------------------------------------------- #
# Contract #1: current_operator_identity() env resolution, in isolation.
# --------------------------------------------------------------------------- #

def test_identity_env_set_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "sandbox_user")
    assert entity_types.current_operator_identity() == "sandbox_user"


def test_identity_env_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "  sandbox_user  ")
    assert entity_types.current_operator_identity() == "sandbox_user"


def test_identity_unset_falls_back_to_operator_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    assert entity_types.current_operator_identity() == entity_types.current_operator_id()
    assert entity_types.current_operator_identity() == "op_greg"


def test_identity_blank_falls_back_to_operator_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "")
    assert entity_types.current_operator_identity() == "op_greg"


def test_identity_whitespace_only_falls_back_to_operator_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "   ")
    assert entity_types.current_operator_identity() == "op_greg"


def test_identity_falls_back_to_custom_operator_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    assert entity_types.current_operator_identity() == "op_sandbox"


def test_identity_env_wins_over_custom_operator_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "sandbox_user")
    assert entity_types.current_operator_identity() == "sandbox_user"


# --------------------------------------------------------------------------- #
# Minimal home.render() harness. Deliberately independent of
# test_home_my_accounts_543.py's _install_home (rather than importing it) so
# that these tests do not depend on that file's operator-identity plumbing
# choices -- and, critically, so operator identity resolution is left to the
# REAL current_operator_identity() (driven via env), which is the thing under
# test here.
# --------------------------------------------------------------------------- #

_ZERO_COVERAGE_REPORT: dict = {
    "weekly": {"count": 0, "target": 4, "remaining": 4, "completed": []},
    "overdue": [],
    "gaps": [],
}


def _by_type_row(site_id: str, name: str, account: str = "Acme") -> dict:
    return {"doc": {"type": "location", "job": site_id, "location": name, "account": account}}


_DIRECTORY_ROWS = [
    _by_type_row("SANDBOX", "Sandbox Site", "Sandbox Co"),
    _by_type_row("S2", "Site Two"),
]


def _views() -> dict[str, list[dict]]:
    return {
        "by_type": list(_DIRECTORY_ROWS),
        "locations_all": [],
        "employees_by_site": [],
        "opportunities_by_site_status": [],
        "visits_by_site_date": [],
    }


def _install_home_minimal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: dict | None,
    coverage_report_fn=None,
    snapshot_fn=None,
) -> SimpleNamespace:
    def fake_query_view(_base, _auth, _db, _ddoc, view, **_kwargs):
        return _views().get(view, [])

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
        home, "coverage_report", coverage_report_fn or (lambda _identity: dict(_ZERO_COVERAGE_REPORT))
    )
    monkeypatch.setattr(
        home, "operator_context_snapshot", snapshot_fn or (lambda _identity: snapshot)
    )
    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


def _extract_panel(body: str, dom_id: str) -> str:
    start = body.index(f'id="{dom_id}"')
    end = body.index("</details>", start)
    return body[start:end]


# --------------------------------------------------------------------------- #
# Contract #2: render() derives operator identity from
# current_operator_identity() -- not current_operator_id() -- and passes the
# SAME identity to both coverage_report and operator_context_snapshot.
# --------------------------------------------------------------------------- #

def test_render_uses_current_operator_identity_not_current_operator_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct unit check on the seam: home imports current_operator_identity
    # and calls it (home no longer has a current_operator_id attribute at
    # all -- see test_home_my_accounts_543.py reconciliation).
    assert not hasattr(home, "current_operator_id")
    monkeypatch.setattr(home, "current_operator_identity", lambda: "patched-identity")

    calls: dict[str, object] = {}

    def spy_coverage(identity):
        calls["coverage"] = identity
        return dict(_ZERO_COVERAGE_REPORT)

    def spy_snapshot(identity):
        calls["resolver"] = identity
        return None

    ctx = _install_home_minimal(
        monkeypatch, snapshot=None, coverage_report_fn=spy_coverage, snapshot_fn=spy_snapshot
    )
    home.render(ctx)

    assert calls["coverage"] == "patched-identity"
    assert calls["resolver"] == "patched-identity"


def test_render_passes_env_identity_to_both_consumers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Integration-style: drive the REAL current_operator_identity() through
    # the env, proving the wiring end to end rather than just the seam.
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "sandbox_user")

    calls: dict[str, object] = {}

    def spy_coverage(identity):
        calls["coverage"] = identity
        return dict(_ZERO_COVERAGE_REPORT)

    def spy_snapshot(identity):
        calls["resolver"] = identity
        return None

    ctx = _install_home_minimal(
        monkeypatch, snapshot=None, coverage_report_fn=spy_coverage, snapshot_fn=spy_snapshot
    )
    home.render(ctx)

    assert calls["coverage"] == "sandbox_user"
    assert calls["resolver"] == "sandbox_user"


def test_render_falls_back_to_operator_id_for_both_consumers_when_identity_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox_fallback")
    expected = entity_types.current_operator_identity()
    assert expected == "op_sandbox_fallback"  # sanity: no identity env set, only operator id

    calls: dict[str, object] = {}

    def spy_coverage(identity):
        calls["coverage"] = identity
        return dict(_ZERO_COVERAGE_REPORT)

    def spy_snapshot(identity):
        calls["resolver"] = identity
        return None

    ctx = _install_home_minimal(
        monkeypatch, snapshot=None, coverage_report_fn=spy_coverage, snapshot_fn=spy_snapshot
    )
    home.render(ctx)

    assert calls["coverage"] == expected
    assert calls["resolver"] == expected
    assert calls["coverage"] == calls["resolver"]


# --------------------------------------------------------------------------- #
# Contract #3: scope/could-not-resolve line naming.
# --------------------------------------------------------------------------- #

def test_scope_line_names_resolved_operator() -> None:
    snapshot = {
        "operator": "Sandy Sandbox",
        "accounts": [{"site_id": "SANDBOX", "site_name": "Sandbox Site", "status": "active"}],
    }
    site_records = {"SANDBOX": SimpleNamespace(name="Sandbox Site")}
    html = home._render_my_accounts_panel(snapshot, site_records, "sandbox_user")
    assert "Sandy Sandbox" in html
    assert "sandbox_user" not in html


def test_scope_line_names_identity_when_not_found() -> None:
    snapshot = {
        "operator": "sandbox_user",
        "accounts": [],
        "resolution": {"kind": "not_found", "query": "sandbox_user"},
    }
    html = home._render_my_accounts_panel(snapshot, {}, "sandbox_user")
    assert "could not resolve operator" in html.lower()
    assert "sandbox_user" in html


def test_scope_line_uses_identity_arg_not_snapshot_operator_field_when_unresolved() -> None:
    # Regression guard: even if the resolver echoes a DIFFERENT string back
    # in snapshot["operator"] on a not_found result, the visible message must
    # name the identity that was actually looked up, not whatever the
    # resolver put in that field.
    snapshot = {
        "operator": "some other echoed value",
        "accounts": [],
        "resolution": {"kind": "not_found", "query": "sandbox_user"},
    }
    html = home._render_my_accounts_panel(snapshot, {}, "sandbox_user")
    assert "sandbox_user" in html
    assert "some other echoed value" not in html


def test_render_not_found_message_uses_env_identity_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "sandbox_user")
    snapshot = {
        "operator": "sandbox_user",
        "accounts": [],
        "resolution": {"kind": "not_found", "query": "sandbox_user"},
    }
    ctx = _install_home_minimal(monkeypatch, snapshot=snapshot)
    body = home.render(ctx)
    panel = _extract_panel(body, "my-accounts")
    assert "sandbox_user" in panel
    assert "could not resolve operator" in panel.lower()
