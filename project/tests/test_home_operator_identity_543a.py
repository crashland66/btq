"""Independent verifier tests for the operator-identity amendment (543a).

This amendment adds ``btq_vault.entity_types.current_operator_identity()`` --
a *resolver-facing* identity (matches employee ``_id``/``person_id``/name)
distinct from ``current_operator_id()`` (the *document-stamping* id, e.g.
``op_greg``) -- and points ``home.render()`` at it for the coverage panel.

Covers:
  1. ``current_operator_identity()``'s env-resolution contract in isolation.
  2. ``home.render()`` derives identity from ``current_operator_identity()``
     (not ``current_operator_id()``) and passes it to ``coverage_report(...)``.

Reconciled 2026-09-04 (prompt 544, "remove My accounts"): the "My accounts"
homepage panel added by 543 was removed, taking with it
``operator_context_snapshot``, ``_normalize_account``,
``_render_my_accounts_panel`` and ``_operator_display_name``. The tests that
spied on ``operator_context_snapshot`` or asserted the panel's scope/
could-not-resolve line (``test_render_uses_current_operator_identity_not_current_operator_id``,
``test_render_passes_env_identity_to_both_consumers``,
``test_render_falls_back_to_operator_id_for_both_consumers_when_identity_unset``,
the three ``test_scope_line_*`` tests, and
``test_render_not_found_message_uses_env_identity_end_to_end``) were removed
or rewritten below to prove the identity-to-``coverage_report`` wiring only.
Coverage of "no My accounts panel" and "operator_context_snapshot never
called" now lives in ``test_home_no_my_accounts_544.py``.

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
# Minimal home.render() harness.
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
    coverage_report_fn=None,
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
    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


# --------------------------------------------------------------------------- #
# Contract #2: render() derives operator identity from
# current_operator_identity() -- not current_operator_id() -- and passes it
# to coverage_report.
# --------------------------------------------------------------------------- #

def test_render_uses_current_operator_identity_not_current_operator_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct unit check on the seam: home imports current_operator_identity
    # and calls it (home has no current_operator_id attribute at all).
    assert not hasattr(home, "current_operator_id")
    monkeypatch.setattr(home, "current_operator_identity", lambda: "patched-identity")

    calls: dict[str, object] = {}

    def spy_coverage(identity):
        calls["coverage"] = identity
        return dict(_ZERO_COVERAGE_REPORT)

    ctx = _install_home_minimal(monkeypatch, coverage_report_fn=spy_coverage)
    home.render(ctx)

    assert calls["coverage"] == "patched-identity"


def test_render_passes_env_identity_to_coverage_report(monkeypatch: pytest.MonkeyPatch) -> None:
    # Integration-style: drive the REAL current_operator_identity() through
    # the env, proving the wiring end to end rather than just the seam.
    _clear_operator_env(monkeypatch)
    monkeypatch.setenv("BTQ_OPERATOR_IDENTITY", "sandbox_user")

    calls: dict[str, object] = {}

    def spy_coverage(identity):
        calls["coverage"] = identity
        return dict(_ZERO_COVERAGE_REPORT)

    ctx = _install_home_minimal(monkeypatch, coverage_report_fn=spy_coverage)
    home.render(ctx)

    assert calls["coverage"] == "sandbox_user"


def test_render_falls_back_to_operator_id_for_coverage_report_when_identity_unset(
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

    ctx = _install_home_minimal(monkeypatch, coverage_report_fn=spy_coverage)
    home.render(ctx)

    assert calls["coverage"] == expected
