from __future__ import annotations

from pathlib import Path

from site_supplies import discover_site_supplies, status_sort, supply_as_export


def write_supply(
    vault_root: Path,
    *,
    account: str = "Summitsteel",
    site_dir: str = "7050 - Summit Wire",
    supply_id: str = "sup_cleaner",
    site_id: str = "7050",
    status: str = "open",
    urgency: str = "high",
    item_name: str = "BrightWash cleaner",
) -> Path:
    path = vault_root / "Accounts" / account / "Locations" / site_dir / "Supplies" / f"{supply_id}__supply.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
type: supply_need
supply_id: {supply_id}
site_id: "{site_id}"
site_name: Summit Wire
account: {account}
item_name: {item_name}
quantity_needed: 2 bottles
urgency: {urgency}
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: {status}
created_at: 2026-05-08T20:00:00+00:00
notes: Supply closet is empty.
related_capture_ids:
  - cap-supply-summit
related_candidate_ids:
  - ac_supply_summit
ordered_at: 2026-05-09T12:00:00+00:00
ordered_by: Jordan
ordered_note: Ordered from supplier.
---
# Supply need: {item_name}
""",
        encoding="utf-8",
    )
    return path


def test_discover_site_supplies_returns_empty_on_clean_vault(tmp_path: Path) -> None:
    report = discover_site_supplies(tmp_path / "vault")

    assert report["supplies"] == []
    assert report["counts"]["total"] == 0


def test_discover_site_supplies_parses_well_formed_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root)

    report = discover_site_supplies(vault_root)
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


def test_discover_site_supplies_filters_by_site_id(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root, supply_id="sup_7050", site_id="7050")
    write_supply(vault_root, account="Contworks", site_dir="7060 - Continental Metalworks", supply_id="sup_7060", site_id="7060")

    report = discover_site_supplies(vault_root, site_id="7060")

    assert [supply.supply_id for supply in report["supplies"]] == ["sup_7060"]


def test_discover_site_supplies_filters_by_status(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root, supply_id="sup_open", status="open")
    write_supply(vault_root, supply_id="sup_ordered", status="ordered")

    report = discover_site_supplies(vault_root, status="ordered")

    assert [supply.supply_id for supply in report["supplies"]] == ["sup_ordered"]


def test_discover_site_supplies_counts_by_status(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root, supply_id="sup_open", status="open")
    write_supply(vault_root, supply_id="sup_ordered", status="ordered")

    report = discover_site_supplies(vault_root)

    assert report["counts"]["by_status"] == {"open": 1, "ordered": 1}


def test_discover_site_supplies_counts_by_urgency(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root, supply_id="sup_high", urgency="high")
    write_supply(vault_root, supply_id="sup_critical", urgency="critical")

    report = discover_site_supplies(vault_root)

    assert report["counts"]["by_urgency"] == {"critical": 1, "high": 1}


def test_discover_site_supplies_reports_warnings_for_malformed_file(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = vault_root / "Accounts" / "Bad" / "Locations" / "999 - Bad" / "Supplies" / "bad.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: supply_need\nsupply_id: sup_bad\n---\n", encoding="utf-8")

    report = discover_site_supplies(vault_root)

    assert report["supplies"] == []
    assert report["warnings"][0]["reason"] == "missing_required_site_id_or_supply_id_or_item_name"


def test_discover_site_supplies_skips_non_supply_need_type(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Supplies" / "note.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntype: site_issue\nsite_id: \"1337\"\n---\n", encoding="utf-8")

    report = discover_site_supplies(vault_root)

    assert report["supplies"] == []
    assert report["warnings"] == []


def test_supply_as_export_includes_vault_path_when_requested(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_supply(vault_root)
    [supply] = discover_site_supplies(vault_root)["supplies"]

    exported = supply_as_export(supply, include_path=True)

    assert exported["vault_supply_path"] == "Accounts/Summitsteel/Locations/7050 - Summit Wire/Supplies/sup_cleaner__supply.md"


def test_status_sort_orders_open_first() -> None:
    assert sorted(["stocked", "ordered", "open", "no_action_needed"], key=status_sort) == ["open", "ordered", "stocked", "no_action_needed"]
