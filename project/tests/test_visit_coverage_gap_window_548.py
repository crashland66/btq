"""Independent verifier tests for open-gaps-computed-and-windowed (548).

Contract under test (event_pipeline.visit_coverage / ops_dashboard.sections.home):

  * A gap for (site, date) is OPEN iff: (a) no non-archived ``type: visit``
    doc exists for that site_id on that date, for ANY operator; (b) date is
    within the last ``OPEN_GAP_WINDOW_DAYS`` (30) days inclusive of ``as_of``;
    (c) the gap is not explicitly closed (``status``/``resolved_at``/
    ``closed_at``).
  * Closure is computed at read time from the visit universe; no gap doc is
    ever written/edited/deleted.
  * ``OPEN_GAP_WINDOW_DAYS == 30`` (a module constant distinct from
    ``OVERDUE_QC_DAYS``, even though both equal 30 today).
  * ``coverage_report()["gap_window_days"] == 30``.
  * The vault-loading path issues a windowed ``visit_gap`` selector and a
    SECOND, narrower ``type: visit`` selector (no operator ``$or``) limited
    to the sites that have in-window gaps -- and skips that second query
    entirely when there are no in-window gaps.
  * The home panel names the window in its subline and empty state, and its
    heading count and Overdue column are unaffected.

Synthetic operator/site ids only ("op_sandbox"/"op_other", "4001".."4009",
"Sandy Sandbox") -- this repo is public, no real B&T identities.
"""

from __future__ import annotations

import datetime as dt

from event_pipeline import visit_coverage
from ops_dashboard.sections import home


OP = "op_sandbox"
OTHER_OP = "op_other"

# Deterministic "now" (D), synthetic far-future date to avoid any confusion
# with live sandbox data.
D = dt.date(2031, 6, 15)
NOW = D.isoformat()

D_MINUS_3 = (D - dt.timedelta(days=3)).isoformat()
D_MINUS_5 = (D - dt.timedelta(days=5)).isoformat()
D_MINUS_30 = (D - dt.timedelta(days=30)).isoformat()
D_MINUS_31 = (D - dt.timedelta(days=31)).isoformat()
D_PLUS_1 = (D + dt.timedelta(days=1)).isoformat()

SITE_A = "4001"
SITE_B = "4002"
SITE_C = "4003"


def _gap(site_id, date, **extra):
    doc = {"type": "visit_gap", "site_id": site_id, "site": f"Site {site_id}", "date": date}
    doc.update(extra)
    return doc


def _visit(site_id, date, *, operator=OP, archived=None, visit_type=None):
    doc = {
        "type": "visit",
        "site_id": site_id,
        "site": f"Site {site_id}",
        "date": date,
        "operator": operator,
    }
    if archived is not None:
        doc["archived"] = archived
    if visit_type is not None:
        doc["visit_type"] = visit_type
    return doc


def _accounts(site_ids):
    return [
        {"type": "site", "site_id": sid, "site_name": f"Site {sid}", "status": "active"}
        for sid in site_ids
    ]


# ==================================================================
# Part 1 -- fixture: closure + window, injected visits/visit_gaps
# ==================================================================


