"""Independent verifier tests for the Visit/QC coverage panel on Home (428a).

The change under test adds a PURE ``_render_coverage_panel(report) -> str`` to
``ops_dashboard.sections.home`` plus a graceful-degrade wrapper in ``render``.

These tests are authored by the INDEPENDENT VERIFIER. They use only SYNTHETIC
report dicts (invented sites "4001"/"Acme North", invented dates). NO live
CouchDB, NO real B&T data.

Contract:
  * "QCs this week: <count> / <target>" + "<remaining> remaining".
  * Completed-QC list (site + date), overdue accounts (site + "Nd since last
    QC" / "never"), open gaps.
  * Lists capped at COVERAGE_PANEL_LIMIT.
  * Text is HTML-escaped.
  * Pure (no CouchDB). render() degrades (no 500) when coverage fetch fails.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import home


# --- pure helper: populated report -------------------------------------------

def _populated_report() -> dict:
    return {
        "weekly": {
            "count": 2,
            "target": 4,
            "remaining": 2,
            "completed": [
                {"site_id": "4001", "site": "Acme North", "date": "2026-06-22", "evidence": "photo"},
                {"site_id": "4002", "site": "Borough Tower", "date": "2026-06-21", "evidence": "photo"},
            ],
        },
        "overdue": [
            {"site_id": "4003", "site_name": "Cedar Commons", "days_since_last_qc": 12},
            {"site_id": "4004", "site_name": "Dale Center", "days_since_last_qc": None},
        ],
        "gaps": [
            {"site_id": "4005", "site": "Echo Plaza", "date": "2026-06-20"},
        ],
    }


def test_panel_shows_count_target_and_remaining() -> None:
    html = home._render_coverage_panel(_populated_report())
    assert "2 / 4" in html
    assert "2 remaining" in html


def test_panel_lists_completed_sites_and_dates() -> None:
    html = home._render_coverage_panel(_populated_report())
    assert "Acme North" in html
    assert "2026-06-22" in html
    assert "Borough Tower" in html
    assert "2026-06-21" in html


def test_panel_overdue_shows_days_and_never() -> None:
    html = home._render_coverage_panel(_populated_report())
    assert "Cedar Commons" in html
    assert "12d since last QC" in html
    # days_since_last_qc is None -> "never"
    assert "Dale Center" in html
    assert "never" in html


def test_panel_shows_open_gaps() -> None:
    html = home._render_coverage_panel(_populated_report())
    assert "Echo Plaza" in html
    assert "2026-06-20" in html


def test_panel_escapes_text() -> None:
    report = {
        "weekly": {
            "count": 1,
            "target": 4,
            "remaining": 3,
            "completed": [{"site_id": "4001", "site": "Acme <b>North</b>", "date": "2026-06-22"}],
        },
        "overdue": [{"site_id": "4002", "site_name": "Beta & Sons", "days_since_last_qc": 9}],
        "gaps": [],
    }
    html = home._render_coverage_panel(report)
    # Raw markup must NOT appear; escaped entities must.
    assert "Acme <b>North</b>" not in html
    assert "Acme &lt;b&gt;North&lt;/b&gt;" in html
    assert "Beta &amp; Sons" in html


# --- pure helper: zero state -------------------------------------------------

def test_panel_zero_state_renders_without_error() -> None:
    report = {
        "weekly": {"count": 0, "target": 4, "remaining": 4, "completed": []},
        "overdue": [],
        "gaps": [],
    }
    html = home._render_coverage_panel(report)
    assert isinstance(html, str) and html
    assert "0 / 4" in html
    assert "4 remaining" in html
    # Sensible empty messages.
    assert "No QCs completed this week." in html
    assert "No overdue QC accounts." in html
    assert "No open visit gaps." in html


def test_panel_empty_dict_does_not_crash() -> None:
    # Defensive: a wholly empty report must still render a panel.
    html = home._render_coverage_panel({})
    assert isinstance(html, str) and html
    assert "0 / 0" in html


# --- pure helper: list capping -----------------------------------------------

def test_panel_caps_overdue_at_limit() -> None:
    limit = home.COVERAGE_PANEL_LIMIT
    overdue = [
        {"site_id": str(4100 + i), "site_name": f"Overdue Site {i}", "days_since_last_qc": i + 1}
        for i in range(limit + 2)
    ]
    report = {
        "weekly": {"count": 0, "target": 4, "remaining": 4, "completed": []},
        "overdue": overdue,
        "gaps": [],
    }
    html = home._render_coverage_panel(report)
    shown = [i for i in range(limit + 2) if f"Overdue Site {i}" in html]
    assert len(shown) == limit, f"expected {limit} overdue rows, got {len(shown)}: {shown}"
    # The first `limit` are shown; the rest are hidden.
    assert shown == list(range(limit))
    # A "+N more" note accounts for the overflow.
    assert "more overdue accounts" in html


# --- render(): graceful degrade ----------------------------------------------

def _install_home(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Minimal home.render() stub harness (mirrors test_home_redesign)."""
    def fake_query_view(_base, _auth, _db, _ddoc, view, **_kwargs):
        return []

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
    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


def test_render_degrades_when_coverage_report_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home(monkeypatch)

    def boom(_operator):
        raise RuntimeError("coverage backend down")

    monkeypatch.setattr(home, "coverage_report", boom)
    monkeypatch.setattr(home, "current_operator_id", lambda: "op-test")

    body = home.render(ctx)
    assert isinstance(body, str) and body
    # Did not 500; the muted unavailable note is present.
    assert "Coverage unavailable." in body
    # And it did NOT crash mid-render leaving a broken page.
    assert "Visit/QC Coverage" in body


def test_render_includes_coverage_panel_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _install_home(monkeypatch)
    monkeypatch.setattr(home, "coverage_report", lambda _operator: _populated_report())
    monkeypatch.setattr(home, "current_operator_id", lambda: "op-test")

    body = home.render(ctx)
    assert "Visit/QC Coverage" in body
    assert "2 / 4" in body
    assert "Acme North" in body
