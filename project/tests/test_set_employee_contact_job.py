from __future__ import annotations

import pytest

import queue_spec as qs
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import employee_updates, people
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_ADD_PERSON, JOB_SET_EMPLOYEE_CONTACT
from tests.test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)


def employee_doc() -> dict:
    return {
        "_id": "employee_sandbox_sandy",
        "type": "employee",
        "operator": "op_sandbox",
        "person_id": "sandbox_sandy",
        "employee_id": "9001",
        "name": "Sandy Sandbox",
        "status": "active",
        "job": "600",
        "btq_job_ids": ["prior-job"],
    }


def test_validate_set_employee_contact() -> None:
    assert qs.validate_job(
        {
            "job_type": JOB_SET_EMPLOYEE_CONTACT,
            "payload": {
                "person": "9001",
                "actor": "Sandbox Operator",
                "contact": {"phone": "2025550100"},
                "source": "employee_message",
            },
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "Sandbox Operator", "contact": {"phone": "2025550100"}},
        {"person": "9001", "contact": {"phone": "2025550100"}},
        {"person": "9001", "actor": "Sandbox Operator", "contact": {}},
        {"person": "9001", "actor": "Sandbox Operator", "contact": {"mobile": "2025550100"}},
        {"person": "9001", "actor": "Sandbox Operator", "contact": {"phone": ""}},
    ],
)
def test_validate_set_employee_contact_rejects_bad_payload(payload: dict) -> None:
    assert not qs.validate_job({"job_type": JOB_SET_EMPLOYEE_CONTACT, "payload": payload})


def test_set_employee_contact_updates_only_contact_and_audit_fields(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)
    queue_file = make_queue_file(context, "set-sandy-contact")
    processed_dir = context.runtime_root / "processed"

    employee_updates.process_set_employee_contact_job(
        queue_file,
        job(
            JOB_SET_EMPLOYEE_CONTACT,
            {
                "person": "9001",
                "actor": "Sandbox Operator",
                "contact": {"phone": "2025550100"},
                "source": "employee_message",
            },
            job_id="set-sandy-contact",
        ),
        context,
        processed_dir,
    )

    doc = store.get_optional("employee_sandbox_sandy")
    assert doc is not None
    assert doc["phone"] == "2025550100"
    assert doc["employee_id"] == "9001"
    assert doc["job"] == "600"
    assert doc["edited_by"] == "Sandbox Operator"
    assert doc["btq_job_ids"] == ["prior-job", "set-sandy-contact"]
    assert (processed_dir / queue_file.name).exists()


def test_add_person_flattens_validated_contact_fields() -> None:
    doc = people._build_employee_entity_doc(
        {
            "name": "Sandy Sandbox",
            "role": "Cleaner",
            "contact": {"phone": "2025550100", "email": "sandy.sandbox@example.com"},
        },
        job(JOB_ADD_PERSON, {"name": "Sandy Sandbox", "role": "Cleaner"}, job_id="add-sandy"),
        "sandbox_sandy",
        "2026-07-13",
    )
    assert doc["phone"] == "2025550100"
    assert doc["email"] == "sandy.sandbox@example.com"


def test_registry_dispatches_employee_contact_job() -> None:
    assert JOB_HANDLERS[JOB_SET_EMPLOYEE_CONTACT] is employee_updates.process_set_employee_contact_job
