from __future__ import annotations

import datetime as dt

from event_pipeline import visit_coverage


OP = "op_riley"


def _visit(site_id: str, site: str, date: str, *, visit_type: str = "qc", operator: str = OP):
    return {
        "type": "visit",
        "site_id": site_id,
        "site": site,
        "date": date,
        "visit_type": visit_type,
        "operator": operator,
    }


def _accounts():
    return [
        {"type": "site", "site_id": "9001", "site_name": "Atlas Lab", "status": "active"},
    ]


def _utc_week_bounds(day: dt.date) -> tuple[str, str]:
    week_start = day - dt.timedelta(days=day.weekday())
    return week_start.isoformat(), (week_start + dt.timedelta(days=6)).isoformat()


def test_tuesday_evening_qc_counts_for_local_week_from_utc_instant():
    now = dt.datetime(2026, 6, 24, 1, 30, 32, tzinfo=dt.timezone.utc)
    visits = [_visit("9001", "Atlas Lab", "2026-06-23")]

    result = visit_coverage.weekly_qc_count(OP, now=now, visits=visits)

    assert result["week_start"] == "2026-06-22"
    assert result["week_end"] == "2026-06-28"
    assert result["count"] == 1
    assert result["completed"][0]["date"] == "2026-06-23"


def test_sunday_night_utc_boundary_uses_local_week_not_next_utc_week():
    sunday = "2026-06-21"
    now = dt.datetime(2026, 6, 22, 1, 0, 0, tzinfo=dt.timezone.utc)
    visits = [_visit("9001", "Atlas Lab", sunday)]

    result = visit_coverage.weekly_qc_count(OP, now=now, visits=visits)

    assert result["week_start"] == "2026-06-15"
    assert result["week_end"] == "2026-06-21"
    assert result["count"] == 1
    assert result["completed"][0]["date"] == sunday

    utc_week_start, utc_week_end = _utc_week_bounds(now.date())
    assert (utc_week_start, utc_week_end) == ("2026-06-22", "2026-06-28")
    assert not (utc_week_start <= sunday <= utc_week_end)


def test_naive_datetime_anchor_matches_date_anchor_without_timezone_shift():
    naive_now = dt.datetime(2026, 6, 21, 21, 0, 0)
    visits = [_visit("9001", "Atlas Lab", "2026-06-21")]

    datetime_result = visit_coverage.weekly_qc_count(OP, now=naive_now, visits=visits)
    date_result = visit_coverage.weekly_qc_count(OP, now=naive_now.date(), visits=visits)

    assert datetime_result["week_start"] == date_result["week_start"] == "2026-06-15"
    assert datetime_result["week_end"] == date_result["week_end"] == "2026-06-21"
    assert datetime_result["count"] == date_result["count"] == 1


def test_date_and_date_string_anchor_are_unchanged():
    visits = [_visit("9001", "Atlas Lab", "2026-06-23")]

    date_result = visit_coverage.weekly_qc_count(
        OP, now=dt.date(2026, 6, 23), visits=visits
    )
    string_result = visit_coverage.weekly_qc_count(OP, now="2026-06-23", visits=visits)

    assert date_result == string_result
    assert date_result["week_start"] == "2026-06-22"
    assert date_result["week_end"] == "2026-06-28"
    assert date_result["count"] == 1


def test_account_coverage_as_of_localizes_utc_boundary_to_sunday():
    now = dt.datetime(2026, 6, 22, 1, 0, 0, tzinfo=dt.timezone.utc)
    visits = [_visit("9001", "Atlas Lab", "2026-06-21")]

    result = visit_coverage.account_coverage(
        OP, now=now, visits=visits, visit_gaps=[], accounts=_accounts()
    )

    row = result["accounts"][0]
    assert row["site_id"] == "9001"
    assert row["last_visit_date"] == "2026-06-21"
    assert row["last_qc_date"] == "2026-06-21"
    assert row["days_since_last_visit"] == 0
    assert row["days_since_last_qc"] == 0
