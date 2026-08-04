"""Gating tests for first-class employee home addresses (Phase 1).

Contract (design spec 2026-07-28, employee-home-address-coverage-opportunities):

  * `add_person` accepts an optional structured `home_address`; malformed or
    unknown address fields fail validation; omitting the address stays valid.
  * `set_employee_home_address` adds, replaces, and clears the address on the
    uniquely resolved canonical employee via canonical RMW; replays are
    idempotent; unresolved/ambiguous targets fail without writing.
  * Unrelated canonical fields survive every mutation.
  * Privacy: static vault projections never render the address, and queue log
    lines never contain it. All fixture addresses are obviously fictional.
"""

from __future__ import annotations

import pytest

import queue_spec as qs
from btq_vault import projector
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import employee_updates, people
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_ADD_PERSON, JOB_SET_EMPLOYEE_HOME_ADDRESS
from tests.test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)


FICTIONAL_ADDRESS = {
    "line1": "123 Example Street",
    "line2": "Apt 9",
    "city": "Exampletown",
    "state": "PA",
    "postal_code": "15900",
    "country": "US",
}

MINIMAL_ADDRESS = {
    "line1": "456 Sample Road",
    "city": "Sampleville",
    "state": "PA",
    "postal_code": "15901",
}


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


def _payload(**overrides: object) -> dict:
    payload: dict = {
        "person": "9001",
        "actor": "Sandbox Operator",
        "home_address": dict(FICTIONAL_ADDRESS),
        "source": "operator_confirmed",
    }
    payload.update(overrides)
    for key in [key for key, value in payload.items() if value is None]:
        del payload[key]
    return payload


# ---------------------------------------------------------------------------
# Validation: set_employee_home_address
# ---------------------------------------------------------------------------

def test_validate_set_home_address_full_and_minimal() -> None:
    assert qs.validate_job({"job_type": JOB_SET_EMPLOYEE_HOME_ADDRESS, "payload": _payload()})
    assert qs.validate_job(
        {"job_type": JOB_SET_EMPLOYEE_HOME_ADDRESS, "payload": _payload(home_address=dict(MINIMAL_ADDRESS))}
    )


def test_validate_clear_action() -> None:
    assert qs.validate_job(
        {
            "job_type": JOB_SET_EMPLOYEE_HOME_ADDRESS,
            "payload": {"person": "9001", "actor": "Sandbox Operator", "action": "clear"},
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        # Missing person / actor.
        {"actor": "Sandbox Operator", "home_address": dict(FICTIONAL_ADDRESS)},
        {"person": "9001", "home_address": dict(FICTIONAL_ADDRESS)},
        # Set with no address at all.
        {"person": "9001", "actor": "Sandbox Operator"},
        # Address must be a dict, not free text.
        _payload(home_address="123 Example Street, Exampletown PA"),
        # Unknown address key.
        _payload(home_address={**MINIMAL_ADDRESS, "apartment": "9"}),
        # Missing required core field.
        _payload(home_address={"line1": "123 Example Street", "state": "PA", "postal_code": "15900"}),
        # Empty required field.
        _payload(home_address={**MINIMAL_ADDRESS, "city": "  "}),
        # Nested structure smuggled into a field.
        _payload(home_address={**MINIMAL_ADDRESS, "line2": {"unit": 9}}),
        # Malformed US postal code.
        _payload(home_address={**MINIMAL_ADDRESS, "postal_code": "159"}),
        # Clear must not also carry an address.
        _payload(action="clear"),
        # Unknown action.
        _payload(action="erase"),
        # Unknown payload key.
        _payload(note="remember this"),
    ],
)
def test_validate_set_home_address_rejects_bad_payload(payload: dict) -> None:
    assert not qs.validate_job({"job_type": JOB_SET_EMPLOYEE_HOME_ADDRESS, "payload": payload})


def test_validate_non_us_postal_code_not_forced_to_us_format() -> None:
    address = {**MINIMAL_ADDRESS, "postal_code": "SW1A 1AA", "country": "GB"}
    assert qs.validate_job(
        {"job_type": JOB_SET_EMPLOYEE_HOME_ADDRESS, "payload": _payload(home_address=address)}
    )


# ---------------------------------------------------------------------------
# Validation: add_person carries an optional home_address
# ---------------------------------------------------------------------------

def test_validate_add_person_accepts_home_address_and_stays_valid_without() -> None:
    base = {"name": "Sandy Sandbox", "role": "Cleaner"}
    assert qs.validate_job({"job_type": JOB_ADD_PERSON, "payload": dict(base)})
    assert qs.validate_job(
        {"job_type": JOB_ADD_PERSON, "payload": {**base, "home_address": dict(FICTIONAL_ADDRESS)}}
    )


@pytest.mark.parametrize(
    "home_address",
    [
        "123 Example Street",
        {**MINIMAL_ADDRESS, "county": "Example"},
        {"line1": "123 Example Street"},
    ],
)
def test_validate_add_person_rejects_malformed_home_address(home_address: object) -> None:
    payload = {"name": "Sandy Sandbox", "role": "Cleaner", "home_address": home_address}
    assert not qs.validate_job({"job_type": JOB_ADD_PERSON, "payload": payload})


def test_add_person_doc_stores_normalized_home_address() -> None:
    doc = people._build_employee_entity_doc(
        {
            "name": "Sandy Sandbox",
            "role": "Cleaner",
            "home_address": {**FICTIONAL_ADDRESS, "line2": None, "city": " Exampletown "},
        },
        job(JOB_ADD_PERSON, {"name": "Sandy Sandbox", "role": "Cleaner"}, job_id="add-sandy"),
        "sandbox_sandy",
        "2026-07-28",
    )
    assert doc["home_address"] == {
        "line1": "123 Example Street",
        "city": "Exampletown",
        "state": "PA",
        "postal_code": "15900",
        "country": "US",
    }


