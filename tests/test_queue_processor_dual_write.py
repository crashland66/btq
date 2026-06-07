from __future__ import annotations

from pathlib import Path
from typing import Any

from btq_vault.markdown_export import export_entity, render_entity_markdown


def employee_doc() -> dict[str, Any]:
    return {
        "_id": "employee_per_test",
        "type": "employee",
        "person_id": "per_test",
        "name": "Eric Daniel Dalton",
        "employee_id": "567",
        "role": "Cleaner",
        "status": "active",
        "created_at": "2026-05-31",
        "btq_job_ids": ["job-add-person"],
        "vault_path": "People/Dalton, Eric Daniel.md",
    }


def visit_doc() -> dict[str, Any]:
    return {
        "_id": "visit_7050_2026-05-31_job",
        "type": "visit",
        "site": "Summit Wire",
        "site_id": "7050",
        "date": "2026-05-31",
        "timestamp": "2026-05-31T12:00:00+00:00",
        "visit_key": "Summit Wire:2026-05-31",
        "source": "ingestion",
        "confidence": "high",
        "evidence": "Visited Summit Wire.",
        "btq_job_ids": ["job-visit-create"],
        "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Visits/2026-05-31.md",
    }


def site_issue_doc() -> dict[str, Any]:
    return {
        "_id": "site_issue_iss_summit_drain",
        "type": "site_issue",
        "issue_id": "iss_summit_drain",
        "site_id": "7050",
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "title": "Restroom drain backup",
        "summary": "Drain backed up.",
        "observations": ["Drain backed up."],
        "category": "maintenance",
        "priority": "high",
        "status": "open",
        "observed_at": "2026-05-06T18:27:03-04:00",
        "reported_by": "Tom",
        "source": "field_capture",
        "resolution_trigger": "Maintenance confirms the drain is clear.",
        "created_at": "2026-05-31T12:00:00+00:00",
        "btq_job_ids": ["job-log-site-issue"],
        "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md",
    }


def supply_doc() -> dict[str, Any]:
    return {
        "_id": "supply_need_sup_summit_brightwash",
        "type": "supply_need",
        "supply_id": "sup_summit_brightwash",
        "site_id": "7050",
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "item_name": "BrightWash cleaner",
        "quantity_needed": "2 bottles",
        "urgency": "high",
        "requested_by": "Tom",
        "observed_at": "2026-05-08T14:12:43+00:00",
        "source": "field_capture",
        "status": "open",
        "created_at": "2026-05-31T12:00:00+00:00",
        "btq_job_ids": ["job-log-supply-need"],
        "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Supplies/sup_summit_brightwash__brightwash-cleaner.md",
    }


def equipment_doc() -> dict[str, Any]:
    return {
        "_id": "equipment_request_eqr_summit_vacuum",
        "type": "equipment_request",
        "equipment_id": "eqr_summit_vacuum",
        "site_id": "7050",
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "equipment_name": "vacuum",
        "reason": "Current vacuum will not start.",
        "priority": "urgent",
        "requested_by": "Tom",
        "observed_at": "2026-05-08T14:12:43+00:00",
        "source": "field_capture",
        "status": "open",
        "created_at": "2026-05-31T12:00:00+00:00",
        "btq_job_ids": ["job-log-equipment-request"],
        "vault_path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/Equipment/eqr_summit_vacuum__vacuum.md",
    }


def personnel_event_doc() -> dict[str, Any]:
    return {
        "_id": "personnel_event_evt_training",
        "type": "personnel_event",
        "event_id": "evt_training",
        "employee": "Eric Daniel Dalton",
        "event_type": "training",
        "summary": "Completed floor care training.",
        "reported_by": "Tom",
        "occurred_at": "2026-05-31T12:00:00+00:00",
        "related_site": "7050",
        "created_at": "2026-05-31T12:00:00+00:00",
        "btq_job_ids": ["job-log-personnel-event"],
        "vault_path": "People/Dalton, Eric Daniel/Events/evt_training__completed-floor-care-training.md",
    }


