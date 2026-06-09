from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from queue_processor import main as qp
from queue_processor.handlers import _shared as shared


class FakeStore:
    def __init__(self, docs: list[dict[str, Any]] | None = None, *, missing_required_doc: bool = False) -> None:
        self.missing_required_doc = missing_required_doc
        self.docs = [dict(doc) for doc in (docs or [])]
        self.update_doc_calls: list[str] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        if self.missing_required_doc:
            return None
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        self.update_doc_calls.append(doc_id)
        current_index = next((index for index, doc in enumerate(self.docs) if doc.get("_id") == doc_id), None)
        current = dict(self.docs[current_index]) if current_index is not None else None
        if current is None:
            if require_existing:
                raise RuntimeError(f"missing required doc: {doc_id}")
            current = create() if create is not None else None
        outgoing = transform(current)
        if outgoing is None:
            if current is None:
                raise RuntimeError(f"no-op has no document: {doc_id}")
            return current
        stored = dict(outgoing)
        if current_index is None:
            self.docs.append(stored)
        else:
            self.docs[current_index] = stored
        return dict(stored)

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs if doc.get("type") == "employee"][:limit]


def store_doc(store: FakeStore, doc_id: str) -> dict[str, Any]:
    matches = [doc for doc in store.docs if doc.get("_id") == doc_id]
    assert len(matches) == 1
    return matches[0]


def canonical_site_doc(*, active: bool = True, status: str | None = None, job_ids: list[str] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "location_7030",
        "type": "location",
        "site_id": "7030",
        "active": active,
        "btq_job_ids": list(job_ids or []),
    }
    if status is not None:
        doc["status"] = status
    return doc


def canonical_employee_doc(*, status: str = "active", job_ids: list[str] | None = None, vault_path: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "employee_per_01JTEST0000000000000000000",
        "type": "employee",
        "person_id": "per_01JTEST0000000000000000000",
        "employee_id": "E-699",
        "name": "Maria Hutton",
        "status": status,
        "btq_job_ids": list(job_ids or []),
    }
    if vault_path is not None:
        doc["vault_path"] = vault_path
    return doc


def context(tmp_path: Path) -> qp.RunContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return qp.RunContext(
        project_root=tmp_path,
        vault_root=vault,
        personal_vault_root=vault,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7030"},
        site_id_to_opportunities_dir={},
    )


def write_site(vault: Path, *, active: bool = True, status: str | None = None) -> Path:
    path = vault / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    path.parent.mkdir(parents=True)
    status_line = f"status: {status}\n" if status is not None else ""
    path.write_text(
        f"""---
type: location
site_id: "7030"
account: Wgtco
location: Western Gas Transmission
active: {"true" if active else "false"}
{status_line}---
# Western Gas Transmission
""",
        encoding="utf-8",
    )
    return path


def write_employee(vault: Path, *, status: str = "active", person_id: str = "per_01J00000000000000000000000") -> Path:
    path = vault / "People" / "Hutton, Maria.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""---
