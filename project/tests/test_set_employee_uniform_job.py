from __future__ import annotations

import pytest

import queue_spec as qs
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import employee_updates
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_SET_EMPLOYEE_UNIFORM
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
        "phone": "2025550100",
        "btq_job_ids": ["prior-job"],
    }


def _payload(**uniform_overrides: object) -> dict:
    uniform = {"status": "needs_shirts", "shirt_count": 1, "shirt_size": "2XL"}
    uniform.update(uniform_overrides)
    return {
        "person": "9001",
        "actor": "Sandbox Operator",
        "uniform": uniform,
        "source": "employee_reply",
    }


def test_validate_employee_uniform_statuses() -> None:
    assert qs.validate_job({"job_type": JOB_SET_EMPLOYEE_UNIFORM, "payload": _payload()})
    assert qs.validate_job(
        {
            "job_type": JOB_SET_EMPLOYEE_UNIFORM,
            "payload": _payload(status="adequate", shirt_count=3, shirt_size="L"),
        }
    )
    assert qs.validate_job(
        {
            "job_type": JOB_SET_EMPLOYEE_UNIFORM,
            "payload": {
                "person": "9001",
                "actor": "Sandbox Operator",
                "uniform": {"status": "unknown"},
            },
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "Sandbox Operator", "uniform": {"status": "unknown"}},
        {"person": "9001", "uniform": {"status": "unknown"}},
        _payload(status="needs_shirts", shirt_count=0, shirt_size=""),
        _payload(status="needs_shirts", shirt_count=None, shirt_size="L"),
        _payload(status="adequate", shirt_count=None),
        _payload(status="maybe", shirt_count=2),
        _payload(status="adequate", shirt_count=-1),
        _payload(status="adequate", shirt_count=100),
        _payload(status="adequate", shirt_count=True),
        {**_payload(), "extra": "not allowed"},
    ],
)
def test_validate_employee_uniform_rejects_bad_payload(payload: dict) -> None:
    assert not qs.validate_job({"job_type": JOB_SET_EMPLOYEE_UNIFORM, "payload": payload})


def _run(store: RecordingRmwVaultStore, payload: dict, tmp_path, job_id: str = "set-sandy-uniform"):
    context = context_for(tmp_path)
    queue_file = make_queue_file(context, job_id)
    processed_dir = context.runtime_root / "processed"
    employee_updates.process_set_employee_uniform_job(
        queue_file,
        job(JOB_SET_EMPLOYEE_UNIFORM, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return context, queue_file, processed_dir


def test_set_employee_uniform_updates_only_uniform_and_audit_fields(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _context, queue_file, processed_dir = _run(store, _payload(), tmp_path)

    doc = store.get_optional("employee_sandbox_sandy")
    assert doc is not None
    assert doc["uniform_status"] == "needs_shirts"
    assert doc["uniform_shirt_count"] == 1
    assert doc["uniform_shirt_size"] == "2XL"
    assert doc["uniform_source"] == "employee_reply"
    assert doc["uniform_updated_at"]
    assert doc["uniform_updated_by"] == "Sandbox Operator"
    assert doc["phone"] == "2025550100"
    assert doc["job"] == "600"
    assert doc["btq_job_ids"] == ["prior-job", "set-sandy-uniform"]
    assert (processed_dir / queue_file.name).exists()


def test_unknown_status_clears_stale_count_and_size(tmp_path, monkeypatch) -> None:
    existing = employee_doc()
    existing.update(
        uniform_status="needs_shirts",
        uniform_shirt_count=0,
        uniform_shirt_size="XL",
    )
    store = RecordingRmwVaultStore([existing])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run(
        store,
        {
            "person": "9001",
            "actor": "Sandbox Operator",
            "uniform": {"status": "unknown"},
        },
        tmp_path,
    )

    doc = store.get_optional("employee_sandbox_sandy")
    assert doc["uniform_status"] == "unknown"
    assert "uniform_shirt_count" not in doc
    assert "uniform_shirt_size" not in doc


def test_registry_dispatches_employee_uniform_job() -> None:
    assert JOB_HANDLERS[JOB_SET_EMPLOYEE_UNIFORM] is employee_updates.process_set_employee_uniform_job
