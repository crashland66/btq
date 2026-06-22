"""INDEPENDENT VERIFIER tests for the assign_employee_site queue job.

Authored against the contract for the (uncommitted) ``assign_employee_site``
job type. NOT written by the implementer. These drive the real handler
(``process_assign_employee_site_job``) through the production-faithful
recording-vault-store harness (the same one the rest of the canonical-write
suite uses), which backs ``apply_canonical_rmw`` with require_existing /
job-id-dedup semantics matching the real CouchDBEntityStore contract.

Contract under test:
  * payload: employee_id (resolver), site_id (bare), actor (required),
    action in {assign, unassign} default assign, source optional.
  * assign appends site_id to employee.site_ids only when absent (dedupe,
    preserve order + existing); unassign removes it when present.
  * doc_type=employee, require_existing=True (no create).
  * idempotent via btq_job_ids marker.
  * EVERY other employee field preserved.
  * fail-closed on couch error (job failure, queue file not consumed).
  * dry-run does not write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import employee_sites
from queue_processor.handlers import people as people_handlers
from queue_processor.main import QueueJobError
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_ASSIGN_EMPLOYEE_SITE, validate_job

# Reuse the exact harness the under-review handler plugs into.
from test_queue_processor_couchdb_write import (
    ExplodingRmwVaultStore,
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)


_EMPLOYEE_NAME = "David Pearson"
_PERSON_ID = "Pearson, David"
_EMPLOYEE_DOC_ID = f"employee_{_PERSON_ID}"


def _employee_doc(*, site_ids: list[str] | None = None, **over: Any) -> dict[str, Any]:
    doc = {
        "_id": _EMPLOYEE_DOC_ID,
        "type": "employee",
        "operator": OPERATOR_ID_GREG,
        "name": _EMPLOYEE_NAME,
        "person_id": _PERSON_ID,
        "employee_id": "567",
        "status": "active",
        "status_date": "2024-01-02",
        "content": "## Notes\nlong-standing cleaner\n",
        "site_ids": list(site_ids) if site_ids is not None else ["591"],
        "btq_job_ids": ["seed-job"],
    }
    doc.update(over)
    return doc


def _payload(**over: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"employee_id": _PERSON_ID, "site_id": "592", "actor": "Greg"}
    p.update(over)
    return p


def _run(
    store: RecordingRmwVaultStore,
    context,
    payload: dict[str, Any],
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    queue_name: str | None = None,
):
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, queue_name or job_id)
    processed_dir = context.runtime_root / "processed"
    employee_sites.process_assign_employee_site_job(
        queue_file,
        job(JOB_ASSIGN_EMPLOYEE_SITE, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return queue_file, processed_dir


def _employee(store: RecordingRmwVaultStore) -> dict[str, Any]:
    doc = store.get_optional(_EMPLOYEE_DOC_ID)
    assert doc is not None
    return doc


# --------------------------------------------------------------------------- #
# Registry + re-export wiring
# --------------------------------------------------------------------------- #
def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_ASSIGN_EMPLOYEE_SITE] is employee_sites.process_assign_employee_site_job
    # main.py re-exports the same callable through people.
    assert people_handlers.process_assign_employee_site_job is employee_sites.process_assign_employee_site_job


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validatable(payload: dict) -> dict:
    return {"job_id": "j", "job_type": JOB_ASSIGN_EMPLOYEE_SITE, "payload": payload}


def test_validate_accepts_minimal_and_full() -> None:
    assert validate_job(_validatable(_payload())) is True
    assert validate_job(_validatable(_payload(action="assign", source="operator_review"))) is True
    assert validate_job(_validatable(_payload(action="unassign"))) is True


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("employee_id"), id="missing-employee_id"),
        pytest.param(lambda p: p.pop("site_id"), id="missing-site_id"),
        pytest.param(lambda p: p.pop("actor"), id="missing-actor"),
        pytest.param(lambda p: p.update(employee_id="  "), id="blank-employee_id"),
        pytest.param(lambda p: p.update(site_id=""), id="empty-site_id"),
        pytest.param(lambda p: p.update(actor="   "), id="blank-actor"),
        pytest.param(lambda p: p.update(action="delete"), id="bad-action"),
        pytest.param(lambda p: p.update(action=""), id="empty-action"),
        pytest.param(lambda p: p.update(source="  "), id="blank-source"),
        pytest.param(lambda p: p.update(site_id=592), id="non-string-site_id"),
        pytest.param(lambda p: p.update(surprise="x"), id="stray-field"),
    ],
)
def test_validate_rejects(mutate) -> None:
    p = _payload()
    mutate(p)
    assert validate_job(_validatable(p)) is False


# --------------------------------------------------------------------------- #
# assign — adds site, preserves every other field (COUCHDB_PATH coverage node)
# --------------------------------------------------------------------------- #
def test_assign_adds_site_and_preserves_other_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591"])])

    queue_file, processed_dir = _run(store, context, _payload(site_id="592"), "assign-1", monkeypatch)

    doc = _employee(store)
    assert doc["site_ids"] == ["591", "592"]  # appended, existing preserved + order
    # Every other field intact.
    assert doc["status"] == "active"
    assert doc["status_date"] == "2024-01-02"
    assert doc["name"] == _EMPLOYEE_NAME
    assert doc["person_id"] == _PERSON_ID
    assert doc["employee_id"] == "567"
    assert doc["content"] == "## Notes\nlong-standing cleaner\n"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["type"] == "employee"
    assert doc["btq_job_ids"] == ["seed-job", "assign-1"]
    # Only the employee doc was ever the RMW target.
    assert store.update_doc_calls == [_EMPLOYEE_DOC_ID]
    # queue file consumed.
    assert (processed_dir / queue_file.name).exists()
    assert not queue_file.exists()


def test_assign_from_empty_site_ids_seeds_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=[])])
    _run(store, context, _payload(site_id="592"), "assign-empty", monkeypatch)
    assert _employee(store)["site_ids"] == ["592"]


def test_assign_missing_site_ids_field_seeds_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    seed = _employee_doc()
    del seed["site_ids"]
    store = RecordingRmwVaultStore([seed])
    _run(store, context, _payload(site_id="592"), "assign-noseed", monkeypatch)
    assert _employee(store)["site_ids"] == ["592"]


# --------------------------------------------------------------------------- #
# assign — idempotent: already-present + replay of same job_id
# --------------------------------------------------------------------------- #
def test_assign_already_present_no_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591", "592"])])
    _run(store, context, _payload(site_id="592"), "assign-dupe", monkeypatch)
    doc = _employee(store)
    assert doc["site_ids"] == ["591", "592"]  # no duplicate
    assert doc["site_ids"].count("592") == 1


def test_replay_same_job_id_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591"])])
    _run(store, context, _payload(site_id="592"), "assign-replay", monkeypatch)
    after_first = _employee(store)
    assert after_first["site_ids"] == ["591", "592"]
    assert after_first["btq_job_ids"] == ["seed-job", "assign-replay"]

    # Replay SAME job_id but action=unassign — the btq_job_ids marker must
    # short-circuit so the doc is untouched (no removal, no second append).
    _run(
        store,
        context,
        _payload(site_id="592", action="unassign"),
        "assign-replay",
        monkeypatch,
        queue_name="assign-replay-2",
    )
    doc = _employee(store)
    assert doc["site_ids"] == ["591", "592"], "replay must not re-apply the mutation"
    assert doc["btq_job_ids"] == ["seed-job", "assign-replay"]


# --------------------------------------------------------------------------- #
# unassign — removes a present site, preserves the rest
# --------------------------------------------------------------------------- #
def test_unassign_removes_present_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591", "592", "593"])])
    _run(store, context, _payload(site_id="592", action="unassign"), "unassign-1", monkeypatch)
    doc = _employee(store)
    assert doc["site_ids"] == ["591", "593"]  # only 592 removed, order preserved
    assert doc["status"] == "active"
    assert doc["btq_job_ids"] == ["seed-job", "unassign-1"]


def test_unassign_absent_site_is_noop_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591"])])
    _run(store, context, _payload(site_id="999", action="unassign"), "unassign-absent", monkeypatch)
    assert _employee(store)["site_ids"] == ["591"]


# --------------------------------------------------------------------------- #
# require_existing — missing employee fails closed (no create)
# --------------------------------------------------------------------------- #
def test_missing_employee_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([])  # no employee docs to resolve against
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "assign-noemp")
    processed_dir = context.runtime_root / "processed"
    with pytest.raises(shared.QueueProcessorError):
        employee_sites.process_assign_employee_site_job(
            queue_file,
            job(JOB_ASSIGN_EMPLOYEE_SITE, _payload(), job_id="assign-noemp"),
            context,
            processed_dir,
        )
    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()


# --------------------------------------------------------------------------- #
# fail-closed — couch write error surfaces as a job failure
# --------------------------------------------------------------------------- #
def test_store_write_error_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    # ExplodingRmwVaultStore raises in update_doc; seed an employee so resolution
    # succeeds and the failure is specifically the canonical write.
    store = ExplodingRmwVaultStore([_employee_doc(site_ids=["591"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "assign-boom")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        employee_sites.process_assign_employee_site_job(
            queue_file,
            job(JOB_ASSIGN_EMPLOYEE_SITE, _payload(), job_id="assign-boom"),
            context,
            processed_dir,
        )
    # Queue file NOT moved to processed — not silent success.
    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()


# --------------------------------------------------------------------------- #
# dry-run — does not write
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    object.__setattr__(context, "dry_run", True)
    store = RecordingRmwVaultStore([_employee_doc(site_ids=["591"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "assign-dry")
    processed_dir = context.runtime_root / "processed"
    employee_sites.process_assign_employee_site_job(
        queue_file,
        job(JOB_ASSIGN_EMPLOYEE_SITE, _payload(site_id="592"), job_id="assign-dry"),
        context,
        processed_dir,
    )
    # No mutation: site_ids unchanged, no RMW update_doc calls, queue file intact.
    assert _employee(store)["site_ids"] == ["591"]
    assert store.update_doc_calls == []
    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()
