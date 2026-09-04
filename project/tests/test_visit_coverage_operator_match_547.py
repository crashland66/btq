"""Independent verifier tests for coverage-operator-match (547).

Covers two changes to ``event_pipeline.visit_coverage`` and
``ops_dashboard.sections.home``:

1. Attribution: visits stamped with the installation's document-stamping
   operator id (``btq_vault.entity_types.current_operator_id()``) now match
   regardless of free-text ``visited_by`` -- fixing the real-world case where
   all vault visit docs are stamped ``operator: op_greg`` but ``visited_by``
   is inconsistent free text ("Greg", "Greg Stoltz", "Gregory Stoltz", "").
2. Overdue threshold: ``account_coverage()["overdue"]`` now excludes rows
   with a QC within the last ``OVERDUE_QC_DAYS`` (30) days; ``accounts``
   still lists every active account; ``coverage_report()`` exposes
   ``overdue_qc_days``; the home panel surfaces the threshold in its subline
   and empty state.

Synthetic operator/site ids only ("sandbox_user"/"op_sandbox"/"SANDBOX" --
the canonical fake persona), no real B&T data.
"""

from __future__ import annotations

import datetime as dt
import inspect

from event_pipeline import visit_coverage
from ops_dashboard.sections import home


SANDBOX_OPERATOR_ID = "op_sandbox"


def _visit(site_id, date, *, visit_type="qc", operator=SANDBOX_OPERATOR_ID, visited_by=None, archived=None):
    doc = {
        "type": "visit",
        "site_id": site_id,
        "site": site_id,
        "date": date,
        "operator": operator,
    }
    if visit_type is not None:
        doc["visit_type"] = visit_type
    if visited_by is not None:
        doc["visited_by"] = visited_by
    if archived is not None:
        doc["archived"] = archived
    return doc


def _accounts(site_ids):
    return [
        {"type": "site", "site_id": sid, "site_name": f"Site {sid}", "status": "active"}
        for sid in site_ids
    ]


# -------------------- 1. operator-id stamp match --------------------


