"""Gating tests for the employee-detail redesign (prompt 373).

Verifier-authored invariant tests driving employee_detail.render() against a
data-rich and a sparse person. The contract:

  * Identity header card: avatar (initials), name H1, muted subline
    `role · primary-site-NAME · status` with the raw id DE-EMPHASIZED (not the
    headline).
  * A compact quick-facts grid (empties suppressed).
  * A 4-metric row (Assigned sites / Captures / Recognitions / Open flags).
  * A "Recent activity & flags" card surfacing personnel_event docs + recent
    captures by person_id (LINKING OUT to capture detail) + data-quality flags
    (e.g. "eHub ID missing").
  * Assigned sites as NAME chips (id -> name).
  * About collapsed in <details> when present.
  * A sparse person renders the shell (no 500).

Shared invariants: UI-only, don't-lose-information, degrade-gracefully (no 500),
names-not-ids.

Mutation checks (tamper the impl -> a gating test fails -> restore):
  1. Render an assigned site as the raw id instead of the resolved name (make
     _site_link return the bare id) -> test_assigned_sites_are_name_chips fails.
  2. Drop the "Recent activity & flags" card from render's section list ->
     test_recent_activity_card_links_out_and_surfaces_flags fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employee_detail as ed


def _rich_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_jordan",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "name": "Jordan Avery",
        "person_id": "jordan_001",
        "first": "Jordan",
        "last": "Avery",
        "status": "active",
        "role": "Cleaner",
        "job": "7050",
        "site_ids": ["7050", "1338"],
        "employee_id": "EHUB9",
        "phone": "555-1212",
        "email": "j@example.com",
        "content": "On the night crew since 2024.",
    }
    doc.update(overrides)
    return doc


_SITE_NAMES = {"7050": "Maple Plaza", "1338": "Birch Tower"}


def _install(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object], *, captures=None, events=None) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    monkeypatch.setattr(ed, "_load_location_name", lambda s: _SITE_NAMES.get(str(s), f"Site {s}"))
    monkeypatch.setattr(ed, "_field_captures", lambda _p: captures or [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _d: events or [])


def _rich(monkeypatch: pytest.MonkeyPatch) -> str:
    _install(
        monkeypatch,
        _rich_doc(),
        captures=[{"capture_id": "cap_abc", "qc_category": "Restrooms", "site_id": "7050", "captured_at": "2026-06-10T08:00:00Z"}],
        events=[{"event_type": "recognition", "summary": "Great QC pass", "occurred_at": "2026-06-11"}],
    )
    return ed.render(SimpleNamespace(query={}), "jordan")


# ---------------------------------------------------------------------------
# Identity header: name H1, subline role · site-NAME · status, id de-emphasized
# ---------------------------------------------------------------------------

def test_identity_header_name_is_h1_id_demoted_to_subline(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    # The NAME is the headline.
    assert "<h1>Jordan Avery</h1>" in body
    # The raw person id is NOT the headline.
    assert "<h1>jordan_001</h1>" not in body
    # Avatar initials present.
    assert ">JA</span>" in body
    # Subline: role · primary-site-NAME · status · ID <id> (id de-emphasized).
    subline = body[body.index('class="subline"'):body.index("</p>", body.index('class="subline"'))]
    assert "Cleaner" in subline
    assert "Maple Plaza" in subline  # primary-site NAME, not "7050" (names-not-ids)
    assert "7050" not in subline
    assert "active" in subline
    assert "ID jordan_001" in subline  # raw id present but de-emphasized in the subline


# ---------------------------------------------------------------------------
# Quick-facts grid (empties suppressed)
# ---------------------------------------------------------------------------

def test_quick_facts_grid_present_and_suppresses_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert ">Quick facts</h2>" in body
    facts = body[body.index(">Quick facts</h2>"):body.index("Recent activity", body.index(">Quick facts</h2>"))]
    assert "<h3>Identity</h3>" in facts
    assert "<h3>Assignment</h3>" in facts
    assert "555-1212" in facts and "j@example.com" in facts

    # Suppression: a doc missing role/phone/email shows no empty labeled rows.
    _install(monkeypatch, _rich_doc(role="", phone="", email=""))
    sparse_facts = ed.render(SimpleNamespace(query={}), "jordan")
    assert "<dt>Role</dt><dd></dd>" not in sparse_facts
    assert "<dt>Phone / Email</dt><dd></dd>" not in sparse_facts


# ---------------------------------------------------------------------------
# 4-metric row
# ---------------------------------------------------------------------------

def test_four_metric_row(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    grid = body[body.index('class="metric-grid"'):body.index("</section>", body.index('class="metric-grid"'))]
    for label in ("Assigned sites", "Captures", "Recognitions", "Open flags"):
        assert f"<span>{label}</span>" in grid
    assert grid.count('<div class="metric-card">') == 4


# ---------------------------------------------------------------------------
# Recent activity & flags card — links out + surfaces flags
# ---------------------------------------------------------------------------

def test_recent_activity_card_links_out_and_surfaces_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: the activity card must be present and surface events +
    # captures (linking out to capture detail) + data-quality flags.
    body = _rich(monkeypatch)
    assert "Recent activity &amp; flags" in body
    # Capture links OUT to the capture detail page.
    assert "/captures?capture_id=cap_abc" in body
    # Personnel event surfaced.
    assert "Great QC pass" in body

    # Data-quality flag surfaces when eHub id is missing.
    _install(monkeypatch, _rich_doc(employee_id="", ehub_id="", ehub=""))
    flagged = ed.render(SimpleNamespace(query={}), "jordan")
    assert "eHub ID missing" in flagged


# ---------------------------------------------------------------------------
# Assigned sites as NAME chips (id -> name)
# ---------------------------------------------------------------------------

def test_assigned_sites_are_name_chips(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: assigned sites render as NAME chips resolved id -> name.
    body = _rich(monkeypatch)
    chips = body[body.index("<h2>Assigned sites</h2>"):body.index("</section>", body.index("<h2>Assigned sites</h2>"))]
    assert '<span class="pill"><a href="/sites/7050">Maple Plaza</a></span>' in chips
    assert '<span class="pill"><a href="/sites/1338">Birch Tower</a></span>' in chips
    # The bare site id must NOT be the visible chip label (names-not-ids).
    assert ">7050</a>" not in chips
    assert ">1338</a>" not in chips


# ---------------------------------------------------------------------------
# About collapsed in <details> when present
# ---------------------------------------------------------------------------

def test_about_is_collapsed_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert "<h3>About</h3>" in body
    # About sits inside a collapsible detail-block.
    about_idx = body.index("<h3>About</h3>")
    preceding = body[:about_idx]
    assert preceding.rfind('<details class="detail-block">') > preceding.rfind("</details>")
    assert "On the night crew since 2024." in body


# ---------------------------------------------------------------------------
# Sparse person renders the shell (no 500)
# ---------------------------------------------------------------------------

def test_sparse_person_renders_shell_no_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"_id": "employee_min", "_rev": "1-x", "type": "employee", "operator": "op", "first": "Min", "last": "Imal"})
    body = ed.render(SimpleNamespace(query={}), "min")
    # Degrade-gracefully: the shell renders, not an error page.
    assert "Error loading employee" not in body
    assert "<h1>Min Imal</h1>" in body
    assert ">Quick facts</h2>" in body
    assert 'class="metric-grid"' in body
    assert "Recent activity &amp; flags" in body
    # A person with no eHub id and no sites surfaces the data-quality flags
    # inside the activity card (so it isn't empty — the flags ARE the content).
    assert "eHub ID missing" in body
    assert "No assigned site recorded" in body
    assert "Phone/email missing" in body
    assert '<span class="pill warning">Data quality</span>' in body
    # Assigned-sites chips degrade to a zero-state em-dash, not a crash.
    sites = body[body.index("<h2>Assigned sites</h2>"):body.index("</section>", body.index("<h2>Assigned sites</h2>"))]
    assert 'class="zero-state"' in sites
