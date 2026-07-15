from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from btq_vault.couch_store import CouchDBEntityStore
from queue_processor.handlers import _shared, employee_updates, people
from queue_processor.handlers._shared import QueueJob, QueueProcessorError, RunContext
from queue_spec import JOB_ADD_PERSON, JOB_SET_EMPLOYEE_CONTACT, validate_job


class SyntheticStore(CouchDBEntityStore):
    """In-memory-only store; every fixture is synthetic and no transport exists."""

    def __init__(self, docs: list[dict[str, Any]] = ()) -> None:
        self.docs = {str(doc["_id"]): deepcopy(doc) for doc in docs}
        self.put_calls: list[dict[str, Any]] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(doc_id)
        return deepcopy(doc) if doc is not None else None

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        return [deepcopy(doc) for doc in self.docs.values() if doc.get("type") == "employee"][:limit]

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        stored = deepcopy(doc)
        stored["_rev"] = "2-synthetic"
        self.docs[str(stored["_id"])] = stored
        self.put_calls.append(deepcopy(stored))
        return deepcopy(stored)

    def upsert(self, doc: dict[str, Any]) -> None:
        stored = deepcopy(doc)
        self.docs[str(stored["_id"])] = stored
        self.put_calls.append(deepcopy(stored))


def context(tmp_path: Path, *, dry_run: bool = False) -> RunContext:
    runtime = tmp_path / "synthetic-runtime"
    runtime.mkdir(parents=True)
    return RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "verify.log",
        dry_run=dry_run,
        run_id="synthetic-verifier-run",
    )


def queue_job(job_id: str, payload: dict[str, Any], job_type: str = JOB_SET_EMPLOYEE_CONTACT) -> QueueJob:
    return QueueJob(job_id, job_type, payload, {}, {})


def install_store(monkeypatch: pytest.MonkeyPatch, store: SyntheticStore) -> None:
    monkeypatch.setattr(_shared, "_vault_store", lambda: store)
    monkeypatch.setattr(_shared, "write_mutation_evidence", lambda *args, **kwargs: None)


def employee_doc(*, doc_id: str = "employee_synthetic_verify", name: str = "Synthetic Verify") -> dict[str, Any]:
    return {
        "_id": doc_id,
        "_rev": "1-synthetic",
        "type": "employee",
        "person_id": doc_id.removeprefix("employee_"),
        "employee_id": "SYN-484",
        "name": name,
        "phone": "+1-202-555-0100",
        "email": "before@example.invalid",
        "site_ids": ["SYN-SITE-01"],
        "status": "active",
        "custom_unrelated": {"preserve": True},
        "btq_job_ids": ["synthetic-prior-job"],
    }


def invoke_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: SyntheticStore,
    job: QueueJob,
    *,
    source_name: str,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    install_store(monkeypatch, store)
    ctx = context(tmp_path, dry_run=dry_run)
    queued = ctx.runtime_root / source_name
    queued.write_text("synthetic queue fixture\n", encoding="utf-8")
    processed = ctx.runtime_root / "processed"
    employee_updates.process_set_employee_contact_job(queued, job, ctx, processed)
    return queued, processed / source_name


def test_schema_accepts_joint_contact_and_null_but_rejects_invalid_shapes() -> None:
    def valid(contact: object) -> bool:
        return validate_job({
            "job_type": JOB_SET_EMPLOYEE_CONTACT,
            "payload": {"person": "SYN-484", "actor": "Synthetic Verifier", "contact": contact},
        })

    assert valid({"phone": "+1-202-555-0101", "email": "verify@example.invalid"})
    assert valid({"phone": None})
    assert not valid({})
    assert not valid({"pager": "+1-202-555-0102"})
    assert not valid({"phone": ""})
    assert not valid({"email": "   "})


