from __future__ import annotations

import io
import json
import urllib.request

import pytest

from site_equipment import _equipment_from_couch_doc, discover_site_equipment, equipment_as_export, priority_sort


def equipment_doc(
    *,
    equipment_id: str = "eqr_vacuum",
    site_id: str = "7050",
    status: str = "open",
    priority: str = "urgent",
    equipment_name: str = "vacuum",
    archived: bool = False,
    **extra: object,
) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": f"equipment_request_{equipment_id}",
        "type": "equipment_request",
        "equipment_id": equipment_id,
        "site_id": site_id,
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "equipment_name": equipment_name,
        "reason": "Current vacuum will not start.",
        "priority": priority,
        "requested_by": "Tom Walsh",
        "observed_at": "2026-05-08T14:12:43+00:00",
        "source": "field_capture",
        "status": status,
        "archived": archived,
        "archived_at": "2026-06-10T12:00:00+00:00",
        "archived_by": "Jordan",
        "created_at": "2026-05-08T20:00:00+00:00",
        "notes": "Needed for lobby carpet.",
        "related_capture_ids": ["cap-equipment-summit"],
        "related_candidate_ids": ["ac_equipment_summit"],
        "approved_at": "2026-05-09T12:00:00+00:00",
        "approved_by": "Jordan",
        "approval_note": "Approved replacement.",
        "ordered_at": "2026-05-09T13:00:00+00:00",
        "ordered_by": "Jordan",
        "ordered_note": "Ordered from supplier.",
        "provided_at": "2026-05-10T13:00:00+00:00",
        "provided_by": "Tom",
        "provided_note": "Delivered to closet.",
    }
    doc.update(extra)
    return doc


def patch_couch(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, object]]) -> None:
    """Drive the CouchDB ``_find`` reader off an in-memory doc list."""
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


def test_discover_site_equipment_returns_empty_when_no_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [])

    report = discover_site_equipment()

    assert report["equipment"] == []
    assert report["counts"]["total"] == 0


def test_discover_site_equipment_maps_couch_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [equipment_doc()])

    report = discover_site_equipment()
    [request] = report["equipment"]

    assert request.equipment_id == "eqr_vacuum"
    assert request.site_id == "7050"
    assert request.site_name == "Summit Wire"
    assert request.account == "Summitsteel"
    assert request.equipment_name == "vacuum"
    assert request.reason == "Current vacuum will not start."
    assert request.priority == "urgent"
    assert request.requested_by == "Tom Walsh"
    assert request.status == "open"
    assert request.notes == "Needed for lobby carpet."
    assert request.related_capture_ids == ("cap-equipment-summit",)
    assert request.related_candidate_ids == ("ac_equipment_summit",)
    assert request.approved_by == "Jordan"
    assert request.provided_note == "Delivered to closet."


def test_discover_site_equipment_filters_by_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            equipment_doc(equipment_id="eqr_7050", site_id="7050"),
            equipment_doc(equipment_id="eqr_7060", site_id="7060"),
        ],
    )

    report = discover_site_equipment(site_id="7060")

    assert [request.equipment_id for request in report["equipment"]] == ["eqr_7060"]


def test_discover_site_equipment_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            equipment_doc(equipment_id="eqr_open", status="open"),
            equipment_doc(equipment_id="eqr_approved", status="approved"),
        ],
    )

    report = discover_site_equipment(status="approved")

    assert [request.equipment_id for request in report["equipment"]] == ["eqr_approved"]


def test_discover_site_equipment_counts_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            equipment_doc(equipment_id="eqr_open", status="open"),
            equipment_doc(equipment_id="eqr_approved", status="approved"),
        ],
    )

    report = discover_site_equipment()

    assert report["counts"]["by_status"] == {"approved": 1, "open": 1}


def test_discover_site_equipment_counts_by_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            equipment_doc(equipment_id="eqr_urgent", priority="urgent"),
            equipment_doc(equipment_id="eqr_high", priority="high"),
        ],
    )

    report = discover_site_equipment()

    assert report["counts"]["by_priority"] == {"high": 1, "urgent": 1}


def test_discover_site_equipment_excludes_archived_by_default_and_can_list_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(
        monkeypatch,
        [
            equipment_doc(equipment_id="eqr_open"),
            equipment_doc(equipment_id="eqr_archived", archived=True),
        ],
    )

    default_report = discover_site_equipment()
    archived_report = discover_site_equipment(include_archived=True, archived_only=True)

    assert [request.equipment_id for request in default_report["equipment"]] == ["eqr_open"]
    assert default_report["counts"]["total"] == 1
    assert [request.equipment_id for request in archived_report["equipment"]] == ["eqr_archived"]
    assert archived_report["counts"]["total"] == 1


def test_discover_site_equipment_skips_non_equipment_request_type(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [{"_id": "note", "type": "site_issue", "site_id": "1337"}])

    report = discover_site_equipment()

    assert report["equipment"] == []
    assert report["warnings"] == []


def test_equipment_as_export_includes_id_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_couch(monkeypatch, [equipment_doc()])
    [request] = discover_site_equipment()["equipment"]

    exported = equipment_as_export(request, include_path=True)

    assert exported["_id"] == "equipment_request_eqr_vacuum"


def test_equipment_from_couch_doc_uses_id_not_legacy_vault_path() -> None:
    request = _equipment_from_couch_doc(
        {
            "_id": "equipment_request_7050_vacuum",
            "type": "equipment_request",
            "site_id": "7050",
            "equipment_id": "eqr_vacuum",
            "equipment_name": "vacuum",
            "status": "open",
            "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Equipment/eqr_vacuum__equipment.md",
        }
    )

    assert request is not None
    assert equipment_as_export(request, include_path=True)["_id"] == "equipment_request_7050_vacuum"


def test_priority_sort_orders_urgent_first() -> None:
    assert sorted(["low", "normal", "urgent", "high"], key=priority_sort) == ["urgent", "high", "normal", "low"]