def test_operator_id_stamp_matches_regardless_of_visited_by_text(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    visits = [
        _visit("SANDBOX", "2026-08-01", visited_by="Sandy"),
        _visit("SANDBOX", "2026-08-02", visited_by=""),
        _visit("SANDBOX", "2026-08-03", visited_by="Sandy Sandbox"),
        _visit("SANDBOX", "2026-08-04", visited_by="Sandy (phone)"),
        # different operator stamp + a name that would not alias-match either
        # -> must NOT count
        _visit("SANDBOX", "2026-08-05", operator="op_other", visited_by="Someone Else"),
    ]
    result = visit_coverage.account_coverage(
        "sandbox_user",
        now="2026-08-10",
        visits=visits,
        visit_gaps=[],
        accounts=_accounts(["SANDBOX"]),
        snapshot={"operator": "Sandy Sandbox"},
    )
    row = result["accounts"][0]
    # Latest QC among the four op_sandbox-stamped visits is 2026-08-04; the
    # op_other visit on 2026-08-05 is excluded despite being later.
    assert row["last_qc_date"] == "2026-08-04"


def test_load_visit_docs_selector_or_clause_includes_current_operator_id(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    captured: dict[str, object] = {}

    def fake_find(selector, *, config=None):
        captured["selector"] = selector
        return []

    monkeypatch.setattr(visit_coverage, "_find_vault_docs", fake_find)
    visit_coverage._load_visit_docs("sandbox_user")

    selector = captured["selector"]
    or_values: list[str] = []
    for clause in selector["$or"]:
        for constraint in clause.values():
            or_values.extend(constraint.get("$in", []))
    assert "op_sandbox" in or_values


# -------------------- 2. name-variant + no-field-capture-path fixture --------------------


def test_last_qc_date_uses_latest_across_name_variants_brookville_shaped(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    visits = [
        _visit("SANDBOX", "2026-06-26", visited_by="Greg"),
        _visit("SANDBOX", "2026-07-13", visited_by="Greg Stoltz"),
        _visit("SANDBOX", "2026-08-14", visited_by="Gregory Stoltz"),
        _visit("VISITONLY", "2026-08-01", visit_type="site_visit", visited_by="Greg"),
        _visit("VISITONLY", "2026-08-05", visit_type=None, visited_by="Greg"),
        # NOVISITS: no visit docs at all
    ]
    result = visit_coverage.account_coverage(
        "sandbox_user",
        now="2026-09-01",
        visits=visits,
        visit_gaps=[],
        accounts=_accounts(["SANDBOX", "VISITONLY", "NOVISITS"]),
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}

    assert by_site["SANDBOX"]["last_qc_date"] == "2026-08-14"

    assert by_site["VISITONLY"]["last_qc_date"] is None
    assert by_site["VISITONLY"]["last_visit_date"] == "2026-08-05"

    assert by_site["NOVISITS"]["last_qc_date"] is None
    assert by_site["NOVISITS"]["last_visit_date"] is None


def test_visit_coverage_module_has_no_field_capture_or_qc_category_path():
    source = inspect.getsource(visit_coverage)
    assert "qc_category" not in source
    assert "field_capture" not in source


# -------------------- 3. overdue threshold --------------------


def test_overdue_threshold_boundary_all_accounts_and_module_constant(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    visits = [
        _visit("A30", "2026-08-04"),  # exactly 30 days before now -> NOT overdue
        _visit("A31", "2026-08-03"),  # 31 days before now -> overdue
        # NEVER: no visits at all -> overdue (never-QC'd)
    ]
    accounts = _accounts(["A30", "A31", "NEVER"])

    result = visit_coverage.account_coverage(
        "sandbox_user", now="2026-09-03", visits=visits, visit_gaps=[], accounts=accounts
    )
    by_site = {row["site_id"]: row for row in result["accounts"]}
    assert by_site["A30"]["days_since_last_qc"] == 30
    assert by_site["A31"]["days_since_last_qc"] == 31

    overdue_ids = [row["site_id"] for row in result["overdue"]]
    assert "A30" not in overdue_ids
    assert set(overdue_ids) == {"A31", "NEVER"}
    # never-first, then oldest
    assert overdue_ids[0] == "NEVER"
    assert overdue_ids[1] == "A31"

    # `accounts` always lists every active account regardless of overdue status
    assert {row["site_id"] for row in result["accounts"]} == {"A30", "A31", "NEVER"}

    report = visit_coverage.coverage_report(
        "sandbox_user", now="2026-09-03", visits=visits, visit_gaps=[], accounts=accounts
    )
    assert report["overdue_qc_days"] == 30

    assert visit_coverage.OVERDUE_QC_DAYS == 30
    monkeypatch.setattr(visit_coverage, "OVERDUE_QC_DAYS", 10)
    tightened = visit_coverage.account_coverage(
        "sandbox_user", now="2026-09-03", visits=visits, visit_gaps=[], accounts=accounts
    )
    tightened_ids = [row["site_id"] for row in tightened["overdue"]]
    # With the module constant lowered to 10 days, A30 (30 days) is now overdue too.
    assert "A30" in tightened_ids


# -------------------- 4. weekly_qc_count --------------------


def test_weekly_qc_count_matches_operator_stamp_regardless_of_visited_by_name(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    now = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)  # Wednesday
    visits = [
        _visit("SANDBOX", "2026-09-01", visited_by="Greg"),  # Tuesday, same ISO week
    ]
    result = visit_coverage.weekly_qc_count("sandbox_user", now=now, visits=visits)
    assert result["count"] == 1


# -------------------- 5. archived / future-dated still excluded --------------------


def test_archived_and_future_visits_excluded_even_with_operator_stamp_match(monkeypatch):
    monkeypatch.setenv("BTQ_OPERATOR_ID", "op_sandbox")
    visits = [
        _visit("SANDBOX", "2026-08-20", archived=True),
        _visit("SANDBOX", "2026-09-10"),  # future relative to now
    ]
    result = visit_coverage.account_coverage(
        "sandbox_user", now="2026-09-03", visits=visits, visit_gaps=[], accounts=_accounts(["SANDBOX"])
    )
    row = result["accounts"][0]
    assert row["last_qc_date"] is None
    assert row["last_visit_date"] is None


# -------------------- 6. home panel --------------------


def _fake_panel_report(*, overdue_count, total_accounts, overdue_qc_days):
    accounts = [{"site_id": str(i), "site_name": f"Account {i}"} for i in range(total_accounts)]
    overdue = [
        {"site_id": str(i), "site_name": f"Account {i}", "days_since_last_qc": None if i == 0 else 40}
        for i in range(overdue_count)
    ]
    report: dict[str, object] = {
        "weekly": {"count": 0, "target": 4, "remaining": 4, "completed": []},
        "accounts": accounts,
        "overdue": overdue,
        "gaps": [],
    }
    if overdue_qc_days is not None:
        report["overdue_qc_days"] = overdue_qc_days
    return report


def test_panel_heading_and_subline_show_overdue_count_and_days():
    report = _fake_panel_report(overdue_count=2, total_accounts=5, overdue_qc_days=30)
    html = home._render_coverage_panel(report)
    assert "Overdue accounts · 2" in html
    assert "30 days" in html
    assert "Account 0" in html
    assert "Account 1" in html


def test_panel_empty_overdue_state_names_threshold():
    report = _fake_panel_report(overdue_count=0, total_accounts=5, overdue_qc_days=30)
    html = home._render_coverage_panel(report)
    assert "All accounts have a recorded QC within 30 days." in html


def test_panel_defaults_overdue_qc_days_to_30_when_report_omits_it():
    report = _fake_panel_report(overdue_count=0, total_accounts=5, overdue_qc_days=None)
    assert "overdue_qc_days" not in report
    html = home._render_coverage_panel(report)
    assert "All accounts have a recorded QC within 30 days." in html
