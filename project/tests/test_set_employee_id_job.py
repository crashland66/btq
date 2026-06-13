"""Independent verification of the `set_employee_id` queue job.

Authored by an INDEPENDENT VERIFIER. These tests do not trust the
implementation: they author behavioral gating assertions against the contract,
probe beyond the stated spec, and are designed to go RED under plausible
mutations of the handler.

Harness reuse: the in-memory `RecordingRmwVaultStore`, `context_for`,
`make_queue_file`, and `job` helpers are imported from the existing CouchDB
write test module rather than re-invented. The fake store's inherited
`find_employee_docs()` returns every `type == "employee"` doc in `self.docs`,
which is exactly what both `resolve_employee_target` and the handler's
cross-person collision guard consume.
"""
from __future__ import annotations

import pytest

import queue_spec as qs
from queue_spec import JOB_SET_EMPLOYEE_ID
from queue_processor.registry import JOB_HANDLERS
from queue_processor.handlers import employee_updates
from queue_processor.handlers import _shared as shared
from queue_processor.handlers._shared import QueueJob, QueueJobError, QueueProcessorError

# Reuse the existing in-memory harness rather than inventing a new one.
from tests.test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)

PROCESS = employee_updates.process_set_employee_id_job


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def employee_doc(
    *,
    person_id: str = "per_01ERICDALTON0000000000",
    employee_id: str | None = None,
    name: str = "Eric Dalton",
    status: str = "active",
    job_ids: list[str] | None = None,
    extra: dict | None = None,
) -> dict:
    doc: dict = {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "person_id": person_id,
        "name": name,
        "status": status,
        "created_at": "2026-01-01T00:00:00+00:00",
        "btq_job_ids": list(job_ids or []),
    }
    if employee_id is not None:
        doc["employee_id"] = employee_id
    if extra:
        doc.update(extra)
    return doc


def set_id_payload(person: str = "Eric Dalton", employee_id="9213", **extra) -> dict:
    payload = {"person": person, "employee_id": employee_id}
    payload.update(extra)
    return payload


def run(store, context, payload, job_id="job-set-id", file_name=None):
    # `file_name` lets a replay run reuse the same logical job_id while landing
    # on a distinct queue file (the real-world replay is the same job_id arriving
    # in a new queue file, not the same file processed twice).
    queue_file = make_queue_file(context, file_name or job_id)
    processed_dir = context.runtime_root / "processed"
    PROCESS(queue_file, job(JOB_SET_EMPLOYEE_ID, payload, job_id=job_id), context, processed_dir)
    return queue_file, processed_dir


# =========================================================================== #
# Contract 1: validate_job
# =========================================================================== #
def test_validate_accepts_wellformed_minimal():
    assert qs.validate_job(
        {"job_type": "set_employee_id", "payload": {"person": "Eric Dalton", "employee_id": "9213"}}
    ) is True


def test_validate_accepts_all_optional_fields():
    assert qs.validate_job(
        {
            "job_type": "set_employee_id",
            "payload": {
                "person": "Eric Dalton",
                "employee_id": "9213",
                "employment_type": "full_time",
                "hire_date": "2026-01-01",
                "status": "active",
                "source": "hr_import",
                "metadata": {"source": "hr"},
            },
        }
    ) is True


@pytest.mark.parametrize(
    "payload",
    [
        {"employee_id": "9213"},  # missing person
        {"person": "", "employee_id": "9213"},  # empty person
        {"person": "   ", "employee_id": "9213"},  # whitespace person
        {"person": 123, "employee_id": "9213"},  # non-string person
        {"person": "Eric Dalton"},  # missing employee_id
        {"person": "Eric Dalton", "employee_id": ""},  # empty employee_id
        {"person": "Eric Dalton", "employee_id": "  "},  # whitespace employee_id
        {"person": "Eric Dalton", "employee_id": None},  # null employee_id
        {"person": "Eric Dalton", "employee_id": "abc"},  # non-numeric employee_id
        {"person": "Eric Dalton", "employee_id": "92-13"},  # invalid chars
        {"person": "Eric Dalton", "employee_id": -1},  # negative int
        {"person": "Eric Dalton", "employee_id": ["9213"]},  # wrong type
        {"person": "Eric Dalton", "employee_id": "9213", "stray": "x"},  # stray key
        {"person": "Eric Dalton", "employee_id": "9213", "name": "Eric"},  # not in allowlist
        {"person": "Eric Dalton", "employee_id": "9213", "employment_type": ""},  # empty opt
        {"person": "Eric Dalton", "employee_id": "9213", "metadata": {"bad": "x"}},  # bad metadata key
    ],
)
def test_validate_rejects_bad_payloads(payload):
    assert qs.validate_job({"job_type": "set_employee_id", "payload": payload}) is False


