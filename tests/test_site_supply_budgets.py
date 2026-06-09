from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Callable

from site_supply_budgets import apply_supply_budget_updates, import_supply_budget_csv, parse_supply_budget_lines


class RecordingBudgetStore:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.update_doc_calls: list[str] = []

    def find_location_docs(self) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs if doc.get("type") == "location"]

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.update_doc_calls.append(doc_id)
        for index, doc in enumerate(self.docs):
            if doc.get("_id") != doc_id:
                continue
            updated = transform(dict(doc))
            if updated is None:
                return dict(doc)
            self.docs[index] = dict(updated)
            return dict(updated)
        raise RuntimeError(f"missing doc: {doc_id}")


def location_doc(*, account: str, location: str, job: str) -> dict[str, Any]:
    return {
        "_id": f"location_{job}",
        "type": "location",
        "account": account,
        "location": location,
        "job": job,
        "existing_field": "keep-me",
    }


def test_parse_supply_budget_lines_normalizes_budget_types() -> None:
    store = RecordingBudgetStore(
        [
            location_doc(account="Summitsteel", location="Summit Wire", job="7050"),
            location_doc(account="Wgtco", location="Western Gas Transmission", job="7030"),
            location_doc(account="Apexco", location="Apex Powdered Metals", job="7080"),
        ]
    )

    lines = [
        "Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED",
        "Western Gas Transmission #7030 Supplies Included: $125.00",
        "Apex Powdered Metals #7080 LABOR ONLY",
        "Unknown Site #9999",
    ]

    records, manual_review = parse_supply_budget_lines(lines, store)

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


def test_apply_supply_budget_updates_preserves_existing_fields() -> None:
    store = RecordingBudgetStore([location_doc(account="Summitsteel", location="Summit Wire", job="7050")])

    records, manual_review = apply_supply_budget_updates(
        ["Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59"],
        store,
        as_of_date=date(2026, 4, 20),
    )

    updated_doc = store.get_optional("location_7050")
    assert updated_doc is not None
    assert manual_review == []
    assert records[0]["monthly_supply_budget"] == 374.59
    assert records[0]["supply_budget_notes"] is None
    assert store.update_doc_calls == ["location_7050"]
    assert updated_doc["existing_field"] == "keep-me"
    assert updated_doc["supply_budget_type"] == "budgeted"
    assert updated_doc["monthly_supply_budget"] == "374.59"
    assert updated_doc["supply_budget_notes"] is None
    assert updated_doc["budget_last_verified"] == "2026-04-20"


def test_apply_supply_budget_updates_marks_unknown_for_manual_review() -> None:
    store = RecordingBudgetStore([location_doc(account="Apexco", location="Apex Powdered Metals", job="7080")])

    records, manual_review = apply_supply_budget_updates(
        ["Apex Powdered Metals #7080 contract renewal pending"],
        store,
        as_of_date=date(2026, 4, 20),
    )

    updated_doc = store.get_optional("location_7080")
    assert updated_doc is not None
    assert records[0]["supply_budget_type"] == "unknown"
    assert records[0]["monthly_supply_budget"] is None
    assert manual_review == records
    assert updated_doc["supply_budget_type"] == "unknown"
    assert updated_doc["monthly_supply_budget"] is None
    assert updated_doc["supply_budget_notes"] is None


def test_apply_supply_budget_updates_marks_missing_location_doc_for_manual_review() -> None:
    store = RecordingBudgetStore([])

    records, manual_review = apply_supply_budget_updates(
        ["Missing Site #9999 Basic Supply Budget: $25.00"],
        store,
        as_of_date=date(2026, 4, 20),
    )

    assert records[0]["site_id"] == "9999"
    assert records[0]["monthly_supply_budget"] == 25.0
    assert manual_review == records
    assert store.update_doc_calls == []


def test_basic_supply_budget_with_restriction_stays_budgeted() -> None:
    store = RecordingBudgetStore([location_doc(account="Summitsteel", location="Summit Wire", job="7050")])

    records, manual_review = apply_supply_budget_updates(
        ["Summit Wire Riverton #7050 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED"],
        store,
        as_of_date=date(2026, 4, 20),
    )

    updated_doc = store.get_optional("location_7050")
    assert updated_doc is not None
    assert manual_review == []
    assert records[0]["supply_budget_type"] == "budgeted"
    assert records[0]["monthly_supply_budget"] == 374.59
    assert records[0]["supply_budget_notes"] == "no consumables included"
    assert updated_doc["supply_budget_type"] == "budgeted"
    assert updated_doc["monthly_supply_budget"] == "374.59"
    assert updated_doc["supply_budget_notes"] == "no consumables included"