type: person
person_id: {person_id}
name: Hutton, Maria
status: {status}
---
# Hutton, Maria
""",
        encoding="utf-8",
    )
    return path


def job(entity_type: str, entity_id: str, status: str, job_id: str = "job-status") -> qp.QueueJob:
    return qp.QueueJob(
        job_id=job_id,
        job_type=qp.JOB_SET_ENTITY_STATUS,
        payload={"entity_type": entity_type, "entity_id": entity_id, "status": status, "reason": "Reviewed.", "source": "test"},
        metadata={},
        intent={},
    )


def run_handler(
    tmp_path: Path,
    queue_job: qp.QueueJob,
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: FakeStore | None = None,
) -> tuple[qp.RunContext, FakeStore]:
    ctx = context(tmp_path)
    queue_dir = ctx.runtime_root / "queue"
    processed = ctx.runtime_root / "processed"
    queue_dir.mkdir()
    job_path = queue_dir / f"{queue_job.job_id}.json"
    job_path.write_text("{}", encoding="utf-8")
    if store is None:
        if queue_job.payload["entity_type"] == "site":
            store = FakeStore([canonical_site_doc(active=True, status="active")])
        else:
            store = FakeStore([canonical_employee_doc(vault_path=str(ctx.vault_root / "People" / "Hutton, Maria.md"))])
    monkeypatch.setattr(shared, "_vault_store", lambda: store)
    qp.process_set_entity_status_job(job_path, queue_job, ctx, processed)
    return ctx, store


def test_set_entity_status_site_inactive_keeps_record_and_patches_canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_path = write_site(tmp_path / "vault", active=True, status="active")
    original_text = site_path.read_text(encoding="utf-8")

    _ctx, store = run_handler(tmp_path, job("site", "7030", "inactive"), monkeypatch)

    assert site_path.read_text(encoding="utf-8") == original_text
    doc = store_doc(store, "location_7030")
    assert doc["active"] is False
    assert "status" not in doc
    assert doc["btq_job_ids"] == ["job-status"]
    assert store.update_doc_calls == ["location_7030"]


def test_set_entity_status_site_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_path = write_site(tmp_path / "vault", active=False, status="inactive")
    original_text = site_path.read_text(encoding="utf-8")

    _ctx, store = run_handler(tmp_path, job("site", "7030", "active"), monkeypatch)

    assert site_path.read_text(encoding="utf-8") == original_text
    doc = store_doc(store, "location_7030")
    assert doc["active"] is True
    assert "status" not in doc
    assert store.update_doc_calls == ["location_7030"]


def test_set_entity_status_employee_inactive_patches_employee_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    employee_path = write_employee(tmp_path / "vault", status="active", person_id="per_01JTEST0000000000000000000")
    original_text = employee_path.read_text(encoding="utf-8")

    _ctx, store = run_handler(tmp_path, job("employee", "Maria Hutton", "inactive"), monkeypatch)

    assert employee_path.read_text(encoding="utf-8") == original_text
    doc = store_doc(store, "employee_per_01JTEST0000000000000000000")
    assert doc["status"] == "inactive"
    assert doc["btq_job_ids"] == ["job-status"]
    assert store.update_doc_calls == ["employee_per_01JTEST0000000000000000000"]


def test_set_entity_status_employee_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    employee_path = write_employee(tmp_path / "vault", status="inactive", person_id="per_01JTEST0000000000000000000")
    original_text = employee_path.read_text(encoding="utf-8")

    _ctx, store = run_handler(tmp_path, job("employee", "Hutton, Maria", "active"), monkeypatch)

    assert employee_path.read_text(encoding="utf-8") == original_text
    doc = store_doc(store, "employee_per_01JTEST0000000000000000000")
    assert doc["status"] == "active"
    assert store.update_doc_calls == ["employee_per_01JTEST0000000000000000000"]


def test_set_entity_status_idempotent_on_job_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_path = write_site(tmp_path / "vault", active=False)
    site_path.write_text(site_path.read_text(encoding="utf-8").replace("active: false", "active: false\nbtq_job_ids:\n  - job-status"), encoding="utf-8")
    store = FakeStore([canonical_site_doc(active=False, job_ids=["job-status"])])

    ctx, store = run_handler(tmp_path, job("site", "7030", "active"), monkeypatch, store=store)

    assert "active: false" in site_path.read_text(encoding="utf-8")
    assert store.update_doc_calls == []
    assert (ctx.runtime_root / "processed" / "job-status.json").exists()


def test_set_entity_status_employee_reference_resolves_from_canonical_without_markdown_person_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    employee_path = write_employee(tmp_path / "vault")
    employee_path.write_text(employee_path.read_text(encoding="utf-8").replace("person_id: per_01J00000000000000000000000\n", ""), encoding="utf-8")
    original_text = employee_path.read_text(encoding="utf-8")

    _ctx, store = run_handler(tmp_path, job("employee", "Hutton, Maria", "inactive"), monkeypatch)

    assert employee_path.read_text(encoding="utf-8") == original_text
    doc = store_doc(store, "employee_per_01JTEST0000000000000000000")
    assert doc["status"] == "inactive"
    assert store.update_doc_calls == ["employee_per_01JTEST0000000000000000000"]


def test_set_entity_status_required_canonical_patch_failure_keeps_queue_and_markdown_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_path = write_site(tmp_path / "vault", active=True)
    ctx = context(tmp_path)
    queue_dir = ctx.runtime_root / "queue"
    queue_dir.mkdir()
    processed = ctx.runtime_root / "processed"
    job_path = queue_dir / "job-status.json"
    job_path.write_text("{}", encoding="utf-8")
    store = FakeStore(missing_required_doc=True)
    monkeypatch.setattr(shared, "_vault_store", lambda: store)

    with pytest.raises(qp.QueueJobError, match="CouchDB required document not found for canonical RMW: location_7030"):
        qp.process_set_entity_status_job(job_path, job("site", "7030", "inactive"), ctx, processed)

    assert "active: true" in site_path.read_text(encoding="utf-8")
    assert job_path.exists()
    assert not (processed / "job-status.json").exists()
    assert store.update_doc_calls == []
