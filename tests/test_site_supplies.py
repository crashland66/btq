from __future__ import annotations

import io
import json
import urllib.request

import pytest

from site_supplies import _supply_from_couch_doc, discover_site_supplies, status_sort, supply_as_export


def supply_doc(
    *,
    supply_id: str = "sup_cleaner",
    site_id: str = "7050",
    status: str = "open",
    urgency: str = "high",
    item_name: str = "BrightWash cleaner",
    archived: bool = False,
    **extra: object,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": f"supply_need_{supply_id}",
        "type": "supply_need",
        "supply_id": supply_id,
        "site_id": site_id,
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "item_name": item_name,
        "quantity_needed": "2 bottles",
        "urgency": urgency,
        "requested_by": "Tom Walsh",
        "observed_at": "2026-05-08T14:12:43+00:00",
        "source": "field_capture",
        "status": status,
        "archived": archived,
        "archived_at": "2026-06-10T12:00:00+00:00",
        "archived_by": "Jordan",
        "created_at": "2026-05-08T20:00:00+00:00",
        "notes": "Supply closet is empty.",
        "related_capture_ids": ["cap-supply-summit"],
        "related_candidate_ids": ["ac_supply_summit"],
        "ordered_at": "2026-05-09T12:00:00+00:00",
        "ordered_by": "Jordan",
        "ordered_note": "Ordered from supplier.",
    }
    doc.update(extra)
    return doc


def patch_couch(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, object]]) -> None:
    """Drive the CouchDB ``_find`` reader off an in-memory doc list.

    The query selector is honoured the way CouchDB would: ``archived`` and
    ``site_id`` constraints in the selector pre-filter the returned docs, so the
    discovery function's own post-filtering is exercised on a realistic set.
    """
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        payload = json.loads(req.data.decode("utf-8"))
        selector = payload["selector"]
        matched: list[dict[str, object]] = []
        for doc in docs:
            if doc.get("type") != selector.get("type"):
                continue
            if "archived" in selector and bool(doc.get("archived")) is not bool(selector["archived"]):
                continue
            if "site_id" in selector and str(doc.get("site_id")) != str(selector["site_id"]):
                continue
            matched.append(doc)
        body = json.dumps({"docs": matched}).encode("utf-8")
        return io.BytesIO(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_discover_site_supplies_returns_empty_when_no_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [])

    report = discover_site_supplies()

    assert report["supplies"] == []
    assert report["counts"]["total"] == 0


def test_discover_site_supplies_maps_couch_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [supply_doc()])

    report = discover_site_supplies()
    [supply] = report["supplies"]

    assert supply.supply_id == "sup_cleaner"
    assert supply.site_id == "7050"
    assert supply.site_name == "Summit Wire"
    assert supply.account == "Summitsteel"
    assert supply.item_name == "BrightWash cleaner"
    assert supply.quantity_needed == "2 bottles"
    assert supply.urgency == "high"
    assert supply.requested_by == "Tom Walsh"
    assert supply.status == "open"
    assert supply.notes == "Supply closet is empty."
    assert supply.related_capture_ids == ("cap-supply-summit",)
    assert supply.related_candidate_ids == ("ac_supply_summit",)
    assert supply.ordered_by == "Jordan"


def test_discover_site_supplies_filters_by_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            supply_doc(supply_id="sup_7050", site_id="7050"),
            supply_doc(supply_id="sup_7060", site_id="7060"),
        ],
    )

    report = discover_site_supplies(site_id="7060")

    assert [supply.supply_id for supply in report["supplies"]] == ["sup_7060"]


def test_discover_site_supplies_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            supply_doc(supply_id="sup_open", status="open"),
            supply_doc(supply_id="sup_ordered", status="ordered"),
        ],
    )

    report = discover_site_supplies(status="ordered")

    assert [supply.supply_id for supply in report["supplies"]] == ["sup_ordered"]


def test_discover_site_supplies_counts_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            supply_doc(supply_id="sup_open", status="open"),
            supply_doc(supply_id="sup_ordered", status="ordered"),
        ],
    )

    report = discover_site_supplies()

    assert report["counts"]["by_status"] == {"open": 1, "ordered": 1}


def test_discover_site_supplies_counts_by_urgency(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            supply_doc(supply_id="sup_high", urgency="high"),
            supply_doc(supply_id="sup_critical", urgency="critical"),
        ],
    )

    report = discover_site_supplies()

    assert report["counts"]["by_urgency"] == {"critical": 1, "high": 1}


def test_discover_site_supplies_excludes_archived_by_default_and_can_list_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            supply_doc(supply_id="sup_open"),
            supply_doc(supply_id="sup_archived", archived=True),
        ],
    )

    default_report = discover_site_supplies()
    archived_report = discover_site_supplies(include_archived=True, archived_only=True)

    assert [supply.supply_id for supply in default_report["supplies"]] == ["sup_open"]
    assert default_report["counts"]["total"] == 1
    assert [supply.supply_id for supply in archived_report["supplies"]] == ["sup_archived"]
    assert archived_report["counts"]["total"] == 1


def test_discover_site_supplies_skips_non_supply_need_type(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [{"_id": "note", "type": "site_issue", "site_id": "1337"}])

    report = discover_site_supplies()

    assert report["supplies"] == []
    assert report["warnings"] == []


def test_supply_as_export_includes_id_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [supply_doc()])
    [supply] = discover_site_supplies()["supplies"]

    exported = supply_as_export(supply, include_path=True)

    assert exported["_id"] == "supply_need_sup_cleaner"


def test_supply_from_couch_doc_uses_id_not_legacy_vault_path() -> None:
    supply = _supply_from_couch_doc(
        {
            "_id": "supply_need_7050_cleaner",
            "type": "supply_need",
            "site_id": "7050",
            "supply_id": "sup_cleaner",
            "item_name": "BrightWash cleaner",
            "status": "open",
            "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Supplies/sup_cleaner__supply.md",
        }
    )

    assert supply is not None
    assert supply_as_export(supply, include_path=True)["_id"] == "supply_need_7050_cleaner"


def test_status_sort_orders_open_first() -> None:
    assert sorted(["stocked", "ordered", "open", "no_action_needed"], key=status_sort) == ["open", "ordered", "stocked", "no_action_needed"]