def test_restriction_extraction_normalizes_phrase() -> None:
    store = RecordingBudgetStore([location_doc(account="Wgtco", location="Western Gas Transmission", job="7030")])

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Basic Supply Budget: $40.00 - no liners or paper products included (Provided by Customer)"],
        store,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 40.0
    assert records[0].supply_budget_notes == "no liners or paper products included"


def test_except_disposables_restriction_is_captured() -> None:
    store = RecordingBudgetStore([location_doc(account="Wgtco", location="Western Gas Transmission", job="7030")])

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Basic Supply Budget:$83 (except disposables)"],
        store,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 83.0
    assert records[0].supply_budget_notes == "except disposables"


def test_supplies_amount_is_treated_as_budgeted() -> None:
    store = RecordingBudgetStore([location_doc(account="Apexco", location="Apex Powdered Metals", job="7080")])

    records, _manual_review = parse_supply_budget_lines(
        ["Apex Powdered Metals, Inc. #7080 Supplies: $51.24 - no PT, TP, liners or soap"],
        store,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 51.24
    assert records[0].supply_budget_notes == "no pt, tp, liners or soap"


def test_paper_towels_restriction_is_captured() -> None:
    store = RecordingBudgetStore([location_doc(account="RHN", location="RHN-Lakeshore Community", job="7020")])

    records, _manual_review = parse_supply_budget_lines(
        ["RHN-Lakeshore Community #7020 Basic Supply Budget: $39 - no liners or paper towels included (Provided by Customer)"],
        store,
    )

    assert records[0].supply_budget_type == "budgeted"
    assert records[0].monthly_supply_budget == 39.0
    assert records[0].supply_budget_notes == "no liners or paper towels included"


def test_no_supplies_is_excluded() -> None:
    store = RecordingBudgetStore([location_doc(account="Wgtco", location="Western Gas Transmission", job="7030")])

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 No Supplies"],
        store,
    )

    assert records[0].supply_budget_type == "excluded"
    assert records[0].monthly_supply_budget is None
    assert records[0].supply_budget_notes is None


def test_labor_only_is_excluded() -> None:
    store = RecordingBudgetStore([location_doc(account="Wgtco", location="Western Gas Transmission", job="7030")])

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 LABOR ONLY"],
        store,
    )

    assert records[0].supply_budget_type == "excluded"
    assert records[0].monthly_supply_budget is None


def test_supplies_included_is_included() -> None:
    store = RecordingBudgetStore([location_doc(account="Wgtco", location="Western Gas Transmission", job="7030")])

    records, _manual_review = parse_supply_budget_lines(
        ["Western Gas Transmission #7030 Supplies Included: $125.00"],
        store,
    )

    assert records[0].supply_budget_type == "included"
    assert records[0].monthly_supply_budget == 125.0
    assert records[0].supply_budget_notes is None


def test_import_supply_budget_csv_updates_canonical_docs_and_writes_outputs(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    store = RecordingBudgetStore([location_doc(account="Summitsteel", location="Summit Wire", job="7050")])

    csv_path = tmp_path / "budgets.csv"
    csv_path.write_text(
        "site,notes\n"
        "\"Summit Wire Riverton #7050\",\"Amount: $12,319.61 ... Basic Supply Budget: $374.59, NO CONSUMABLES INCLUDED\"\n"
        "\"Unknown Site #9999\",\"Amount: $500\"\n",
        encoding="utf-8",
    )

    parsed_path, manual_review_path, records, manual_review = import_supply_budget_csv(
        csv_path=csv_path,
        project_root=project_root,
        store=store,
        as_of_date=date(2026, 4, 20),
    )

    updated_doc = store.get_optional("location_7050")
    assert updated_doc is not None
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

    assert updated_doc["supply_budget_type"] == "budgeted"
    assert updated_doc["monthly_supply_budget"] == "374.59"
    assert updated_doc["supply_budget_notes"] == "no consumables included"
    assert updated_doc["budget_last_verified"] == "2026-04-20"
