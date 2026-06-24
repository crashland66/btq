from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pytest

import queue_processor.main as qp
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.main import QueueJob, QueueJobError, RunContext
from test_helpers.queue_processor_stores import RecordingVaultStore


def _attach_vault_dir(context: RunContext, vault_root: Path) -> RunContext:
    """Expose a throwaway ``vault_root`` temp dir on a (frozen) RunContext.

    The markdown-projection vault root was removed from the production
    ``RunContext``. These tests still use a temp directory as a convenient place
    to (a) seed legacy projection fixtures that doc-id resolution derives ids
    from and (b) assert the projection is NOT written. We stash it as a bare
    instance attribute (bypassing the frozen dataclass) without reintroducing a
    production field.
    """
    object.__setattr__(context, "vault_root", vault_root)
    return context


class FailingVaultStore:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    def get_optional(self, doc_id: str) -> dict | None:
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
        seed = create() if create is not None else None
        outgoing = transform(seed)
        if outgoing is not None:
            self.docs.append(dict(outgoing))
        raise RuntimeError("boom")

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict]:
        return []

    def find_visit_docs(self, site_id: str, date: str, *, limit: int = 10000) -> list[dict]:
        return []

    def find_open_site_issue_docs(self, site_id: str, *, limit: int = 500) -> list[dict]:
        return []

    def upsert(self, doc: dict) -> None:
        self.docs.append(doc)
        raise RuntimeError("boom")


class RecordingRmwVaultStore(RecordingVaultStore):
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.docs = [dict(doc) for doc in (docs or [])]
        self.update_doc_calls: list[str] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def find_open_site_issue_docs(self, site_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        site_id_variants = {str(site_id), f'"{site_id}"'}
        matches = [
            {
                "_id": doc.get("_id"),
                "issue_id": doc.get("issue_id"),
                "title": doc.get("title"),
                "status": doc.get("status"),
            }
            for doc in self.docs
            if doc.get("type") == "site_issue"
            and str(doc.get("site_id")) in site_id_variants
            and str(doc.get("status") or "").strip() == "open"
        ]
        return matches[:limit]

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
            seed = create() if create is not None else None
        else:
            seed = current
        outgoing = transform(seed)
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


class ExplodingRmwVaultStore(RecordingRmwVaultStore):
    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
        raise RuntimeError("couchdb unavailable")


class ExplodingVisitFindStore(RecordingRmwVaultStore):
    def find_visit_docs(self, site_id: str, date: str, *, limit: int = 10000) -> list[dict[str, Any]]:
        raise RuntimeError("couchdb unavailable")


class ExplodingGetOptionalStore(RecordingRmwVaultStore):
    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        raise RuntimeError("couchdb unavailable")


class MissingCanonicalEmployeeDocStore(RecordingRmwVaultStore):
    def __init__(self, employee_doc: dict[str, Any]) -> None:
        super().__init__([])
        self.employee_doc = dict(employee_doc)

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        return [dict(self.employee_doc)]


def context_for(root: Path) -> RunContext:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    context = RunContext(
        project_root=root,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7060"},
        site_id_to_opportunities_dir={},
    )
    return _attach_vault_dir(context, root / "vault")


def write_site(vault: Path) -> Path:
    path = vault / "Accounts" / "Contworks" / "Locations" / "7060 - Continental Metalworks" / "about.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: location\n"
        "site_id: 7060\n"
        "account: Contworks\n"
        "location: Continental Metalworks\n"
        "---\n"
        "# Continental Metalworks\n",
        encoding="utf-8",
    )
    return path


def make_queue_file(context: RunContext, job_id: str) -> Path:
    queue_file = context.runtime_root / f"{job_id}.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    return queue_file


