from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from queue_processor import main as qp
from queue_processor.handlers import _shared as shared
from test_helpers.queue_processor_stores import RecordingVaultStore


class RmwRecordingVaultStore(RecordingVaultStore):
    def __init__(self) -> None:
        super().__init__()
        self.update_doc_calls: list[str] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
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


@dataclass(frozen=True)
class CanonicalJobCase:
    job_type: str
    payload: dict[str, Any]
    expected_doc_type: str | None = None
    expected_doc_id: str | None = None
    expected_doc_id_prefix: str | None = None
    expected_patch_id: str | None = None
    expected_patch_status: str | None = None
    setup: Callable[[Path], None] | None = None


def write_frontmatter_file(path: Path, fields: list[tuple[str, str]], body: str = "") -> None:
    lines = ["---", *[f"{key}: {value}" for key, value in fields], "---"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + f"\n{body}", encoding="utf-8")


def write_summit_wire_site(vault_root: Path) -> None:
    write_frontmatter_file(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md",
        [
            ("type", "location"),
            ("site_id", "7050"),
            ("job", "7050"),
            ("location", "Summit Wire"),
            ("account", "Summitsteel"),
        ],
        "# Summit Wire\n",
    )


def write_supply_need(vault_root: Path, *, supply_id: str, status: str) -> None:
    write_frontmatter_file(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Supplies" / f"{supply_id}__cleaner.md",
        [
            ("type", "supply_need"),
            ("supply_id", supply_id),
            ("site_id", "7050"),
            ("site_name", "Summit Wire"),
            ("account", "Summitsteel"),
            ("item_name", "BrightWash cleaner"),
            ("status", status),
            ("urgency", "normal"),
            ("requested_by", "Jordan"),
            ("created_at", "2026-05-08T17:00:00+00:00"),
        ],
        "Existing supply record.\n",
    )


def write_equipment_request(vault_root: Path, *, equipment_id: str, status: str) -> None:
    write_frontmatter_file(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Equipment" / f"{equipment_id}__vacuum.md",
        [
            ("type", "equipment_request"),
            ("equipment_id", equipment_id),
            ("site_id", "7050"),
            ("site_name", "Summit Wire"),
            ("account", "Summitsteel"),
            ("equipment_name", "vacuum"),
            ("status", status),
            ("priority", "normal"),
            ("requested_by", "Jordan"),
            ("created_at", "2026-05-08T17:00:00+00:00"),
        ],
        "Existing equipment record.\n",
    )


def write_site_issue(vault_root: Path, *, issue_id: str, status: str) -> None:
    write_frontmatter_file(
        vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Issues" / f"{issue_id}__drain.md",
        [
            ("type", "site_issue"),
            ("issue_id", issue_id),
            ("site_id", "7050"),
            ("site_name", "Summit Wire"),
            ("account", "Summitsteel"),
            ("title", "Restroom drain backup"),
            ("status", status),
            ("priority", "normal"),
            ("category", "maintenance"),
            ("created_at", "2026-05-08T17:00:00+00:00"),
        ],
        "Existing issue record.\n",
    )


