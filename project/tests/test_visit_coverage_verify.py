from __future__ import annotations

import datetime as dt

from event_pipeline import visit_coverage


# Synthetic operator + accounts. No real B&T / Interfuse / PHN / Liberty / people.
OP = "op_pat"

# A deterministic "now": Wednesday 2031-05-14 (ISO week Mon 2031-05-12 .. Sun 2031-05-18).
NOW = dt.datetime(2031, 5, 14, 9, 0, 0, tzinfo=dt.timezone.utc)
WEEK_START = "2031-05-12"
WEEK_END = "2031-05-18"


def _visit(site_id, site, date, *, visit_type=None, operator=OP, visited_by=None):
    doc = {
        "type": "visit",
        "site_id": site_id,
        "site": site,
        "date": date,
        "operator": operator,
    }
    if visit_type is not None:
        doc["visit_type"] = visit_type
    if visited_by is not None:
        doc["visited_by"] = visited_by
    return doc


# -------------------- weekly_qc_count --------------------


def test_weekly_qc_count_only_qc_this_operator_this_week_and_not_future():
    visits = [
        # this operator, this week, lower "qc" -> counts
        _visit("4001", "Acme North", "2031-05-12", visit_type="qc"),
        # this operator, this week, uppercase "QC" -> counts (case-insensitive)
        _visit("4002", "Acme South", "2031-05-13", visit_type="QC"),
        # this operator, this week, null visit_type -> NOT a qc
        _visit("4003", "Acme East", "2031-05-13", visit_type=None),
        # this operator, this week, other visit_type -> NOT a qc
        _visit("4004", "Acme West", "2031-05-14", visit_type="walkthrough"),
        # this operator, this week, FUTURE qc (after now) -> excluded
        _visit("4005", "Acme Future", "2031-05-15", visit_type="qc"),
        # other operator, this week, qc -> excluded
        _visit("4006", "Other Co", "2031-05-12", visit_type="qc", operator="op_other"),
        # this operator, PRIOR week, qc -> excluded
        _visit("4007", "Acme Old", "2031-05-05", visit_type="qc"),
    ]
    result = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)

    assert result["week_start"] == WEEK_START
    assert result["week_end"] == WEEK_END
    assert result["count"] == 2
    assert result["target"] == 4
    assert result["remaining"] == 2

    sites = {row["site_id"] for row in result["completed"]}
    assert sites == {"4001", "4002"}
    # the uppercase one is included -> case-insensitivity proven
    assert "4002" in sites
    # the future qc is excluded
    assert "4005" not in sites


def test_weekly_qc_count_remaining_floor_and_full_completion():
    visits = [
        _visit("4001", "Acme North", "2031-05-12", visit_type="qc"),
        _visit("4002", "Acme South", "2031-05-13", visit_type="qc"),
        _visit("4003", "Acme East", "2031-05-14", visit_type="qc"),
        _visit("4004", "Acme West", "2031-05-14", visit_type="qc"),
        _visit("4008", "Acme Plus", "2031-05-14", visit_type="qc"),
    ]
    result = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert result["count"] == 5
    assert result["remaining"] == 0  # floored at 0, never negative


def test_weekly_qc_count_operator_isolation():
    visits = [
        _visit("4001", "Acme North", "2031-05-12", visit_type="qc", operator="op_other"),
        _visit("4002", "Acme South", "2031-05-13", visit_type="qc", operator="op_other"),
    ]
    result = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert result["count"] == 0
    assert result["completed"] == []


# -------------------- account_coverage --------------------


def _accounts():
    return [
        {"type": "site", "site_id": "4001", "site_name": "Acme North", "status": "active"},
        {"type": "site", "site_id": "4002", "site_name": "Acme South", "status": "active"},
        {"type": "site", "site_id": "4003", "site_name": "Acme East", "status": "active"},
    ]


def test_account_coverage_last_visit_last_qc_and_days_since():
    visits = [
        # 4001: recent qc 4 days ago + a generic visit 2 days ago
        _visit("4001", "Acme North", "2031-05-10", visit_type="qc"),
        _visit("4001", "Acme North", "2031-05-12", visit_type="walkthrough"),
        # 4002: a generic visit but NO qc ever
        _visit("4002", "Acme South", "2031-05-11", visit_type="walkthrough"),
        # 4003: no visits at all
    ]
    result = visit_coverage.account_coverage(OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts())
    by_site = {row["site_id"]: row for row in result["accounts"]}

    # all three accounts present even though 4003 has no visits
    assert set(by_site) == {"4001", "4002", "4003"}

    a1 = by_site["4001"]
    assert a1["last_visit_date"] == "2031-05-12"
    assert a1["last_qc_date"] == "2031-05-10"
    assert a1["days_since_last_visit"] == 2
    assert a1["days_since_last_qc"] == 4

    a2 = by_site["4002"]
    assert a2["last_visit_date"] == "2031-05-11"
    assert a2["last_qc_date"] is None
    assert a2["days_since_last_visit"] == 3
    assert a2["days_since_last_qc"] is None

    a3 = by_site["4003"]
    assert a3["last_visit_date"] is None
    assert a3["last_qc_date"] is None
    assert a3["days_since_last_visit"] is None
    assert a3["days_since_last_qc"] is None