# ---------------------------------------------------------------------------
# Handler: add, replace, clear — canonical RMW with audit fields
# ---------------------------------------------------------------------------

def _run(store: RecordingRmwVaultStore, payload: dict, tmp_path, job_id: str = "set-sandy-address"):
    context = context_for(tmp_path)
    queue_file = make_queue_file(context, job_id)
    processed_dir = context.runtime_root / "processed"
    employee_updates.process_set_employee_home_address_job(
        queue_file,
        job(JOB_SET_EMPLOYEE_HOME_ADDRESS, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return context, queue_file, processed_dir


def test_set_home_address_adds_address_and_preserves_unrelated_fields(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _context, queue_file, processed_dir = _run(store, _payload(), tmp_path)

    doc = store.get_optional("employee_sandbox_sandy")
    assert doc is not None
    assert doc["home_address"] == FICTIONAL_ADDRESS
    assert doc["home_address_source"] == "operator_confirmed"
    assert doc["edited_by"] == "Sandbox Operator"
    assert doc["updated_at"]
    # Unrelated canonical fields survive.
    assert doc["employee_id"] == "9001"
    assert doc["phone"] == "2025550100"
    assert doc["job"] == "600"
    assert doc["status"] == "active"
    assert doc["btq_job_ids"] == ["prior-job", "set-sandy-address"]
    assert (processed_dir / queue_file.name).exists()


def test_set_home_address_replaces_existing_address(tmp_path, monkeypatch) -> None:
    existing = employee_doc()
    existing["home_address"] = dict(MINIMAL_ADDRESS)
    store = RecordingRmwVaultStore([existing])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run(store, _payload(), tmp_path, job_id="replace-sandy-address")

    doc = store.get_optional("employee_sandbox_sandy")
    assert doc["home_address"] == FICTIONAL_ADDRESS


def test_clear_home_address_removes_address_and_provenance(tmp_path, monkeypatch) -> None:
    existing = employee_doc()
    existing["home_address"] = dict(FICTIONAL_ADDRESS)
    existing["home_address_source"] = "operator_confirmed"
    store = RecordingRmwVaultStore([existing])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run(
        store,
        {"person": "9001", "actor": "Sandbox Operator", "action": "clear"},
        tmp_path,
        job_id="clear-sandy-address",
    )

    doc = store.get_optional("employee_sandbox_sandy")
    assert "home_address" not in doc
    assert "home_address_source" not in doc
    assert doc["name"] == "Sandy Sandbox"


def test_replay_with_same_job_id_is_idempotent(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    _run(store, _payload(), tmp_path / "first-run", job_id="set-sandy-address")
    writes_after_first = len(store.update_doc_calls)
    # A replay arrives in a fresh runtime (e.g. an iCloud re-sync); the job-id
    # marker on the canonical doc is what must make it a no-op.
    _run(store, _payload(home_address=dict(MINIMAL_ADDRESS)), tmp_path / "replay-run", job_id="set-sandy-address")

    doc = store.get_optional("employee_sandbox_sandy")
    # The replay is a no-op: the first write's address survives.
    assert doc["home_address"] == FICTIONAL_ADDRESS
    assert len(store.update_doc_calls) == writes_after_first
    assert doc["btq_job_ids"].count("set-sandy-address") == 1


def test_unresolved_person_fails_without_writing(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    with pytest.raises(shared.QueueProcessorError):
        _run(store, _payload(person="nobody-known"), tmp_path, job_id="set-nobody-address")

    assert not store.update_doc_calls
    assert "home_address" not in store.get_optional("employee_sandbox_sandy")


def test_ambiguous_person_fails_without_writing(tmp_path, monkeypatch) -> None:
    twin_one = employee_doc()
    twin_two = employee_doc()
    twin_two["_id"] = "employee_sandbox_sam"
    twin_two["person_id"] = "sandbox_sam"
    twin_two["name"] = "Sam Sandbox"
    twin_two["employee_id"] = "9001"  # duplicate eHub id — ambiguous reference
    store = RecordingRmwVaultStore([twin_one, twin_two])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    with pytest.raises(shared.QueueProcessorError):
        _run(store, _payload(person="9001"), tmp_path, job_id="set-twin-address")

    assert not store.update_doc_calls


# ---------------------------------------------------------------------------
# Registry dispatch
# ---------------------------------------------------------------------------

def test_registry_dispatches_home_address_job() -> None:
    assert JOB_HANDLERS[JOB_SET_EMPLOYEE_HOME_ADDRESS] is employee_updates.process_set_employee_home_address_job


# ---------------------------------------------------------------------------
# Privacy: projections and logs never carry the address
# ---------------------------------------------------------------------------

def test_vault_entity_projection_never_renders_home_address() -> None:
    doc = employee_doc()
    doc["home_address"] = dict(FICTIONAL_ADDRESS)
    doc["home_address_source"] = "operator_confirmed"

    page = projector.build_entity_detail(doc)

    assert "123 Example Street" not in page
    assert "Exampletown" not in page
    assert "home_address" not in page
    # The page still renders the employee normally.
    assert "Sandy Sandbox" in page


def test_queue_log_lines_never_contain_the_address(tmp_path, monkeypatch) -> None:
    store = RecordingRmwVaultStore([employee_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)

    context, _queue_file, _processed_dir = _run(store, _payload(), tmp_path)

    log_text = context.log_path.read_text(encoding="utf-8")
    assert "123 Example Street" not in log_text
    assert "Exampletown" not in log_text
    assert "set-employee-home-address" in log_text