def write_queue_job(runtime_root: Path, case: CanonicalJobCase) -> Path:
    queue_dir = runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_path = queue_dir / f"2026-05-30T12-00-00Z__{case.job_type}.json"
    job_payload = {
        "job_id": f"job-{case.job_type}",
        "job_type": case.job_type,
        "payload": case.payload,
    }
    job_path.write_text(json.dumps(job_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return job_path


def build_context(tmp_path: Path) -> tuple[Path, Path, qp.RunContext, Path, Path]:
    project_root = tmp_path / "project"
    vault_root = tmp_path / "vault"
    runtime_root = tmp_path / "runtime"
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    for path in (
        project_root,
        vault_root / "Accounts",
        vault_root / "People",
        vault_root / "Journal",
        processed_dir,
        failed_dir,
        runtime_root / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    write_summit_wire_site(vault_root)
    context = qp.RunContext(
        project_root=project_root,
        vault_root=vault_root,
        personal_vault_root=vault_root,
        runtime_root=runtime_root,
        log_path=runtime_root / "logs" / "run.log",
        dry_run=False,
        valid_site_ids={"7050"},
        site_id_to_opportunities_dir={},
    )
    return vault_root, runtime_root, context, processed_dir, failed_dir


def markdown_snapshot(vault_root: Path) -> dict[Path, str]:
    return {
        path.relative_to(vault_root): path.read_text(encoding="utf-8")
        for path in sorted(vault_root.rglob("*.md"))
    }


def setup_supply_open(vault_root: Path) -> None:
    write_supply_need(vault_root, supply_id="sup_prod_default", status="open")


def setup_supply_ordered(vault_root: Path) -> None:
    write_supply_need(vault_root, supply_id="sup_prod_default", status="ordered")


def setup_supply_delivered(vault_root: Path) -> None:
    write_supply_need(vault_root, supply_id="sup_prod_default", status="delivered")


def setup_equipment_open(vault_root: Path) -> None:
    write_equipment_request(vault_root, equipment_id="eqr_prod_default", status="open")


def setup_equipment_approved(vault_root: Path) -> None:
    write_equipment_request(vault_root, equipment_id="eqr_prod_default", status="approved")


def setup_equipment_ordered(vault_root: Path) -> None:
    write_equipment_request(vault_root, equipment_id="eqr_prod_default", status="ordered")


def setup_issue_open(vault_root: Path) -> None:
    write_site_issue(vault_root, issue_id="issue_prod_default", status="open")


def setup_issue_monitoring(vault_root: Path) -> None:
    write_site_issue(vault_root, issue_id="issue_prod_default", status="monitoring")


def setup_issue_resolved(vault_root: Path) -> None:
    write_site_issue(vault_root, issue_id="issue_prod_default", status="resolved")


def canonical_source_status_for_transition(job_type: str) -> str:
    return {
        "mark_supply_ordered": "open",
        "mark_supply_delivered": "ordered",
        "mark_supply_stocked": "delivered",
        "mark_supply_no_action_needed": "open",
        "mark_equipment_approved": "open",
        "mark_equipment_denied": "approved",
        "mark_equipment_ordered": "approved",
        "mark_equipment_provided": "ordered",
        "mark_equipment_no_action_needed": "open",
        "mark_issue_monitoring": "open",
        "mark_issue_resolved": "monitoring",
        "mark_issue_open": "resolved",
    }[job_type]


def canonical_transition_doc(case: CanonicalJobCase) -> dict[str, Any]:
    assert case.expected_patch_id is not None
    status = canonical_source_status_for_transition(case.job_type)
    if case.expected_patch_id.startswith("supply_need_"):
        supply_id = str(case.payload["supply_id"])
        return {
            "_id": case.expected_patch_id,
            "type": "supply_need",
            "supply_id": supply_id,
            "site_id": "7050",
            "site_name": "Summit Wire",
            "account": "Summitsteel",
            "item_name": "BrightWash cleaner",
            "status": status,
            "urgency": "normal",
            "requested_by": "Jordan",
            "created_at": "2026-05-08T17:00:00+00:00",
        }
    if case.expected_patch_id.startswith("site_issue_"):
        issue_id = str(case.payload["issue_id"])
        return {
            "_id": case.expected_patch_id,
            "type": "site_issue",
            "issue_id": issue_id,
            "site_id": "7050",
            "site_name": "Summit Wire",
            "account": "Summitsteel",
            "title": "Restroom drain backup",
            "summary": "Drain backed up onto the restroom floor.",
            "status": status,
            "priority": "normal",
            "category": "maintenance",
            "created_at": "2026-05-08T17:00:00+00:00",
        }
    equipment_id = str(case.payload["equipment_id"])
    return {
        "_id": case.expected_patch_id,
        "type": "equipment_request",
        "equipment_id": equipment_id,
        "site_id": "7050",
        "site_name": "Summit Wire",
        "account": "Summitsteel",
        "equipment_name": "vacuum",
        "status": status,
        "priority": "normal",
        "requested_by": "Jordan",
        "created_at": "2026-05-08T17:00:00+00:00",
    }


CANONICAL_JOB_CASES = [
    CanonicalJobCase(
        job_type="visit_create",
        payload={
            "site": "7050",
            "confidence": "high",
            "source": "production-default-test",
            "evidence": "Production default visit evidence.",
        },
        expected_doc_type="visit",
        expected_doc_id_prefix="visit_7050_",
    ),
    CanonicalJobCase(
        job_type="add_person",
        payload={"name": "Taylor Production", "role": "Cleaner", "employee_id": "9001"},
        expected_doc_type="employee",
        expected_doc_id_prefix="employee_",
    ),
    CanonicalJobCase(
        job_type="log_site_issue",
        payload={
            "site_id": "7050",
            "issue_id": "issue_prod_default",
            "title": "Restroom drain backup",
            "summary": "Drain backed up onto the restroom floor.",
            "category": "maintenance",
            "priority": "high",
            "status": "open",
            "reported_by": "Jordan",
            "client_notified": False,
            "resolution_trigger": "Drain is clear.",
        },
        expected_doc_type="site_issue",
        expected_doc_id="site_issue_issue_prod_default",
    ),
    CanonicalJobCase(
        job_type="log_supply_need",
        payload={
            "site_id": "7050",
            "supply_id": "sup_prod_default_created",
            "item_name": "BrightWash cleaner",
            "quantity_needed": "2 bottles",
            "urgency": "high",
            "requested_by": "Jordan",
        },
        expected_doc_type="supply_need",
        expected_doc_id="supply_need_sup_prod_default_created",
    ),
    CanonicalJobCase(
        job_type="log_equipment_request",
        payload={
            "site_id": "7050",
            "equipment_id": "eqr_prod_default_created",
            "equipment_name": "vacuum",
            "reason": "Current vacuum will not start.",
            "priority": "urgent",
            "requested_by": "Jordan",
        },
        expected_doc_type="equipment_request",
        expected_doc_id="equipment_request_eqr_prod_default_created",
    ),
    CanonicalJobCase(
        job_type="log_personnel_event",
        payload={
            "employee": "Taylor Production",
            "event_id": "pe_prod_default",
            "event_type": "attendance",
            "summary": "No call no show for opening shift.",
            "occurred_at": "2026-05-18T05:30:00-04:00",
            "reported_by": "Jordan",
            "severity": "concern",
            "status": "open",
            "related_site": "7050",
            "client_notified": False,
            "resolution_trigger": "Two covered shifts without recurrence.",
        },
        expected_doc_type="personnel_event",
        expected_doc_id="personnel_event_pe_prod_default",
    ),
    CanonicalJobCase(
        job_type="mark_supply_ordered",
        payload={"supply_id": "sup_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="supply_need_sup_prod_default",
        expected_patch_status="ordered",
        setup=setup_supply_open,
    ),
    CanonicalJobCase(
        job_type="mark_supply_delivered",
        payload={"supply_id": "sup_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="supply_need_sup_prod_default",
        expected_patch_status="delivered",
        setup=setup_supply_ordered,
    ),
    CanonicalJobCase(
        job_type="mark_supply_stocked",
        payload={"supply_id": "sup_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="supply_need_sup_prod_default",
        expected_patch_status="stocked",
        setup=setup_supply_delivered,
    ),
    CanonicalJobCase(
        job_type="mark_supply_no_action_needed",
        payload={"supply_id": "sup_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="supply_need_sup_prod_default",
        expected_patch_status="no_action_needed",
        setup=setup_supply_open,
    ),
    CanonicalJobCase(
        job_type="mark_equipment_approved",
        payload={"equipment_id": "eqr_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="equipment_request_eqr_prod_default",
        expected_patch_status="approved",
        setup=setup_equipment_open,
    ),
    CanonicalJobCase(
        job_type="mark_equipment_denied",
        payload={"equipment_id": "eqr_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="equipment_request_eqr_prod_default",
        expected_patch_status="denied",
        setup=setup_equipment_approved,
    ),
    CanonicalJobCase(
        job_type="mark_equipment_ordered",
        payload={"equipment_id": "eqr_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="equipment_request_eqr_prod_default",
        expected_patch_status="ordered",
        setup=setup_equipment_approved,
    ),
    CanonicalJobCase(
        job_type="mark_equipment_provided",
        payload={"equipment_id": "eqr_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="equipment_request_eqr_prod_default",
        expected_patch_status="provided",
        setup=setup_equipment_ordered,
    ),
    CanonicalJobCase(
        job_type="mark_equipment_no_action_needed",
        payload={"equipment_id": "eqr_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="equipment_request_eqr_prod_default",
        expected_patch_status="no_action_needed",
        setup=setup_equipment_open,
    ),
    CanonicalJobCase(
        job_type="mark_issue_monitoring",
        payload={"issue_id": "issue_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="site_issue_issue_prod_default",
        expected_patch_status="monitoring",
        setup=setup_issue_open,
    ),
    CanonicalJobCase(
        job_type="mark_issue_resolved",
        payload={"issue_id": "issue_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="site_issue_issue_prod_default",
        expected_patch_status="resolved",
        setup=setup_issue_monitoring,
    ),
    CanonicalJobCase(
        job_type="mark_issue_open",
        payload={"issue_id": "issue_prod_default", "actor": "Jordan", "occurred_at": "2026-05-30T12:00:00+00:00"},
        expected_patch_id="site_issue_issue_prod_default",
        expected_patch_status="open",
        setup=setup_issue_resolved,
    ),
]


@pytest.mark.parametrize("case", CANONICAL_JOB_CASES, ids=[case.job_type for case in CANONICAL_JOB_CASES])
def test_job_type_production_default_writes_couchdb_not_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: CanonicalJobCase,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    store = RmwRecordingVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    vault_root, runtime_root, context, processed_dir, failed_dir = build_context(tmp_path)
    if case.setup is not None:
        case.setup(vault_root)
    if case.expected_patch_id is not None:
        store.docs.append(canonical_transition_doc(case))
    before_markdown = markdown_snapshot(vault_root)
    job_path = write_queue_job(runtime_root, case)

    qp.process_job(job_path, context, processed_dir, failed_dir)

    assert not (failed_dir / job_path.name).exists()
    assert (processed_dir / job_path.name).exists()
    if case.expected_doc_type is not None:
        assert len(store.docs) == 1
        doc = store.docs[0]
        assert doc["type"] == case.expected_doc_type
        if case.expected_doc_id is not None:
            assert doc["_id"] == case.expected_doc_id
        if case.expected_doc_id_prefix is not None:
            assert doc["_id"].startswith(case.expected_doc_id_prefix)
    else:
        assert len(store.docs) == 1
        assert store.docs[0]["_id"] == case.expected_patch_id
        expected_type = "supply_need"
        if case.expected_patch_id.startswith("equipment_request_"):
            expected_type = "equipment_request"
        if case.expected_patch_id.startswith("site_issue_"):
            expected_type = "site_issue"
        assert store.docs[0]["type"] == expected_type
        assert store.docs[0]["status"] == case.expected_patch_status
        assert store.update_doc_calls == [case.expected_patch_id]
        assert store.patch_status_calls == []
    after_markdown = markdown_snapshot(vault_root)
    assert after_markdown == before_markdown


def test_markdown_default_off_is_the_suite_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)

    assert not hasattr(qp, "_markdown_write_enabled")