def test_gap_fixture_closure_by_any_operator_and_window_bounds():
    gaps = [
        _gap(SITE_A, D_MINUS_3),  # no visit -> stays open
        _gap(SITE_B, D_MINUS_3),  # closed by op_other's same-day visit
        _gap(SITE_A, D_MINUS_5),  # only an ARCHIVED visit that day -> stays open
        _gap(SITE_A, D_MINUS_31),  # outside the 30-day window -> absent
        _gap(SITE_C, D_MINUS_30),  # exactly the window boundary (inclusive) -> open
        _gap(SITE_C, D_PLUS_1),  # in the future relative to as_of -> absent
    ]
    visits = [
        # closes the B@D-3 gap even though it's a DIFFERENT operator
        _visit(SITE_B, D_MINUS_3, operator=OTHER_OP),
        # an archived visit on A@D-5 must NOT close that gap
        _visit(SITE_A, D_MINUS_5, operator=OP, archived=True),
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=gaps, accounts=_accounts([SITE_A, SITE_B, SITE_C])
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}

    # A: D-3 (no visit) and D-5 (archived-only) both stay open; D-31 absent.
    assert by_site[SITE_A]["open_gap_dates"] == [D_MINUS_5, D_MINUS_3]

    # B: the op_other same-day visit closes the only gap -> empty.
    assert by_site[SITE_B]["open_gap_dates"] == []
    # ...and that closure visit (another operator) must NOT set this
    # operator's last_visit_date -- last-visit attribution is unchanged.
    assert by_site[SITE_B]["last_visit_date"] is None

    # C: D-30 (inclusive boundary) stays open; D+1 (future) is absent.
    assert by_site[SITE_C]["open_gap_dates"] == [D_MINUS_30]

    # gaps flattened == union of the rows' open_gap_dates.
    report = visit_coverage.coverage_report(
        OP, now=NOW, visits=visits, visit_gaps=gaps, accounts=_accounts([SITE_A, SITE_B, SITE_C])
    )
    flattened_dates = sorted(
        (row["site_id"], date) for row in by_site.values() for date in row["open_gap_dates"]
    )
    report_dates = sorted((g["site_id"], g["date"]) for g in report["gaps"])
    assert flattened_dates == report_dates
    assert flattened_dates == [
        (SITE_A, D_MINUS_5),
        (SITE_A, D_MINUS_3),
        (SITE_C, D_MINUS_30),
    ]


