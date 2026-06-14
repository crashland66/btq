"""Gating tests for the home-redesign QA fixes (prompt 375).

These lock in the corrected, deployed behavior of the ops-dashboard home
landing. Each test maps to one of the five contract points:

  #0  full-width command center — `.home-grid` is a single full-width column;
      "Needs you" renders as a horizontal metric row; the two-column right-rail
      template is gone (no `home-main` / `home-rail` wrappers, no clipping).
  #1  no dev-build version — the nav footer never emits `vdev` or a bare SHA;
      a clean version span only when a genuine package version is available.
  #2  no-visit view-all dedup — expanding "No visit in 30 days · N" shows the
      REMAINING sites, not a repeat of the preview rows.
  #3  no-visit excludes inactive sites — driven by the canonical `active` flag,
      an inactive location is neither counted nor listed; an active no-visit
      site is.
  #4  single capture affordance — the landing shows the "Capture observation"
      label exactly ONCE, with the full voice card behind the quick-capture
      <details> rather than dominating the landing.

The stubs mirror tests/test_ops_dashboard_home.py::install_full_home so these
exercise the real render() path (projector views + counts + photos stubbed).
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from ops_dashboard import layout
from ops_dashboard.sections import home
from tests.test_ops_dashboard import request_text


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _install_home(monkeypatch: pytest.MonkeyPatch, view_rows: dict[str, list[dict]]) -> None:
    """Drive home.render() off the supplied projector view rows."""

    def fake_query_view(
        _base_url: str,
        _auth_headers: dict,
        _database: str,
        _ddoc: str,
        view: str,
        **_kwargs: object,
    ) -> list[dict]:
        return view_rows.get(view, [])

    monkeypatch.setattr(
        home._inbox_mod,
        "console_counts",
        lambda _ctx: {"review": 0, "issues": 0, "supplies": 0, "equipment": 0},
    )
    monkeypatch.setattr(home, "query_view", fake_query_view)
    monkeypatch.setattr(home, "_voice_memo_employees_lookup", lambda: [])
    monkeypatch.setattr(
        home._field_photos_mod, "latest_photo_cards", lambda _runtime_root, limit: ("", False)
    )


def _location_row(site_id: str, name: str, *, active: object = True, account: str = "Acct") -> dict:
    doc = {
        "type": "location",
        "_id": f"location_{site_id}",
        "site_id": site_id,
        "location": name,
        "account": account,
    }
    if active is not None:
        doc["active"] = active
    return {"key": ["location", f"location_{site_id}"], "value": None, "doc": doc}


def _sparse_rows() -> dict[str, list[dict]]:
    return {
        "by_type": [],
        "locations_all": [],
        "employees_by_site": [],
        "opportunities_by_site_status": [],
        "visits_by_site_date": [],
    }


def _data_rich_rows(*, count: int = 10) -> dict[str, list[dict]]:
    """A data-rich set: `count` active locations, none visited recently (so all
    are "no visit in 30 days"). Names are zero-padded so the sort order is
    deterministic across the preview/view-all boundary."""
    rows = _sparse_rows()
    rows["by_type"] = [
        _location_row(f"s{i:02d}", f"Site {i:02d}") for i in range(count)
    ]
    return rows


def _capture_group(body: str) -> str:
    start = body.index('<div class="home-group home-group--capture">')
    end = body.index("</div></div>", start)
    return body[start:end]


# ---------------------------------------------------------------------------
# #0 full-width command center
# ---------------------------------------------------------------------------

def test_home_grid_is_single_full_width_column(tmp_path: Path) -> None:
    """The CSS `.home-grid` is a single full-width column — no narrow right rail."""
    status, _content_type, css = request_text("GET", "/static/admin.css", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    grid_start = css.index(".home-grid {")
    grid_block = css[grid_start:css.index("}", grid_start)]
    # Single full-width column.
    assert "grid-template-columns: minmax(0, 1fr);" in grid_block
    # NOT a two-column "content + rail" template (would clip the wide panels).
    assert "170px" not in grid_block
    assert "240px" not in grid_block
    assert "minmax(0, 1fr) auto" not in grid_block


def test_home_grid_renders_metric_row_full_width_no_rail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """"Needs you" is a horizontal metric row inside the single grid; the old
    two-column rail wrappers are gone, so nothing is clipped into a narrow rail."""
    _install_home(monkeypatch, _data_rich_rows())

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert body.count('<div class="home-grid">') == 1
    grid_start = body.index('<div class="home-grid">')
    grid = body[grid_start:body.index("</main>", grid_start)]
    # The metric row (full-width "Needs you") lives inside the grid.
    assert '<div class="metric-grid" aria-label="Needs you queues">' in grid
    # No two-column right-rail wrappers anywhere.
    assert "home-main" not in body
    assert "home-rail" not in body


# ---------------------------------------------------------------------------
# #1 no dev-build version
# ---------------------------------------------------------------------------

def test_nav_footer_never_emits_dev_build_version(tmp_path: Path) -> None:
    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "vdev" not in body
    # The version helper resolves to a clean "v<ver>" only for a genuine,
    # non-"dev" package version — never a dev-build marker.
    assert layout._dashboard_version() != "vdev"
    assert not layout._dashboard_version().lower().endswith("dev")


def test_dashboard_version_blank_when_no_genuine_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A "dev" package version (or none) yields an empty string — render nothing
    rather than a dev/SHA placeholder."""
    monkeypatch.setattr(layout, "pkg_version", lambda _name: "dev")
    assert layout._dashboard_version() == ""

    monkeypatch.setattr(layout, "pkg_version", lambda _name: "1.4.2")
    assert layout._dashboard_version() == "v1.4.2"