def test_account_coverage_overdue_never_qc_first_then_oldest_qc():
    # RECONCILED for coverage-operator-match (547): `overdue` now excludes
    # rows within OVERDUE_QC_DAYS (30). The original 1-day/10-day QCs no
    # longer qualify as overdue at all, so both visits are pushed past the
    # 30-day threshold (35 and 45 days since qc) to keep exercising the
    # never-first-then-oldest ordering.
    visits = [
        _visit("4001", "Acme North", "2031-04-09", visit_type="qc"),   # 35 days since qc
        _visit("4002", "Acme South", "2031-03-30", visit_type="qc"),   # 45 days since qc
        # 4003: never qc'd
    ]
    result = visit_coverage.account_coverage(OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts())
    overdue_order = [row["site_id"] for row in result["overdue"]]

    # never-QC'd (4003) is most overdue -> first; then oldest qc (4002); then recent (4001)
    assert overdue_order[0] == "4003"
    assert overdue_order.index("4002") < overdue_order.index("4001")


def test_account_coverage_open_gap_dates_from_injected_gaps():
    gaps = [
        {"type": "visit_gap", "site_id": "4001", "date": "2031-05-06"},
        {"type": "visit_gap", "site_id": "4001", "date": "2031-05-09"},
        # closed gap -> excluded
        {"type": "visit_gap", "site_id": "4001", "date": "2031-05-02", "status": "closed"},
        # resolved gap -> excluded
        {"type": "visit_gap", "site_id": "4002", "date": "2031-05-07", "resolved_at": "2031-05-08"},
        # future gap -> excluded
        {"type": "visit_gap", "site_id": "4002", "date": "2031-06-01"},
        # open gap for 4002
        {"type": "visit_gap", "site_id": "4002", "date": "2031-05-05"},
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=[], visit_gaps=gaps, accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site["4001"]["open_gap_dates"] == ["2031-05-06", "2031-05-09"]
    assert by_site["4002"]["open_gap_dates"] == ["2031-05-05"]
    assert by_site["4003"]["open_gap_dates"] == []


def test_account_coverage_operator_isolation():
    visits = [
        # other operator visited/qc'd 4001 -> must NOT leak in
        _visit("4001", "Acme North", "2031-05-13", visit_type="qc", operator="op_other"),
    ]
    result = visit_coverage.account_coverage(OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts())
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site["4001"]["last_visit_date"] is None
    assert by_site["4001"]["last_qc_date"] is None


def test_account_coverage_visited_by_alias_matches_operator():
    # operator id op_pat must match a visit recorded under visited_by="Pat Manager"
    visits = [
        {
            "type": "visit",
            "site_id": "4001",
            "site": "Acme North",
            "date": "2031-05-12",
            "visit_type": "qc",
            "visited_by": "Pat Manager",
        }
    ]
    result = visit_coverage.account_coverage(OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts())
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site["4001"]["last_qc_date"] == "2031-05-12"


# -------------------- coverage_report --------------------


def test_coverage_report_combines_weekly_accounts_overdue_and_gaps():
    visits = [
        _visit("4001", "Acme North", "2031-05-12", visit_type="qc"),
        _visit("4002", "Acme South", "2031-05-13", visit_type="qc"),
        _visit("4002", "Acme South", "2031-05-04", visit_type="qc"),
    ]
    gaps = [
        {"type": "visit_gap", "site_id": "4003", "date": "2031-05-06"},
    ]
    report = visit_coverage.coverage_report(
        OP, now=NOW, visits=visits, visit_gaps=gaps, accounts=_accounts()
    )

    assert report["operator"] == OP
    assert "generated_at" in report

    # weekly: 4001 and 4002 qc'd this week
    assert report["weekly"]["count"] == 2
    assert report["weekly"]["remaining"] == 2

    # accounts present for all three
    assert {row["site_id"] for row in report["accounts"]} == {"4001", "4002", "4003"}

    # overdue: 4003 never qc'd -> most overdue
    assert report["overdue"][0]["site_id"] == "4003"

    # gaps flattened coherently
    assert report["gaps"] == [
        {"site_id": "4003", "site_name": "Acme East", "date": "2031-05-06"}
    ]


def test_coverage_report_injected_now_controls_week_bounds():
    report = visit_coverage.coverage_report(OP, now=NOW, visits=[], visit_gaps=[], accounts=_accounts())
    assert report["weekly"]["week_start"] == WEEK_START
    assert report["weekly"]["week_end"] == WEEK_END