def test_explicit_status_closed_and_resolved_at_still_honored_with_no_visit():
    # Even with NO visit at all on that date, an explicitly-closed gap must
    # stay excluded -- explicit closure is not overridden by the new rule.
    gaps = [
        _gap(SITE_A, D_MINUS_3, status="closed"),
        _gap(SITE_A, D_MINUS_5, resolved_at=f"{D_MINUS_5}T12:00:00Z"),
        _gap(SITE_A, D_MINUS_30),  # no explicit closure, no visit -> stays open
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=[], visit_gaps=gaps, accounts=_accounts([SITE_A])
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site[SITE_A]["open_gap_dates"] == [D_MINUS_30]


def test_window_boundary_off_by_one_d30_in_d31_out():
    gaps = [
        _gap(SITE_A, D_MINUS_30),
        _gap(SITE_A, D_MINUS_31),
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=[], visit_gaps=gaps, accounts=_accounts([SITE_A])
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site[SITE_A]["open_gap_dates"] == [D_MINUS_30]


# ==================================================================
# Part 2 -- constants and report key
# ==================================================================


def test_open_gap_window_days_constant_is_30_and_distinct_from_overdue():
    assert visit_coverage.OPEN_GAP_WINDOW_DAYS == 30
    assert visit_coverage.OVERDUE_QC_DAYS == 30
    # They are separate module constants even though both equal 30 today.
    assert "OPEN_GAP_WINDOW_DAYS" in vars(visit_coverage)
    assert "OVERDUE_QC_DAYS" in vars(visit_coverage)


def test_coverage_report_exposes_gap_window_days():
    report = visit_coverage.coverage_report(
        OP, now=NOW, visits=[], visit_gaps=[], accounts=_accounts([SITE_A])
    )
    assert report["gap_window_days"] == 30
    # 547's overdue threshold key is untouched by this change.
    assert report["overdue_qc_days"] == 30


# ==================================================================
# Part 3 -- vault-loading path: selectors issued by _find_vault_docs
# ==================================================================


def test_vault_path_gap_selector_is_windowed(monkeypatch):
    calls: list[dict] = []

    def fake_find(selector, *, config=None):
        calls.append(selector)
        return []

    monkeypatch.setattr(visit_coverage, "_find_vault_docs", fake_find)
    visit_coverage.coverage_report(OP, now=NOW, accounts=_accounts([SITE_A, SITE_B]))

    gap_selectors = [c for c in calls if c.get("type") == "visit_gap"]
    assert len(gap_selectors) == 1
    date_selector = gap_selectors[0]["date"]
    assert date_selector["$gte"] == D_MINUS_30
    assert date_selector["$lte"] == D.isoformat()


def test_vault_path_second_visit_selector_has_no_or_and_is_gap_site_scoped(monkeypatch):
    calls: list[dict] = []

    def fake_find(selector, *, config=None):
        calls.append(selector)
        if selector.get("type") == "visit_gap":
            # one in-window gap, only on SITE_A
            return [_gap(SITE_A, D_MINUS_3)]
        return []

    monkeypatch.setattr(visit_coverage, "_find_vault_docs", fake_find)
    visit_coverage.coverage_report(OP, now=NOW, accounts=_accounts([SITE_A, SITE_B]))

    visit_selectors = [c for c in calls if c.get("type") == "visit"]
    # exactly two type:visit queries: the operator-prefiltered _load_visit_docs
    # (has $or) and the narrower any-operator closure query (no $or).
    assert len(visit_selectors) == 2
    with_or = [s for s in visit_selectors if "$or" in s]
    without_or = [s for s in visit_selectors if "$or" not in s]
    assert len(with_or) == 1
    assert len(without_or) == 1
    closure_selector = without_or[0]
    assert closure_selector["site_id"]["$in"] == [SITE_A]  # gap sites only, not SITE_B
    assert closure_selector["date"]["$gte"] == D_MINUS_30
    assert closure_selector["date"]["$lte"] == D.isoformat()


def test_vault_path_no_closure_query_when_no_in_window_gaps(monkeypatch):
    calls: list[dict] = []

    def fake_find(selector, *, config=None):
        calls.append(selector)
        if selector.get("type") == "visit_gap":
            # only an out-of-window gap; nothing in-window survives the
            # in-memory window re-check.
            return [_gap(SITE_A, D_MINUS_31)]
        return []

    monkeypatch.setattr(visit_coverage, "_find_vault_docs", fake_find)
    visit_coverage.coverage_report(OP, now=NOW, accounts=_accounts([SITE_A, SITE_B]))

    visit_selectors_without_or = [
        c for c in calls if c.get("type") == "visit" and "$or" not in c
    ]
    assert visit_selectors_without_or == []


# ==================================================================
# Part 4 -- home panel copy
# ==================================================================


def _report(gaps, *, gap_window_days=30):
    return {
        "weekly": {"count": 2, "target": 4, "remaining": 2, "completed": []},
        "overdue": [
            {"site_id": "4003", "site_name": "Cedar Commons", "days_since_last_qc": 12},
            {"site_id": "4004", "site_name": "Dale Center", "days_since_last_qc": None},
        ],
        "overdue_qc_days": 30,
        "gap_window_days": gap_window_days,
        "gaps": gaps,
    }


def test_panel_subline_names_the_window_in_days():
    html = home._render_coverage_panel(_report([]))
    assert "Activity logged with no visit recorded that day, last 30 days" in html


def test_panel_empty_state_names_the_window():
    html = home._render_coverage_panel(_report([]))
    assert "No open visit gaps in the last 30 days." in html


def test_panel_reads_gap_window_days_from_report_not_hardcoded():
    # Guards against a panel that hardcodes "30" instead of reading the
    # report's gap_window_days -- if this ever diverges from the module
    # constant, the panel must reflect what the report actually says.
    html = home._render_coverage_panel(_report([], gap_window_days=45))
    assert "Activity logged with no visit recorded that day, last 45 days" in html
    assert "No open visit gaps in the last 45 days." in html


def test_panel_falls_back_to_30_days_when_report_omits_gap_window_days():
    report = _report([])
    del report["gap_window_days"]
    html = home._render_coverage_panel(report)
    assert "No open visit gaps in the last 30 days." in html
    assert "Activity logged with no visit recorded that day, last 30 days" in html


def test_panel_heading_counts_report_gaps():
    gaps = [
        {"site_id": SITE_A, "site_name": "Site 4001", "date": D_MINUS_3},
        {"site_id": SITE_C, "site_name": "Site 4003", "date": D_MINUS_30},
    ]
    html = home._render_coverage_panel(_report(gaps))
    assert "Open visit gaps · 2" in html
    assert "Site 4001" in html
    assert D_MINUS_3 in html


def test_panel_overdue_column_output_unchanged():
    # Regression: the gap-window change must not touch the Overdue column.
    html = home._render_coverage_panel(_report([]))
    assert "Overdue accounts · 2" in html
    assert "Cedar Commons" in html
    assert "12d since last QC" in html
    assert "Dale Center" in html
    assert "never" in html
