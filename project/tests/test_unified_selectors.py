"""Gates for the unified site/employee selectors.

The operator's report: the batch-image tool listed inactive employees, and
every site <select> across the dashboard had drifted — different sources,
different label formats, different orderings. The contract now:

  * common.site_selector_options() is THE site list: active registry sites,
    "Name (id)" labels, name-sorted, static-SITES fallback on outage.
  * common.employee_selector_options() is THE user list: mirror values, but
    the canonical vault decides who is active (normalized-name knockout for
    stale mirror rows).
  * Every form builds from these — sentinel options injected into the shared
    helpers must appear in every selector render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ops_dashboard import common


SENTINEL_SITES = [("999", "ZZZ Sentinel Site (999)")]
SENTINEL_PEOPLE = [("sentinel-sam", "Sentinel Sam (sentinel-sam)")]


# ---------------------------------------------------------------------------
# site_selector_options
# ---------------------------------------------------------------------------

def test_site_options_labeled_sorted_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Registry:
        def list_sites(self):
            return [
                {"site_id": "1337", "canonical": "Liberty Wire"},
                {"site_id": "600", "canonical": "PHN Latrobe"},
                {"site_id": "1337", "canonical": "Liberty Wire"},  # duplicate row
                {"site_id": "7050", "canonical": ""},  # nameless -> id label
                {"site_id": "", "canonical": "ghost"},  # invalid -> dropped
            ]

    monkeypatch.setattr(common, "CouchDBSiteRegistry", _Registry)
    options = common.site_selector_options()
    assert options == [
        ("7050", "7050 (7050)"),
        ("1337", "Liberty Wire (1337)"),
        ("600", "PHN Latrobe (600)"),
    ]


def test_site_options_fall_back_to_static_table_on_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    class _DownRegistry:
        def list_sites(self):
            raise RuntimeError("couch down")

    monkeypatch.setattr(common, "CouchDBSiteRegistry", _DownRegistry)
    monkeypatch.setattr(common, "SITES", [{"site_id": "42", "canonical": "Fallback Site"}])
    assert common.site_selector_options() == [("42", "Fallback Site (42)")]


# ---------------------------------------------------------------------------
# employee_selector_options
# ---------------------------------------------------------------------------

def _install_people(monkeypatch: pytest.MonkeyPatch, mirror: list[dict], vault: list[dict]) -> None:
    def fake_find(database: str, selector: dict) -> list[dict]:
        return mirror if "people" in database else vault

    monkeypatch.setattr(common, "_selector_couch_find", fake_find)
    monkeypatch.setattr(common.couchdb_config, "people_database", lambda: "btq_people")
    monkeypatch.setattr(common.couchdb_config, "vault_database", lambda: "btq_vault")


def test_employee_options_knock_out_vault_inactive_despite_stale_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MUTATION GUARD: the operator's actual bug — Scott is "active" in the
    # mirror but inactive in the canonical vault (different id conventions,
    # so only the name can join them). He must not be selectable.
    _install_people(
        monkeypatch,
        mirror=[
            {"_id": "seigh-scott", "first": "Scott", "last": "Seigh", "status": "active"},
            {"_id": "baronie-john", "first": "John", "last": "Baronie", "status": "active"},
        ],
        vault=[
            {"_id": "employee_seigh_scott", "name": "Scott Seigh", "status": "inactive"},
            {"_id": "employee_baronie_john", "name": "John Baronie", "status": "active"},
        ],
    )
    options = common.employee_selector_options()
    assert options == [("baronie-john", "John Baronie (baronie-john)")]


def test_employee_options_sorted_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_people(
        monkeypatch,
        mirror=[
            {"_id": "z-person", "name": "Zed Zane", "status": "active"},
            {"_id": "a-person", "name": "Amy Able", "status": "active"},
        ],
        vault=[],
    )
    assert [label for _pid, label in common.employee_selector_options()] == [
        "Amy Able (a-person)",
        "Zed Zane (z-person)",
    ]


def test_employee_options_degrade_when_vault_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky_find(database: str, selector: dict) -> list[dict]:
        calls["n"] += 1
        if "vault" in database:
            raise RuntimeError("vault down")
        return [{"_id": "baronie-john", "name": "John Baronie", "status": "active"}]

    monkeypatch.setattr(common, "_selector_couch_find", flaky_find)
    monkeypatch.setattr(common.couchdb_config, "people_database", lambda: "btq_people")
    monkeypatch.setattr(common.couchdb_config, "vault_database", lambda: "btq_vault")
    assert common.employee_selector_options() == [("baronie-john", "John Baronie (baronie-john)")]


# ---------------------------------------------------------------------------
# Every selector render goes through the shared helpers (sentinel-through)
# ---------------------------------------------------------------------------

@pytest.fixture()
def sentinel_helpers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(common, "site_selector_options", lambda: list(SENTINEL_SITES))
    monkeypatch.setattr(common, "employee_selector_options", lambda: list(SENTINEL_PEOPLE))


def test_batch_images_uses_shared_helpers(sentinel_helpers) -> None:
    from ops_dashboard.sections import batch_images

    assert batch_images.load_sites() == [{"site_id": "999", "label": "ZZZ Sentinel Site (999)"}]
    assert batch_images.load_people() == [{"person_id": "sentinel-sam", "label": "Sentinel Sam (sentinel-sam)"}]


def test_field_photos_uses_shared_site_options(sentinel_helpers) -> None:
    from ops_dashboard.sections import field_photos

    assert field_photos._load_site_options() == SENTINEL_SITES


def test_photos_uses_shared_site_options(sentinel_helpers) -> None:
    from ops_dashboard.sections import photos

    assert photos._load_site_options() == SENTINEL_SITES


def test_prospect_detail_uses_shared_site_options(sentinel_helpers) -> None:
    from ops_dashboard.sections import prospect_detail

    options, ok = prospect_detail._site_options()
    assert options == SENTINEL_SITES
    assert ok


def test_home_voice_card_uses_shared_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    # Home curates its own active site set but must format through the shared
    # helper: "Name (id)" labels in name order, same as every other selector.
    from btq_vault.projector import _SiteRecord
    from ops_dashboard.sections import home

    records = {
        "9": _SiteRecord(site_id="9", name="Zebra Plant", account="Z"),
        "7": _SiteRecord(site_id="7", name="Acme Mill", account="A"),
    }
    html_out = home._render_voice_card(records, [])
    assert "Acme Mill (7)" in html_out
    assert "Zebra Plant (9)" in html_out
    assert html_out.index("Acme Mill (7)") < html_out.index("Zebra Plant (9)")
    assert "— none —" in html_out


def test_format_site_options_matches_selector_formatting() -> None:
    # MUTATION GUARD: the two entry points must never drift apart.
    assert common.format_site_options([("1337", "Liberty Wire"), ("600", "PHN")]) == [
        ("1337", "Liberty Wire (1337)"),
        ("600", "PHN (600)"),
    ]


def test_render_site_id_options_uses_shared_helper(sentinel_helpers) -> None:
    html_out = common.render_site_id_options("999")
    assert 'value="999" selected' in html_out
    assert "ZZZ Sentinel Site (999)" in html_out
    assert "Select site" in html_out
