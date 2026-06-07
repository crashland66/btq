from __future__ import annotations

from datetime import date
from pathlib import Path

import json

from site_supply_budgets import apply_supply_budget_updates, import_supply_budget_csv, parse_supply_budget_lines


def write_site(path: Path, *, account: str, location: str, job: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"account: {account}\n"
        f"location: {location}\n"
        f"job: {job}\n"
        "type: location\n"
        "existing_field: keep-me\n"
        "---\n\n"
        f"# {location}\n",
        encoding="utf-8",
    )


def test_parse_supply_budget_lines_normalizes_budget_types(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md", account="Summitsteel", location="Summit Wire", job="7050")
    write_site(vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md", account="Wgtco", location="Western Gas Transmission", job="7030")
    write_site(vault_root / "Accounts" / "Apexco" / "Locations" / "7080 - Apex Powdered Metals" / "about.md", account="Apexco", location="Apex Powdered Metals", job="7080")

    lines = [
        "Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED",
        "Western Gas Transmission #7030 Supplies Included: $125.00",
        "Apex Powdered Metals #7080 LABOR ONLY",
        "Unknown Site #9999",
    ]

    records, manual_review = parse_supply_budget_lines(lines, vault_root)

    assert records[0].site_id == "7050"
    assert records[0].site_name == "Summit Wire"
    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 374.59
    assert records[0].supply_budget_notes == "no consumables included"

    assert records[1].supply_budget_type == "included"
    assert records[1].monthly_supply_budget == 125.0
    assert records[1].supply_budget_notes is None

    assert records[2].supply_budget_type == "excluded"
    assert records[2].monthly_supply_budget is None
    assert records[2].supply_budget_notes is None

    assert records[3].site_id == "9999"
    assert records[3].supply_budget_type == "unknown"
    assert records[3].monthly_supply_budget is None
    assert len(manual_review) == 1


def test_apply_supply_budget_updates_preserves_existing_fields(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    about_path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md"
    write_site(about_path, account="Summitsteel", location="Summit Wire", job="7050")

    records, manual_review = apply_supply_budget_updates(
        ["Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59"],
        vault_root,
        as_of_date=date(2026, 4, 20),
    )

    assert manual_review == []
    assert records[0]["monthly_supply_budget"] == 374.59
    assert records[0]["supply_budget_notes"] is None
    updated_text = about_path.read_text(encoding="utf-8")
    assert "existing_field: keep-me" in updated_text
    assert "supply_budget_type: budgeted" in updated_text
    assert "monthly_supply_budget: 374.59" in updated_text
    assert "supply_budget_notes: null" in updated_text
    assert "budget_last_verified: 2026-04-20" in updated_text


def test_apply_supply_budget_updates_marks_unknown_for_manual_review(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    about_path = vault_root / "Accounts" / "Apexco" / "Locations" / "7080 - Apex Powdered Metals" / "about.md"
    write_site(about_path, account="Apexco", location="Apex Powdered Metals", job="7080")

    records, manual_review = apply_supply_budget_updates(
        ["Apex Powdered Metals #7080 contract renewal pending"],
        vault_root,
        as_of_date=date(2026, 4, 20),
    )

    assert records[0]["supply_budget_type"] == "unknown"
    assert records[0]["monthly_supply_budget"] is None
    assert manual_review == records
    updated_text = about_path.read_text(encoding="utf-8")
    assert "supply_budget_type: unknown" in updated_text
    assert "monthly_supply_budget: null" in updated_text
    assert "supply_budget_notes: null" in updated_text


def test_basic_supply_budget_with_restriction_stays_budgeted(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    about_path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md"
    write_site(about_path, account="Summitsteel", location="Summit Wire", job="7050")

    records, manual_review = apply_supply_budget_updates(
        ["Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED"],
        vault_root,
        as_of_date=date(2026, 4, 20),
    )

    assert manual_review == []
    assert records[0]["supply_budget_type"] == "budgeted"
    assert records[0]["monthly_supply_budget"] == 374.59
    assert records[0]["supply_budget_notes"] == "no consumables included"
    updated_text = about_path.read_text(encoding="utf-8")
    assert "supply_budget_type: budgeted" in updated_text
    assert "monthly_supply_budget: 374.59" in updated_text
    assert "supply_budget_notes: no consumables included" in updated_text


def test_restriction_extraction_normalizes_phrase(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        account="Wgtco",
        location="Western Gas Transmission",
        job="7030",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Basic Supply Budget: $40.00 - no liners or paper products included (Provided by Customer)"],
        vault_root,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 40.0
    assert records[0].supply_budget_notes == "no liners or paper products included"


def test_except_disposables_restriction_is_captured(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        account="Wgtco",
        location="Western Gas Transmission",
        job="7030",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Basic Supply Budget:$83 (except disposables)"],
        vault_root,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 83.0
    assert records[0].supply_budget_notes == "except disposables"


def test_supplies_amount_is_treated_as_budgeted(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Apexco" / "Locations" / "7080 - Apex Powdered Metals" / "about.md",
        account="Apexco",
        location="Apex Powdered Metals",
        job="7080",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Apex Powdered Metals, Inc. #7080 Supplies: $51.24 - no PT, TP, liners or soap"],
        vault_root,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 51.24
    assert records[0].supply_budget_notes == "no pt, tp, liners or soap"


def test_paper_towels_restriction_is_captured(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "RHN" / "Locations" / "7020 - Lakeshore Community Health Center - RHN" / "about.md",
        account="RHN",
        location="RHN-Lakeshore Community",
        job="7020",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["RHN-Lakeshore Community #7020 Basic Supply Budget: $39 - no liners or paper towels included (Provided by Customer)"],
        vault_root,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 39.0
    assert records[0].supply_budget_notes == "no liners or paper towels included"


def test_no_supplies_is_excluded(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        account="Wgtco",
        location="Western Gas Transmission",
        job="7030",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 No Supplies"],
        vault_root,
    )

    assert records[0].supply_budget_type == "excluded"
    assert records[0].monthly_supply_budget is None
    assert records[0].supply_budget_notes is None


def test_labor_only_is_excluded(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        account="Wgtco",
        location="Western Gas Transmission",
        job="7030",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 LABOR ONLY"],
        vault_root,
    )

    assert records[0].supply_budget_type == "excluded"
    assert records[0].monthly_supply_budget is None


def test_supplies_included_is_included(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    write_site(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        account="Wgtco",
        location="Western Gas Transmission",
        job="7030",
    )

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Supplies Included: $125.00"],
        vault_root,
    )

    assert records[0].supply_budget_type == "included"
    assert records[0].monthly_supply_budget == 125.0
    assert records[0].supply_budget_notes is None


def test_import_supply_budget_csv_updates_vault_and_writes_outputs(tmp_path: Path) -> None:
    vault_root = tmp_path / "vault"
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    about_path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md"
    write_site(about_path, account="Summitsteel", location="Summit Wire", job="7050")

    csv_path = tmp_path / "budgets.csv"
    csv_path.write_text(
        "site,notes\n"
        "\"Summit Wire Riverton #7050\",\"Amount: $12,319.61 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED\"\n"
        "\"Unknown Site #9999\",\"Amount: $500\"\n",
        encoding="utf-8",
    )

    parsed_path, manual_review_path, records, manual_review = import_supply_budget_csv(
        csv_path=csv_path,
        vault_root=vault_root,
        project_root=project_root,
        as_of_date=date(2026, 4, 20),
    )

    assert parsed_path.exists()
    assert manual_review_path.exists()
    assert len(records) == 2
    assert len(manual_review) == 1
    parsed_payload = json.loads(parsed_path.read_text(encoding="utf-8"))
    manual_payload = json.loads(manual_review_path.read_text(encoding="utf-8"))
    assert parsed_payload[0]["site_id"] == "7050"
    assert parsed_payload[0]["supply_budget_type"] == "budgeted"
    assert parsed_payload[0]["supply_budget_notes"] == "no consumables included"
    assert manual_payload[0]["site_id"] == "9999"

    updated_text = about_path.read_text(encoding="utf-8")
    assert "supply_budget_type: budgeted" in updated_text
    assert "monthly_supply_budget: 374.59" in updated_text
    assert "supply_budget_notes: no consumables included" in updated_text
    assert "budget_last_verified: 2026-04-20" in updated_text
