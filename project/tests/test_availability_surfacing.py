"""Independent-verifier gating tests for prompt 400 (availability surfacing).

These tests are authored by the VERIFIER (not the executor). They pin the
read-path surfacing of availability constraints on the person + site detail
pages:

  * Person page (employee_detail._availability_section / render): shows UPCOMING
    unavailable_date(s) and the earliest future last_working_day ("Last day:
    ..."); a PAST unavailable_date is hidden; empty state when nothing upcoming.
  * Site page (site_detail._coverage_gap_rows / _coverage_gaps_section / render):
    "Upcoming coverage gaps" lists each ROSTERED cleaner (from employees_by_site)
    that has upcoming constraints; a rostered cleaner with no upcoming constraints
    is omitted; a NON-rostered person's constraints never appear; empty state
    when none.
  * Forward filter (date >= today) applied on BOTH surfaces.
  * Graceful degradation: a failing/empty view query renders the empty state and
    never crashes; a data dict missing coverage_gap_rows does not break render.

`date.today()` is not injectable in the code under review, so dates are seeded
RELATIVE to the real today (clearly-past / clearly-future) to stay stable.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employee_detail, site_detail


# --------------------------------------------------------------------------- #
# Stable relative dates                                                        #
# --------------------------------------------------------------------------- #
TODAY = date.today()
PAST = (TODAY - timedelta(days=30)).isoformat()
PAST_FAR = (TODAY - timedelta(days=90)).isoformat()
FUT_NEAR = (TODAY + timedelta(days=5)).isoformat()
FUT_FAR = (TODAY + timedelta(days=40)).isoformat()


# --------------------------------------------------------------------------- #
# Person section harness                                                       #
# --------------------------------------------------------------------------- #
EMP = "forsythe_adriana"
PERSON = "person_forsythe_adriana"


def _person_rows(*values: dict[str, object]) -> list[dict[str, object]]:
    return [{"key": PERSON, "value": v} for v in values]


def _patch_person_view(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(
        employee_detail, "_cdb", lambda: ("http://x", {}, "db", 1.0)
    )
    # _availability_section calls site_detail.query_view(...).
    monkeypatch.setattr(
        site_detail, "query_view", lambda *a, **k: rows
    )


def test_person_section_shows_upcoming_unavailable_and_last_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _person_rows(
        {"constraint_type": "unavailable_date", "date": FUT_NEAR},
        {"constraint_type": "unavailable_date", "date": FUT_FAR},
        {"constraint_type": "last_working_day", "date": FUT_FAR},
    )
    _patch_person_view(monkeypatch, rows)
    out = employee_detail._availability_section(EMP, {"person_id": PERSON})

    assert "<h2>Availability constraints</h2>" in out
    assert FUT_NEAR in out
    assert FUT_FAR in out
    assert f"Last day:</strong> {FUT_FAR}" in out
    assert "No upcoming unavailability recorded." not in out


def test_person_section_hides_past_unavailable_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _person_rows(
        {"constraint_type": "unavailable_date", "date": PAST},
        {"constraint_type": "unavailable_date", "date": FUT_NEAR},
    )
    _patch_person_view(monkeypatch, rows)
    out = employee_detail._availability_section(EMP, {"person_id": PERSON})

    assert FUT_NEAR in out
    # The past date must NOT leak onto the page (forward filter).
    assert PAST not in out


def test_person_section_earliest_future_last_working_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A past last_working_day must be ignored; among futures, the earliest wins.
    rows = _person_rows(
        {"constraint_type": "last_working_day", "date": PAST_FAR},
        {"constraint_type": "last_working_day", "date": FUT_FAR},
        {"constraint_type": "last_working_day", "date": FUT_NEAR},
    )
    _patch_person_view(monkeypatch, rows)
    out = employee_detail._availability_section(EMP, {"person_id": PERSON})

    assert f"Last day:</strong> {FUT_NEAR}" in out
    assert PAST_FAR not in out


def test_person_section_empty_state_when_no_upcoming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _person_rows(
        {"constraint_type": "unavailable_date", "date": PAST},
        {"constraint_type": "last_working_day", "date": PAST_FAR},
    )
    _patch_person_view(monkeypatch, rows)
    out = employee_detail._availability_section(EMP, {"person_id": PERSON})

    assert "No upcoming unavailability recorded." in out


def test_person_section_degrades_when_view_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> list[dict[str, object]]:
        raise RuntimeError("no couchdb")

    monkeypatch.setattr(employee_detail, "_cdb", lambda: ("http://x", {}, "db", 1.0))
    monkeypatch.setattr(site_detail, "query_view", _boom)
    out = employee_detail._availability_section(EMP, {"person_id": PERSON})

    assert "<h2>Availability constraints</h2>" in out
    assert "No upcoming unavailability recorded." in out


def test_person_render_appends_availability_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = {
        "_id": f"employee_{EMP}",
        "_rev": "1-a",
        "type": "employee",
        "operator": "op_greg",
        "name": "Adriana Forsythe",
        "person_id": PERSON,
        "status": "active",
        "site_ids": ["789"],
    }
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _id: doc)
    monkeypatch.setattr(employee_detail, "_field_captures", lambda *_a, **_k: [])
    monkeypatch.setattr(employee_detail, "_personnel_events", lambda *_a, **_k: [])
    monkeypatch.setattr(employee_detail, "_data_quality_flags", lambda *_a, **_k: [])
    monkeypatch.setattr(employee_detail, "_site_name_map", lambda *_a, **_k: {})
    rows = _person_rows(
        {"constraint_type": "unavailable_date", "date": FUT_NEAR},
    )
    _patch_person_view(monkeypatch, rows)

    out = employee_detail.render(SimpleNamespace(query={}), EMP)
    assert "<h2>Availability constraints</h2>" in out
    assert FUT_NEAR in out


# --------------------------------------------------------------------------- #
# Site section harness                                                         #
# --------------------------------------------------------------------------- #
SITE = "789"
P_ROSTERED_A = "person_rostered_a"
P_ROSTERED_B = "person_rostered_b"
P_NON_ROSTERED = "person_outsider"


def _roster(*person_ids: str) -> list[dict[str, object]]:
    return [
        {"id": f"employee_{pid}", "doc": {"person_id": pid, "name": pid.upper()}}
        for pid in person_ids
    ]


def _constraint(person_id: str, ctype: str, day: str) -> dict[str, object]:
    return {"key": person_id, "value": {"constraint_type": ctype, "date": day}}


def _patch_site_view(
    monkeypatch: pytest.MonkeyPatch, constraint_rows: list[dict[str, object]]
) -> None:
    monkeypatch.setattr(site_detail, "_cdb", lambda: ("http://x", {}, "db", 1.0))
    monkeypatch.setattr(
        site_detail, "query_view", lambda *a, **k: constraint_rows
    )


def test_site_gaps_lists_rostered_cleaner_with_upcoming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A, P_ROSTERED_B)
    constraints = [
        _constraint(P_ROSTERED_A, "unavailable_date", FUT_NEAR),
        _constraint(P_ROSTERED_A, "last_working_day", FUT_FAR),
    ]
    _patch_site_view(monkeypatch, constraints)
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    assert "Upcoming coverage gaps" in out
    assert "PERSON_ROSTERED_A" in out
    assert FUT_NEAR in out
    assert f"Last day: {FUT_FAR}" in out
    # rostered_b has no constraints → omitted.
    assert "PERSON_ROSTERED_B" not in out
    assert len(rows) == 1


def test_site_gaps_omits_rostered_cleaner_without_upcoming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A, P_ROSTERED_B)
    constraints = [
        # B only has a PAST constraint → no upcoming → omitted.
        _constraint(P_ROSTERED_B, "unavailable_date", PAST),
        _constraint(P_ROSTERED_A, "unavailable_date", FUT_NEAR),
    ]
    _patch_site_view(monkeypatch, constraints)
    rows = site_detail._coverage_gap_rows(roster)
    person_ids = {r["person_id"] for r in rows}

    assert person_ids == {P_ROSTERED_A}


def test_site_gaps_excludes_non_rostered_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A)
    constraints = [
        _constraint(P_ROSTERED_A, "unavailable_date", FUT_NEAR),
        # An outsider with upcoming constraints must NOT surface on this site.
        _constraint(P_NON_ROSTERED, "unavailable_date", FUT_FAR),
    ]
    _patch_site_view(monkeypatch, constraints)
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    person_ids = {r["person_id"] for r in rows}
    assert person_ids == {P_ROSTERED_A}
    assert P_NON_ROSTERED not in out
    assert "person_outsider" not in out


def test_site_gaps_forward_filter_hides_past(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A)
    constraints = [
        _constraint(P_ROSTERED_A, "unavailable_date", PAST),
        _constraint(P_ROSTERED_A, "unavailable_date", FUT_NEAR),
    ]
    _patch_site_view(monkeypatch, constraints)
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    assert FUT_NEAR in out
    assert PAST not in out


def test_site_gaps_empty_state_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A)
    _patch_site_view(monkeypatch, [])
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    assert rows == []
    assert "No upcoming coverage gaps." in out


def test_site_gaps_degrades_when_view_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A)

    def _boom(*_a: object, **_k: object) -> list[dict[str, object]]:
        raise RuntimeError("no couchdb")

    monkeypatch.setattr(site_detail, "_cdb", lambda: ("http://x", {}, "db", 1.0))
    monkeypatch.setattr(site_detail, "query_view", _boom)
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    assert rows == []
    assert "No upcoming coverage gaps." in out


def test_site_gaps_malformed_rows_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster(P_ROSTERED_A)
    constraints = [
        {"key": P_ROSTERED_A, "value": None},          # non-dict value
        {"key": P_ROSTERED_A},                          # no value
        {"value": {"constraint_type": "unavailable_date", "date": FUT_NEAR}},  # no key
        _constraint(P_ROSTERED_A, "unavailable_date", "not-a-date"),  # bad date
        _constraint(P_ROSTERED_A, "unavailable_date", FUT_NEAR),     # the real one
    ]
    _patch_site_view(monkeypatch, constraints)
    rows = site_detail._coverage_gap_rows(roster)
    out = site_detail._coverage_gaps_section(rows)

    assert {r["person_id"] for r in rows} == {P_ROSTERED_A}
    assert FUT_NEAR in out


def test_site_related_sections_handles_missing_coverage_gap_rows() -> None:
    # Defensive .get: a data dict missing coverage_gap_rows must not break render.
    data = {
        "employee_rows": [],
        "opportunity_rows": [],
        "recent_visits": [],
        "notes": [],
        "visit_rows": [],
    }
    sections = site_detail._related_sections(data)
    titles = [title for title, _count, _html in sections]
    assert "Upcoming coverage gaps" in titles


def test_site_render_shows_coverage_gaps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = {
        "_id": "location_789",
        "_rev": "1-a",
        "type": "location",
        "operator": "op_greg",
        "account": "Sandy Sandbox",
        "location": "Sandbox Site",
        "site_id": SITE,
        "status": "active",
    }
    roster = _roster(P_ROSTERED_A)
    related = {
        "notes": [],
        "employee_rows": roster,
        "coverage_gap_rows": [
            {
                "person_id": P_ROSTERED_A,
                "name": "PERSON_ROSTERED_A",
                "unavailable_dates": [date.fromisoformat(FUT_NEAR)],
                "last_working_day": None,
            }
        ],
        "opportunity_rows": [],
        "visit_rows": [],
        "recent_visits": [],
    }
    monkeypatch.setattr(site_detail, "_load_location", lambda _sid: doc)
    monkeypatch.setattr(site_detail, "_related_data", lambda _sid: related)
    monkeypatch.setattr(
        site_detail, "_site_capture_records", lambda _ctx, _sid: ([], False, 0)
    )
    monkeypatch.setattr(site_detail, "submitters_by_capture", lambda _root: {})
    ctx = SimpleNamespace(runtime_root=tmp_path / "runtime", query={})
    out = site_detail.render(ctx, SITE)

    assert "Upcoming coverage gaps" in out
    assert "PERSON_ROSTERED_A" in out
    assert FUT_NEAR in out
