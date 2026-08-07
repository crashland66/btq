"""Gating tests for the potential-coverage lookup (Phase 3).

Contract (design spec 2026-07-28, employee-home-address-coverage-opportunities):

  * Active employees with a usable home ZIP rank by approximate distance to
    the target site, computed locally from the bundled Census centroid table.
  * Inactive/separated employees are excluded by default; employees without a
    usable home location degrade to a count, never a crash.
  * The page carries the explicit proximity-is-not-availability warning,
    links to employee records, and calls results potential coverage.
  * Privacy: no employee address or ZIP appears anywhere on the page.
  * No result path creates an assignment or sends a message — the page has no
    forms and no POST actions.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import coverage_geo
from ops_dashboard.sections import coverage


# Real PA geography, fictional people. Site: Hollsopple 15935.
_SITE_DOC = {
    "_id": "location_1200",
    "type": "location",
    "location": "North American Hoganas",
    "account": "Hoganas",
    "address": "111 Hoganas Way, Hollsopple, PA, 159356416",
}


def _employee(person_id: str, name: str, *, status: str = "active", postal: str | None = None, site_ids: list[str] | None = None) -> dict:
    doc: dict = {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "name": name,
        "status": status,
        "role": "cleaner",
        "site_ids": site_ids or [],
    }
    if postal is not None:
        doc["home_address"] = {
            "line1": "123 Example Street",
            "city": "Exampletown",
            "state": "PA",
            "postal_code": postal,
        }
    return doc


def _fleet() -> list[dict]:
    return [
        _employee("far_fiona", "Far Fiona", postal="15650"),        # Latrobe, ~22 mi
        _employee("near_ned", "Near Ned", postal="15935"),          # Hollsopple, ~0 mi
        _employee("mid_mira", "Mid Mira", postal="15905"),          # Johnstown, ~6 mi
        _employee("gone_gary", "Gone Gary", status="inactive", postal="15935"),
        _employee("lost_lou", "Lost Lou"),                          # no address
        _employee("odd_ozzy", "Odd Ozzy", postal="00000"),          # unknown ZIP
    ]


def _install(monkeypatch: pytest.MonkeyPatch, *, site: dict | None = _SITE_DOC, employees: list[dict] | None = None) -> None:
    monkeypatch.setattr(coverage, "_load_location", lambda _sid: site)
    monkeypatch.setattr(coverage, "_employee_docs", lambda: employees if employees is not None else _fleet())
    monkeypatch.setattr(coverage, "_site_names", lambda ids: {i: f"Site {i}" for i in ids})


def _render(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> str:
    _install(monkeypatch, **kwargs)
    return coverage.render(SimpleNamespace(query={}), "1200")


# ---------------------------------------------------------------------------
# Geo primitives
# ---------------------------------------------------------------------------

def test_postal_parsing_variants() -> None:
    assert coverage_geo.postal_code_zip5("15935-6416") == "15935"
    assert coverage_geo.postal_code_zip5("159356416") == "15935"
    assert coverage_geo.postal_code_zip5("") == ""
    assert coverage_geo.site_postal_code({"address": "529 Lloyd Ave, Latrobe, PA, 156501721"}) == "15650"
    assert coverage_geo.site_postal_code({"address": "12345 Big Number Rd, Town, PA, 15905"}) == "15905"
    assert coverage_geo.site_postal_code({}) == ""


def test_centroid_lookup_and_distance_sanity() -> None:
    johnstown = coverage_geo.zip_centroid("15905")
    hollsopple = coverage_geo.zip_centroid("15935")
    assert johnstown is not None and hollsopple is not None
    miles = coverage_geo.haversine_miles(johnstown, hollsopple)
    assert 3 < miles < 15
    assert coverage_geo.haversine_miles(johnstown, johnstown) == 0
    assert coverage_geo.zip_centroid("00000") is None
    assert coverage_geo.zip_centroid(None) is None


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_ranking_orders_by_distance_and_partitions_fleet() -> None:
    site_centroid = coverage_geo.zip_centroid("15935")
    assert site_centroid is not None
    ranked, unlocatable = coverage.rank_candidates(_fleet(), site_centroid)

    names = [doc["name"] for _miles, doc in ranked]
    assert names == ["Near Ned", "Mid Mira", "Far Fiona"]
    # Inactive Gary is not a candidate even though he lives on-site.
    assert "Gone Gary" not in names
    # Lou (no address) and Ozzy (unknown ZIP) degrade to the count.
    assert unlocatable == 2
    # Distances are plausible and monotonic.
    miles = [m for m, _doc in ranked]
    assert miles == sorted(miles)
    assert miles[0] < 2 and 15 < miles[2] < 40


# ---------------------------------------------------------------------------
# Page contract
# ---------------------------------------------------------------------------

def test_page_ranks_links_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch)
    # Site naming matches the site page's own convention (sites.canonical_name).
    assert "Potential coverage near North American Hoganas" in body
    # MUTATION GUARD: the explicit warning is present and prominent.
    assert "Proximity is not availability" in body
    assert "Potential coverage only." in body
    # Candidates link out to their employee records, closest first.
    assert body.index('href="/employees/near_ned"') < body.index('href="/employees/mid_mira"') < body.index('href="/employees/far_fiona"')
    assert "~22 mi" in body or "~21 mi" in body or "~23 mi" in body
    # Excluded-count footnote surfaces the not-shown employees.
    assert "2 active employees not shown" in body
    # Inactive employees never render.
    assert "Gone Gary" not in body


def test_page_never_renders_addresses_or_zips(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch)
    # MUTATION GUARD (privacy): no address fragment or employee ZIP on the page.
    assert "123 Example Street" not in body
    assert "Exampletown" not in body
    for employee_zip in ("15650", "15905"):
        assert employee_zip not in body
    assert "postal" not in body.lower()


def test_page_has_no_forms_or_post_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch)
    # MUTATION GUARD: decision support only — nothing submits, assigns, or messages.
    assert "<form" not in body
    assert "method=\"post\"" not in body.lower()


def test_site_without_postal_code_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    site = dict(_SITE_DOC)
    site["address"] = "The Old Mill, Hollsopple, PA"
    body = _render(monkeypatch, site=site)
    assert "no usable postal code" in body
    assert "Proximity is not availability" in body  # warning still shown
    assert "Site not found" not in body


def test_no_locatable_employees_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, employees=[_employee("lost_lou", "Lost Lou")])
    assert "No active employees have a usable home location yet." in body


def test_unknown_site_renders_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, site=None)
    assert "Site not found" in body
