from __future__ import annotations

import datetime as dt

import pytest

from event_pipeline import visit_coverage


# Independent-verifier tests for the QC-undercount fix:
# the coverage read model now counts BOTH visit_type "qc" and "qc_inspection"
# (the capture pipeline's QC marker). All fixtures below are SYNTHETIC —
# invented operator/sites/dates. No real B&T / Interfuse / PHN / Liberty data.

OP = "op_quinn"

# Deterministic "now": Wednesday 2032-03-17 (ISO week Mon 2032-03-15 .. Sun 2032-03-21).
NOW = dt.datetime(2032, 3, 17, 9, 0, 0, tzinfo=dt.timezone.utc)
WEEK_START = "2032-03-15"
WEEK_END = "2032-03-21"


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


def _accounts():
    return [
        {"type": "site", "site_id": "7001", "site_name": "Vega Plant", "status": "active"},
        {"type": "site", "site_id": "7002", "site_name": "Lyra Depot", "status": "active"},
        {"type": "site", "site_id": "7003", "site_name": "Orion Hub", "status": "active"},
    ]


# -------------------- is_qc_visit_type --------------------


@pytest.mark.parametrize(
    "value",
    [
        "qc",
        "QC",
        "qc_inspection",
        "Qc_Inspection",
        " qc ",            # leading/trailing whitespace stripped
        " qc_inspection ",
        "QC_INSPECTION",
    ],
)
def test_is_qc_visit_type_truthy(value):
    assert visit_coverage.is_qc_visit_type(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "service_completion",
        None,
        "",
        "visit",
        "qc_x",            # superset must NOT loosen to substring/prefix match
        "inspection",
        "qcinspection",   # missing underscore -> not a member
        "walkthrough",
    ],
)
def test_is_qc_visit_type_falsy(value):
    assert visit_coverage.is_qc_visit_type(value) is False


# -------------------- weekly_qc_count (the regression-fixing assertion) --------------------


def test_weekly_qc_count_counts_qc_inspection_alongside_qc():
    visits = [
        # this operator, this week, "qc" -> counts
        _visit("7001", "Vega Plant", "2032-03-15", visit_type="qc"),
        # this operator, this week, "qc_inspection" -> NOW counts (the undercount fix)
        _visit("7002", "Lyra Depot", "2032-03-16", visit_type="qc_inspection"),
        # mixed-case capture marker -> counts (case-insensitive)
        _visit("7003", "Orion Hub", "2032-03-16", visit_type="Qc_Inspection"),
        # service_completion -> NOT counted
        _visit("7004", "Pavo Annex", "2032-03-17", visit_type="service_completion"),
        # null visit_type -> NOT counted
        _visit("7005", "Cetus Yard", "2032-03-17", visit_type=None),
        # future qc_inspection (after now) -> excluded
        _visit("7006", "Aries Lot", "2032-03-19", visit_type="qc_inspection"),
        # other operator qc_inspection -> excluded
        _visit("7007", "Draco Site", "2032-03-15", visit_type="qc_inspection", operator="op_else"),
        # prior week qc_inspection -> excluded
        _visit("7008", "Hydra Site", "2032-03-08", visit_type="qc_inspection"),
    ]
    result = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)

    assert result["week_start"] == WEEK_START
    assert result["week_end"] == WEEK_END
    # 3 counted: one "qc" + two "qc_inspection" (one mixed-case)
    assert result["count"] == 3
    assert result["target"] == 4
    assert result["remaining"] == 1

    sites = {row["site_id"] for row in result["completed"]}
    assert sites == {"7001", "7002", "7003"}
    # The capture-pipeline qc_inspection visit is now counted (regression fix):
    assert "7002" in sites
    assert "7003" in sites
    # service_completion / null / future / other-op / other-week all excluded:
    assert sites.isdisjoint({"7004", "7005", "7006", "7007", "7008"})


def test_weekly_qc_count_qc_inspection_only_still_counts():
    # A week with ONLY capture-detected qc_inspection visits must not be zero.
    visits = [
        _visit("7001", "Vega Plant", "2032-03-15", visit_type="qc_inspection"),
        _visit("7002", "Lyra Depot", "2032-03-16", visit_type="qc_inspection"),
    ]
    result = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert result["count"] == 2
    assert {row["site_id"] for row in result["completed"]} == {"7001", "7002"}


# -------------------- account_coverage --------------------


def test_account_coverage_last_qc_date_reflects_qc_inspection():
    visits = [
        # 7001: only a qc_inspection -> last_qc_date must be set (was the undercount bug)
        _visit("7001", "Vega Plant", "2032-03-10", visit_type="qc_inspection"),
        # 7002: a service_completion (generic) -> visit recorded, but NO qc
        _visit("7002", "Lyra Depot", "2032-03-11", visit_type="service_completion"),
        # 7003: a plain "qc" -> last_qc_date set
        _visit("7003", "Orion Hub", "2032-03-12", visit_type="qc"),
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}

    # qc_inspection counts as a QC:
    assert by_site["7001"]["last_qc_date"] == "2032-03-10"
    assert by_site["7001"]["last_visit_date"] == "2032-03-10"

    # service_completion is a visit but NOT a QC:
    assert by_site["7002"]["last_visit_date"] == "2032-03-11"
    assert by_site["7002"]["last_qc_date"] is None

    # plain qc still counts:
    assert by_site["7003"]["last_qc_date"] == "2032-03-12"


def test_account_coverage_latest_qc_inspection_wins_over_older_qc():
    visits = [
        _visit("7001", "Vega Plant", "2032-03-05", visit_type="qc"),
        # newer capture qc_inspection should become the last_qc_date
        _visit("7001", "Vega Plant", "2032-03-14", visit_type="qc_inspection"),
    ]
    result = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site["7001"]["last_qc_date"] == "2032-03-14"