# ---------------------------------------------------------------------------
# #2 no-visit view-all dedup
# ---------------------------------------------------------------------------

def test_no_visit_view_all_does_not_repeat_preview_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expanding "No visit in 30 days · N" shows the REMAINING sites; the
    preview's sites do not appear twice in the rendered HTML."""
    _install_home(monkeypatch, _data_rich_rows(count=10))

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    # Scope to the "No visit in 30 days" panel (preview + nested view-all
    # <details>); the site directory and capture <select> also mention each site
    # but are unrelated to this fix.
    panel_start = body.index("No visit in 30 days")
    panel = body[panel_start:body.index("</section>", panel_start)]
    # 10 no-visit sites; preview shows the first 6 (sorted by name), view-all
    # shows the remaining 4. Each name appears exactly once within the panel.
    assert "No visit in 30 days &middot; 10" in panel
    for i in range(10):
        assert panel.count(f"Site {i:02d}") == 1, f"Site {i:02d} duplicated across preview + view-all"


def test_no_visit_panel_unit_dedupes_preview_and_view_all() -> None:
    """Unit-level guard on the panel renderer itself (independent of the route):
    the preview rows and the view-all rows are disjoint."""
    from btq_vault.projector import _Location

    locations = [_Location(f"s{i:02d}", f"Site {i:02d}", "Acct") for i in range(9)]
    html = home._render_named_location_panel(
        title="No visit in 30 days",
        locations=locations,
        empty="none",
        details_id="no-visit-sites-all",
    )

    details_at = html.index("<details")
    preview, details = html[:details_at], html[details_at:]
    # Preview: first 6 by name; details: remaining 3 — no overlap.
    for i in range(6):
        assert preview.count(f"Site {i:02d}") == 1
        assert details.count(f"Site {i:02d}") == 0
    for i in range(6, 9):
        assert preview.count(f"Site {i:02d}") == 0
        assert details.count(f"Site {i:02d}") == 1


# ---------------------------------------------------------------------------
# #3 no-visit excludes inactive sites
# ---------------------------------------------------------------------------

def test_no_visit_excludes_inactive_includes_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The no-visit count/list is driven by the canonical `active` flag: an
    inactive no-visit location is excluded; an active no-visit location is
    counted and listed."""
    rows = _sparse_rows()
    rows["by_type"] = [
        _location_row("active1", "Active Yard", active=True),
        _location_row("dead1", "Decommissioned Depot", active=False),
    ]
    _install_home(monkeypatch, rows)

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    # Active no-visit site -> counted + listed.
    assert "Active Yard" in body
    assert "No visit in 30 days &middot; 1" in body
    assert "/sites/active1" in body
    # Inactive site -> excluded everywhere (count, list, directory links).
    assert "Decommissioned Depot" not in body
    assert "/sites/dead1" not in body


def test_inactive_flag_drives_exclusion_not_hardcoded_ids() -> None:
    """The exclusion set is computed from the `active is False` flag, not a
    hard-coded id list — flip the flag and the membership flips with it."""
    inactive_row = {
        "doc": {"type": "location", "_id": "location_xyz", "site_id": "xyz", "active": False}
    }
    active_row = {
        "doc": {"type": "location", "_id": "location_xyz", "site_id": "xyz", "active": True}
    }
    assert home._inactive_location_site_ids([inactive_row]) == {"xyz"}
    assert home._inactive_location_site_ids([active_row]) == set()


# ---------------------------------------------------------------------------
# #4 single capture affordance
# ---------------------------------------------------------------------------

def test_single_capture_affordance_label_appears_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The "Capture observation" label appears exactly once; the full voice card
    is behind the quick-capture <details>, not dominating the landing."""
    _install_home(monkeypatch, _data_rich_rows())

    status, _content_type, body = request_text("GET", "/", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    # The label appears once — the header button.
    assert body.count("Capture observation") == 1
    assert ">Capture observation</a>" in body
    # The voice card is gated behind the quick-capture <details> "Text or voice
    # note" summary, not the landing's leading content.
    capture = _capture_group(body)
    assert '<details class="detail-block" id="quick-capture">' in capture
    assert "<summary>Text or voice note</summary>" in capture
    assert '<section><form id="captureForm"' in capture
    # The label is NOT duplicated inside the capture surface itself.
    assert capture.count("Capture observation") == 0