def test_validate_accepts_int_and_numeric_string_employee_id():
    # Beyond-spec: int 9213 and "9213" both accepted by the spec validator.
    assert qs.validate_job({"job_type": "set_employee_id", "payload": set_id_payload(employee_id=9213)}) is True
    assert qs.validate_job({"job_type": "set_employee_id", "payload": set_id_payload(employee_id="9213")}) is True


# =========================================================================== #
# Contract 2: handler happy path
# =========================================================================== #
def test_happy_path(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton", job_ids=["prior-job"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    queue_file, processed_dir = run(store, context, set_id_payload("Eric Dalton", "9213"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc is not None
    assert doc["employee_id"] == "9213"
    # Identity preserved.
    assert doc["person_id"] == "per_01ERICDALTON0000000000"
    assert doc["_id"] == "employee_per_01ERICDALTON0000000000"
    assert doc["type"] == "employee"
    assert doc["created_at"] == "2026-01-01T00:00:00+00:00"
    # job_id appended (not replacing the prior one).
    assert doc["btq_job_ids"] == ["prior-job", "job-set-id"]
    # Queue file moved to processed.
    assert (processed_dir / queue_file.name).exists()
    assert not queue_file.exists()


def test_happy_path_optional_fields_set(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(
        store,
        context,
        set_id_payload(
            "Eric Dalton",
            "9213",
            employment_type="full_time",
            hire_date="2026-02-01",
            status="active",
        ),
    )

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"
    assert doc["employment_type"] == "full_time"
    assert doc["hire_date"] == "2026-02-01"
    assert doc["status"] == "active"


def test_happy_path_only_whitelisted_optionals_touched(tmp_path, monkeypatch):
    # `source` and `metadata` are spec-allowed payload keys but the handler
    # transform only writes employment_type/hire_date/status onto the doc.
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", "9213", source="hr_import"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"
    # `source` is NOT mirrored onto the canonical employee doc by the transform.
    assert "source" not in doc


def test_preserves_unrelated_preexisting_field(tmp_path, monkeypatch):
    # Beyond-spec: an unrelated pre-existing field must survive the RMW merge.
    store = RecordingRmwVaultStore(
        [employee_doc(name="Eric Dalton", extra={"custom_field": "keep-me", "phone": "555-1234"})]
    )
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", "9213"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["custom_field"] == "keep-me"
    assert doc["phone"] == "555-1234"
    assert doc["employee_id"] == "9213"


def test_status_payload_does_not_clobber_identity(tmp_path, monkeypatch):
    # Beyond-spec: a `status` in the payload should set status but not destroy
    # person_id/_id/type/created_at.
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton", status="active")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", "9213", status="on_leave"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["status"] == "on_leave"
    assert doc["person_id"] == "per_01ERICDALTON0000000000"
    assert doc["_id"] == "employee_per_01ERICDALTON0000000000"
    assert doc["type"] == "employee"
    assert doc["created_at"] == "2026-01-01T00:00:00+00:00"


# =========================================================================== #
# Beyond-spec: resolution by person_id / employee_id / name; int payload
# =========================================================================== #
def test_resolves_by_person_id(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("per_01ERICDALTON0000000000", "9213"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"


def test_resolves_by_existing_employee_id(tmp_path, monkeypatch):
    # Person reference is an existing employee_id; new employee_id is set.
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton", employee_id="OLD-1")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("OLD-1", "9213"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"


def test_resolves_by_name(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", "9213"))

    assert store.get_optional("employee_per_01ERICDALTON0000000000")["employee_id"] == "9213"


def test_int_payload_employee_id_stored_as_numeric_string(tmp_path, monkeypatch):
    # Beyond-spec: payload employee_id given as int -> handler str()/strip ->
    # canonical value is the numeric string "9213".
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", 9213))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"


# =========================================================================== #
# Contract 3: replay safety
# =========================================================================== #
def test_replay_same_job_id_is_noop(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    run(store, context, set_id_payload("Eric Dalton", "9213"), job_id="replay-1", file_name="replay-1a")
    doc_after_first = dict(store.get_optional("employee_per_01ERICDALTON0000000000"))
    assert doc_after_first["btq_job_ids"] == ["replay-1"]
    calls_after_first = list(store.update_doc_calls)

    # Second run with the SAME job_id (new queue file) must not double-apply.
    queue_file, processed_dir = run(
        store, context, set_id_payload("Eric Dalton", "9213"), job_id="replay-1", file_name="replay-1b"
    )
    doc_after_second = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc_after_second == doc_after_first
    assert doc_after_second["btq_job_ids"] == ["replay-1"]  # not duplicated
    # apply_canonical_rmw returns None (marker present) -> the handler skips the
    # mutation. update_doc IS entered (the marker check is inside the RMW), but
    # the transform is short-circuited so the stored doc is byte-identical.
    assert store.docs == [doc_after_first]
    # The second queue file is still moved to processed.
    assert (processed_dir / queue_file.name).exists()


# =========================================================================== #
# Contract 4: fail-safe (unresolvable + ambiguous)
# =========================================================================== #
def test_unresolvable_person_raises_no_write(tmp_path, monkeypatch):
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    with pytest.raises(QueueProcessorError):
        run(store, context, set_id_payload("Nonexistent Person", "9213"))

    assert store.update_doc_calls == []
    assert store.get_optional("employee_per_01ERICDALTON0000000000").get("employee_id") is None


def test_ambiguous_person_raises_no_write(tmp_path, monkeypatch):
    # Two employees share the name -> ambiguous name match raises.
    store = RecordingRmwVaultStore(
        [
            employee_doc(person_id="per_A", name="Eric Dalton"),
            employee_doc(person_id="per_B", name="Eric Dalton"),
        ]
    )
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    with pytest.raises(QueueProcessorError, match="[Aa]mbiguous"):
        run(store, context, set_id_payload("Eric Dalton", "9213"))

    assert store.update_doc_calls == []
    assert store.get_optional("employee_per_A").get("employee_id") is None
    assert store.get_optional("employee_per_B").get("employee_id") is None


# =========================================================================== #
# Contract 5: cross-person collision guard
# =========================================================================== #
def test_collision_with_different_person_raises_no_write(tmp_path, monkeypatch):
    # "9213" already belongs to a DIFFERENT person.
    store = RecordingRmwVaultStore(
        [
            employee_doc(person_id="per_TARGET", name="Eric Dalton"),
            employee_doc(person_id="per_OTHER", name="Other Person", employee_id="9213"),
        ]
    )
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    with pytest.raises(QueueJobError, match="already assigned to another person"):
        run(store, context, set_id_payload("Eric Dalton", "9213"))

    # Nothing written: target still has no employee_id, holder unchanged.
    assert store.update_doc_calls == []
    assert store.get_optional("employee_per_TARGET").get("employee_id") is None
    assert store.get_optional("employee_per_OTHER")["employee_id"] == "9213"


def test_setting_same_value_target_already_has_is_noop_success(tmp_path, monkeypatch):
    # Target already holds "9213"; re-asserting it is a success (no collision
    # against itself) and appends the job_id.
    store = RecordingRmwVaultStore([employee_doc(name="Eric Dalton", employee_id="9213")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    context = context_for(tmp_path)

    queue_file, processed_dir = run(store, context, set_id_payload("Eric Dalton", "9213"))

    doc = store.get_optional("employee_per_01ERICDALTON0000000000")
    assert doc["employee_id"] == "9213"
    assert doc["btq_job_ids"] == ["job-set-id"]
    assert (processed_dir / queue_file.name).exists()


# =========================================================================== #
# Contract 6: registry dispatch
# =========================================================================== #
def test_registry_dispatches_to_employee_updates_handler():
    assert JOB_HANDLERS[JOB_SET_EMPLOYEE_ID] is employee_updates.process_set_employee_id_job
    assert JOB_HANDLERS[JOB_SET_EMPLOYEE_ID].__module__ == "queue_processor.handlers.employee_updates"
