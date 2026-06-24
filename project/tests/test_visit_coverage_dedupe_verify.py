from __future__ import annotations

import datetime as dt

from event_pipeline import visit_coverage


# Synthetic operator + accounts only. No real B&T data.
OP = "op_riley"
NOW = dt.datetime(2034, 7, 19, 12, 0, 0, tzinfo=dt.timezone.utc)


def _visit(
    site_id,
    site,
    date,
    *,
    visit_type="qc",
    operator=OP,
    evidence=None,
    timestamp=None,
    timestamp_local=None,
    archived=None,
):
    doc = {
        "type": "visit",
        "site_id": site_id,
        "site": site,
        "date": date,
        "operator": operator,
    }
    if visit_type is not None:
        doc["visit_type"] = visit_type
    if evidence is not None:
        doc["evidence"] = evidence
    if timestamp is not None:
        doc["timestamp"] = timestamp
    if timestamp_local is not None:
        doc["timestamp_local"] = timestamp_local
    if archived is not None:
        doc["archived"] = archived
    return doc


def _accounts():
    return [
        {"type": "site", "site_id": "8101", "site_name": "Helio Plant", "status": "active"},
        {"type": "site", "site_id": "8102", "site_name": "Nova Depot", "status": "active"},
        {"type": "site", "site_id": "8103", "site_name": "Aster Yard", "status": "active"},
    ]


def test_weekly_qc_dedupes_same_operator_site_date_and_prefers_canonical_site():
    visits = [
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="QC"),
        _visit("8101", "8101", "2034-07-17", visit_type="qc_inspection"),
    ]

    weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert weekly["count"] == 1
    assert weekly["completed"] == [
        {"site_id": "8101", "site": "Helio Plant", "date": "2034-07-17", "evidence": None}
    ]

    coverage = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in coverage["accounts"]}
    assert by_site["8101"]["last_qc_date"] == "2034-07-17"


def test_weekly_qc_completed_has_one_row_per_site_date_credit():
    visits = [
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="qc", evidence="a"),
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="QC", evidence="ab"),
        _visit("8101", "8101", "2034-07-17", visit_type="qc_inspection", evidence="abc"),
        _visit("8102", "Nova Depot", "2034-07-18", visit_type="qc"),
    ]

    weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert weekly["count"] == 2
    assert [(row["site_id"], row["date"]) for row in weekly["completed"]] == [
        ("8101", "2034-07-17"),
        ("8102", "2034-07-18"),
    ]


def test_generic_visit_same_site_date_does_not_merge_or_suppress_qc_credit():
    visits = [
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="employee_support"),
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="qc"),
        _visit("8102", "Nova Depot", "2034-07-18", visit_type=None),
    ]

    weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert weekly["count"] == 1
    assert weekly["completed"][0]["site_id"] == "8101"
    assert weekly["completed"][0]["date"] == "2034-07-17"


def test_archived_visits_are_ignored_in_memory_and_couch_selector_excludes_them(monkeypatch):
    visits = [
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="qc", archived=True),
        _visit("8102", "Nova Depot", "2034-07-18", visit_type="QC", archived=True),
        _visit("8102", "Nova Depot", "2034-07-18", visit_type="qc_inspection"),
    ]

    weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert weekly["count"] == 1
    assert weekly["completed"][0]["site_id"] == "8102"

    coverage = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in coverage["accounts"]}
    assert by_site["8101"]["last_qc_date"] is None
    assert by_site["8102"]["last_qc_date"] == "2034-07-18"

    captured = {}

    def fake_find(selector, *, config=None, limit=visit_coverage.DEFAULT_FIND_LIMIT):
        captured["selector"] = selector
        return []

    monkeypatch.setattr(visit_coverage, "_find_vault_docs", fake_find)
    visit_coverage._load_visit_docs(
        OP,
        start_date=dt.date(2034, 7, 17),
        end_date=dt.date(2034, 7, 19),
        config=object(),
    )
    assert captured["selector"]["type"] == "visit"
    assert captured["selector"]["archived"] == {"$ne": True}


def test_weekly_qc_tiebreak_prefers_latest_timestamp_independent_of_input_order():
    early = _visit(
        "8103",
        "Aster Yard Early",
        "2034-07-17",
        visit_type="qc",
        evidence="same",
        timestamp="2034-07-17T13:00:00+00:00",
    )
    late = _visit(
        "8103",
        "Aster Yard Late",
        "2034-07-17",
        visit_type="qc_inspection",
        evidence="same",
        timestamp="2034-07-17T15:00:00+00:00",
    )

    for visits in ([early, late], [late, early]):
        weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
        assert weekly["count"] == 1
        assert weekly["completed"][0]["site_id"] == "8103"
        assert weekly["completed"][0]["evidence"] == "same"
        assert weekly["completed"][0]["site"] == "Aster Yard Late"


def test_account_coverage_single_last_qc_date_with_many_duplicate_same_site_date_qcs():
    # Spec req B: duplicate same-site/date QC docs must collapse to ONE last_qc_date.
    # Three duplicate QC docs on 07-17 and two on 07-18 (both in the current ISO week).
    visits = [
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="qc"),
        _visit("8101", "8101", "2034-07-17", visit_type="qc_inspection"),
        _visit("8101", "Helio Plant", "2034-07-17", visit_type="QC", evidence="x"),
        _visit("8101", "Helio Plant", "2034-07-18", visit_type="qc"),
        _visit("8101", "8101", "2034-07-18", visit_type="qc_inspection"),
    ]

    coverage = visit_coverage.account_coverage(
        OP, now=NOW, visits=visits, visit_gaps=[], accounts=_accounts()
    )
    by_site = {row["site_id"]: row for row in coverage["accounts"]}
    # Single scalar last_qc_date == the latest QC date, not a list / not inflated.
    assert by_site["8101"]["last_qc_date"] == "2034-07-18"

    # And the weekly view credits exactly two (site,date) coverage credits.
    weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
    assert weekly["count"] == 2
    assert [(r["site_id"], r["date"]) for r in weekly["completed"]] == [
        ("8101", "2034-07-17"),
        ("8101", "2034-07-18"),
    ]


def test_site_equal_to_site_id_is_non_canonical_and_loses_regardless_of_order():
    # A row whose `site` equals the bare `site_id` is NOT a canonical name and
    # must lose rule 1 to a row carrying a real site name, in either input order.
    bare = _visit("8101", "8101", "2034-07-17", visit_type="qc")
    named = _visit("8101", "Helio Plant", "2034-07-17", visit_type="qc_inspection")

    for visits in ([bare, named], [named, bare]):
        weekly = visit_coverage.weekly_qc_count(OP, now=NOW, visits=visits)
        assert weekly["count"] == 1
        assert weekly["completed"][0]["site"] == "Helio Plant"