def test_handler_updates_phone_and_clears_email_while_preserving_unrelated_fields_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = employee_doc()
    store = SyntheticStore([original])
    job = queue_job(
        "synthetic-contact-job-484",
        {
            "person": "SYN-484",
            "actor": "Synthetic Verifier",
            "contact": {"phone": "+1-202-555-0103", "email": None},
        },
    )

    first_source, first_processed = invoke_contact(
        tmp_path, monkeypatch, store, job, source_name="synthetic-first.json"
    )
    after_first = deepcopy(store.docs[original["_id"]])
    assert not first_source.exists() and first_processed.exists()
    assert after_first["phone"] == "+1-202-555-0103"
    assert after_first["email"] is None
    assert after_first["site_ids"] == original["site_ids"]
    assert after_first["status"] == original["status"]
    assert after_first["custom_unrelated"] == original["custom_unrelated"]
    assert after_first["btq_job_ids"] == ["synthetic-prior-job", "synthetic-contact-job-484"]
    assert after_first["edited_by"] == "Synthetic Verifier"
    assert len(store.put_calls) == 1

    second_root = tmp_path / "second-run"
    second_source, second_processed = invoke_contact(
        second_root, monkeypatch, store, job, source_name="synthetic-repeat.json"
    )
    assert not second_source.exists() and second_processed.exists()
    assert store.docs[original["_id"]] == after_first
    assert len(store.put_calls) == 1


def test_missing_and_ambiguous_employee_resolution_fail_without_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_store = SyntheticStore()
    install_store(monkeypatch, missing_store)
    missing_ctx = context(tmp_path / "missing")
    missing_path = missing_ctx.runtime_root / "synthetic-missing.json"
    missing_path.write_text("synthetic\n", encoding="utf-8")
    missing_job = queue_job(
        "synthetic-missing-job",
        {"person": "Nobody Synthetic", "actor": "Synthetic Verifier", "contact": {"phone": None}},
    )
    with pytest.raises(QueueProcessorError, match="Could not resolve canonical employee target"):
        employee_updates.process_set_employee_contact_job(
            missing_path, missing_job, missing_ctx, missing_ctx.runtime_root / "processed"
        )
    assert missing_path.exists() and missing_store.put_calls == []

    ambiguous_store = SyntheticStore([
        employee_doc(doc_id="employee_synthetic_one", name="Synthetic Duplicate"),
        employee_doc(doc_id="employee_synthetic_two", name="Synthetic Duplicate"),
    ])
    install_store(monkeypatch, ambiguous_store)
    ambiguous_ctx = context(tmp_path / "ambiguous")
    ambiguous_path = ambiguous_ctx.runtime_root / "synthetic-ambiguous.json"
    ambiguous_path.write_text("synthetic\n", encoding="utf-8")
    ambiguous_job = queue_job(
        "synthetic-ambiguous-job",
        {"person": "Synthetic Duplicate", "actor": "Synthetic Verifier", "contact": {"email": None}},
    )
    with pytest.raises(QueueProcessorError, match="Ambiguous canonical employee target"):
        employee_updates.process_set_employee_contact_job(
            ambiguous_path, ambiguous_job, ambiguous_ctx, ambiguous_ctx.runtime_root / "processed"
        )
    assert ambiguous_path.exists() and ambiguous_store.put_calls == []


def test_dry_run_neither_mutates_canonical_doc_nor_moves_queue_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = employee_doc()
    store = SyntheticStore([original])
    job = queue_job(
        "synthetic-dry-run-job",
        {
            "person": "SYN-484",
            "actor": "Synthetic Verifier",
            "contact": {"phone": "+1-202-555-0104", "email": "dry-run@example.invalid"},
        },
    )
    source, processed = invoke_contact(
        tmp_path, monkeypatch, store, job, source_name="synthetic-dry-run.json", dry_run=True
    )
    assert source.exists()
    assert not processed.exists()
    assert store.docs[original["_id"]] == original
    assert store.put_calls == []


def test_add_person_with_valid_contact_flattens_phone_and_email_into_canonical_doc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SyntheticStore()
    install_store(monkeypatch, store)
    ctx = context(tmp_path)
    source = ctx.runtime_root / "synthetic-add-person.json"
    source.write_text("synthetic\n", encoding="utf-8")
    payload = {
        "name": "Verifier, Synthetic",
        "first": "Synthetic",
        "last": "Verifier",
        "role": "Synthetic Cleaner",
        "contact": {"phone": "+1-202-555-0105", "email": "new-person@example.invalid"},
    }
    assert validate_job({"job_type": JOB_ADD_PERSON, "payload": payload})
    job = queue_job("synthetic-add-person-job", payload, JOB_ADD_PERSON)

    people.process_add_person_job(source, job, ctx, ctx.runtime_root / "processed")

    created = store.docs["employee_verifier_synthetic"]
    assert created["phone"] == "+1-202-555-0105"
    assert created["email"] == "new-person@example.invalid"
    assert "contact" not in created
    assert created["name"] == "Verifier, Synthetic"
    assert not source.exists()
