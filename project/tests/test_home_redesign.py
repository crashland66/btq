"""Gating tests for the home landing redesign (prompt 372).

These are INVARIANT tests authored by the verifier (not the implementer). They
drive ops_dashboard.sections.home.render() against a data-rich stub and a sparse
stub and enforce the redesign contract:

  * Greeting + a "Capture" button (the quick-capture promoted to a button, NOT
    the full inline form on the landing).
  * A "Needs you" metric-card row (Needs approval / Open issues / Supply needs /
    Equipment) where EACH card LINKS to its queue (don't-lose-info).
  * Two-up "needs attention" as NAMED site lists ("No visit in 30 days · N" /
    "Open opportunities · N") with counts.
  * A recent field-photos NAMED thumbnail strip linking out, with NO inline
    filter form on the landing.
  * Directories as localStorage-sticky <details> with NAMES (no raw-ID column).
  * Sentence-case headings; the "All Clear" empty strip preserved for a quiet day
    (zero counts -> no broken box).

Shared invariants: UI-only, don't-lose-information, degrade-gracefully (no 500),
names-not-ids.

Mutation checks (tamper the impl -> a gating test fails -> restore):
  1. Render a metric card WITHOUT its <a href> link (drop _render_action_center's
     href) -> test_needs_you_cards_link_to_their_queues fails.
  2. Re-introduce the photos filter form inline on the landing
     (render_filter_form in _render_photos_home_group) ->
     test_landing_has_no_inline_photos_filter_form fails.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import home


def _install_home(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counts: dict[str, int],
    view_rows: dict[str, list[dict]],
    photo_cards: tuple[str, bool] = ("", False),
) -> SimpleNamespace:
    def fake_query_view(_base, _auth, _db, _ddoc, view, **_kwargs):
        return view_rows.get(view, [])

    monkeypatch.setattr(home._inbox_mod, "console_counts", lambda _ctx: counts)
    monkeypatch.setattr(home, "query_view", fake_query_view)
    monkeypatch.setattr(home, "_voice_memo_employees_lookup", lambda: [])
    monkeypatch.setattr(home._field_photos_mod, "latest_photo_cards", lambda _rt, limit: photo_cards)
    monkeypatch.setattr(
        home.couchdb_config,
        "from_env",
        lambda: SimpleNamespace(base_url="http://x", auth_header=lambda: {}, timeout=10),
    )
    monkeypatch.setattr(home.couchdb_config, "vault_database", lambda: "db")
    return SimpleNamespace(runtime_root=Path(tempfile.mkdtemp()))


# A data-rich stub: one site with an open opportunity + no recent visit, an
# active employee, and non-zero needs-you counts.
_RICH_VIEWS: dict[str, list[dict]] = {
    "by_type": [
        {
            "key": ["opportunity", "opp_x"],
            "value": None,
            "doc": {
                "type": "opportunity",
                "_id": "opp_x",
                "site_id": "42",
                "location": "Maple Plaza",
                "account": "AcmeCo",
            },
        }
    ],
    "locations_all": [],
    "employees_by_site": [
        {
            "key": ["42", "employee-rae"],
            "doc": {
                "_id": "employee-rae",
                "first": "Rae",
                "last": "Quinn",
                "phone": "8145550100",
                "email": "rae@example.com",
                "job": "42",
                "status": "active",
            },
        }
    ],
    "opportunities_by_site_status": [
        {"key": ["42", "open", "opp_x"], "value": {"title": "Add weekend service"}}
    ],
    "visits_by_site_date": [],
}

_SPARSE_VIEWS: dict[str, list[dict]] = {
    "by_type": [],
    "locations_all": [],
    "employees_by_site": [],
    "opportunities_by_site_status": [],
    "visits_by_site_date": [],
}


def _rich(monkeypatch: pytest.MonkeyPatch) -> str:
    ctx = _install_home(
        monkeypatch,
        counts={"review": 3, "issues": 2, "supplies": 0, "equipment": 1},
        view_rows=_RICH_VIEWS,
        photo_cards=("<article>Photo One</article>", False),
    )
    return home.render(ctx)


def _sparse(monkeypatch: pytest.MonkeyPatch) -> str:
    ctx = _install_home(
        monkeypatch,
        counts={"review": 0, "issues": 0, "supplies": 0, "equipment": 0},
        view_rows=_SPARSE_VIEWS,
        photo_cards=("", False),
    )
    return home.render(ctx)


# ---------------------------------------------------------------------------
# Greeting + promoted Capture button (NOT inline form on the landing)
# ---------------------------------------------------------------------------

def test_greeting_and_capture_button_promoted(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert "<h1>Good " in body and ", Greg</h1>" in body
    # The Capture quick-action is a BUTTON that opens the quick-capture surface,
    # not the full inline form sitting open on the landing.
    assert '<a class="button" href="#quick-capture"' in body
    # The voice/capture form lives behind the collapsed quick-capture <details>.
    capture_anchor = body.index('id="quick-capture"')
    summary_after = body.index("<summary>", capture_anchor)
    form_after = body.index('action="/vault-home/voice-memo"')
    # The form is INSIDE the details block (after its <summary>), not bare on the page.
    assert summary_after < form_after


# ---------------------------------------------------------------------------
# Needs-you metric row — each card LINKS to its queue (don't-lose-info)
# ---------------------------------------------------------------------------

def test_needs_you_cards_link_to_their_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert "<h2>Needs you</h2>" in body
    grid = body[body.index('class="metric-grid"'):body.index("</section>", body.index('class="metric-grid"'))]
    for stat_id, href in (
        ("pending_candidates", "/candidates?status=pending_approval"),
        ("open_site_issues", "/field-capture/issues"),
        ("open_supply_needs", "/supplies?status=open"),
        ("open_equipment_requests", "/equipment?status=open"),
    ):
        # MUTATION GUARD: each metric card must be an anchor carrying its href.
        assert f'<a class="metric-card stat-badge" data-stat-id="{stat_id}"' in grid
        assert f'href="{href}"' in grid
    assert grid.count("<a class=\"metric-card stat-badge\"") == 4


def test_nonzero_needs_you_counts_are_accented(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    # Non-zero counts use the pending accent; zero counts stay neutral.
    grid = body[body.index('class="metric-grid"'):body.index("</section>", body.index('class="metric-grid"'))]
    # review=3 -> pending accent on pending_candidates
    pending = grid[grid.index('data-stat-id="pending_candidates"'):grid.index('data-stat-id="open_site_issues"')]
    assert 'class="count-pending"' in pending
    # supplies=0 -> neutral
    supplies = grid[grid.index('data-stat-id="open_supply_needs"'):grid.index('data-stat-id="open_equipment_requests"')]
    assert 'class="count-neutral"' in supplies


# ---------------------------------------------------------------------------
# Two-up needs-attention NAMED site lists with counts
# ---------------------------------------------------------------------------

def test_needs_attention_named_lists_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    # Named, counted, sentence-case panels.
    assert "No visit in 30 days &middot; 1" in body
    assert "Open opportunities &middot; 1" in body
    # The SITE NAME (not the raw id) is what shows in the list (names-not-ids).
    attention_idx = body.index("No visit in 30 days")
    attention = body[attention_idx:body.index("Recent activity", attention_idx)]
    assert "Maple Plaza" in attention
    assert '<a href="/sites/42">Maple Plaza</a>' in attention


# ---------------------------------------------------------------------------
# Recent photos NAMED strip linking out, NO inline filter form
# ---------------------------------------------------------------------------

def test_recent_photos_strip_links_out(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert "<h2>Recent field photos</h2>" in body
    photos = body[body.index("<h2>Recent field photos</h2>"):]
    photos = photos[:photos.index("</section>")]
    assert '<a href="/field-photos">View all</a>' in photos
    assert '<div class="site-gallery site-gallery--strip">' in photos
    assert "Photo One" in photos


def test_landing_has_no_inline_photos_filter_form(monkeypatch: pytest.MonkeyPatch) -> None:
    # MUTATION GUARD: the photos filter form must NOT be inlined on the landing.
    # (data-submit-on-change appears once in the shared layout script — that's the
    # generic handler, not a photos form; the invariant is no <form> targeting
    # /field-photos and no filter inputs in the recent-photos strip.)
    body = _rich(monkeypatch)
    assert '<form method="get" action="/field-photos"' not in body
    photos = body[body.index("<h2>Recent field photos</h2>"):]
    photos = photos[:photos.index("</section>")]
    assert "<form" not in photos
    assert 'name="has_photo"' not in photos and 'name="site"' not in photos


# ---------------------------------------------------------------------------
# Directories — sticky <details>, names, no raw-ID column
# ---------------------------------------------------------------------------

def test_directories_are_sticky_collapsibles_with_names(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rich(monkeypatch)
    assert '<details class="home-collapsible" id="site-directory">' in body
    assert '<details class="home-collapsible" id="employee-directory">' in body
    # Sentence-case summaries with counts.
    assert "<summary>All sites · 1</summary>" in body
    assert "<summary>Employee directory · 1</summary>" in body
    # localStorage-sticky state keys (the 363/364 sticky behavior is preserved).
    assert "btq-home-sites-collapsed" in body
    assert "btq-home-employees-collapsed" in body
    # NAMES, not raw-ID column: the account directory has only Site + Contact cols.
    directory = body[body.index("home-group--directory"):]
    assert "<thead><tr><th>Site</th><th>Contact</th></tr></thead>" in directory
    assert "<th>Raw ID</th>" not in directory
    assert "<th>Site ID</th>" not in directory
    # Employee names render (names-not-ids).
    assert "Quinn, Rae" in directory


# ---------------------------------------------------------------------------
# Sparse / quiet-day: All Clear strip, no broken box, no 500
# ---------------------------------------------------------------------------

def test_sparse_render_shows_all_clear_and_no_500(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _sparse(monkeypatch)
    # Degrade-gracefully: a fully-quiet day still renders the shell.
    assert "home-grid" in body
    assert "<h1>Good " in body
    # The All Clear empty strip is preserved (zero counts -> no broken box).
    assert '<section class="empty-strip">' in body
    assert "All Clear" in body
    # Empty named lists fall back to zero-state copy, not a crash.
    assert "No visit in 30 days &middot; 0" in body
    assert "Open opportunities &middot; 0" in body
    # Empty directories degrade to zero-state, still collapsible.
    assert '<p class="zero-state">No employee records.</p>' in body
