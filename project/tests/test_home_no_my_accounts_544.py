"""Independent verifier tests for the "My accounts" panel removal (544).

The operator asked for the homepage "My accounts" panel (added by 543,
see the now-deleted ``test_home_my_accounts_543.py``) to be removed.
``home.py`` no longer defines ``_operator_display_name``,
``_render_my_accounts_panel``, or imports ``operator_context_snapshot`` /
``_normalize_account``; ``render()`` no longer calls
``operator_context_snapshot(...)`` at all.

KEPT on purpose (not this file's concern, covered elsewhere): the
directory label "All sites · N" (same DOM id ``site-directory``, storage
key ``btq-home-sites-collapsed``) and ``render()`` still passing
``current_operator_identity()`` to ``coverage_report(...)`` (see
``test_home_operator_identity_543a.py`` and
``test_home_coverage_panel_428a.py``).

Contract:
  1. The rendered homepage contains "All sites · <n>", contains NO
     ``id="my-accounts"``, NO "Sites assigned to", NO "My accounts"; the
     directory group has exactly two ``class="home-collapsible"`` blocks
     (All sites, Employee directory) and ``btq-home-sites-collapsed`` is
     present.
  2. ``home`` no longer has attributes ``operator_context_snapshot``,
     ``_normalize_account``, ``_render_my_accounts_panel``,
     ``_operator_display_name``, and ``operator_context_snapshot`` is
     never called during render (patching
     ``event_pipeline.context_resolver.operator_context_snapshot`` to
     raise must not break render()).

Sandbox identities only; no live CouchDB (the repo-root conftest.py
hermetic guard blocks real :5984 connections regardless).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from event_pipeline import context_resolver
from ops_dashboard.sections import home


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


def _install_home_minimal(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
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
    monkeypatch.setattr(home, "coverage_report", lambda _identity: dict(_ZERO_COVERAGE_REPORT))
    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


def _directory_group(body: str) -> str:
    start = body.index('<div class="home-group home-group--directory">')
    next_group = body.find('<div class="home-group ', start + 1)
    end_grid = body.find("</div></div>", start)
    end = next_group if next_group != -1 else end_grid
    return body[start:end]


# --------------------------------------------------------------------------- #
# Contract #1: rendered homepage has no My accounts panel; All sites intact.
# --------------------------------------------------------------------------- #

def test_render_has_no_my_accounts_panel_but_keeps_all_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home_minimal(monkeypatch)
    body = home.render(ctx)

    assert f"All sites · {len(_DIRECTORY_ROWS)}" in body
    assert 'id="my-accounts"' not in body
    assert "Sites assigned to" not in body
    assert "My accounts" not in body


def test_directory_group_has_exactly_two_collapsibles(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home_minimal(monkeypatch)
    body = home.render(ctx)
    directory = _directory_group(body)

    assert directory.count('class="home-collapsible"') == 2
    assert 'id="site-directory"' in directory
    assert 'id="employee-directory"' in directory
    assert 'id="my-accounts"' not in directory
    assert "btq-home-sites-collapsed" in directory


# --------------------------------------------------------------------------- #
# Contract #3: the my-accounts symbols are gone; the resolver is never called.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "attr",
    [
        "operator_context_snapshot",
        "_normalize_account",
        "_render_my_accounts_panel",
        "_operator_display_name",
    ],
)
def test_home_no_longer_has_my_accounts_symbols(attr: str) -> None:
    assert not hasattr(home, attr)


def test_render_never_calls_operator_context_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_identity):
        raise AssertionError("operator_context_snapshot must not be called by render() after 544")

    monkeypatch.setattr(context_resolver, "operator_context_snapshot", boom)

    ctx = _install_home_minimal(monkeypatch)
    body = home.render(ctx)

    assert isinstance(body, str) and body
    assert 'id="my-accounts"' not in body
