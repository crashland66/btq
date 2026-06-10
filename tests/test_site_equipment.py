from __future__ import annotations

from pathlib import Path

from site_equipment import _equipment_from_couch_doc, discover_site_equipment, equipment_as_export, priority_sort


def write_equipment(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    equipment_id: str = "eqr_vacuum",
    site_id: str = "7050",
    status: str = "open",
    priority: str = "urgent",
    equipment_name: str = "vacuum",
    archived: bool = False,
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Equipment" / f"{equipment_id}__equipment.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: equipment_request
equipment_id: {equipment_id}
site_id: "{site_id}"
site_name: Summit Wire
account: {account}
equipment_name: {equipment_name}
reason: Current vacuum will not start.
priority: {priority}
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: {status}
archived: {str(archived).lower()}
archived_at: 2026-06-10T12:00:00+00:00
archived_by: Jordan
created_at: 2026-05-08T20:00:00+00:00
notes: Needed for lobby carpet.
related_capture_ids:
  - cap-equipment-summit
related_candidate_ids:
  - ac_equipment_summit
approved_at: 2026-05-09T12:00:00+00:00
approved_by: Jordan
approval_note: Approved replacement.
ordered_at: 2026-05-09T13:00:00+00:00
ordered_by: Jordan
ordered_note: Ordered from supplier.
provided_at: 2026-05-10T13:00:00+00:00
provided_by: Tom
provided_note: Delivered to closet.
---
# Equipment request: {equipment_name}
""",
        encoding="utf-8",
    )
    return path


def test_discover_site_equipment_returns_empty_on_clean_vault(tmp_path: Path) -> None:
    report = discover_site_equipment(tmp_path / "vault")

    assert report["equipment"] == []
    assert report["counts"]["total"] == 0


def test_discover_site_equipment_parses_well_formed_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root)

    report = discover_site_equipment(vault_root)
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


def test_discover_site_equipment_filters_by_site_id(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root, equipment_id="eqr_7050", site_id="7050")
    write_equipment(vault_root, account="Contworks", site_dir="7060 - Continental Metalworks", equipment_id="eqr_7060", site_id="7060")

    report = discover_site_equipment(vault_root, site_id="7060")

    assert [request.equipment_id for request in report["equipment"]] == ["eqr_7060"]


def test_discover_site_equipment_filters_by_status(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root, equipment_id="eqr_open", status="open")
    write_equipment(vault_root, equipment_id="eqr_approved", status="approved")

    report = discover_site_equipment(vault_root, status="approved")

    assert [request.equipment_id for request in report["equipment"]] == ["eqr_approved"]


def test_discover_site_equipment_counts_by_status(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root, equipment_id="eqr_open", status="open")
    write_equipment(vault_root, equipment_id="eqr_approved", status="approved")

    report = discover_site_equipment(vault_root)

    assert report["counts"]["by_status"] == {"approved": 1, "open": 1}


def test_discover_site_equipment_counts_by_priority(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root, equipment_id="eqr_urgent", priority="urgent")
    write_equipment(vault_root, equipment_id="eqr_high", priority="high")

    report = discover_site_equipment(vault_root)

    assert report["counts"]["by_priority"] == {"high": 1, "urgent": 1}


def test_discover_site_equipment_excludes_archived_by_default_and_can_list_archived(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root, equipment_id="eqr_open")
    write_equipment(vault_root, equipment_id="eqr_archived", archived=True)

    default_report = discover_site_equipment(vault_root)
    archived_report = discover_site_equipment(vault_root, include_archived=True, archived_only=True)

    assert [request.equipment_id for request in default_report["equipment"]] == ["eqr_open"]
    assert default_report["counts"]["total"] == 1
    assert [request.equipment_id for request in archived_report["equipment"]] == ["eqr_archived"]
    assert archived_report["counts"]["total"] == 1


def test_discover_site_equipment_reports_warnings_for_malformed_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = vault_root / "Accounts" / "Bad" / "Locations" / "999 - Bad" / "Equipment" / "bad.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: equipment_request\nequipment_id: eqr_bad\n---\n", encoding="utf-8")

    report = discover_site_equipment(vault_root)

    assert report["equipment"] == []
    assert report["warnings"][0]["reason"] == "missing_required_site_id_or_equipment_id_or_equipment_name"


def test_discover_site_equipment_skips_non_equipment_request_type(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Equipment" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: site_issue\nsite_id: \"1337\"\n---\n", encoding="utf-8")

    report = discover_site_equipment(vault_root)

    assert report["equipment"] == []
    assert report["warnings"] == []


def test_equipment_as_export_includes_id_when_requested(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_equipment(vault_root)
    [request] = discover_site_equipment(vault_root)["equipment"]

    exported = equipment_as_export(request, include_path=True)

    assert exported["_id"] == "eqr_vacuum"


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