def make_process_job_file(context: RunContext, job_type: str, payload: dict, job_id: str) -> Path:
    queue_dir = context.runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_file = queue_dir / f"{job_id}.json"
    queue_file.write_text(
        json.dumps({"job_id": job_id, "job_type": job_type, "payload": payload}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return queue_file


def job(job_type: str, payload: dict, job_id: str = "job-one") -> QueueJob:
    return QueueJob(job_id=job_id, job_type=job_type, payload=payload, metadata={}, intent={})


def site_issue_payload() -> dict:
    return {
        "site_id": "7060",
        "title": "Broken railing",
        "summary": "Railing is loose near the dock.",
        "reported_by": "Jordan",
        "resolution_trigger": "Repair before next shift",
        "client_notified": False,
        "issue_id": "issue-fixed",
    }


def supply_need_payload() -> dict:
    return {
        "site_id": "7060",
        "item_name": "Gloves",
        "requested_by": "Jordan",
        "supply_id": "supply-fixed",
    }


def equipment_request_payload() -> dict:
    return {
        "site_id": "7060",
        "equipment_name": "Floor scrubber",
        "requested_by": "Jordan",
        "equipment_id": "equipment-fixed",
    }


def supply_transition_payload() -> dict:
    return {
        "supply_id": "supply-fixed",
        "actor": "Jordan",
        "occurred_at": "2026-05-30T12:00:00+00:00",
        "note": "Ordered from Staples.",
    }


def equipment_transition_payload() -> dict:
    return {
        "equipment_id": "equipment-fixed",
        "actor": "Jordan",
        "occurred_at": "2026-05-30T12:00:00+00:00",
        "note": "Approved for replacement.",
    }


def issue_transition_payload() -> dict:
    return {
        "issue_id": "issue-fixed",
        "actor": "Jordan",
        "occurred_at": "2026-05-30T12:00:00+00:00",
        "note": "Maintenance cleared it.",
    }


def canonical_supply_doc(*, status: str = "open", job_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "_id": "supply_need_supply-fixed",
        "type": "supply_need",
        "supply_id": "supply-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "account": "Contworks",
        "item_name": "Gloves",
        "requested_by": "Jordan",
        "status": status,
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": list(job_ids or []),
    }


def canonical_issue_doc(*, status: str = "open", job_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "_id": "site_issue_issue-fixed",
        "type": "site_issue",
        "issue_id": "issue-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "account": "Contworks",
        "title": "Broken railing",
        "summary": "Railing is loose near the dock.",
        "status": status,
        "created_at": "2026-05-01T00:00:00+00:00",
        "related_capture_ids": ["cap-issue"],
        "related_candidate_ids": ["ac-issue"],
        "btq_job_ids": list(job_ids or []),
    }


def canonical_equipment_doc(*, status: str = "open", job_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "_id": "equipment_request_equipment-fixed",
        "type": "equipment_request",
        "equipment_id": "equipment-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "account": "Contworks",
        "equipment_name": "Floor scrubber",
        "requested_by": "Jordan",
        "status": status,
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": list(job_ids or []),
    }


def canonical_location_status_doc(*, active: bool = True, status: str | None = None, job_ids: list[str] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "location_7060",
        "type": "location",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "account": "Contworks",
        "active": active,
        "btq_job_ids": list(job_ids or []),
    }
    if status is not None:
        doc["status"] = status
    return doc


def canonical_location_content_doc(
    *,
    site_id: str = "7030",
    site_name: str = "Western Gas Transmission",
    content: str = "# Western Gas Transmission\n",
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "_id": f"location_{site_id}",
        "type": "location",
        "site_id": site_id,
        "site_name": site_name,
        "account": "Wgtco",
        "content": content,
        "btq_job_ids": list(job_ids or []),
    }


def canonical_employee_status_doc(
    *,
    person_id: str = "per_01JASMINE000000000000000",
    employee_id: str = "E-200",
    name: str = "Maria Hutton",
    status: str = "active",
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "_id": f"employee_{person_id}",
        "type": "employee",
        "person_id": person_id,
        "employee_id": employee_id,
        "name": name,
        "status": status,
        "btq_job_ids": list(job_ids or []),
    }


def canonical_employee_content_doc(
    *,
    person_id: str = "per_01PETERNASH00000000000",
    employee_id: str = "E-300",
    name: str = "Peter Nash",
    content: str = "# Peter Nash\n",
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    doc = canonical_employee_status_doc(
        person_id=person_id,
        employee_id=employee_id,
        name=name,
        status="active",
        job_ids=job_ids,
    )
    doc["content"] = content
    return doc


def flag_retention_risk_payload() -> dict:
    return {
        "employee": "Peter Nash",
        "site": "Western Gas Transmission",
        "details": "May leave if evening load stays unchanged.",
        "date": "2026-04-19",
    }


def flag_access_constraint_payload() -> dict:
    return {
        "site": "Western Gas Transmission",
        "details": "Only one employee has the badge.",
        "date": "2026-04-19",
    }


def trigger_recruiting_payload() -> dict:
    return {
        "site": "Western Gas Transmission",
        "priority": "emergency",
        "open_positions": 2,
        "details": "Two openings remain on site.",
        "date": "2026-04-19",
    }


def close_recruiting_payload(*, outcome: str = "filled", filled_by: str | None = "Peter Nash") -> dict:
    payload = {
        "site": "Western Gas Transmission",
        "outcome": outcome,
        "date": "2026-04-19",
        "notes": "Coverage plan changed.",
    }
    if filled_by is not None:
        payload["filled_by"] = filled_by
    return payload


def remove_from_schedule_payload() -> dict:
    return {
        "employee": "Peter Nash",
        "site": "Western Gas Transmission",
        "date": "2026-04-19",
    }


def write_employee_markdown(vault: Path, *, job_ids: list[str] | None = None) -> Path:
    path = vault / "People" / "Nash, Peter.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    job_id_lines = "".join(f"  - {job_id}\n" for job_id in (job_ids or []))
    marker = f"btq_job_ids:\n{job_id_lines}" if job_id_lines else ""
    path.write_text(
        "---\n"
        "type: employee\n"
        "name: Peter Nash\n"
        "first: Peter\n"
        "last: Nash\n"
        "status: active\n"
        f"{marker}"
        "---\n"
        "# Peter Nash\n",
        encoding="utf-8",
    )
    return path


def write_location_markdown(vault: Path, *, job_ids: list[str] | None = None) -> Path:
    path = vault / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    job_id_lines = "".join(f"  - {job_id}\n" for job_id in (job_ids or []))
    marker = f"btq_job_ids:\n{job_id_lines}" if job_id_lines else ""
    path.write_text(
        "---\n"
        "type: location\n"
        "site_id: 7030\n"
        "job: 7030\n"
        "account: Wgtco\n"
        "location: Western Gas Transmission\n"
        f"{marker}"
        "---\n"
        "# Western Gas Transmission\n",
        encoding="utf-8",
    )
    return path


def personnel_event_payload() -> dict:
    return {
        "employee": "Maria Hutton",
        "summary": "Completed safety training.",
        "reported_by": "Jordan",
        "occurred_at": "2026-05-29T12:00:00+00:00",
        "event_type": "training",
        "event_id": "personnel-fixed",
        "related_site": "7060",
    }


def visit_payload(*, occurred_at: str | None = None, evidence: str = "Walked the dock.") -> dict:
    payload: dict[str, Any] = {
        "site": "7060",
        "source": "field_visit",
        "evidence": evidence,
        "confidence": "high",
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return payload


def canonical_visit_doc(
    *,
    doc_id: str = "visit_7060_existing",
    site_id: str = "7060",
    date: str | None = None,
    evidence: str = "Walked the dock.",
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    visit_date = date or datetime.now(timezone.utc).date().isoformat()
    return {
        "_id": doc_id,
        "type": "visit",
        "site_id": site_id,
        "date": visit_date,
        "evidence": evidence,
        "btq_job_ids": list(job_ids or []),
    }


def set_entity_status_payload(entity_type: str, entity_id: str, status: str) -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "status": status,
        "reason": "Reviewed.",
        "source": "test",
    }


def add_person_payload() -> dict:
    return {
        "name": "Alex Smith",
        "role": "Cleaner",
        "site_ids": ["7060"],
        "employee_id": "emp-fixed",
    }


def photo_capture_payload(
    *,
    note: str = "Trash accumulation at admin entrance.",
    filename: str = "admin entrance.jpg",
    data_url: str = "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
) -> dict:
    return {
        "site": "Summit Wire",
        "qc_category": "Restrooms",
        "note": note,
        "captured_at": "2026-04-30T14:00:00-04:00",
        "exported_at": "2026-04-30T14:02:00-04:00",
        "photos": [
            {
                "filename": filename,
                "mime_type": "image/jpeg",
                "data_url": data_url,
            }
        ],
    }


Handler = Callable[[Path, QueueJob, RunContext, Path], None]


HANDLER_CASES: tuple[tuple[str, Handler, Callable[[], dict]], ...] = (
    ("visit_create", qp.process_visit_create_job, visit_payload),
    ("log_site_issue", qp.process_log_site_issue_job, site_issue_payload),
    ("log_supply_need", qp.process_log_supply_need_job, supply_need_payload),
    ("log_equipment_request", qp.process_log_equipment_request_job, equipment_request_payload),
    ("log_personnel_event", qp.process_log_personnel_event_job, personnel_event_payload),
    ("add_person", qp.process_add_person_job, add_person_payload),
)


def test_canonical_write_failure_fails_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = FailingVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        qp.process_log_site_issue_job(queue_file, job("log_site_issue", site_issue_payload()), context, processed_dir)

    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()
    assert store.docs[0]["_id"] == "site_issue_issue-fixed"


def test_canonical_rmw_failure_raises_queue_job_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", FailingVaultStore())
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    queue_file = make_queue_file(context, "job-one")

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        qp.process_log_site_issue_job(
            queue_file,
            job("log_site_issue", site_issue_payload()),
            context,
            context.runtime_root / "processed",
        )


@pytest.mark.parametrize(("job_type", "handler", "payload_factory"), HANDLER_CASES)
def test_each_canonical_write_site_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    handler: Handler,
    payload_factory: Callable[[], dict],
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = FailingVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    queue_file = make_queue_file(context, f"{job_type}-job")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        handler(queue_file, job(job_type, payload_factory(), job_id=f"{job_type}-job"), context, processed_dir)

    assert len(store.docs) == 1
    assert store.docs[0]["_id"]
    assert not (processed_dir / queue_file.name).exists()


def test_visit_create_skips_when_job_id_in_canonical_day_visit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    # Pin the operational date deterministically: occurred_at is a fixed NY-evening
    # instant, so the handler derives date 2026-05-30 (local) regardless of UTC wall
    # clock, and the seeded canonical doc carries the same local date so the
    # job-id-marker dedup check finds it.
    store = RecordingRmwVaultStore([canonical_visit_doc(date="2026-05-30", job_ids=["job-one"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(
        queue_file,
        job("visit_create", visit_payload(occurred_at="2026-05-30T21:15:00-04:00"), job_id="job-one"),
        context,
        processed_dir,
    )

    assert store.docs == [canonical_visit_doc(date="2026-05-30", job_ids=["job-one"])]
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_skips_on_duplicate_canonical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    # Same local date (2026-05-30) and same evidence => duplicate-evidence skip,
    # keyed on the operational/local date rather than the UTC processing date.
    store = RecordingRmwVaultStore([canonical_visit_doc(date="2026-05-30", job_ids=["job-old"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(
        queue_file,
        job("visit_create", visit_payload(occurred_at="2026-05-30T21:15:00-04:00"), job_id="job-one"),
        context,
        processed_dir,
    )

    assert store.docs == [canonical_visit_doc(date="2026-05-30", job_ids=["job-old"])]
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_uses_operator_local_date_not_utc_processing_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the UTC-vs-local-date bug: an occurred_at on the evening of
    # 2026-06-23 in America/New_York (which is the NEXT calendar day in UTC) must
    # produce a canonical visit whose operational date is 2026-06-23, with the
    # timestamp normalized to UTC and the dedupe identity keyed on the local date.
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-local-date")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(
        queue_file,
        job(
            "visit_create",
            visit_payload(occurred_at="2026-06-23T21:30:32-04:00"),
            job_id="job-local-date",
        ),
        context,
        processed_dir,
    )

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    visit_doc = visit_docs[0]
    assert visit_doc["date"] == "2026-06-23"
    # occurred_at is 21:30:32-04:00 == 2026-06-24T01:30:32Z, normalized to UTC.
    assert visit_doc["timestamp"] == "2026-06-24T01:30:32+00:00"
    assert visit_doc["timestamp_local"] == "2026-06-23T21:30:32-04:00"
    assert visit_doc["date_timezone"] == "America/New_York"
    # _id and visit_key are keyed on the LOCAL date, not the UTC date (2026-06-24).
    assert visit_doc["_id"] == "visit_7060_2026-06-23_job-loca"
    assert visit_doc["visit_key"] == "7060:2026-06-23"
    assert "2026-06-24" not in visit_doc["_id"]
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_zulu_occurred_at_yields_local_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A Z-suffixed (UTC) occurred_at is accepted and converted to the NY-local date:
    # 2026-06-24T01:30:32Z == 2026-06-23 21:30 Eastern.
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-zulu")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(
        queue_file,
        job("visit_create", visit_payload(occurred_at="2026-06-24T01:30:32Z"), job_id="job-zulu"),
        context,
        processed_dir,
    )

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["date"] == "2026-06-23"
    assert visit_docs[0]["timestamp"] == "2026-06-24T01:30:32+00:00"
    assert visit_docs[0]["date_timezone"] == "America/New_York"
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_same_evening_duplicate_dedupes_on_local_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two occurred_at on the same NY evening (one before, one after UTC midnight)
    # are the same operational date 2026-06-23. With identical evidence the second
    # is deduped — proving the dedupe key is the local date, not the UTC date
    # (which would be 06-23 then 06-24 and therefore NOT dedupe).
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    processed_dir = context.runtime_root / "processed"

    first = make_queue_file(context, "job-evening-1")
    qp.process_visit_create_job(
        first,
        job(
            "visit_create",
            # 23:50 Eastern == still 2026-06-23 local, 2026-06-24T03:50 UTC.
            visit_payload(occurred_at="2026-06-23T23:50:00-04:00", evidence="Locked the gate."),
            job_id="job-evening-1",
        ),
        context,
        processed_dir,
    )

    second = make_queue_file(context, "job-evening-2")
    qp.process_visit_create_job(
        second,
        job(
            "visit_create",
            # 22:10 Eastern earlier the SAME local evening == 2026-06-24T02:10 UTC.
            visit_payload(occurred_at="2026-06-23T22:10:00-04:00", evidence="Locked the gate."),
            job_id="job-evening-2",
        ),
        context,
        processed_dir,
    )

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["date"] == "2026-06-23"
    assert visit_docs[0]["btq_job_ids"] == ["job-evening-1"]
    # Second job was skipped as duplicate evidence and moved to processed.
    assert (processed_dir / second.name).exists()


def test_visit_create_without_occurred_at_uses_local_date_back_compat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Back-compat: omitting occurred_at is still valid and derives the operator's
    # local (NY) date for "now" — NOT the UTC processing date. Computed in-test so
    # it stays correct across the UTC-midnight-Eastern window.
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-now")
    processed_dir = context.runtime_root / "processed"

    expected_local_date = (
        datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    )

    qp.process_visit_create_job(
        queue_file,
        job("visit_create", visit_payload(), job_id="job-now"),
        context,
        processed_dir,
    )

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["date"] == expected_local_date
    assert visit_docs[0]["date_timezone"] == "America/New_York"
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_rejects_naive_occurred_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The handler must reject a naive occurred_at (no timezone) — the local date
    # would be ambiguous.
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-naive")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(qp.QueueProcessorError):
        qp.process_visit_create_job(
            queue_file,
            job("visit_create", visit_payload(occurred_at="2026-06-23T21:30:32"), job_id="job-naive"),
            context,
            processed_dir,
        )

    assert [doc for doc in store.docs if doc.get("type") == "visit"] == []


def test_visit_create_rejects_non_datetime_occurred_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-bad")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(qp.QueueProcessorError):
        qp.process_visit_create_job(
            queue_file,
            job("visit_create", visit_payload(occurred_at="not a datetime"), job_id="job-bad"),
            context,
            processed_dir,
        )

    assert [doc for doc in store.docs if doc.get("type") == "visit"] == []


def test_visit_create_second_visit_same_day_different_evidence_creates_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_visit_doc(evidence="Checked the entry gate.", job_ids=["job-old"])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(queue_file, job("visit_create", visit_payload(), job_id="job-one"), context, processed_dir)

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 2
    assert {doc["evidence"] for doc in visit_docs} == {"Checked the entry gate.", "Walked the dock."}
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    visit_date = datetime.now(timezone.utc).date().isoformat()
    visit_path = site_path.parent / "Visits" / f"{visit_date}.md"
    visit_path.parent.mkdir(parents=True, exist_ok=True)
    visit_path.write_text(
        "---\n"
        "btq_job_ids:\n"
        "  - job-one\n"
        "---\n"
        "evidence: Walked the dock.\n",
        encoding="utf-8",
    )
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(queue_file, job("visit_create", visit_payload(), job_id="job-one"), context, processed_dir)

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["btq_job_ids"] == ["job-one"]
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_succeeds_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    visit_date = datetime.now(timezone.utc).date().isoformat()
    assert not (site_path.parent / "Visits" / f"{visit_date}.md").exists()
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_visit_create_job(queue_file, job("visit_create", visit_payload(), job_id="job-one"), context, processed_dir)

    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["evidence"] == "Walked the dock."
    assert (processed_dir / queue_file.name).exists()


def test_visit_create_store_error_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingVisitFindStore())
    queue_file = make_process_job_file(context, "visit_create", visit_payload(), "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb visit dedup check failed job_type=visit_create job_id=" in log_text
    assert "site_id=7060" in log_text


def test_photo_capture_creates_canonical_journal_doc_with_operator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_photo_capture_job(
        queue_file,
        job("photo_capture", photo_capture_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("journal_operational_2026-04-30")
    assert doc is not None
    assert doc["type"] == "journal"
    assert doc["scope"] == "operational"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert "Photo Capture - 2026-04-30T14:00:00-04:00" in doc["content"]
    assert "Trash accumulation at admin entrance." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert (processed_dir / queue_file.name).exists()


def test_photo_capture_appends_second_capture_same_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    processed_dir = context.runtime_root / "processed"

    first_queue_file = make_queue_file(context, "job-one")
    qp.process_photo_capture_job(
        first_queue_file,
        job("photo_capture", photo_capture_payload(note="First capture.", filename="first.jpg"), job_id="job-one"),
        context,
        processed_dir,
    )
    second_queue_file = make_queue_file(context, "job-two")
    qp.process_photo_capture_job(
        second_queue_file,
        job("photo_capture", photo_capture_payload(note="Second capture.", filename="second.jpg"), job_id="job-two"),
        context,
        processed_dir,
    )

    journal_docs = [doc for doc in store.docs if doc.get("_id") == "journal_operational_2026-04-30"]
    assert len(journal_docs) == 1
    doc = journal_docs[0]
    assert "First capture." in doc["content"]
    assert "Second capture." in doc["content"]
    assert doc["content"].count("Photo Capture - 2026-04-30T14:00:00-04:00") == 2
    assert doc["btq_job_ids"] == ["job-one", "job-two"]


def test_photo_capture_replay_skips_without_rewriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    existing_content = "### Photo Capture - 2026-04-30T14:00:00-04:00\n\nSeeded capture.\n"
    store = RecordingRmwVaultStore(
        [
            {
                "_id": "journal_operational_2026-04-30",
                "type": "journal",
                "date": "2026-04-30",
                "scope": "operational",
                "operator": OPERATOR_ID_GREG,
                "content": existing_content,
                "btq_job_ids": ["job-one"],
            }
        ]
    )
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    attachment_path = context.vault_root / "Journal" / "Attachments" / "2026-04-30" / "admin-entrance.jpg"
    attachment_path.parent.mkdir(parents=True, exist_ok=True)
    attachment_path.write_bytes(b"original")
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_photo_capture_job(
        queue_file,
        job("photo_capture", photo_capture_payload(data_url="data:image/jpeg;base64,cmVwbGF5"), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("journal_operational_2026-04-30")
    assert doc is not None
    assert doc["content"] == existing_content
    assert doc["btq_job_ids"] == ["job-one"]
    assert attachment_path.read_bytes() == b"original"
    assert (processed_dir / queue_file.name).exists()


def test_photo_capture_store_error_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingGetOptionalStore())
    queue_file = make_process_job_file(context, "photo_capture", photo_capture_payload(), "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb journal dedup check failed job_type=photo_capture job_id=" in log_text


def test_photo_capture_succeeds_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    journal_path = context.vault_root / "Journal" / "2026-04-30.md"
    assert not journal_path.exists()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_photo_capture_job(
        queue_file,
        job("photo_capture", photo_capture_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("journal_operational_2026-04-30")
    assert doc is not None
    assert "Trash accumulation at admin entrance." in doc["content"]
    # The attachment link is recorded in the canonical journal content; no
    # markdown-projection photo bytes are written under the (dead) vault root.
    assert "Attachments/2026-04-30/admin-entrance.jpg" in doc["content"]
    assert not (context.vault_root / "Journal" / "Attachments" / "2026-04-30" / "admin-entrance.jpg").exists()
    assert (processed_dir / queue_file.name).exists()


def test_photo_capture_records_attachment_links_without_writing_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_photo_capture_job(
        queue_file,
        job("photo_capture", photo_capture_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("journal_operational_2026-04-30")
    assert doc is not None
    assert "Attachments/2026-04-30/admin-entrance.jpg" in doc["content"]
    attachment_path = context.vault_root / "Journal" / "Attachments" / "2026-04-30" / "admin-entrance.jpg"
    assert not attachment_path.exists()


def test_log_site_issue_does_not_write_projection_after_canonical_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.setenv("BTQ_VAULT_MARKDOWN_WRITE", "1")
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"
    original_atomic_write_text = shared.atomic_write_text

    def fail_markdown_write(path: Path, text: str) -> None:
        if "Issues" in path.parts:
            raise RuntimeError("markdown boom")
        original_atomic_write_text(path, text)

    monkeypatch.setattr(shared, "atomic_write_text", fail_markdown_write)

    qp.process_log_site_issue_job(queue_file, job("log_site_issue", site_issue_payload()), context, processed_dir)

    assert store.docs[0]["_id"] == "site_issue_issue-fixed"
    assert (processed_dir / queue_file.name).exists()
    assert not queue_file.exists()
    assert not list((context.vault_root / "Accounts" / "Contworks" / "Locations" / "7060 - Continental Metalworks" / "Issues").glob("*.md"))


def test_log_site_issue_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    payload = site_issue_payload()
    existing_doc = {
        "_id": "site_issue_issue-fixed",
        "type": "site_issue",
        "issue_id": "issue-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "title": "Broken railing",
        "reported_by": "Jordan",
        "resolution_trigger": "Repair before next shift",
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": ["job-one"],
    }
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_site_issue_job(queue_file, job("log_site_issue", payload), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_log_site_issue_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = site_issue_payload()
    store = RecordingRmwVaultStore([
        {
            "_id": "site_issue_issue-fixed",
            "type": "site_issue",
            "issue_id": "issue-fixed",
            "site_id": "7060",
            "site_name": "Continental Metalworks",
            "title": "Broken railing",
            "reported_by": "Jordan",
            "resolution_trigger": "Repair before next shift",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": [],
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    target_path = site_path.parent / "Issues" / "stale-site-issue.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("---\nbtq_job_ids:\n  - job-one\n---\n# stale projection\n", encoding="utf-8")

    qp.process_log_site_issue_job(
        queue_file,
        job("log_site_issue", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["site_issue_issue-fixed"]


def test_log_site_issue_couchdb_failure_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingRmwVaultStore())
    payload = site_issue_payload()
    queue_file = make_process_job_file(context, "log_site_issue", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=log_site_issue job_id=" in log_text
    assert "entity_id=site_issue_issue-fixed" in log_text


def test_log_site_issue_creates_canonical_doc_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = site_issue_payload()
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_site_issue_job(
        queue_file,
        job("log_site_issue", payload, job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["_id"] == "site_issue_issue-fixed"
    assert doc["type"] == "site_issue"
    assert doc["btq_job_ids"] == ["job-one"]
    assert doc["created_at"]
    assert not (site_path.parent / "Issues").exists()
    assert (processed_dir / queue_file.name).exists()


def test_log_supply_need_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    payload = supply_need_payload()
    existing_doc = {
        "_id": "supply_need_supply-fixed",
        "type": "supply_need",
        "supply_id": "supply-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "item_name": "Gloves",
        "requested_by": "Jordan",
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": ["job-one"],
    }
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_supply_need_job(queue_file, job("log_supply_need", payload), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_log_supply_need_merges_preserves_created_at_and_list_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    payload = supply_need_payload()
    payload["status"] = "ordered"
    store = RecordingRmwVaultStore([
        {
            "_id": "supply_need_supply-fixed",
            "type": "supply_need",
            "supply_id": "supply-fixed",
            "site_id": "7060",
            "site_name": "Continental Metalworks",
            "item_name": "Gloves",
            "requested_by": "Jordan",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": ["job-old"],
            "related_capture_ids": ["cap-old"],
            "custom_field": "keep-me",
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_log_supply_need_job(
        queue_file,
        job("log_supply_need", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["created_at"] == "2026-05-01T00:00:00+00:00"
    assert doc["btq_job_ids"] == ["job-old", "job-one"]
    assert doc["related_capture_ids"] == ["cap-old"]
    assert doc["custom_field"] == "keep-me"
    assert doc["status"] == "ordered"


def test_log_supply_need_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = supply_need_payload()
    store = RecordingRmwVaultStore([
        {
            "_id": "supply_need_supply-fixed",
            "type": "supply_need",
            "supply_id": "supply-fixed",
            "site_id": "7060",
            "site_name": "Continental Metalworks",
            "item_name": "Gloves",
            "requested_by": "Jordan",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": [],
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    target_path = site_path.parent / "Supplies" / "stale-supply-need.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("---\nbtq_job_ids:\n  - job-one\n---\n# stale projection\n", encoding="utf-8")

    qp.process_log_supply_need_job(
        queue_file,
        job("log_supply_need", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["supply_need_supply-fixed"]


def test_log_supply_need_couchdb_failure_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingRmwVaultStore())
    payload = supply_need_payload()
    queue_file = make_process_job_file(context, "log_supply_need", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=log_supply_need job_id=" in log_text
    assert "entity_id=supply_need_supply-fixed" in log_text


def test_log_supply_need_creates_canonical_doc_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = supply_need_payload()
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_supply_need_job(
        queue_file,
        job("log_supply_need", payload, job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["_id"] == "supply_need_supply-fixed"
    assert doc["type"] == "supply_need"
    assert doc["btq_job_ids"] == ["job-one"]
    assert doc["created_at"]
    assert not (site_path.parent / "Supplies").exists()
    assert (processed_dir / queue_file.name).exists()


def test_log_equipment_request_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    payload = equipment_request_payload()
    existing_doc = {
        "_id": "equipment_request_equipment-fixed",
        "type": "equipment_request",
        "equipment_id": "equipment-fixed",
        "site_id": "7060",
        "site_name": "Continental Metalworks",
        "equipment_name": "Floor scrubber",
        "requested_by": "Jordan",
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": ["job-one"],
    }
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_equipment_request_job(queue_file, job("log_equipment_request", payload), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_log_equipment_request_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = equipment_request_payload()
    store = RecordingRmwVaultStore([
        {
            "_id": "equipment_request_equipment-fixed",
            "type": "equipment_request",
            "equipment_id": "equipment-fixed",
            "site_id": "7060",
            "site_name": "Continental Metalworks",
            "equipment_name": "Floor scrubber",
            "requested_by": "Jordan",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": [],
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    target_path = site_path.parent / "Equipment" / "stale-equipment-request.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("---\nbtq_job_ids:\n  - job-one\n---\n# stale projection\n", encoding="utf-8")

    qp.process_log_equipment_request_job(
        queue_file,
        job("log_equipment_request", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("equipment_request_equipment-fixed")
    assert doc is not None
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["equipment_request_equipment-fixed"]


def test_log_equipment_request_couchdb_failure_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingRmwVaultStore())
    payload = equipment_request_payload()
    queue_file = make_process_job_file(context, "log_equipment_request", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=log_equipment_request job_id=" in log_text
    assert "entity_id=equipment_request_equipment-fixed" in log_text


def test_log_equipment_request_creates_canonical_doc_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    payload = equipment_request_payload()
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_equipment_request_job(
        queue_file,
        job("log_equipment_request", payload, job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("equipment_request_equipment-fixed")
    assert doc is not None
    assert doc["_id"] == "equipment_request_equipment-fixed"
    assert doc["type"] == "equipment_request"
    assert doc["btq_job_ids"] == ["job-one"]
    assert doc["created_at"]
    assert not (site_path.parent / "Equipment").exists()
    assert (processed_dir / queue_file.name).exists()


def test_mark_issue_resolved_valid_transition_uses_canonical_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_issue_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = issue_transition_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_mark_issue_resolved_job(queue_file, job("mark_issue_resolved", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["status"] == "resolved"
    assert doc["resolved_at"] == "2026-05-30T12:00:00+00:00"
    assert doc["resolved_by"] == "Jordan"
    assert doc["resolved_note"] == "Maintenance cleared it."
    assert doc["btq_job_ids"] == ["job-one"]
    assert doc["related_capture_ids"] == ["cap-issue"]
    assert doc["related_candidate_ids"] == ["ac-issue"]
    assert store.update_doc_calls == ["site_issue_issue-fixed"]
    assert (processed_dir / queue_file.name).exists()


def test_mark_issue_monitoring_advances_status_from_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_issue_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = issue_transition_payload()
    queue_file = make_queue_file(context, "job-one")

    qp.process_mark_issue_monitoring_job(queue_file, job("mark_issue_monitoring", payload, job_id="job-one"), context, context.runtime_root / "processed")

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["status"] == "monitoring"
    assert doc["monitoring_at"] == "2026-05-30T12:00:00+00:00"
    assert doc["monitoring_by"] == "Jordan"
    assert doc["monitoring_note"] == "Maintenance cleared it."


def test_mark_issue_open_reopens_monitoring_or_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    for index, status in enumerate(("monitoring", "resolved"), start=1):
        issue_id = f"issue-fixed-{index}"
        doc = canonical_issue_doc(status=status)
        doc["_id"] = f"site_issue_{issue_id}"
        doc["issue_id"] = issue_id
        store = RecordingRmwVaultStore([doc])
        monkeypatch.setattr(shared, "_VAULT_STORE", store)
        payload = issue_transition_payload()
        payload["issue_id"] = issue_id
        queue_file = make_queue_file(context, f"job-{index}")

        qp.process_mark_issue_open_job(queue_file, job("mark_issue_open", payload, job_id=f"job-{index}"), context, context.runtime_root / "processed")

        updated = store.get_optional(f"site_issue_{issue_id}")
        assert updated is not None
        assert updated["status"] == "open"
        assert updated["open_at"] == "2026-05-30T12:00:00+00:00"
        assert updated["open_by"] == "Jordan"


def test_mark_issue_invalid_transition_fails_even_with_stale_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    stale_path = (
        context.vault_root
        / "Accounts"
        / "Contworks"
        / "Locations"
        / "7060 - Continental Metalworks"
        / "Issues"
        / "issue-fixed__railing.md"
    )
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_text = (
        "---\n"
        "type: site_issue\n"
        "issue_id: issue-fixed\n"
        "site_id: 7060\n"
        "site_name: Continental Metalworks\n"
        "account: Contworks\n"
        "title: Broken railing\n"
        "status: open\n"
        "created_at: 2026-05-01T00:00:00+00:00\n"
        "---\n"
        "# stale issue\n"
    )
    stale_path.write_text(stale_text, encoding="utf-8")
    store = RecordingRmwVaultStore([canonical_issue_doc(status="resolved")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = issue_transition_payload()
    queue_file = make_process_job_file(context, "mark_issue_monitoring", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["status"] == "resolved"
    assert stale_path.read_text(encoding="utf-8") == stale_text
    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert "Cannot transition issue issue-fixed to monitoring from current status resolved" in context.log_path.read_text(encoding="utf-8")


def test_mark_issue_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    existing_doc = canonical_issue_doc(status="open", job_ids=["job-one"])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = issue_transition_payload()
    queue_file = make_queue_file(context, "job-one")

    qp.process_mark_issue_resolved_job(queue_file, job("mark_issue_resolved", payload, job_id="job-one"), context, context.runtime_root / "processed")

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]


def archive_payload(record_type: str, record_id: str) -> dict:
    return {"record_type": record_type, "record_id": record_id, "actor": "Jordan", "note": "Duplicate."}


def edit_payload(record_type: str, record_id: str, fields: dict[str, object]) -> dict:
    return {"record_type": record_type, "record_id": record_id, "fields": fields, "actor": "Jordan"}


@pytest.mark.parametrize(
    ("record_type", "record_id", "doc_factory", "handler", "job_type"),
    [
        ("site_issue", "issue-fixed", canonical_issue_doc, qp.process_mark_record_archived_job, "mark_record_archived"),
        ("supply_need", "supply-fixed", canonical_supply_doc, qp.process_mark_record_archived_job, "mark_record_archived"),
        ("equipment_request", "equipment-fixed", canonical_equipment_doc, qp.process_mark_record_archived_job, "mark_record_archived"),
    ],
)
def test_mark_record_archived_sets_archive_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_type: str,
    record_id: str,
    doc_factory: Callable[[], dict[str, Any]],
    handler: Handler,
    job_type: str,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([doc_factory()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    handler(queue_file, job(job_type, archive_payload(record_type, record_id), job_id="job-one"), context, context.runtime_root / "processed")

    doc = store.get_optional(f"{record_type}_{record_id}")
    assert doc is not None
    assert doc["archived"] is True
    assert doc["archived_at"]
    assert doc["archived_by"] == "Jordan"
    assert doc["archive_note"] == "Duplicate."
    assert doc["btq_job_ids"] == ["job-one"]


@pytest.mark.parametrize(
    ("record_type", "record_id", "doc_factory", "fields", "expected"),
    [
        (
            "site_issue",
            "issue-fixed",
            canonical_issue_doc,
            {"site_id": "7050", "title": "Corrected title", "summary": "Corrected summary.", "priority": "urgent", "category": "safety", "resolution_trigger": "Repair confirmed."},
            {"site_id": "7050", "site_name": "Summit Wire", "title": "Corrected title", "summary": "Corrected summary.", "priority": "urgent", "category": "safety", "resolution_trigger": "Repair confirmed."},
        ),
        (
            "supply_need",
            "supply-fixed",
            canonical_supply_doc,
            {"site_id": "7050", "item_name": "Corrected gloves", "quantity_needed": "12", "urgency": "critical", "notes": "Corrected note."},
            {"site_id": "7050", "site_name": "Summit Wire", "item_name": "Corrected gloves", "quantity_needed": "12", "urgency": "critical", "notes": "Corrected note."},
        ),
        (
            "equipment_request",
            "equipment-fixed",
            canonical_equipment_doc,
            {"site_id": "7050", "equipment_name": "Corrected scrubber", "reason": "Corrected reason.", "priority": "high", "notes": "Corrected note."},
            {"site_id": "7050", "site_name": "Summit Wire", "equipment_name": "Corrected scrubber", "reason": "Corrected reason.", "priority": "high", "notes": "Corrected note."},
        ),
    ],
)
def test_edit_record_fields_applies_only_allowlisted_fields_and_site_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_type: str,
    record_id: str,
    doc_factory: Callable[[], dict[str, Any]],
    fields: dict[str, object],
    expected: dict[str, object],
) -> None:
    context = context_for(tmp_path)
    existing_doc = doc_factory()
    existing_doc.update({"archived": False, "created_at": "2026-05-01T00:00:00+00:00"})
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_edit_record_fields_job(
        queue_file,
        job("edit_record_fields", edit_payload(record_type, record_id, fields), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional(f"{record_type}_{record_id}")
    assert doc is not None
    for key, value in expected.items():
        assert doc[key] == value
    assert doc["status"] == "open"
    assert doc["archived"] is False
    assert doc["created_at"] == "2026-05-01T00:00:00+00:00"
    assert doc["updated_at"]
    assert doc["edited_by"] == "Jordan"
    assert doc["btq_job_ids"] == ["job-one"]


def test_edit_record_fields_rejects_non_allowlisted_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    existing_doc = canonical_issue_doc()
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    with pytest.raises(qp.QueueProcessorError):
        qp.process_edit_record_fields_job(
            queue_file,
            job("edit_record_fields", edit_payload("site_issue", "issue-fixed", {"summary": "ok", "status": "resolved"}), job_id="job-one"),
            context,
            context.runtime_root / "processed",
        )

    assert store.update_doc_calls == []
    assert store.get_optional("site_issue_issue-fixed") == existing_doc


def test_edit_record_fields_skips_when_job_id_already_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    existing_doc = canonical_supply_doc(job_ids=["job-one"])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_edit_record_fields_job(
        queue_file,
        job("edit_record_fields", edit_payload("supply_need", "supply-fixed", {"item_name": "Corrected"}), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]


def test_mark_record_unarchived_clears_archive_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    archived_doc = canonical_issue_doc(job_ids=["archive-job"])
    archived_doc.update({"archived": True, "archived_at": "2026-06-10T12:00:00+00:00", "archived_by": "Jordan", "archive_note": "Duplicate."})
    store = RecordingRmwVaultStore([archived_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "restore-job")

    qp.process_mark_record_unarchived_job(
        queue_file,
        job("mark_record_unarchived", archive_payload("site_issue", "issue-fixed"), job_id="restore-job"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("site_issue_issue-fixed")
    assert doc is not None
    assert doc["archived"] is False
    assert "archived_at" not in doc
    assert "archived_by" not in doc
    assert "archive_note" not in doc
    assert doc["btq_job_ids"] == ["archive-job", "restore-job"]


def test_mark_record_archived_skips_when_job_id_already_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    existing_doc = canonical_issue_doc(job_ids=["job-one"])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_mark_record_archived_job(
        queue_file,
        job("mark_record_archived", archive_payload("site_issue", "issue-fixed"), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]


def test_mark_supply_valid_transition_uses_canonical_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_supply_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_mark_supply_ordered_job(queue_file, job("mark_supply_ordered", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["status"] == "ordered"
    assert doc["ordered_at"] == "2026-05-30T12:00:00+00:00"
    assert doc["ordered_by"] == "Jordan"
    assert doc["ordered_note"] == "Ordered from Staples."
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["supply_need_supply-fixed"]
    assert (processed_dir / queue_file.name).exists()


def test_mark_supply_invalid_transition_fails_even_with_stale_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    stale_path = (
        context.vault_root
        / "Accounts"
        / "Contworks"
        / "Locations"
        / "7060 - Continental Metalworks"
        / "Supplies"
        / "supply-fixed__gloves.md"
    )
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_text = (
        "---\n"
        "type: supply_need\n"
        "supply_id: supply-fixed\n"
        "site_id: 7060\n"
        "site_name: Continental Metalworks\n"
        "account: Contworks\n"
        "item_name: Gloves\n"
        "status: open\n"
        "requested_by: Jordan\n"
        "created_at: 2026-05-01T00:00:00+00:00\n"
        "---\n"
        "# stale supply\n"
    )
    stale_path.write_text(stale_text, encoding="utf-8")
    store = RecordingRmwVaultStore([canonical_supply_doc(status="delivered")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_process_job_file(context, "mark_supply_ordered", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["status"] == "delivered"
    assert stale_path.read_text(encoding="utf-8") == stale_text
    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert "Cannot transition supply supply-fixed to ordered from current status delivered" in context.log_path.read_text(encoding="utf-8")


def test_mark_supply_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_process_job_file(context, "mark_supply_ordered", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=mark_supply_ordered job_id=" in log_text
    assert "entity_id=supply_need_supply-fixed" in log_text


def test_mark_supply_transition_works_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_supply_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_process_job_file(context, "mark_supply_ordered", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["status"] == "ordered"
    assert not list((context.vault_root / "Accounts").glob("*/Locations/*/Supplies/supply-fixed__*.md"))
    assert (processed_dir / queue_file.name).exists()
    assert not (failed_dir / queue_file.name).exists()


def test_mark_supply_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    existing_doc = canonical_supply_doc(status="open", job_ids=["job-one"])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_mark_supply_ordered_job(queue_file, job("mark_supply_ordered", payload, job_id="job-one"), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_mark_supply_transition_preserves_canonical_list_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    existing_doc = canonical_supply_doc(status="open", job_ids=["job-old"])
    existing_doc.update(
        {
            "related_capture_ids": ["cap-old"],
            "related_candidate_ids": ["candidate-old"],
            "related_media": ["/media/cap-old/shelf.jpg"],
            "source_artifacts": ["artifact-old"],
            "custom_field": "keep-me",
            "delivered_at": "2026-05-02T00:00:00+00:00",
        }
    )
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = supply_transition_payload()
    queue_file = make_queue_file(context, "job-one")

    qp.process_mark_supply_ordered_job(
        queue_file,
        job("mark_supply_ordered", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("supply_need_supply-fixed")
    assert doc is not None
    assert doc["related_capture_ids"] == ["cap-old"]
    assert doc["related_candidate_ids"] == ["candidate-old"]
    assert doc["related_media"] == ["/media/cap-old/shelf.jpg"]
    assert doc["source_artifacts"] == ["artifact-old"]
    assert doc["custom_field"] == "keep-me"
    assert doc["delivered_at"] == "2026-05-02T00:00:00+00:00"
    assert doc["ordered_at"] == "2026-05-30T12:00:00+00:00"
    assert doc["btq_job_ids"] == ["job-old", "job-one"]


def test_mark_equipment_valid_transition_uses_canonical_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_equipment_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = equipment_transition_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_mark_equipment_approved_job(queue_file, job("mark_equipment_approved", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("equipment_request_equipment-fixed")
    assert doc is not None
    assert doc["status"] == "approved"
    assert doc["approved_at"] == "2026-05-30T12:00:00+00:00"
    assert doc["approved_by"] == "Jordan"
    assert doc["approval_note"] == "Approved for replacement."
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["equipment_request_equipment-fixed"]
    assert (processed_dir / queue_file.name).exists()


def test_mark_equipment_invalid_transition_fails_even_with_stale_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    stale_path = (
        context.vault_root
        / "Accounts"
        / "Contworks"
        / "Locations"
        / "7060 - Continental Metalworks"
        / "Equipment"
        / "equipment-fixed__floor-scrubber.md"
    )
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_text = (
        "---\n"
        "type: equipment_request\n"
        "equipment_id: equipment-fixed\n"
        "site_id: 7060\n"
        "site_name: Continental Metalworks\n"
        "account: Contworks\n"
        "equipment_name: Floor scrubber\n"
        "status: open\n"
        "requested_by: Jordan\n"
        "created_at: 2026-05-01T00:00:00+00:00\n"
        "---\n"
        "# stale equipment\n"
    )
    stale_path.write_text(stale_text, encoding="utf-8")
    store = RecordingRmwVaultStore([canonical_equipment_doc(status="provided")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = equipment_transition_payload()
    queue_file = make_process_job_file(context, "mark_equipment_approved", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = store.get_optional("equipment_request_equipment-fixed")
    assert doc is not None
    assert doc["status"] == "provided"
    assert stale_path.read_text(encoding="utf-8") == stale_text
    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert "Cannot transition equipment equipment-fixed to approved from current status provided" in context.log_path.read_text(encoding="utf-8")


def test_mark_equipment_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = equipment_transition_payload()
    queue_file = make_process_job_file(context, "mark_equipment_approved", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=mark_equipment_approved job_id=" in log_text
    assert "entity_id=equipment_request_equipment-fixed" in log_text


def test_mark_equipment_transition_works_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore([canonical_equipment_doc(status="open")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = equipment_transition_payload()
    queue_file = make_process_job_file(context, "mark_equipment_approved", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = store.get_optional("equipment_request_equipment-fixed")
    assert doc is not None
    assert doc["status"] == "approved"
    assert not list((context.vault_root / "Accounts").glob("*/Locations/*/Equipment/equipment-fixed__*.md"))
    assert (processed_dir / queue_file.name).exists()
    assert not (failed_dir / queue_file.name).exists()


def test_set_entity_status_employee_resolves_via_canonical_not_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([canonical_employee_status_doc(status="active")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("employee", "E-200", "inactive")
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_set_entity_status_job(queue_file, job("set_entity_status", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("employee_per_01JASMINE000000000000000")
    assert doc is not None
    assert doc["status"] == "inactive"
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01JASMINE000000000000000"]
    assert not (context.vault_root / "People").exists()
    assert (processed_dir / queue_file.name).exists()


def test_set_entity_status_site_sets_active_and_removes_legacy_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    original_text = site_path.read_text(encoding="utf-8")
    store = RecordingRmwVaultStore([canonical_location_status_doc(active=False, status="inactive")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("site", "7060", "active")
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_set_entity_status_job(queue_file, job("set_entity_status", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["active"] is True
    assert "status" not in doc
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["location_7060"]
    assert site_path.read_text(encoding="utf-8") == original_text
    assert (processed_dir / queue_file.name).exists()


def test_set_entity_status_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_site(context.vault_root)
    site_path.write_text(
        site_path.read_text(encoding="utf-8").replace("site_id: 7060", "site_id: 7060\nbtq_job_ids:\n  - job-one"),
        encoding="utf-8",
    )
    store = RecordingRmwVaultStore([canonical_location_status_doc(active=False, status="inactive")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("site", "7060", "active")
    queue_file = make_queue_file(context, "job-one")

    qp.process_set_entity_status_job(
        queue_file,
        job("set_entity_status", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["active"] is True
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["location_7060"]


def test_set_entity_status_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("site", "7060", "inactive")
    queue_file = make_process_job_file(context, "set_entity_status", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=set_entity_status job_id=" in log_text
    assert "entity_id=location_7060" in log_text


def test_set_entity_status_ambiguous_employee_reference_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    first = canonical_employee_status_doc(person_id="per_first", employee_id="E-201", name="Maria Hutton")
    second = canonical_employee_status_doc(person_id="per_second", employee_id="E-202", name="Maria Hutton")
    store = RecordingRmwVaultStore([first, second])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("employee", "Maria Hutton", "inactive")
    queue_file = make_process_job_file(context, "set_entity_status", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert store.update_doc_calls == []
    assert "Ambiguous canonical employee target for name: Maria Hutton" in context.log_path.read_text(encoding="utf-8")


def test_set_entity_status_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_site(context.vault_root)
    existing_doc = canonical_location_status_doc(active=False, status="inactive", job_ids=["job-one"])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = set_entity_status_payload("site", "7060", "active")
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_set_entity_status_job(queue_file, job("set_entity_status", payload, job_id="job-one"), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_flag_access_constraint_appends_to_canonical_location_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_flag_access_constraint_job(
        queue_file,
        job("flag_access_constraint", flag_access_constraint_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "## Operational Notes" in doc["content"]
    assert "### Access Constraints" in doc["content"]
    assert "2026-04-19 — Only one employee has the badge." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert (processed_dir / queue_file.name).exists()


def test_flag_access_constraint_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root, job_ids=["job-one"])
    store = RecordingRmwVaultStore([canonical_location_content_doc(job_ids=[])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_flag_access_constraint_job(
        queue_file,
        job("flag_access_constraint", flag_access_constraint_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "2026-04-19 — Only one employee has the badge." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]


def test_flag_access_constraint_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_visit_doc(site_id="7030", date="2026-04-19")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = flag_access_constraint_payload()
    queue_file = make_process_job_file(context, "flag_access_constraint", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=flag_access_constraint job_id=" in log_text
    assert "entity_id=location_7030" in log_text


def test_flag_access_constraint_no_active_visit_creates_canonical_visit_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_flag_access_constraint_job(
        queue_file,
        job("flag_access_constraint", flag_access_constraint_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    gap_doc = store.get_optional("visit_gap_7030_2026-04-19")
    assert gap_doc == {
        "_id": "visit_gap_7030_2026-04-19",
        "type": "visit_gap",
        "operator": OPERATOR_ID_GREG,
        "site": "Western Gas Transmission",
        "site_id": "7030",
        "date": "2026-04-19",
        "reason": "event_without_visit",
        "btq_job_ids": ["job-one"],
    }
    assert "type: visit_gap" not in site_path.read_text(encoding="utf-8")


def test_flag_access_constraint_active_visit_creates_no_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_flag_access_constraint_job(
        queue_file,
        job("flag_access_constraint", flag_access_constraint_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert 'visit_key: "Western Gas Transmission:2026-04-19"' in doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is None


def test_flag_access_constraint_appends_without_site_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_flag_access_constraint_job(
        queue_file,
        job("flag_access_constraint", flag_access_constraint_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "2026-04-19 — Only one employee has the badge." in doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is not None
    assert not (context.vault_root / "Accounts").exists()


def test_trigger_recruiting_appends_to_canonical_location_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_trigger_recruiting_job(
        queue_file,
        job("trigger_recruiting", trigger_recruiting_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "## Operational Notes" in doc["content"]
    assert "### Recruiting Triggers" in doc["content"]
    assert "2026-04-19 — priority=emergency | open_positions=2 — Two openings remain on site." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert (processed_dir / queue_file.name).exists()


def test_trigger_recruiting_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root, job_ids=["job-one"])
    store = RecordingRmwVaultStore([canonical_location_content_doc(job_ids=[])])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_trigger_recruiting_job(
        queue_file,
        job("trigger_recruiting", trigger_recruiting_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "2026-04-19 — priority=emergency | open_positions=2 — Two openings remain on site." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]


def test_trigger_recruiting_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_visit_doc(site_id="7030", date="2026-04-19")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = trigger_recruiting_payload()
    queue_file = make_process_job_file(context, "trigger_recruiting", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=trigger_recruiting job_id=" in log_text
    assert "entity_id=location_7030" in log_text


def test_trigger_recruiting_no_active_visit_creates_canonical_visit_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_trigger_recruiting_job(
        queue_file,
        job("trigger_recruiting", trigger_recruiting_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    gap_doc = store.get_optional("visit_gap_7030_2026-04-19")
    assert gap_doc is not None
    assert gap_doc["site"] == "Western Gas Transmission"
    assert gap_doc["site_id"] == "7030"
    assert gap_doc["date"] == "2026-04-19"
    assert gap_doc["reason"] == "event_without_visit"
    assert gap_doc["operator"] == OPERATOR_ID_GREG
    assert gap_doc["btq_job_ids"] == ["job-one"]
    assert "type: visit_gap" not in site_path.read_text(encoding="utf-8")


def test_trigger_recruiting_active_visit_creates_no_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_trigger_recruiting_job(
        queue_file,
        job("trigger_recruiting", trigger_recruiting_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert 'visit_key: "Western Gas Transmission:2026-04-19"' in doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is None


def test_trigger_recruiting_appends_without_site_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_trigger_recruiting_job(
        queue_file,
        job("trigger_recruiting", trigger_recruiting_payload(), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("location_7030")
    assert doc is not None
    assert "2026-04-19 — priority=emergency | open_positions=2 — Two openings remain on site." in doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is not None
    assert not (context.vault_root / "Accounts").exists()


def test_close_recruiting_filled_updates_site_and_employee_canonical_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    write_employee_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_employee_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    site_doc = store.get_optional("location_7030")
    employee_doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert site_doc is not None
    assert employee_doc is not None
    assert "### Recruiting Closed" in site_doc["content"]
    assert "2026-04-19 — outcome=filled — filled_by=Peter Nash — Coverage plan changed." in site_doc["content"]
    assert "## Schedule Changes" in employee_doc["content"]
    assert "2026-04-19 — placed at Western Gas Transmission — Coverage plan changed." in employee_doc["content"]
    assert site_doc["btq_job_ids"] == ["job-one"]
    assert employee_doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01PETERNASH00000000000", "location_7030"]
    assert (processed_dir / queue_file.name).exists()


def test_close_recruiting_not_filled_updates_site_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_employee_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(outcome="cancelled", filled_by=None), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    site_doc = store.get_optional("location_7030")
    employee_doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert site_doc is not None
    assert employee_doc is not None
    assert "### Recruiting Closed" in site_doc["content"]
    assert "outcome=cancelled" in site_doc["content"]
    assert employee_doc["content"] == "# Peter Nash\n"
    assert store.update_doc_calls == ["location_7030"]


def test_close_recruiting_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root, job_ids=["job-one"])
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(job_ids=[]),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(outcome="cancelled", filled_by=None), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    site_doc = store.get_optional("location_7030")
    assert site_doc is not None
    assert "2026-04-19 — outcome=cancelled — Coverage plan changed." in site_doc["content"]
    assert site_doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["location_7030"]


def test_close_recruiting_missing_site_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_visit_doc(site_id="7030", date="2026-04-19")])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = close_recruiting_payload(outcome="cancelled", filled_by=None)
    queue_file = make_process_job_file(context, "close_recruiting", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=close_recruiting job_id=" in log_text
    assert "entity_id=location_7030" in log_text


def test_close_recruiting_filled_unknown_employee_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = close_recruiting_payload(filled_by="Unknown Employee")
    queue_file = make_process_job_file(context, "close_recruiting", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    site_doc = store.get_optional("location_7030")
    assert site_doc is not None
    assert site_doc["content"] == "# Western Gas Transmission\n"
    assert site_doc["btq_job_ids"] == []
    assert store.update_doc_calls == []
    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert "Could not resolve canonical employee target: Unknown Employee" in context.log_path.read_text(encoding="utf-8")


def test_close_recruiting_no_active_visit_creates_canonical_visit_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    site_path = write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([canonical_location_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(outcome="cancelled", filled_by=None), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    gap_doc = store.get_optional("visit_gap_7030_2026-04-19")
    assert gap_doc is not None
    assert gap_doc["site"] == "Western Gas Transmission"
    assert gap_doc["site_id"] == "7030"
    assert gap_doc["date"] == "2026-04-19"
    assert gap_doc["reason"] == "event_without_visit"
    assert gap_doc["operator"] == OPERATOR_ID_GREG
    assert gap_doc["btq_job_ids"] == ["job-one"]
    assert "type: visit_gap" not in site_path.read_text(encoding="utf-8")
    assert store.update_doc_calls == ["visit_gap_7030_2026-04-19", "location_7030"]


def test_close_recruiting_active_visit_creates_no_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_visit_doc(site_id="7030", date="2026-04-19"),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(outcome="cancelled", filled_by=None), job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    site_doc = store.get_optional("location_7030")
    assert site_doc is not None
    assert 'visit_key: "Western Gas Transmission:2026-04-19"' in site_doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is None


def test_close_recruiting_succeeds_without_markdown_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([
        canonical_location_content_doc(),
        canonical_employee_content_doc(),
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    site_doc = store.get_optional("location_7030")
    employee_doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert site_doc is not None
    assert employee_doc is not None
    assert "### Recruiting Closed" in site_doc["content"]
    assert "placed at Western Gas Transmission" in employee_doc["content"]
    assert store.get_optional("visit_gap_7030_2026-04-19") is not None
    assert not (context.vault_root / "Accounts").exists()
    assert not (context.vault_root / "People").exists()
    assert (processed_dir / queue_file.name).exists()


def test_close_recruiting_replay_skips_without_double_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_location_markdown(context.vault_root)
    existing_site_doc = canonical_location_content_doc(
        content="# Western Gas Transmission\n\n## Operational Notes\n### Recruiting Closed\n\n2026-04-19 — outcome=filled — filled_by=Peter Nash — Coverage plan changed.\n",
        job_ids=["job-one"],
    )
    existing_employee_doc = canonical_employee_content_doc(
        content="# Peter Nash\n\n## Schedule Changes\n2026-04-19 — placed at Western Gas Transmission — Coverage plan changed.\n",
        job_ids=["job-one"],
    )
    store = RecordingRmwVaultStore([
        existing_site_doc,
        existing_employee_doc,
        {
            "_id": "visit_gap_7030_2026-04-19",
            "type": "visit_gap",
            "site": "Western Gas Transmission",
            "site_id": "7030",
            "date": "2026-04-19",
            "reason": "event_without_visit",
            "btq_job_ids": ["job-one"],
        },
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_close_recruiting_job(
        queue_file,
        job("close_recruiting", close_recruiting_payload(), job_id="job-one"),
        context,
        processed_dir,
    )

    assert store.update_doc_calls == []
    assert store.get_optional("location_7030") == existing_site_doc
    assert store.get_optional("employee_per_01PETERNASH00000000000") == existing_employee_doc
    assert (processed_dir / queue_file.name).exists()


def test_flag_retention_risk_appends_to_canonical_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_employee_markdown(context.vault_root)
    existing_doc = canonical_employee_content_doc()
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = flag_retention_risk_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_flag_retention_risk_job(queue_file, job("flag_retention_risk", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "## Retention Risks" in doc["content"]
    assert "2026-04-19 — Western Gas Transmission — May leave if evening load stays unchanged." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01PETERNASH00000000000"]
    assert (processed_dir / queue_file.name).exists()


def test_flag_retention_risk_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_employee_markdown(context.vault_root, job_ids=["job-one"])
    existing_doc = canonical_employee_content_doc(job_ids=[])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = flag_retention_risk_payload()
    queue_file = make_queue_file(context, "job-one")

    qp.process_flag_retention_risk_job(
        queue_file,
        job("flag_retention_risk", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "2026-04-19 — Western Gas Transmission — May leave if evening load stays unchanged." in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01PETERNASH00000000000"]


def test_flag_retention_risk_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = MissingCanonicalEmployeeDocStore(canonical_employee_content_doc())
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = flag_retention_risk_payload()
    queue_file = make_process_job_file(context, "flag_retention_risk", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=flag_retention_risk job_id=" in log_text
    assert "entity_id=employee_per_01PETERNASH00000000000" in log_text


def test_flag_retention_risk_appends_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([canonical_employee_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = flag_retention_risk_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_flag_retention_risk_job(queue_file, job("flag_retention_risk", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "2026-04-19 — Western Gas Transmission — May leave if evening load stays unchanged." in doc["content"]
    assert not (context.vault_root / "People").exists()
    assert (processed_dir / queue_file.name).exists()


def test_remove_from_schedule_appends_to_canonical_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_employee_markdown(context.vault_root)
    existing_doc = canonical_employee_content_doc()
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = remove_from_schedule_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_remove_from_schedule_job(queue_file, job("remove_from_schedule", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "## Schedule Changes" in doc["content"]
    assert "2026-04-19 — removed from schedule for Western Gas Transmission" in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01PETERNASH00000000000"]
    assert (processed_dir / queue_file.name).exists()


def test_remove_from_schedule_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    write_employee_markdown(context.vault_root, job_ids=["job-one"])
    existing_doc = canonical_employee_content_doc(job_ids=[])
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = remove_from_schedule_payload()
    queue_file = make_queue_file(context, "job-one")

    qp.process_remove_from_schedule_job(
        queue_file,
        job("remove_from_schedule", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "2026-04-19 — removed from schedule for Western Gas Transmission" in doc["content"]
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["employee_per_01PETERNASH00000000000"]


def test_remove_from_schedule_missing_canonical_doc_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = MissingCanonicalEmployeeDocStore(canonical_employee_content_doc())
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = remove_from_schedule_payload()
    queue_file = make_process_job_file(context, "remove_from_schedule", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=remove_from_schedule job_id=" in log_text
    assert "entity_id=employee_per_01PETERNASH00000000000" in log_text


def test_remove_from_schedule_appends_without_markdown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([canonical_employee_content_doc()])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = remove_from_schedule_payload()
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_remove_from_schedule_job(queue_file, job("remove_from_schedule", payload, job_id="job-one"), context, processed_dir)

    doc = store.get_optional("employee_per_01PETERNASH00000000000")
    assert doc is not None
    assert "2026-04-19 — removed from schedule for Western Gas Transmission" in doc["content"]
    assert not (context.vault_root / "People").exists()
    assert (processed_dir / queue_file.name).exists()


def test_remove_from_schedule_ambiguous_employee_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    first = canonical_employee_content_doc(person_id="per_first", employee_id="E-301", name="Peter Nash")
    second = canonical_employee_content_doc(person_id="per_second", employee_id="E-302", name="Peter Nash")
    store = RecordingRmwVaultStore([first, second])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = remove_from_schedule_payload()
    queue_file = make_process_job_file(context, "remove_from_schedule", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    assert store.update_doc_calls == []
    assert "Ambiguous canonical employee target for name: Peter Nash" in context.log_path.read_text(encoding="utf-8")


def test_log_personnel_event_skips_when_job_id_in_canonical_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    payload = personnel_event_payload()
    existing_doc = {
        "_id": "personnel_event_personnel-fixed",
        "type": "personnel_event",
        "event_id": "personnel-fixed",
        "employee": "Maria Hutton",
        "event_type": "training",
        "reported_by": "Jordan",
        "created_at": "2026-05-01T00:00:00+00:00",
        "btq_job_ids": ["job-one"],
    }
    store = RecordingRmwVaultStore([existing_doc])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_personnel_event_job(queue_file, job("log_personnel_event", payload), context, processed_dir)

    assert store.update_doc_calls == []
    assert store.docs == [existing_doc]
    assert (processed_dir / queue_file.name).exists()


def test_log_personnel_event_merges_job_id_and_preserves_created_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    payload = personnel_event_payload()
    payload["status"] = "resolved"
    store = RecordingRmwVaultStore([
        {
            "_id": "personnel_event_personnel-fixed",
            "type": "personnel_event",
            "event_id": "personnel-fixed",
            "employee": "Maria Hutton",
            "event_type": "training",
            "reported_by": "Jordan",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": ["job-old"],
            "custom_field": "keep-me",
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")

    qp.process_log_personnel_event_job(
        queue_file,
        job("log_personnel_event", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("personnel_event_personnel-fixed")
    assert doc is not None
    assert doc["created_at"] == "2026-05-01T00:00:00+00:00"
    assert doc["btq_job_ids"] == ["job-old", "job-one"]
    assert doc["custom_field"] == "keep-me"
    assert doc["status"] == "resolved"


def test_log_personnel_event_stale_markdown_does_not_cause_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    payload = personnel_event_payload()
    store = RecordingRmwVaultStore([
        {
            "_id": "personnel_event_personnel-fixed",
            "type": "personnel_event",
            "event_id": "personnel-fixed",
            "employee": "Maria Hutton",
            "event_type": "training",
            "reported_by": "Jordan",
            "created_at": "2026-05-01T00:00:00+00:00",
            "btq_job_ids": [],
        }
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    # A stale markdown projection must be ignored entirely: the canonical
    # CouchDB personnel_event doc is authoritative.
    stale_projection = context.vault_root / "People" / "Hutton, Maria" / "Events" / "stale.md"
    stale_projection.parent.mkdir(parents=True, exist_ok=True)
    stale_projection.write_text("---\nbtq_job_ids:\n  - job-one\n---\n# stale projection\n", encoding="utf-8")

    qp.process_log_personnel_event_job(
        queue_file,
        job("log_personnel_event", payload, job_id="job-one"),
        context,
        context.runtime_root / "processed",
    )

    doc = store.get_optional("personnel_event_personnel-fixed")
    assert doc is not None
    assert doc["btq_job_ids"] == ["job-one"]
    assert store.update_doc_calls == ["personnel_event_personnel-fixed"]


def test_log_personnel_event_couchdb_failure_fails_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    store = ExplodingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    payload = personnel_event_payload()
    payload["event_type"] = "other"
    queue_file = make_process_job_file(context, "log_personnel_event", payload, "job-one")
    processed_dir = context.runtime_root / "processed"
    failed_dir = context.runtime_root / "failed"

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = context.log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=log_personnel_event job_id=" in log_text
    assert "entity_id=personnel_event_personnel-fixed" in log_text


def test_log_personnel_event_creates_canonical_doc_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = context_for(tmp_path)
    payload = personnel_event_payload()
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "job-one")
    processed_dir = context.runtime_root / "processed"

    qp.process_log_personnel_event_job(
        queue_file,
        job("log_personnel_event", payload, job_id="job-one"),
        context,
        processed_dir,
    )

    doc = store.get_optional("personnel_event_personnel-fixed")
    assert doc is not None
    assert doc["_id"] == "personnel_event_personnel-fixed"
    assert doc["type"] == "personnel_event"
    assert doc["btq_job_ids"] == ["job-one"]
    assert doc["created_at"]
    # No markdown projection is written under the (dead) vault root.
    assert not (context.vault_root / "People").exists()
    assert (processed_dir / queue_file.name).exists()