def test_employee_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(employee_doc()) or (None, "")

    assert path == Path("People/Dalton, Eric Daniel.md")
    assert text == (
        "---\n"
        "type: person\n"
        "person_id: per_test\n"
        "\n"
        "name: Eric Daniel Dalton\n"
        "first: Eric Daniel\n"
        "last: Dalton\n"
        "employee_id: 567\n"
        "\n"
        "role: Cleaner\n"
        "status: active\n"
        "\n"
        "created: 2026-05-31\n"
        "source: btq\n"
        "\n"
        "btq_job_ids:\n"
        "  - job-add-person\n"
        "---\n"
        "\n"
        "# Eric Daniel Dalton\n"
        "\n"
        "## Notes\n"
        "\n"
        "## Schedule\n"
        "\n"
        "## Training\n"
        "\n"
        "## Incidents\n"
    )


def test_visit_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(visit_doc()) or (None, "")

    assert path == Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Visits/2026-05-31.md")
    assert text == (
        "---\n"
        "type: visit\n"
        "timestamp: 2026-05-31T12:00:00+00:00\n"
        "site: Summit Wire\n"
        "date: 2026-05-31\n"
        'visit_key: "Summit Wire:2026-05-31"\n'
        "source: ingestion\n"
        "confidence: high\n"
        "evidence: Visited Summit Wire.\n"
        "btq_job_ids:\n"
        "  - job-visit-create\n"
        "---\n"
    )


def test_site_issue_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(site_issue_doc()) or (None, "")

    assert path == Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md")
    assert text.startswith("---\ntype: site_issue\nissue_id: iss_summit_drain\n")
    assert "title: Restroom drain backup\n" in text
    assert "## Evidence\n- No structured evidence references supplied.\n" in text
    assert text.endswith("- 2026-05-31T12:00:00+00:00: queue job job-log-site-issue logged/updated this issue.\n")


def test_supply_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(supply_doc()) or (None, "")

    assert path == Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Supplies/sup_summit_brightwash__brightwash-cleaner.md")
    assert text.startswith("---\ntype: supply_need\nsupply_id: sup_summit_brightwash\n")
    assert "item_name: BrightWash cleaner\n" in text
    assert "## Notes\nNo notes recorded.\n" in text
    assert text.endswith("- 2026-05-31T12:00:00+00:00: queue job job-log-supply-need logged/updated this supply need.\n")


def test_equipment_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(equipment_doc()) or (None, "")

    assert path == Path("Accounts/Summitsteel/Locations/7050 - Summit Wire/Equipment/eqr_summit_vacuum__vacuum.md")
    assert text.startswith("---\ntype: equipment_request\nequipment_id: eqr_summit_vacuum\n")
    assert "equipment_name: vacuum\n" in text
    assert "## Reason\nCurrent vacuum will not start.\n" in text
    assert text.endswith("- 2026-05-31T12:00:00+00:00: queue job job-log-equipment-request logged/updated this equipment request.\n")


def test_personnel_event_adapter_projection_is_byte_stable() -> None:
    path, text = render_entity_markdown(personnel_event_doc()) or (None, "")

    assert path == Path("People/Dalton, Eric Daniel/Events/evt_training__completed-floor-care-training.md")
    assert text.startswith("---\ntype: personnel_event\nevent_id: evt_training\n")
    assert "employee: Eric Daniel Dalton\n" in text
    assert "## Summary\nCompleted floor care training.\n" in text
    assert text.endswith("- 2026-05-31T12:00:00+00:00: queue job job-log-personnel-event logged/updated this personnel event.\n")


def test_export_entity_writes_projection_only_when_invoked(tmp_path: Path) -> None:
    target = export_entity(site_issue_doc(), tmp_path)

    assert target == tmp_path / "Accounts/Summitsteel/Locations/7050 - Summit Wire/Issues/iss_summit_drain__restroom-drain-backup.md"
    assert target.read_text(encoding="utf-8").startswith("---\ntype: site_issue\n")
