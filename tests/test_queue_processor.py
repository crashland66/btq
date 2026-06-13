import io
import json
import os
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from event_to_queue.adapter import event_to_job
from queue_spec import validate_job
import btq
from btq_vault.couch_store import CouchDBEntityStore
from btq_vault.entity_types import OPERATOR_ID_GREG
from event_pipeline import couchdb_config
from event_pipeline.couchdb import setup_databases
from queue_processor import repair
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import misc
from queue_processor import processed_index
from test_helpers.queue_processor_stores import RecordingVaultStore


pytestmark = pytest.mark.usefixtures("recording_vault_store")


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "project"
MODULE_PATH = PROJECT_ROOT / "queue_processor" / "main.py"
SUPPLY_EMAIL_HTML = """
<html>
  <body>
    <div>Order Number: 99887766</div>
    <div>Order Date: 04/20/2026</div>
    <div>PO Number: PO-7080-APR</div>
    <div>Shipping Address</div>
    <div>Apex Powdered Metals</div>
    <div>700 Martha St</div>
    <div>Springfield, PA 00000</div>
    <table>
      <tr><th>Item #</th><th>Description</th><th>Qty</th><th>Unit Price</th><th>Amount</th></tr>
      <tr><td>123456</td><td>Trash Liners 56 Gal</td><td>2</td><td>$18.49</td><td>$36.98</td></tr>
      <tr><td>998100</td><td>Foaming Hand Soap</td><td>4</td><td>$6.25</td><td>$25.00</td></tr>
    </table>
    <table>
      <tr><td>Subtotal</td><td>$61.98</td></tr>
      <tr><td>Tax</td><td>$3.72</td></tr>
      <tr><td>Shipping</td><td>$0.00</td></tr>
      <tr><td>Total</td><td>$65.70</td></tr>
    </table>
  </body>
</html>
"""


def load_queue_processor_module() -> types.ModuleType:
    module = types.ModuleType("queue_processor_main_for_tests")
    module.__file__ = str(MODULE_PATH)
    sys.modules[module.__name__] = module
    source = MODULE_PATH.read_text(encoding="utf-8")
    compiled = compile(
        "from __future__ import annotations\n" + source,
        str(MODULE_PATH),
        "exec",
    )
    exec(compiled, module.__dict__)
    return module


qp = load_queue_processor_module()


def write_frontmatter_file(path: Path, fields: list[tuple[str, str]], body: str = "") -> None:
    lines = ["---", *[f"{key}: {value}" for key, value in fields], "---"]
    text = "\n".join(lines)
    if body:
        text = f"{text}\n{body}"
    else:
        text = f"{text}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_job(queue_dir: Path, filename: str, payload: dict) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_path = queue_dir / filename
    job_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return job_path


def add_person_job_payload(job_id: str = "job-add-person", idempotency_key: Optional[str] = None) -> dict:
    payload = {
        "job_id": job_id,
        "job_type": "add_person",
        "payload": {
            "name": "Eric Daniel Dalton",
            "employee_id": "567",
            "role": "Cleaner",
            "employment_type": "part_time",
            "status": "active",
            "additional_jobs": ["7071"],
            "assignments": [
                {
                    "job": "7060",
                    "account": "Contworks",
                    "location": "Continental Metalworks Holdings",
                    "shift": "evening",
                }
            ],
            "contact": {
                "phone": None,
                "email": None,
            },
            "metadata": {
                "source": "manager_journal",
            },
        },
    }
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key
    return payload


def build_context(
    project_root: Path,
    vault_root: Path,
    runtime_root: Path,
    log_path: Path,
    dry_run: bool,
    personal_vault_root: Optional[Path] = None,
) -> qp.RunContext:
    context = qp.RunContext(
        project_root=project_root,
        runtime_root=runtime_root,
        log_path=log_path,
        dry_run=dry_run,
    )
    # vault_root was removed from the production RunContext; tests still pass a
    # throwaway temp dir for seeding legacy projection fixtures.
    object.__setattr__(context, "vault_root", vault_root)
    object.__setattr__(context, "personal_vault_root", vault_root if personal_vault_root is None else personal_vault_root)
    return context


def run_jobs(
    project_root: Path,
    vault_root: Path,
    runtime_root: Path,
    log_path: Path,
    dry_run: bool = False,
    personal_vault_root: Optional[Path] = None,
) -> tuple[str, str]:
    queue_dir = runtime_root / "queue"
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    queue_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run, personal_vault_root)
    if not dry_run:
        seed_projection_docs_as_canonical(vault_root)

    stdout_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer):
        for job_path in sorted(path for path in queue_dir.iterdir() if path.is_file()):
            qp.process_job(job_path, context, processed_dir, failed_dir)
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return stdout_buffer.getvalue(), log_text


def make_roots(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project_root = (tmp_path / "project").resolve()
    vault_root = (tmp_path / "vault").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    log_path = (runtime_root / "logs" / "run.log").resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    (vault_root / "Accounts").mkdir(parents=True, exist_ok=True)
    (vault_root / "People").mkdir(parents=True, exist_ok=True)
    (vault_root / "Journal").mkdir(parents=True, exist_ok=True)
    return project_root, vault_root, runtime_root, log_path


def test_voice_memo_read_helper_logs_then_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    _project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    context = build_context(tmp_path / "project", vault_root, runtime_root, log_path, dry_run=True)

    def fail_resolve_employee_target(_store: object, _employee: str) -> None:
        raise RuntimeError("employee target unavailable")

    monkeypatch.setattr(misc, "resolve_employee_target", fail_resolve_employee_target)

    assert misc.voice_memo_person_link(context, {"slug": "keller-bruce", "name": "Bruce Keller"}) == "Bruce Keller"
    assert "voice memo person link fallback slug=keller-bruce name=Bruce Keller" in caplog.text


@pytest.fixture
def legacy_markdown_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_VAULT_MARKDOWN_WRITE", "1")


@pytest.fixture(autouse=True)
def reset_recording_vault_store() -> None:
    shared._VAULT_STORE = None
    shared._PERSONAL_JOURNAL_STORE = None
    yield
    shared._VAULT_STORE = None
    shared._PERSONAL_JOURNAL_STORE = None


class RmwRecordingVaultStore(RecordingVaultStore):
    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        return None

    def find_unknown_capture_docs(self, status: str | None = "unresolved", *, limit: int = 10000) -> list[dict[str, Any]]:
        docs = [
            dict(doc)
            for doc in self.docs
            if doc.get("type") == "unknown_capture" and (status is None or doc.get("status") == status)
        ]
        return docs[:limit]

    def find_location_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        locations = [dict(doc) for doc in self.docs if doc.get("type") == "location"]
        return locations[:limit]

    def find_open_site_issue_docs(self, site_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        return super().find_open_site_issue_docs(site_id, limit=limit)

    def job_id_applied_doc_id(self, job_id: str) -> str | None:
        for doc in self.docs:
            job_ids = doc.get("btq_job_ids")
            if isinstance(job_ids, list) and job_id in [str(item) for item in job_ids]:
                doc_id = doc.get("_id")
                return str(doc_id) if doc_id else None
        return None

    def scan_job_id_docs(self, *, limit: int = 100000) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for doc in self.docs:
            job_ids = doc.get("btq_job_ids")
            if not isinstance(job_ids, list) or not job_ids:
                continue
            results.append(
                {
                    "_id": doc.get("_id"),
                    "type": doc.get("type"),
                    "btq_job_ids": list(job_ids),
                    "content": doc.get("content", ""),
                }
            )
            if len(results) >= limit:
                break
        return results

    def update_doc(
        self,
        doc_id: str,
        transform: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        *,
        create: Callable[[], dict[str, Any]] | None = None,
        require_existing: bool = True,
        max_conflict_retries: int = 1,
    ) -> dict[str, Any]:
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


def use_recording_vault_store(monkeypatch: pytest.MonkeyPatch) -> RmwRecordingVaultStore:
    store = RmwRecordingVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    return store


def use_recording_personal_journal_store(monkeypatch: pytest.MonkeyPatch) -> RmwRecordingVaultStore:
    store = RmwRecordingVaultStore()
    store.database = couchdb_config.DEFAULT_PERSONAL_JOURNAL_DB
    monkeypatch.setattr(shared, "_PERSONAL_JOURNAL_STORE", store)
    return store


def test_recording_vault_store_job_id_applied_doc_id_hit_and_miss() -> None:
    store = RmwRecordingVaultStore()
    store.docs.extend(
        [
            {"_id": "note_without_jobs", "type": "note"},
            {"_id": "note_with_jobs", "type": "note", "btq_job_ids": ["job-hit"]},
        ]
    )

    assert store.job_id_applied_doc_id("job-hit") == "note_with_jobs"
    assert store.job_id_applied_doc_id("job-miss") is None


def test_recording_vault_store_scan_job_id_docs_returns_non_empty_job_id_docs() -> None:
    store = RmwRecordingVaultStore()
    store.docs.extend(
        [
            {"_id": "note_without_jobs", "type": "note", "content": "ignored"},
            {"_id": "note_empty_jobs", "type": "note", "btq_job_ids": [], "content": "ignored"},
            {"_id": "note_with_jobs", "type": "note", "btq_job_ids": ["job-hit"], "content": "visible"},
        ]
    )

    assert store.scan_job_id_docs() == [
        {"_id": "note_with_jobs", "type": "note", "btq_job_ids": ["job-hit"], "content": "visible"}
    ]


def test_recording_vault_store_find_open_site_issue_docs_filters_site_and_status() -> None:
    store = RmwRecordingVaultStore()
    store.docs.extend(
        [
            {"_id": "site_issue_open", "type": "site_issue", "issue_id": "open", "site_id": "7050", "status": "open", "title": "Drain"},
            {"_id": "site_issue_quoted", "type": "site_issue", "issue_id": "quoted", "site_id": '"7050"', "status": "open", "title": "Sink"},
            {"_id": "site_issue_closed", "type": "site_issue", "issue_id": "closed", "site_id": "7050", "status": "resolved", "title": "Drain"},
            {"_id": "site_issue_other", "type": "site_issue", "issue_id": "other", "site_id": "7060", "status": "open", "title": "Drain"},
        ]
    )

    assert store.find_open_site_issue_docs("7050") == [
        {"_id": "site_issue_open", "issue_id": "open", "title": "Drain", "status": "open"},
        {"_id": "site_issue_quoted", "issue_id": "quoted", "title": "Sink", "status": "open"},
    ]


def test_couchdb_entity_store_job_id_applied_doc_id_hit_and_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CouchDBEntityStore("http://couchdb.invalid", {}, "btq_vault")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request_json(method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, doc_id, payload))
        if payload and payload["selector"]["btq_job_ids"]["$elemMatch"]["$eq"] == "job-hit":
            return {"docs": [{"_id": "note_with_jobs"}]}
        return {"docs": []}

    monkeypatch.setattr(store, "_request_json", fake_request_json)

    assert store.job_id_applied_doc_id("job-hit") == "note_with_jobs"
    assert store.job_id_applied_doc_id("job-miss") is None
    assert calls == [
        (
            "POST",
            "_find",
            {
                "selector": {"btq_job_ids": {"$elemMatch": {"$eq": "job-hit"}}},
                "fields": ["_id"],
                "limit": 1,
            },
        ),
        (
            "POST",
            "_find",
            {
                "selector": {"btq_job_ids": {"$elemMatch": {"$eq": "job-miss"}}},
                "fields": ["_id"],
                "limit": 1,
            },
        ),
    ]


def test_couchdb_entity_store_scan_job_id_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CouchDBEntityStore("http://couchdb.invalid", {}, "btq_vault")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request_json(method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, doc_id, payload))
        return {
            "docs": [
                {"_id": "note_with_jobs", "type": "note", "btq_job_ids": ["job-hit"], "content": "visible"},
                "ignored",
            ]
        }

    monkeypatch.setattr(store, "_request_json", fake_request_json)

    assert store.scan_job_id_docs() == [
        {"_id": "note_with_jobs", "type": "note", "btq_job_ids": ["job-hit"], "content": "visible"}
    ]
    assert calls == [
        (
            "POST",
            "_find",
            {
                "selector": {"btq_job_ids": {"$exists": True}},
                "fields": ["_id", "type", "btq_job_ids", "content"],
                "limit": 100000,
            },
        )
    ]


def test_couchdb_entity_store_find_open_site_issue_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CouchDBEntityStore("http://couchdb.invalid", {}, "btq_vault")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_request_json(method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        calls.append((method, doc_id, payload))
        return {
            "docs": [
                {"_id": "site_issue_iss_one", "issue_id": "iss_one", "title": "Drain", "status": "open"},
                "ignored",
            ]
        }

    monkeypatch.setattr(store, "_request_json", fake_request_json)

    assert store.find_open_site_issue_docs("7050") == [
        {"_id": "site_issue_iss_one", "issue_id": "iss_one", "title": "Drain", "status": "open"}
    ]
    assert calls == [
        (
            "POST",
            "_find",
            {
                "selector": {
                    "type": "site_issue",
                    "status": "open",
                    "$or": [
                        {"site_id": "7050"},
                        {"site_id": '"7050"'},
                    ],
                },
                "fields": ["_id", "issue_id", "title", "status"],
                "limit": 500,
            },
        )
    ]


def seed_projection_docs_as_canonical(vault_root: Path, store: RecordingVaultStore | None = None) -> RecordingVaultStore:
    if store is None:
        existing_store = shared._VAULT_STORE
        existing_vault_root = getattr(existing_store, "_seed_vault_root", None)
        if isinstance(existing_store, RecordingVaultStore) and existing_vault_root in {None, str(vault_root)}:
            store = existing_store
        else:
            store = RmwRecordingVaultStore()
            shared._VAULT_STORE = store
    setattr(store, "_seed_vault_root", str(vault_root))

    def canonical_metadata(markdown_text: str) -> dict[str, Any]:
        try:
            fields, _body = shared.parse_frontmatter(markdown_text)
        except Exception:
            return {}
        metadata: dict[str, Any] = {}
        for key in (
            "account",
            "employee_id",
            "first",
            "job",
            "last",
            "location",
            "name",
            "person_id",
            "site_id",
            "status",
        ):
            value = shared.get_frontmatter_value(fields, key)
            if value is not None and str(value).strip():
                metadata[key] = value
        return metadata

    def upsert_doc(doc_id: str, doc_type: str, markdown_text: str) -> None:
        content = shared.canonical_content_body(markdown_text)
        metadata = canonical_metadata(markdown_text)
        metadata.setdefault("vault_path", str(path_for_doc_id(doc_id).resolve()))
        for doc in store.docs:
            if doc.get("_id") == doc_id:
                doc.setdefault("type", doc_type)
                doc.setdefault("content", content)
                for key, value in metadata.items():
                    doc.setdefault(key, value)
                return
        store.docs.append({"_id": doc_id, "type": doc_type, "content": content, **metadata})

    def path_for_doc_id(doc_id: str) -> Path:
        if doc_id.startswith("location_"):
            for site_path in sorted((vault_root / "Accounts").glob("*/Locations/*/about.md")):
                try:
                    text = site_path.read_text(encoding="utf-8")
                    if shared.canonical_location_doc_id_for_projection(site_path, text) == doc_id:
                        return site_path
                except Exception:
                    continue
        if doc_id.startswith("employee_"):
            for employee_path in sorted((vault_root / "People").glob("*.md")):
                try:
                    text = employee_path.read_text(encoding="utf-8")
                    if shared.canonical_employee_doc_id_for_projection(employee_path, text) == doc_id:
                        return employee_path
                except Exception:
                    continue
        return vault_root

    for site_path in sorted((vault_root / "Accounts").glob("*/Locations/*/about.md")):
        try:
            text = site_path.read_text(encoding="utf-8")
            upsert_doc(shared.canonical_location_doc_id_for_projection(site_path, text), "location", text)
        except Exception:
            continue
    for employee_path in sorted((vault_root / "People").glob("*.md")):
        try:
            text = employee_path.read_text(encoding="utf-8")
            upsert_doc(shared.canonical_employee_doc_id_for_projection(employee_path, text), "employee", text)
        except Exception:
            continue
    return store


def write_unknown_capture(
    path: Path,
    timestamp: str,
    audio_file: str,
    normalized_text: str,
    notes: str = "#unknown #needs-review",
) -> None:
    content = (
        "---\n"
        "type: unknown_capture\n"
        f"timestamp: {timestamp}\n"
        f"audio_file: {audio_file}\n"
        "status: unresolved\n"
        "retry_count: 0\n"
        "last_attempted: null\n"
        "---\n\n"
        "## Original Transcript\n"
        "Original transcript text.\n\n"
        "## Normalized Transcript\n"
        f"{normalized_text}\n\n"
        "## Notes\n"
        f"{notes}\n"
    )
    path.write_text(content, encoding="utf-8")


def seed_unknown_capture_doc(
    store: RecordingVaultStore,
    *,
    journal_path: str,
    timestamp: str,
    audio_file: str,
    normalized_text: str,
    notes: str = "#unknown #needs-review",
    status: str = "unresolved",
    retry_count: int = 0,
    last_attempted: str | None = None,
) -> str:
    from queue_processor.handlers.unknowns import derive_source_unknown_id

    sid = derive_source_unknown_id(Path(journal_path), timestamp, audio_file)
    store.docs.append(
        {
            "_id": f"unknown_capture_{sid}",
            "type": "unknown_capture",
            "operator": "op_greg",
            "source_unknown_id": sid,
            "timestamp": timestamp,
            "audio_file": audio_file,
            "status": status,
            "retry_count": retry_count,
            "last_attempted": last_attempted,
            "original_transcript": "Original transcript text.",
            "normalized_transcript": normalized_text,
            "notes": notes,
            "btq_job_ids": [],
        }
    )
    return f"unknown_capture_{sid}"


def assert_frontmatter_job_id(path: Path, job_payload: dict) -> None:
    text = path.read_text(encoding="utf-8")
    expected_job_id = qp.compute_job_id(job_payload)
    assert "btq_job_ids:" in text
    assert f"  - {expected_job_id}" in text


def frontmatter_value(path: Path, key: str) -> str:
    fields, _body = qp.parse_frontmatter(path.read_text(encoding="utf-8"))
    value = qp.get_frontmatter_value(fields, key)
    assert value is not None
    return value


def recording_doc(store: RecordingVaultStore, doc_id: str) -> dict:
    matches = [doc for doc in store.docs if doc.get("_id") == doc_id]
    assert len(matches) == 1
    return matches[0]


def seed_canonical_visit(store: RecordingVaultStore, *, site_id: str = "7030", date_value: str = "2026-04-19") -> None:
    store.upsert(
        {
            "_id": f"visit_{site_id}_{date_value}",
            "type": "visit",
            "site_id": site_id,
            "date": date_value,
            "evidence": "Seeded canonical visit.",
            "btq_job_ids": ["job-seeded-visit"],
        }
    )


def write_apex_site(vault_root: Path) -> Path:
    site_path = vault_root / "Accounts" / "Apexco" / "Locations" / "7080 - Apex Powdered Metals" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("account", "Apexco"),
            ("location", "Apex Powdered Metals"),
            ("job", "7080"),
            ("address", "700 Martha St, Springfield, PA 00000"),
            ("site_aliases", "[Apex Powdered Metals, Apex]"),
            ("monthly_supply_budget", "250.0"),
            ("budget_basis", "monthly_actual"),
            ("type", "location"),
        ],
        body="# Apex Powdered Metals\n",
    )
    return site_path


def write_supply_email(project_root: Path, html: str = SUPPLY_EMAIL_HTML, filename: str = "staples-order-99887766.html") -> Path:
    email_path = project_root.parent / "emails" / filename
    email_path.parent.mkdir(parents=True, exist_ok=True)
    email_path.write_text(html, encoding="utf-8")
    return email_path


def parse_supply_email_payload(filename: str = "staples-order-99887766.html", subject: str = "Staples order confirmation 99887766") -> dict:
    return {
        "job_type": "parse_supply_email",
        "payload": {
            "html_path": f"emails/{filename}",
            "subject": subject,
            "source_email_date": "2026-04-20T08:15:00+00:00",
        },
    }


def make_processor_dirs(runtime_root: Path, log_path: Path) -> tuple[Path, Path]:
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return processed_dir, failed_dir


def test_replace_or_insert_subsection_inserts_when_child_absent() -> None:
    existing = "# Site\n\n## Operational Notes\n\n### Entry / Access\n\nUse front door.\n\n## Field Capture Reviews\n\n"

    updated = qp.replace_or_insert_subsection(
        existing,
        "## Operational Notes",
        "### Supplies / Equipment",
        "Inventory body.\n",
    )

    assert "### Entry / Access\n\nUse front door.\n\n### Supplies / Equipment\nInventory body.\n## Field Capture Reviews" in updated


def test_replace_or_insert_subsection_replaces_body_when_child_present() -> None:
    existing = "# Site\n\n## Operational Notes\n\n### Supplies / Equipment\nOld body.\n\n### Zones / Sequence\nZone notes.\n"

    updated = qp.replace_or_insert_subsection(
        existing,
        "## Operational Notes",
        "### Supplies / Equipment",
        "New body.\n",
    )

    assert "### Supplies / Equipment\nNew body.\n### Zones / Sequence" in updated
    assert "Old body" not in updated
    assert "Zone notes." in updated


def test_replace_or_insert_subsection_does_not_cross_next_parent() -> None:
    existing = "# Site\n\n## Operational Notes\n\n### Supplies / Equipment\nOld body.\n\n## Field Capture Reviews\n\n### Supplies / Equipment\nReview body.\n"

    updated = qp.replace_or_insert_subsection(
        existing,
        "## Operational Notes",
        "### Supplies / Equipment",
        "New body.\n",
    )

    assert "## Operational Notes\n\n### Supplies / Equipment\nNew body.\n## Field Capture Reviews" in updated
    assert "## Field Capture Reviews\n\n### Supplies / Equipment\nReview body." in updated


def test_replace_or_insert_subsection_raises_when_parent_absent() -> None:
    with pytest.raises(qp.QueueProcessorError, match="parent heading not found: ## Operational Notes"):
        qp.replace_or_insert_subsection("# Site\n", "## Operational Notes", "### Supplies / Equipment", "Body.\n")


def write_summit_wire_site(vault_root: Path) -> Path:
    site_path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("type", "location"),
            ("site_id", "7050"),
            ("job", "7050"),
            ("location", "Summit Wire"),
            ("account", "Summitsteel"),
        ],
        body="# Summit Wire\n",
    )
    return site_path


def write_continental_site(vault_root: Path, body: Optional[str] = None) -> Path:
    site_path = vault_root / "Accounts" / "Contworks" / "Locations" / "7060 - Continental Metalworks" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("type", "location"),
            ("site_id", "7060"),
            ("job", "7060"),
            ("location", "Continental Metalworks"),
            ("account", "Contworks"),
        ],
        body=body
        if body is not None
        else "# Continental Metalworks\n\n## Operational Notes\n\n### Entry / Access\n\nUse main entry.\n\n### Parking / Loading\n\nUse dock.\n\n## Field Capture Reviews\n\n",
    )
    return site_path


def test_target_path_hint_returns_canonical_identifiers(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    context = build_context(project_root, vault_root, runtime_root, log_path, False)

    assert qp.target_path_hint(qp.QueueJob("j1", "append_to_note", {"path": "Journal/2026-04-19.md"}, {}, {}), context) == "note_journal_2026-04-19"
    assert qp.target_path_hint(qp.QueueJob("j2", "append_to_note", {"path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"}, {}, {}), context) == "location_7050"
    assert qp.target_path_hint(qp.QueueJob("j3", "log_supply_need", {"site_id": "7050"}, {}, {}), context) == "location_7050"
    assert qp.target_path_hint(qp.QueueJob("j4", "remove_from_schedule", {"employee": "Pearson, David"}, {}, {}), context) == "employee_pearson_david"
    assert qp.target_path_hint(qp.QueueJob("j5", "personal_journal_entry", {"date": "2026-04-19"}, {}, {}), context) == "journal_personal_2026-04-19"
    assert qp.target_path_hint(qp.QueueJob("j6", "photo_capture", {"captured_at": "2026-04-20T10:00:00Z"}, {}, {}), context) == "journal_operational_2026-04-20"
    assert qp.target_path_hint(qp.QueueJob("j7", "reclassify_unknown", {"path": "Journal/2026-04-19-unknown.md"}, {}, {}), context) == "Journal/2026-04-19-unknown.md"


def log_site_issue_job_payload(job_id: str = "job-log-site-issue") -> dict:
    return {
        "job_id": job_id,
        "job_type": "log_site_issue",
        "payload": {
            "site_id": "7050",
            "title": "Restroom drain backup and inoperable stall",
            "summary": "Drain backed up and the sink drain pushed water onto the restroom floor.",
            "observations": [
                "Drain backed up in the restroom.",
                "Sink drain backed up onto the floor.",
                "Metal stall is inoperable.",
                "Missing mop limits immediate cleanup.",
            ],
            "category": "maintenance",
            "priority": "high",
            "status": "open",
            "observed_at": "2026-05-06T18:27:03-04:00",
            "reported_by": "Tom Walsh",
            "source": "field_capture",
            "client_notified": True,
            "client_notified_at": "2026-05-08T15:10:00-04:00",
            "client_notified_by": "Jordan",
            "client_notified_method": "email",
            "client_notified_note": "Emailed client with photo/context.",
            "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
            "related_capture_ids": ["cap-photo-summit-drain"],
            "related_candidate_ids": ["ac_386bdf44bf4f08764e5a7bb7"],
            "related_media": ["/media/cap-photo-summit-drain/drain.jpg"],
            "source_artifacts": ["field_capture_review_export"],
        },
    }


def log_supply_need_job_payload(job_id: str = "job-log-supply-need") -> dict:
    return {
        "job_id": job_id,
        "job_type": "log_supply_need",
        "payload": {
            "site_id": "7050",
            "item_name": "BrightWash cleaner",
            "quantity_needed": "2 bottles",
            "urgency": "high",
            "requested_by": "Tom Walsh",
            "observed_at": "2026-05-08T14:12:43+00:00",
            "source": "field_capture",
            "notes": "Supply closet is empty.",
            "related_capture_ids": ["cap-supply-summit"],
            "related_candidate_ids": ["ac_supply_summit"],
            "related_media": ["/media/cap-supply-summit/shelf.jpg"],
            "source_artifacts": ["field_capture_supply_review"],
        },
    }


def log_equipment_request_job_payload(job_id: str = "job-log-equipment-request") -> dict:
    return {
        "job_id": job_id,
        "job_type": "log_equipment_request",
        "payload": {
            "site_id": "7050",
            "equipment_name": "vacuum",
            "reason": "Current vacuum will not start.",
            "priority": "urgent",
            "requested_by": "Tom Walsh",
            "observed_at": "2026-05-08T14:12:43+00:00",
            "source": "field_capture",
            "notes": "Needed for lobby carpet.",
            "related_capture_ids": ["cap-equipment-summit"],
            "related_candidate_ids": ["ac_equipment_summit"],
            "related_media": ["/media/cap-equipment-summit/vacuum.jpg"],
            "source_artifacts": ["field_capture_equipment_review"],
        },
    }


def log_personnel_event_job_payload(job_id: str = "job-log-personnel-event") -> dict:
    return {
        "job_id": job_id,
        "job_type": "log_personnel_event",
        "payload": {
            "employee": "Tate, Marcus",
            "event_type": "attendance",
            "severity": "concern",
            "status": "open",
            "summary": "No call no show for Summit Wire opening shift.",
            "occurred_at": "2026-05-18T05:30:00-04:00",
            "reported_by": "Jordan",
            "related_site": "7050",
            "source": "field_capture",
            "notes": "First attendance event.",
            "client_notified": False,
            "resolution_trigger": "Two consecutive covered shifts without recurrence.",
        },
    }


def update_site_equipment_job_payload(job_id: str = "job-update-site-equipment") -> dict:
    return {
        "job_id": job_id,
        "job_type": "update_site_equipment",
        "payload": {
            "site_id": "7060",
            "equipment": [
                {
                    "description": "Large walk-behind scrubber",
                    "brand": "Viper",
                    "color": "Red",
                    "status": "operational",
                    "notes": "Used 1x/week",
                },
                {
                    "description": "Small walk-behind scrubber",
                    "brand": "(unknown)",
                    "color": "Blue",
                    "status": "non_functional",
                    "notes": "Leaks; replacement vs. repair pending",
                },
            ],
            "inspection_date": "2026-05-13",
            "inspected_by": "Jordan",
        },
    }


def mark_supply_job_payload(
    job_type: str = "mark_supply_ordered",
    job_id: str = "job-mark-supply-ordered",
    supply_id: str = "sup_summit_brightwash",
) -> dict:
    return {
        "job_id": job_id,
        "job_type": job_type,
        "payload": {
            "supply_id": supply_id,
            "actor": "Jordan",
            "occurred_at": "2026-05-08T18:00:00+00:00",
        },
    }


def mark_equipment_job_payload(
    job_type: str = "mark_equipment_approved",
    job_id: str = "job-mark-equipment-approved",
    equipment_id: str = "eqr_summit_vacuum",
) -> dict:
    return {
        "job_id": job_id,
        "job_type": job_type,
        "payload": {
            "equipment_id": equipment_id,
            "actor": "Jordan",
            "occurred_at": "2026-05-08T18:00:00+00:00",
        },
    }


def test_append_to_note_writes_canonical_note_doc(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-00-00Z__job-append.json",
        {
            "job_id": "job-append",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-19.md",
                "content": "First queue note.",
                "destination": "journal",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "updated" in stdout
    assert "action=append-to-note status=success" in log_text
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    note_doc = recording_doc(store, "note_journal_2026-04-19")
    assert "First queue note." in note_doc["content"]
    assert note_doc["type"] == "note"
    assert (runtime_root / "processed" / "2026-04-19T23-00-00Z__job-append.json").exists()


def test_append_to_site_note_patches_canonical_content_before_projection_write(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_summit_wire_site(vault_root)
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-02-00Z__field-append.json",
        {
            "job_id": "job-field-append",
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
                "content": "Reviewed staff request note.",
                "destination": "site_note",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "location_7050")["content"]
    assert "Reviewed staff request note." in canonical_content
    assert "Reviewed staff request note." not in site_path.read_text(encoding="utf-8")


def test_append_to_legacy_site_note_without_type_still_patches_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("site_id", "7050"),
            ("job", "7050"),
            ("location", "Summit Wire"),
            ("account", "Summitsteel"),
        ],
        body="# Summit Wire\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-02-00Z__field-append.json",
        {
            "job_id": "job-field-append",
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
                "content": "Reviewed staff request note.",
                "destination": "site_note",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "location_7050")["content"]
    assert "Reviewed staff request note." in canonical_content
    assert "Reviewed staff request note." not in site_path.read_text(encoding="utf-8")


def test_append_to_note_site_writes_canonical_location_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_summit_wire_site(vault_root)
    seed_projection_docs_as_canonical(vault_root, store)
    payload = {
        "job_id": "job-site-canonical-append",
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
            "content": "Canonical site append_to_note content.",
            "destination": "site_note",
        },
    }
    expected_job_id = qp.compute_job_id(payload)
    event_date = datetime.utcnow().date().isoformat()
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    processed_dir, _failed_dir = make_processor_dirs(runtime_root, log_path)
    first_job_path = write_job(runtime_root / "queue", "site-canonical-first.json", payload)

    qp.process_append_to_note_job(first_job_path, qp.load_job(first_job_path), context, processed_dir)

    location_doc = recording_doc(store, "location_7050")
    gap_doc = recording_doc(store, f"visit_gap_7050_{event_date}")
    assert "Canonical site append_to_note content." in location_doc["content"]
    assert location_doc["btq_job_ids"] == [expected_job_id]
    assert gap_doc["operator"] == OPERATOR_ID_GREG
    assert gap_doc["reason"] == "event_without_visit"
    assert gap_doc["btq_job_ids"] == [expected_job_id]
    assert "Canonical site append_to_note content." not in site_path.read_text(encoding="utf-8")

    replay_job_path = write_job(runtime_root / "queue", "site-canonical-replay.json", payload)
    stdout_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer):
        qp.process_append_to_note_job(replay_job_path, qp.load_job(replay_job_path), context, processed_dir)

    assert "job_id marker already present" in stdout_buffer.getvalue()
    assert recording_doc(store, "location_7050")["content"].count("Canonical site append_to_note content.") == 1
    assert recording_doc(store, f"visit_gap_7050_{event_date}")["btq_job_ids"] == [expected_job_id]
    assert site_path.read_text(encoding="utf-8").count("Canonical site append_to_note content.") == 0


def test_append_to_note_journal_writes_canonical_note_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    processed_dir, _failed_dir = make_processor_dirs(runtime_root, log_path)
    first_payload = {
        "job_id": "job-journal-canonical-first",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-19.md",
            "content": "First canonical journal note.",
            "destination": "journal",
        },
    }
    second_payload = {
        "job_id": "job-journal-canonical-second",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-19.md",
            "content": "Second canonical journal note.",
            "destination": "journal",
        },
    }
    first_job_id = qp.compute_job_id(first_payload)
    second_job_id = qp.compute_job_id(second_payload)
    first_job_path = write_job(runtime_root / "queue", "journal-canonical-first.json", first_payload)
    second_job_path = write_job(runtime_root / "queue", "journal-canonical-second.json", second_payload)

    qp.process_append_to_note_job(first_job_path, qp.load_job(first_job_path), context, processed_dir)
    qp.process_append_to_note_job(second_job_path, qp.load_job(second_job_path), context, processed_dir)

    note_doc = recording_doc(store, "note_journal_2026-04-19")
    assert note_doc["type"] == "note"
    assert note_doc["operator"] == OPERATOR_ID_GREG
    assert note_doc["date"] == "2026-04-19"
    assert note_doc["content"].index("First canonical journal note.") < note_doc["content"].index("Second canonical journal note.")
    assert note_doc["btq_job_ids"] == [first_job_id, second_job_id]
    assert not (vault_root / "Journal" / "2026-04-19.md").exists()

    replay_job_path = write_job(runtime_root / "queue", "journal-canonical-replay.json", first_payload)
    stdout_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer):
        qp.process_append_to_note_job(replay_job_path, qp.load_job(replay_job_path), context, processed_dir)

    assert "job_id marker already present" in stdout_buffer.getvalue()
    assert recording_doc(store, "note_journal_2026-04-19")["content"].count("First canonical journal note.") == 1
    assert not (vault_root / "Journal" / "2026-04-19.md").exists()


def test_canonical_location_resolution_failure_is_wrapped_with_job_diagnostics() -> None:
    job = qp.QueueJob(job_id="job-field-append", job_type="append_to_note", payload={}, metadata={}, intent={})

    with pytest.raises(qp.QueueJobError) as exc_info:
        shared.patch_canonical_location_content(Path("about.md"), "# Site\n", job)

    message = str(exc_info.value)
    assert "canonical couchdb content patch failed" in message
    assert "job_type=append_to_note" in message
    assert "job_id=job-field-append" in message
    assert "entity_id=unknown" in message


def test_append_to_note_store_error_fails_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingAppendToNoteStore(RmwRecordingVaultStore):
        def get_optional(self, doc_id: str) -> dict[str, Any] | None:
            raise RuntimeError(f"boom for {doc_id}")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_summit_wire_site(vault_root)
    original_text = site_path.read_text(encoding="utf-8")
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingAppendToNoteStore())
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-02-00Z__field-append.json",
        {
            "job_id": "job-field-append",
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
                "content": "Reviewed staff request note.",
                "destination": "site_note",
            },
        },
    )

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert site_path.read_text(encoding="utf-8") == original_text
    assert not (runtime_root / "processed" / "2026-05-08T16-02-00Z__field-append.json").exists()
    assert (runtime_root / "failed" / "2026-05-08T16-02-00Z__field-append.json").exists()


def test_append_to_note_strips_vault_name_prefix_at_load(tmp_path: Path) -> None:
    """External producers (cowork, remote semantic prompts) sometimes emit
    payload paths with a leading "<vault_name>/" prefix. The queue boundary
    must normalize so the path doesn't double up when joined with the vault
    root. Regression for 2026-05-19 continental-imop-clarification failure.
    """
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    target_path = vault_root / "Accounts" / "Contworks" / "Locations" / "7060" / "about.md"
    write_frontmatter_file(
        target_path,
        [
            ("type", "location"),
            ("site_id", "7060"),
            ("job", "7060"),
            ("location", "Continental Metalworks"),
            ("account", "Contworks"),
        ],
        body="# 1200\n",
    )
    # Path uses the live vault name as a prefix — the very shape the
    # offending remote producer emitted.
    from config import get_config

    vault_name = get_config().vault_dir.name
    prefixed_path = f"{vault_name}/Accounts/Contworks/Locations/7060/about.md"
    write_job(
        runtime_root / "queue",
        "2026-05-19T14-45-00Z__continental-prefix.json",
        {
            "job_id": "continental-prefix",
            "job_type": "append_to_note",
            "payload": {
                "path": prefixed_path,
                "content": "Vault-prefix path should be normalized.",
                "destination": "site_note",
            },
        },
    )

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "action=append-to-note status=success" in log_text
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "Vault-prefix path should be normalized." in recording_doc(store, "location_7060")["content"]
    assert "Vault-prefix path should be normalized." not in target_path.read_text(encoding="utf-8")
    # Confirm no doubled-prefix directory was created next to the vault.
    assert not (vault_root / vault_name).exists()


def test_process_all_skip_unknowns_processes_durable_queue_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    unknown_path = vault_root / "Journal" / "2026-05-08-unknown.md"
    write_unknown_capture(
        unknown_path,
        "2026-05-08T10:00:00+00:00",
        "unknown.m4a",
        "Unknown transcript.",
        notes="#site: Summit Wire",
    )
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-00-00Z__append.json",
        {
            "job_id": "job-durable-only",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-05-08.md",
                "content": "Process only the durable queue.",
                "destination": "journal",
            },
        },
    )

    report = qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )

    assert report["discovered"] == 1
    assert report["processed"] == 1
    assert report["failed"] == 0
    assert report["queue_before"] == 1
    assert report["queue_after"] == 0
    assert report["unknown_reclassification_skipped"] is True
    assert report["unknown_reclassification_jobs_created"] == 0
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "Process only the durable queue." in recording_doc(store, "note_journal_2026-05-08")["content"]
    assert not list((runtime_root / "queue").glob("*.json"))
    assert (runtime_root / "processed" / "2026-05-08T16-00-00Z__append.json").exists()
    assert not list((runtime_root / "failed").glob("*.json"))
    assert [doc for doc in store.docs if doc.get("type") == "unknown_capture"] == []
    assert not (runtime_root / "unknown_reclassification").exists()


def test_process_all_loads_processed_index_once_for_multiple_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    index_path = runtime_root / "processed_index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "computed_job_id": "unrelated",
                "job_type": "append_to_note",
                "target_path": "unknown",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "handler_version": "test",
                "source_queue_file": "unrelated.json",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_job(
        runtime_root / "queue",
        "first.json",
        {
            "job_id": "first",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-06-02.md",
                "content": "First cached-index pass note.",
                "destination": "journal",
            },
        },
    )
    write_job(
        runtime_root / "queue",
        "second.json",
        {
            "job_id": "second",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-06-02.md",
                "content": "Second cached-index pass note.",
                "destination": "journal",
            },
        },
    )
    original_iter_records = processed_index.iter_records
    calls = 0

    def counting_iter_records(path: Path) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return original_iter_records(path)

    monkeypatch.setattr(processed_index, "iter_records", counting_iter_records)

    report = qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )

    assert report["processed"] == 2
    assert calls == 1


def test_existing_processed_destination_with_indexed_job_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    payload = {
        "job_id": "indexed-replay",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-06-02.md",
            "content": "Already indexed replay note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "indexed-replay.json", payload)
    qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )
    write_job(runtime_root / "queue", "indexed-replay.json", payload)

    report = qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )

    assert report["failed"] == 0
    assert report["queue_after"] == 0
    assert (runtime_root / "processed" / "indexed-replay.json").exists()
    assert not (runtime_root / "failed" / "indexed-replay.json").exists()
    events_text = (runtime_root / "logs" / "queue_processor_events.jsonl").read_text(encoding="utf-8")
    assert "queue_archive_collision_already_handled" in events_text
    assert "indexed-replay.json" in events_text


def test_existing_processed_destination_with_applied_job_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    payload = {
        "job_id": "applied-replay",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-06-02.md",
            "content": "Already applied replay note.",
            "destination": "journal",
        },
    }
    computed_job_id = qp.compute_job_id(payload)
    store.docs.append({"_id": "note_journal_2026-06-02", "type": "note", "btq_job_ids": [computed_job_id]})
    write_job(runtime_root / "queue", "applied-replay.json", payload)
    processed_path = runtime_root / "processed" / "applied-replay.json"
    processed_path.parent.mkdir(parents=True, exist_ok=True)
    processed_path.write_text(
        json.dumps(
            {
                "job_type": "append_to_note",
                "payload": {
                    "path": "Journal/unrelated.md",
                    "content": "Unrelated archived payload.",
                    "destination": "journal",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )

    assert report["failed"] == 0
    assert report["queue_after"] == 0
    assert processed_path.exists()
    assert not (runtime_root / "failed" / "applied-replay.json").exists()
    events_text = (runtime_root / "logs" / "queue_processor_events.jsonl").read_text(encoding="utf-8")
    assert "queue_archive_collision_already_handled" in events_text
    assert "applied-marker" in events_text
    assert "job-id-marker-present" in events_text


def test_ambiguous_failed_destination_collision_stays_retryable_and_logs(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    queue_path = runtime_root / "queue" / "bad.json"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text("{bad-json\n", encoding="utf-8")
    failed_path = runtime_root / "failed" / "bad.json"
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    failed_path.write_text('{"existing":"failure"}\n', encoding="utf-8")

    report = qp.process_all(
        project_root=project_root,
        runtime_root=runtime_root,
        dry_run=False,
        skip_unknowns=True,
    )

    assert report["failed"] == 0
    assert report["queue_after"] == 1
    assert queue_path.exists()
    assert failed_path.read_text(encoding="utf-8") == '{"existing":"failure"}\n'
    log_text = (runtime_root / "logs" / "queue_processor" / "queue_processor.log").read_text(encoding="utf-8")
    assert "failed to move queue file: Destination already exists" in log_text
    events_text = (runtime_root / "logs" / "queue_processor_events.jsonl").read_text(encoding="utf-8")
    assert "queue_archive_collision_ambiguous" in events_text


def test_processed_job_id_exists_compatibility_path_preserves_dedupe_sources(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "compat-dedupe",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-06-02.md",
            "content": "Compatibility dedupe note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "compat-dedupe.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    job_id = qp.compute_job_id(payload)

    assert qp.processed_job_id_exists(runtime_root, runtime_root / "processed", job_id) == (True, "index")

    (runtime_root / "processed_index.jsonl").unlink()

    assert qp.processed_job_id_exists(runtime_root, runtime_root / "processed", job_id) == (True, "processed-scan")


def test_process_durable_queue_command_does_not_stage_outbox_or_working_triggers(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-01-00Z__append.json",
        {
            "job_id": "job-durable-command",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-05-08.md",
                "content": "Durable command processed this.",
                "destination": "journal",
            },
        },
    )
    intake_outbox = runtime_root / "intake" / "outbox"
    working_dir = runtime_root / "working"
    intake_outbox.mkdir(parents=True)
    working_dir.mkdir(parents=True)
    (intake_outbox / "old-transport.json").write_text('{"job_type":"append_to_note"}\n', encoding="utf-8")
    (working_dir / "nightly-digest-2026-05-08.trigger").write_text("trigger\n", encoding="utf-8")

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["discovered"] == 1
    assert output["processed"] == 1
    assert output["failed"] == 0
    assert output["unknown_reclassification_skipped"] is True
    assert (intake_outbox / "old-transport.json").exists()
    assert (working_dir / "nightly-digest-2026-05-08.trigger").exists()
    assert not (runtime_root / "completed").exists()
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "Durable command processed this." in recording_doc(store, "note_journal_2026-05-08")["content"]
    assert not (vault_root / "Journal" / "2026-05-08.md").exists()


def test_process_durable_queue_command_processes_field_capture_job_mix(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    seed_projection_docs_as_canonical(vault_root, store)
    append_job = {
        "job_id": "job-field-append",
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
            "content": "Reviewed staff request note.",
            "destination": "site_note",
        },
    }
    issue_job = log_site_issue_job_payload("job-field-issue")
    write_job(runtime_root / "queue", "2026-05-08T16-02-00Z__field-append.json", append_job)
    write_job(runtime_root / "queue", "2026-05-08T16-03-00Z__field-issue.json", issue_job)

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["discovered"] == 2
    assert output["processed"] == 2
    assert output["failed"] == 0
    assert not list((runtime_root / "queue").glob("*.json"))
    assert len(list((runtime_root / "processed").glob("*.json"))) == 2
    location_doc = recording_doc(store, "location_7050")
    assert "Reviewed staff request note." in location_doc["content"]
    issue_docs = [doc for doc in store.docs if doc["type"] == "site_issue"]
    assert len(issue_docs) == 1
    assert issue_docs[0]["site_id"] == "7050"
    assert issue_docs[0]["title"] == "Restroom drain backup and inoperable stall"
    issue_files = list((vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Issues").glob("*.md"))
    assert issue_files == []


def test_process_durable_queue_accepts_field_capture_log_site_issue_draft_shape(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    write_job(
        runtime_root / "queue",
        "ajd_ce9aa8ee95f31a91b785df90__f02ff975127ae481.json",
        {
            "job_id": "ajd_ce9aa8ee95f31a91b785df90",
            "job_type": "log_site_issue",
            "metadata": {
                "candidate_id": "ac_386bdf44bf4f08764e5a7bb7",
                "channel": "field_capture",
                "source": "approved_candidate_draft",
                "draft_path": str(runtime_root / "action_drafts" / "field_capture" / "ajd_ce9aa8ee95f31a91b785df90.json"),
            },
            "payload": {
                "site_id": "7050",
                "title": "Restroom drain backup and inoperable stall",
                "summary": "Drain backed up and the sink drain pushed water onto the restroom floor.",
                "observations": [
                    "Drain backed up.",
                    "Sink drain appears backed up.",
                    "Water is coming out onto the floor.",
                    "Metal stall is inoperable.",
                ],
                "category": "maintenance",
                "priority": "high",
                "status": "open",
                "observed_at": "2026-05-08T14:12:43.223836+00:00",
                "reported_by": "walsh-tom",
                "source": "field_capture",
                "client_notified": True,
                "client_notified_at": "2026-05-08T14:53:18.033008+00:00",
                "client_notified_by": "Jordan",
                "client_notified_method": "email",
                "client_notified_note": "Emailed client with photo/context.",
                "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
                "related_capture_ids": ["cap-photo-2026-05-06T18-27-03-04-00"],
                "related_candidate_ids": ["ac_386bdf44bf4f08764e5a7bb7"],
                "source_artifacts": [
                    str(runtime_root / "uploads" / "field_capture" / "cap-photo-2026-05-06T18-27-03-04-00.semantic.json"),
                    str(runtime_root / "uploads" / "field_capture" / "cap-photo-2026-05-06T18-27-03-04-00.audio.whisper.txt"),
                ],
            },
        },
    )

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["discovered"] == 1
    assert output["processed"] == 1
    assert output["failed"] == 0
    assert len(store.docs) == 1
    issue_doc = store.docs[0]
    assert issue_doc["type"] == "site_issue"
    assert issue_doc["client_notified"] is True
    assert issue_doc["reported_by"] == "walsh-tom"
    assert issue_doc["related_capture_ids"] == ["cap-photo-2026-05-06T18-27-03-04-00"]
    assert issue_doc["site_id"] == "7050"
    assert issue_doc["title"] == "Restroom drain backup and inoperable stall"
    assert issue_doc["summary"] == "Drain backed up and the sink drain pushed water onto the restroom floor."


def test_log_site_issue_skips_same_site_same_title_new_issue_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_job = log_site_issue_job_payload("job-site-issue-first")
    second_job = log_site_issue_job_payload("job-site-issue-second")
    second_job["payload"]["observed_at"] = "2026-05-09T18:27:03-04:00"
    second_job["payload"]["related_capture_ids"] = ["cap-photo-summit-drain-second"]
    write_job(runtime_root / "queue", "001-first-site-issue.json", first_job)
    write_job(runtime_root / "queue", "002-second-site-issue.json", second_job)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "reason=duplicate-site-issue-content" in log_text
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 1
    assert issue_docs[0]["title"] == "Restroom drain backup and inoperable stall"
    # btq_job_ids holds the COMPUTED job id; the skipped duplicate must NOT be merged in -> still one.
    assert len(issue_docs[0]["btq_job_ids"]) == 1
    assert len(list((runtime_root / "processed").glob("*.json"))) == 2
    records = list(processed_index.iter_records(runtime_root / "processed_index.jsonl"))
    # Both jobs processed-indexed (the skipped dup still records its processing) — 2 distinct computed ids.
    assert len({record["computed_job_id"] for record in records}) == 2


def test_log_site_issue_allows_different_title_same_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_job = log_site_issue_job_payload("job-site-issue-first")
    second_job = log_site_issue_job_payload("job-site-issue-different")
    second_job["payload"]["title"] = "Supply closet lock is sticking"
    second_job["payload"]["observed_at"] = "2026-05-09T18:27:03-04:00"
    second_job["payload"]["related_capture_ids"] = ["cap-photo-summit-lock"]
    write_job(runtime_root / "queue", "001-first-site-issue.json", first_job)
    write_job(runtime_root / "queue", "002-different-site-issue.json", second_job)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "reason=duplicate-site-issue-content" not in log_text
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 2
    assert {doc["title"] for doc in issue_docs} == {
        "Restroom drain backup and inoperable stall",
        "Supply closet lock is sticking",
    }


def test_log_site_issue_same_issue_id_updates_existing_doc_despite_title_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    seed_projection_docs_as_canonical(vault_root, store)
    store.docs.append(
        {
            "_id": "site_issue_issue-stable",
            "type": "site_issue",
            "issue_id": "issue-stable",
            "site_id": "7050",
            "site_name": "Summit Wire",
            "title": "Restroom drain backup and inoperable stall",
            "status": "open",
            "summary": "Original summary.",
            "created_at": "2026-05-08T00:00:00+00:00",
            "btq_job_ids": ["job-site-issue-original"],
        }
    )
    replay_job = log_site_issue_job_payload("job-site-issue-replay")
    replay_job["payload"]["issue_id"] = "issue-stable"
    replay_job["payload"]["summary"] = "Updated summary."
    write_job(runtime_root / "queue", "same-issue-id.json", replay_job)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "reason=duplicate-site-issue-content" not in log_text
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 1
    issue_doc = issue_docs[0]
    assert issue_doc["summary"] == "Updated summary."
    # Same issue_id (true replay) updates the existing doc and appends BOTH computed job ids.
    assert len(issue_doc["btq_job_ids"]) == 2


def test_log_site_issue_closed_same_title_does_not_block_new_open_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    seed_projection_docs_as_canonical(vault_root, store)
    store.docs.append(
        {
            "_id": "site_issue_issue-resolved",
            "type": "site_issue",
            "issue_id": "issue-resolved",
            "site_id": "7050",
            "site_name": "Summit Wire",
            "title": "Restroom drain backup and inoperable stall",
            "status": "resolved",
            "summary": "Resolved issue.",
            "created_at": "2026-05-08T00:00:00+00:00",
            "btq_job_ids": ["job-site-issue-resolved"],
        }
    )
    new_job = log_site_issue_job_payload("job-site-issue-new")
    new_job["payload"]["issue_id"] = "issue-new"
    write_job(runtime_root / "queue", "new-open-after-resolved.json", new_job)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "reason=duplicate-site-issue-content" not in log_text
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 2
    assert recording_doc(store, "site_issue_issue-new")["status"] == "open"


def test_process_durable_queue_invalid_job_moves_to_failed(tmp_path: Path, capsys) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-04-00Z__invalid.json",
        {
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-05-08.md",
                "destination": "journal",
            },
        },
    )

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["discovered"] == 1
    assert output["processed"] == 0
    assert output["failed"] == 1
    assert output["failed_paths"] == [str(runtime_root / "failed" / "2026-05-08T16-04-00Z__invalid.json")]
    assert (runtime_root / "failed" / "2026-05-08T16-04-00Z__invalid.json").exists()
    assert not list((runtime_root / "queue").glob("*.json"))


def test_process_durable_queue_missing_supply_canonical_doc_moves_to_failed(
    tmp_path: Path,
    capsys,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    create_supply_need(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    store.docs = [doc for doc in store.docs if doc.get("_id") != "supply_need_sup_summit_brightwash"]
    write_job(
        runtime_root / "queue",
        "2026-05-08T18-00-00Z__mark-supply-ordered.json",
        mark_supply_job_payload(),
    )
    capsys.readouterr()

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["discovered"] == 1
    assert output["processed"] == 0
    assert output["failed"] == 1
    assert output["failed_paths"] == [str(runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json")]
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()
    assert not (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_process_durable_queue_respects_processor_lock(tmp_path: Path, capsys) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    lock_path = runtime_root / "temp" / "processor.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": os.getpid(), "hostname": "test", "started_at": "now", "command": "pytest"}) + "\n", encoding="utf-8")
    write_job(
        runtime_root / "queue",
        "2026-05-08T16-05-00Z__append.json",
        {
            "job_id": "job-lock-held",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-05-08.md",
                "content": "Should not process while locked.",
                "destination": "journal",
            },
        },
    )

    exit_code = btq.run(
        [
            "process-durable-queue",
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--personal-vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert "Queue processor already running" in output["error"]
    assert (runtime_root / "queue" / "2026-05-08T16-05-00Z__append.json").exists()
    assert not (vault_root / "Journal" / "2026-05-08.md").exists()


def test_photo_capture_writes_canonical_journal_entry_and_attachments(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_type": "photo_capture",
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Restrooms",
            "note": "Trash accumulation at admin entrance.",
            "captured_at": "2026-04-30T14:00:00-04:00",
            "exported_at": "2026-04-30T14:02:00-04:00",
            "photos": [
                {
                    "filename": "admin entrance.jpg",
                    "mime_type": "image/jpeg",
                    "data_url": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
                }
            ],
        },
    }
    write_job(runtime_root / "queue", "2026-04-30T18-00-00Z__photo-capture.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    attachment_path = vault_root / "Journal" / "Attachments" / "2026-04-30" / "admin-entrance.jpg"
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    journal_doc = recording_doc(store, "journal_operational_2026-04-30")
    journal_text = journal_doc["content"]
    assert "Photo Capture - 2026-04-30T14:00:00-04:00" in journal_text
    assert "Area / QC Category: Restrooms" in journal_text
    assert "Exported At: 2026-04-30T14:02:00-04:00" in journal_text
    assert "Trash accumulation at admin entrance." in journal_text
    assert "Severity" not in journal_text
    assert "![[Attachments/2026-04-30/admin-entrance.jpg]]" in journal_text
    # The attachment link is recorded in the canonical journal; no projection
    # photo bytes are written under the (dead) vault root.
    assert not attachment_path.exists()
    assert "action=photo-capture status=success" in log_text
    assert "updated" not in stdout
    assert (runtime_root / "processed" / "2026-04-30T18-00-00Z__photo-capture.json").exists()
    assert not (vault_root / "Journal" / "2026-04-30.md").exists()


def test_photo_capture_accepts_stored_upload_path(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    upload_path = runtime_root / "uploads" / "2026-04-30" / "cap-photo-test" / "admin-entrance.jpg"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_bytes(b"\xff\xd8stored")
    payload = {
        "job_id": "stored-photo-capture",
        "job_type": "photo_capture",
        "payload": {
            "site": "Summit Wire",
            "qc_category": "Restrooms",
            "note": "Trash accumulation at admin entrance.",
            "captured_at": "2026-04-30T14:00:00-04:00",
            "exported_at": "2026-04-30T14:02:00-04:00",
            "photos": [
                {
                    "filename": "admin entrance.jpg",
                    "mime_type": "image/jpeg",
                    "stored_path": str(upload_path),
                }
            ],
        },
    }
    write_job(runtime_root / "queue", "2026-04-30T18-01-00Z__stored-photo-capture.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    # The capture is recorded canonically; the markdown-projection attachment
    # bytes are no longer written under the (dead) vault root.
    attachment_path = vault_root / "Journal" / "Attachments" / "2026-04-30" / "admin-entrance.jpg"
    assert not attachment_path.exists()
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    journal_doc = recording_doc(store, "journal_operational_2026-04-30")
    assert "![[Attachments/2026-04-30/admin-entrance.jpg]]" in journal_doc["content"]
    assert not (vault_root / "Journal" / "2026-04-30.md").exists()


def test_personal_journal_writes_canonical_doc_to_personal_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    personal_vault_root = (tmp_path / "personal-vault").resolve()
    personal_vault_root.mkdir(parents=True)
    store = use_recording_personal_journal_store(monkeypatch)
    payload = {
        "job_id": "job-personal",
        "job_type": "personal_journal_entry",
        "metadata": {"capture_id": "vm-personal"},
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T14:00:00+00:00",
            "audio_file": "personal.m4a",
            "body": "Today I need to think privately.",
            "raw_transcript_path": "/tmp/personal.m4a.whisper.txt",
        },
    }
    expected_job_id = qp.compute_job_id(payload)
    write_job(runtime_root / "queue", "2026-04-26T14-00-00Z__job-personal.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, personal_vault_root=personal_vault_root)

    doc = recording_doc(store, "journal_personal_2026-04-26")
    assert getattr(store, "database") == "btq_personal_journal"
    assert "updated journal_personal_2026-04-26" in stdout
    assert "action=personal-journal-entry status=success" in log_text
    assert doc["type"] == "journal"
    assert doc["date"] == "2026-04-26"
    assert doc["scope"] == "personal"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["btq_job_ids"] == [expected_job_id]
    assert doc["voice_memo_capture_ids"] == ["vm-personal"]
    assert "### 2026-04-26T14:00:00+00:00" in doc["content"]
    assert "Today I need to think privately." in doc["content"]
    assert "source_audio: personal.m4a" in doc["content"]
    assert "raw_transcript: /tmp/personal.m4a.whisper.txt" in doc["content"]
    assert not (personal_vault_root / "Journal" / "2026-04-26.md").exists()
    assert not (vault_root / "Journal" / "2026-04-26.md").exists()
    assert (runtime_root / "processed" / "2026-04-26T14-00-00Z__job-personal.json").exists()


def test_personal_journal_uses_personal_journal_database(monkeypatch: pytest.MonkeyPatch) -> None:
    class TrackingStore:
        def __init__(self, database: str) -> None:
            self.database = database

        @classmethod
        def for_database_from_env(cls, database: str) -> "TrackingStore":
            return cls(database)

    for name in (
        "BTQ_COUCHDB_URL",
        "BTQ_COUCHDB_USER",
        "BTQ_COUCHDB_PASSWORD",
        "BTQ_COUCHDB_TIMEOUT",
        "BTQ_COUCHDB_PERSONAL_JOURNAL_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shared, "CouchDBEntityStore", TrackingStore)

    store = shared._personal_journal_store()

    assert store.database == "btq_personal_journal"
    assert store.database != couchdb_config.vault_database()


def test_voice_memo_site_note_patches_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    write_job(
        runtime_root / "queue",
        "2026-05-10T17-20-23Z__voice-site.json",
        {
            "job_id": "job-voice-site",
            "job_type": "voice_memo_note",
            "payload": {
                "capture_id": "vm-test-site",
                "timestamp": "2026-05-10T17:20:23+00:00",
                "audio_file": "vm-test-site.webm",
                "raw_transcript_path": "/tmp/vm-test-site.webm.whisper.txt",
                "transcript_text": "The hallway looked good.",
                "routing_flag": "site_tagged",
                "site_id": "7060",
                "site": "Continental Metalworks",
                "note": "",
                "employees": [],
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "location_7060")["content"]
    assert "### 2026-05-10T17:20:23+00:00 — voice memo" in canonical_content
    assert "### 2026-05-10T17:20:23+00:00 — voice memo" not in site_path.read_text(encoding="utf-8")


def test_personal_journal_appends_second_entry_same_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    personal_vault_root = (tmp_path / "personal-vault").resolve()
    personal_vault_root.mkdir(parents=True)
    store = use_recording_personal_journal_store(monkeypatch)
    first_payload = {
        "job_id": "job-personal-first",
        "job_type": "personal_journal_entry",
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T14:00:00+00:00",
            "audio_file": "personal-first.m4a",
            "body": "First private entry.",
            "raw_transcript_path": "/tmp/personal-first.m4a.whisper.txt",
        },
    }
    first_job_id = qp.compute_job_id(first_payload)
    second_payload = {
        "job_id": "job-personal-second",
        "job_type": "personal_journal_entry",
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T15:00:00+00:00",
            "audio_file": "personal-second.m4a",
            "body": "Second private entry.",
            "raw_transcript_path": "/tmp/personal-second.m4a.whisper.txt",
        },
    }
    second_job_id = qp.compute_job_id(second_payload)
    write_job(runtime_root / "queue", "first-personal.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path, personal_vault_root=personal_vault_root)

    write_job(runtime_root / "queue", "second-personal.json", second_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path, personal_vault_root=personal_vault_root)

    doc = recording_doc(store, "journal_personal_2026-04-26")
    assert "First private entry." in doc["content"]
    assert "Second private entry." in doc["content"]
    assert doc["content"].index("First private entry.") < doc["content"].index("Second private entry.")
    assert doc["btq_job_ids"] == [first_job_id, second_job_id]
    assert not (personal_vault_root / "Journal" / "2026-04-26.md").exists()


def test_personal_journal_replay_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    personal_vault_root = (tmp_path / "personal-vault").resolve()
    personal_vault_root.mkdir(parents=True)
    store = use_recording_personal_journal_store(monkeypatch)
    payload = {
        "job_id": "job-personal-replay",
        "job_type": "personal_journal_entry",
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T16:00:00+00:00",
            "audio_file": "personal-replay.m4a",
            "body": "This should not append.",
            "raw_transcript_path": "/tmp/personal-replay.m4a.whisper.txt",
        },
    }
    expected_job_id = qp.compute_job_id(payload)
    store.docs.append(
        {
            "_id": "journal_personal_2026-04-26",
            "type": "journal",
            "date": "2026-04-26",
            "scope": "personal",
            "operator": OPERATOR_ID_GREG,
            "content": "Existing private entry.\n",
            "btq_job_ids": [expected_job_id],
        }
    )
    write_job(runtime_root / "queue", "replay-personal.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, personal_vault_root=personal_vault_root)

    doc = recording_doc(store, "journal_personal_2026-04-26")
    assert "job_id marker already present" in stdout
    assert "reason=job-id-marker-present" in log_text
    assert doc["content"] == "Existing private entry.\n"
    assert not (personal_vault_root / "Journal" / "2026-04-26.md").exists()
    assert (runtime_root / "processed" / "replay-personal.json").exists()


def test_personal_journal_store_error_fails_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingPersonalJournalStore(RmwRecordingVaultStore):
        def get_optional(self, doc_id: str) -> dict[str, Any] | None:
            raise RuntimeError(f"boom for {doc_id}")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    personal_vault_root = (tmp_path / "personal-vault").resolve()
    personal_vault_root.mkdir(parents=True)
    monkeypatch.setattr(shared, "_PERSONAL_JOURNAL_STORE", ExplodingPersonalJournalStore())
    payload = {
        "job_id": "job-personal-error",
        "job_type": "personal_journal_entry",
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T17:00:00+00:00",
            "audio_file": "personal-error.m4a",
            "body": "This should fail closed.",
            "raw_transcript_path": "/tmp/personal-error.m4a.whisper.txt",
        },
    }
    expected_job_id = qp.compute_job_id(payload)
    write_job(runtime_root / "queue", "personal-error.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, personal_vault_root=personal_vault_root)

    assert "failed" in stdout
    assert f"canonical couchdb write failed job_type=personal_journal_entry job_id={expected_job_id} entity_id=journal_personal_2026-04-26" in log_text
    assert (runtime_root / "failed" / "personal-error.json").exists()
    assert not (runtime_root / "processed" / "personal-error.json").exists()
    assert not (personal_vault_root / "Journal" / "2026-04-26.md").exists()


def test_personal_journal_db_in_required_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_PERSONAL_JOURNAL_DB", raising=False)

    assert couchdb_config.DEFAULT_PERSONAL_JOURNAL_DB in setup_databases.REQUIRED_DATABASES
    assert couchdb_config.personal_journal_database() == "btq_personal_journal"

    monkeypatch.setenv("BTQ_COUCHDB_PERSONAL_JOURNAL_DB", "custom_personal")

    assert couchdb_config.personal_journal_database() == "custom_personal"


def test_add_person_creates_canonical_person_file(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload()
    write_job(runtime_root / "queue", "2026-05-01T15-00-00Z__add-eric-dalton.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    created = datetime.now(qp.timezone.utc).date().isoformat()
    doc = canonical_doc("employee_dalton_eric")
    assert doc["type"] == "employee"
    assert doc["person_id"] == "dalton_eric"
    assert doc["name"] == "Eric Daniel Dalton"
    assert doc["employee_id"] == "567"
    assert doc["role"] == "Cleaner"
    assert doc["employment_type"] == "part_time"
    assert doc["status"] == "active"
    assert doc["additional_jobs"] == ["7071"]
    assert doc["assignments"] == [
        {
            "job": "7060",
            "account": "Contworks",
            "location": "Continental Metalworks Holdings",
            "shift": "evening",
        }
    ]
    assert doc["created_at"] == created
    assert len(doc.get("btq_job_ids", [])) == 1
    assert "action=add-person status=success" in log_text


def test_add_person_preserves_explicit_first_last(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload("job-add-person-explicit-name-parts")
    payload["payload"]["name"] = "Jordan J Avery"
    payload["payload"]["first"] = "Jordan"
    payload["payload"]["last"] = "Avery"
    write_job(runtime_root / "queue", "2026-05-01T15-00-00Z__add-jordan-avery.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employees = [doc for doc in store.docs if doc.get("type") == "employee"]
    assert len(employees) == 1
    assert employees[0]["first"] == "Jordan"
    assert employees[0]["last"] == "Avery"
    assert (runtime_root / "processed" / "2026-05-01T15-00-00Z__add-jordan-avery.json").exists()


def test_add_person_duplicate_employee_id_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_existing",
        "type": "employee",
        "name": "Existing Person",
        "employee_id": "567",
    })
    payload = add_person_job_payload("job-add-person-duplicate-id")
    write_job(runtime_root / "queue", "duplicate-id.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert (runtime_root / "failed" / "duplicate-id.json").exists()
    assert "Duplicate employee_id for add_person: 567" in log_text


def test_add_person_duplicate_name_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_existing",
        "type": "employee",
        "name": "Eric Daniel Dalton",
        "employee_id": "999",
    })
    payload = add_person_job_payload("job-add-person-duplicate-name")
    payload["payload"]["employee_id"] = "568"
    write_job(runtime_root / "queue", "duplicate-name.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert (runtime_root / "failed" / "duplicate-name.json").exists()
    assert "Duplicate person name for add_person: Eric Daniel Dalton" in log_text


def test_add_person_duplicate_reversed_name_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: an existing "First Last" record must be recognized as a duplicate
    # of an incoming "Last, First M." record (the formatting that slipped past the
    # original exact-name guard and produced two of the same worker).
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_dalton_eric",
        "type": "employee",
        "person_id": "dalton_eric",
        "name": "Eric Daniel Dalton",
    })
    payload = add_person_job_payload("job-add-person-reversed-name")
    payload["payload"]["name"] = "Dalton, Eric D."
    payload["payload"].pop("employee_id", None)
    write_job(runtime_root / "queue", "reversed-name.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert (runtime_root / "failed" / "reversed-name.json").exists()
    assert "Duplicate person name for add_person: Dalton, Eric D." in log_text


def test_add_person_assigns_readable_lastname_firstname_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The readable id is derived from last + first token (no middle initial),
    # even when the incoming name is in "Last, First M." form.
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    payload = add_person_job_payload("job-add-person-readable-id")
    payload["payload"]["name"] = "Dalton, Eric D."
    payload["payload"]["employee_id"] = "1265"
    write_job(runtime_root / "queue", "readable-id.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    created = recording_doc(store, "employee_dalton_eric")
    assert created["person_id"] == "dalton_eric"
    assert "action=add-person status=success" in log_text


def test_add_person_duplicate_name_in_couchdb_fails_without_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_existing",
        "type": "employee",
        "name": "Eric Daniel Dalton",
        "employee_id": "999",
    })
    payload = add_person_job_payload("job-add-person-canonical-duplicate-name")
    payload["payload"]["employee_id"] = "568"
    write_job(runtime_root / "queue", "canonical-duplicate-name.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert (runtime_root / "failed" / "canonical-duplicate-name.json").exists()
    assert "Duplicate person name for add_person: Eric Daniel Dalton" in log_text
    assert len([doc for doc in store.docs if doc.get("type") == "employee"]) == 1


def test_add_person_duplicate_employee_id_in_couchdb_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_existing",
        "type": "employee",
        "name": "Existing Person",
        "employee_id": "567",
    })
    payload = add_person_job_payload("job-add-person-canonical-duplicate-id")
    payload["payload"]["name"] = "Fresh Person"
    write_job(runtime_root / "queue", "canonical-duplicate-id.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Person, Fresh.md").exists()
    assert (runtime_root / "failed" / "canonical-duplicate-id.json").exists()
    assert "Duplicate employee_id for add_person: 567" in log_text
    assert len([doc for doc in store.docs if doc.get("type") == "employee"]) == 1


def test_add_person_non_duplicate_succeeds_with_canonical_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    payload = add_person_job_payload("job-add-person-canonical-success")
    write_job(runtime_root / "queue", "canonical-success.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    employee_docs = [doc for doc in store.docs if doc.get("type") == "employee"]
    assert len(employee_docs) == 1
    assert employee_docs[0]["name"] == "Eric Daniel Dalton"
    assert employee_docs[0]["employee_id"] == "567"
    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert (runtime_root / "processed" / "canonical-success.json").exists()
    assert "action=add-person status=success" in log_text


def test_add_person_person_id_uniqueness_checks_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    # An existing person whose readable id base ("dalton_eric") collides with the
    # incoming hire, but whose name differs (accent) enough to clear the
    # duplicate-name guard — so the id must disambiguate with a numeric suffix.
    store.upsert({
        "_id": "employee_dalton_eric",
        "type": "employee",
        "person_id": "dalton_eric",
        "name": "Éric Dalton",
        "employee_id": "999",
    })
    payload = add_person_job_payload("job-add-person-canonical-person-id")
    write_job(runtime_root / "queue", "canonical-person-id.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    created = recording_doc(store, "employee_dalton_eric_2")
    assert created["person_id"] == "dalton_eric_2"
    assert created["name"] == "Eric Daniel Dalton"
    assert "action=add-person status=success" in log_text


def test_add_person_succeeds_without_people_markdown_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    (vault_root / "People").rmdir()
    store = use_recording_vault_store(monkeypatch)
    payload = add_person_job_payload("job-add-person-no-people-dir")
    write_job(runtime_root / "queue", "no-people-dir.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    employee_docs = [doc for doc in store.docs if doc.get("type") == "employee"]
    assert len(employee_docs) == 1
    assert employee_docs[0]["name"] == "Eric Daniel Dalton"
    assert not (vault_root / "People").exists()
    assert (runtime_root / "processed" / "no-people-dir.json").exists()
    assert "action=add-person status=success" in log_text


def test_add_person_stale_projection_path_does_not_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    stale_path = vault_root / "People" / "Dalton, Eric Daniel.md"
    stale_path.write_text("# Stale projection\n", encoding="utf-8")
    store = use_recording_vault_store(monkeypatch)
    payload = add_person_job_payload("job-add-person-stale-projection")
    write_job(runtime_root / "queue", "stale-projection.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    employee_docs = [
        doc
        for doc in store.docs
        if doc.get("type") == "employee" and doc.get("name") == "Eric Daniel Dalton"
    ]
    assert len(employee_docs) == 1
    assert (runtime_root / "processed" / "stale-projection.json").exists()
    assert "action=add-person status=success" in log_text


def test_add_person_canonical_duplicate_still_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    store.upsert({
        "_id": "employee_existing",
        "type": "employee",
        "name": "Eric Daniel Dalton",
        "employee_id": "999",
    })
    payload = add_person_job_payload("job-add-person-canonical-duplicate-still-blocks")
    payload["payload"]["employee_id"] = "568"
    write_job(runtime_root / "queue", "canonical-duplicate-still-blocks.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert (runtime_root / "failed" / "canonical-duplicate-still-blocks.json").exists()
    assert "Duplicate person name for add_person: Eric Daniel Dalton" in log_text
    assert len([doc for doc in store.docs if doc.get("type") == "employee"]) == 1


def test_add_person_invalid_payload_rejected(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload("job-add-person-invalid")
    del payload["payload"]["role"]
    write_job(runtime_root / "queue", "invalid-add-person.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert (runtime_root / "failed" / "invalid-add-person.json").exists()
    assert "Job does not match queue_spec" in log_text


def test_add_person_path_injection_rejected(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload("job-add-person-path-injection")
    payload["payload"]["path"] = "People/Eric Daniel Dalton.md"
    write_job(runtime_root / "queue", "path-injection.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert not (vault_root / "People" / "Eric Daniel Dalton.md").exists()
    assert (runtime_root / "failed" / "path-injection.json").exists()
    assert "Job does not match queue_spec" in log_text


def test_add_person_generates_unique_person_ids(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    first = add_person_job_payload("job-add-person-first")
    first["payload"]["name"] = "Eric Daniel Dalton"
    first["payload"]["employee_id"] = "567"
    second = add_person_job_payload("job-add-person-second")
    second["payload"]["name"] = "Taylor Jordan Reed"
    second["payload"]["employee_id"] = "568"
    write_job(runtime_root / "queue", "first-person.json", first)
    write_job(runtime_root / "queue", "second-person.json", second)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    first_id = canonical_doc("employee_dalton_eric")["person_id"]
    second_id = canonical_doc("employee_reed_taylor")["person_id"]
    assert first_id == "dalton_eric"
    assert second_id == "reed_taylor"
    assert first_id != second_id


def test_add_person_idempotent_replay_success_after_restart(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload("job-add-person-idempotent", idempotency_key="ehub-567")
    write_job(runtime_root / "queue", "first-idempotent.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    original_doc = dict(canonical_doc("employee_dalton_eric"))

    # Prove the key ledger, not the processed archive, handles this replay.
    (runtime_root / "processed" / "first-idempotent.json").unlink()
    processed_index = runtime_root / "processed_index.jsonl"
    if processed_index.exists():
        processed_index.unlink()
    write_job(runtime_root / "queue", "second-idempotent.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employees = [doc for doc in store.docs if doc.get("type") == "employee"]
    assert len(employees) == 1
    assert canonical_doc("employee_dalton_eric") == original_doc
    assert "idempotency key already completed" in stdout
    assert "reason=idempotency-key-already-completed" in log_text
    assert (runtime_root / "processed" / "second-idempotent.json").exists()


def test_add_person_idempotent_replay_skips_canonical_duplicate_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_VAULT_MARKDOWN_WRITE", raising=False)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    payload = add_person_job_payload("job-add-person-idempotent-canonical", idempotency_key="ehub-567")
    write_job(runtime_root / "queue", "first-idempotent.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    (runtime_root / "processed" / "first-idempotent.json").unlink()
    processed_index = runtime_root / "processed_index.jsonl"
    if processed_index.exists():
        processed_index.unlink()
    write_job(runtime_root / "queue", "second-idempotent.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    employee_docs = [doc for doc in store.docs if doc.get("type") == "employee"]
    assert len(employee_docs) == 1
    assert not (vault_root / "People" / "Dalton, Eric Daniel.md").exists()
    assert "idempotency key already completed" in stdout
    assert "reason=idempotency-key-already-completed" in log_text
    assert (runtime_root / "processed" / "second-idempotent.json").exists()


def test_add_person_idempotent_replay_payload_mismatch_fails(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = add_person_job_payload("job-add-person-idempotent", idempotency_key="ehub-567")
    write_job(runtime_root / "queue", "first-idempotent.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    mismatch = add_person_job_payload("job-add-person-mismatch", idempotency_key="ehub-567")
    mismatch["payload"]["role"] = "Supervisor"
    mismatch["payload"]["employee_id"] = "568"
    write_job(runtime_root / "queue", "mismatch-idempotent.json", mismatch)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert (runtime_root / "failed" / "mismatch-idempotent.json").exists()
    assert "Idempotency key conflict for ehub-567" in log_text
    assert not (vault_root / "People" / "2, Eric Daniel Dalton.md").exists()


def test_add_person_failed_job_does_not_mark_idempotency_key_success(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    invalid = add_person_job_payload("job-add-person-invalid-keyed", idempotency_key="ehub-567")
    del invalid["payload"]["role"]
    write_job(runtime_root / "queue", "invalid-keyed.json", invalid)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    assert (runtime_root / "failed" / "invalid-keyed.json").exists()
    ledger_path = runtime_root / "idempotency_keys.jsonl"
    assert not ledger_path.exists()

    valid = add_person_job_payload("job-add-person-valid-after-failure", idempotency_key="ehub-567")
    write_job(runtime_root / "queue", "valid-after-failure.json", valid)
    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert any(doc.get("_id") == "employee_dalton_eric" for doc in store.docs)
    assert "action=add-person status=success" in log_text


def test_add_person_without_idempotency_key_still_uses_duplicate_protection(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    first = add_person_job_payload("job-add-person-first")
    write_job(runtime_root / "queue", "first-unkeyed.json", first)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    duplicate = add_person_job_payload("job-add-person-unkeyed-replay")
    write_job(runtime_root / "queue", "second-unkeyed.json", duplicate)
    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert (runtime_root / "failed" / "second-unkeyed.json").exists()
    assert "Duplicate employee_id for add_person: 567" in log_text
    assert len([doc for doc in store.docs if doc.get("type") == "employee"]) == 1


def test_append_to_note_skips_duplicate_existing_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    target_path = vault_root / "Journal" / "2026-04-19.md"
    target_path.write_text("Existing\nSite surfaces are difficult to clean, show marks, and generate complaints\n", encoding="utf-8")
    store.docs.append(
        {
            "_id": "note_journal_2026-04-19",
            "type": "note",
            "operator": OPERATOR_ID_GREG,
            "date": "2026-04-19",
            "content": "Existing\nSite surfaces are difficult to clean, show marks, and generate complaints\n",
        }
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-30-00Z__job-append-duplicate.json",
        {
            "job_id": "job-append-duplicate",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-19.md",
                "content": "Site surfaces are difficult to clean, show marks, and generate complaints",
                "destination": "journal",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    updated_text = recording_doc(store, "note_journal_2026-04-19")["content"]
    assert "duplicate append_to_note content skipped" in stdout
    assert "reason=duplicate-append-to-note-content" in log_text
    assert updated_text.count("Site surfaces are difficult to clean, show marks, and generate complaints") == 1
    assert (runtime_root / "processed" / "2026-04-19T23-30-00Z__job-append-duplicate.json").exists()


def test_append_to_note_with_observations_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    content = (
        "Summit Wire candidate interview call confirmed evening availability\n\n"
        "**Observations:**\n"
        "- Candidate volunteered denial of drug and alcohol issues without prompting\n"
        "- Phone battery died mid-call; candidate said he would call back"
    )
    write_job(
        runtime_root / "queue",
        "2026-04-20T23-30-00Z__job-append-observation.json",
        {
            "job_id": "job-append-observation",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-20.md",
                "content": content,
                "destination": "journal",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    assert "updated" in stdout
    assert "action=append-to-note status=success" in log_text

    write_job(
        runtime_root / "queue",
        "2026-04-20T23-31-00Z__job-append-observation-repeat.json",
        {
            "job_id": "job-append-observation-repeat",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-20.md",
                "content": content,
                "destination": "journal",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    updated_text = recording_doc(store, "note_journal_2026-04-20")["content"]

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert updated_text.count("- Candidate volunteered denial of drug and alcohol issues without prompting") == 1
    assert updated_text.count("- Phone battery died mid-call; candidate said he would call back") == 1


def test_reprocessing_same_job_twice_does_not_duplicate_vault_change(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-append-idempotent",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-20.md",
            "content": "First queue note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "2026-04-20T23-32-00Z__job-append-idempotent.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-04-20T23-33-00Z__job-append-idempotent-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    updated_text = recording_doc(store, "note_journal_2026-04-20")["content"]
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert updated_text.count("First queue note.") == 1
    # btq_job_ids stores the computed job_id (hash); reprocessing must not duplicate it.
    assert len(recording_doc(store, "note_journal_2026-04-20")["btq_job_ids"]) == 1


def test_log_site_issue_creates_structured_issue_file(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_site_issue_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T16-00-00Z__log-site-issue.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 1
    issue = issue_docs[0]
    assert "updated" in stdout
    assert "action=log-site-issue status=success" in log_text
    assert str(issue["issue_id"]).startswith("iss_")
    assert issue["site_id"] == "7050"
    assert issue["site_name"] == "Summit Wire"
    assert issue["title"] == "Restroom drain backup and inoperable stall"
    assert issue["status"] == "open"
    assert issue["priority"] == "high"
    assert issue["category"] == "maintenance"
    assert issue["reported_by"] == "Tom Walsh"
    assert issue["client_notified"] is True
    assert issue["client_notified_method"] == "email"
    assert issue["client_notified_note"] == "Emailed client with photo/context."
    assert issue["resolution_trigger"] == "Maintenance confirms the drain is clear and the stall is operable."
    assert issue["related_capture_ids"] == ["cap-photo-summit-drain"]
    assert issue["related_candidate_ids"] == ["ac_386bdf44bf4f08764e5a7bb7"]
    assert issue["summary"] == "Drain backed up and the sink drain pushed water onto the restroom floor."
    assert len(issue.get("btq_job_ids", [])) == 1
    assert (runtime_root / "processed" / "2026-05-08T16-00-00Z__log-site-issue.json").exists()


def test_log_site_issue_records_notes_in_issue_file(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_site_issue_job_payload()
    payload["payload"]["notes"] = "Distinct from the acute cleaning-failure issue; the two are linked but not collapsed."
    write_job(runtime_root / "queue", "2026-05-08T16-00-00Z__log-site-issue.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 1
    assert issue_docs[0]["notes"] == "Distinct from the acute cleaning-failure issue; the two are linked but not collapsed."


def test_log_site_issue_reprocessing_same_payload_does_not_duplicate_issue_file(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_site_issue_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T16-00-00Z__log-site-issue.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-05-08T16-01-00Z__log-site-issue-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    issue_docs = [doc for doc in store.docs if doc.get("type") == "site_issue"]
    assert len(issue_docs) == 1
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert len(issue_docs[0].get("btq_job_ids", [])) == 1


def test_log_site_issue_explicit_issue_id_updates_existing_issue(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_payload = log_site_issue_job_payload("job-log-site-issue-open")
    first_payload["payload"]["issue_id"] = "iss_summit_wire_drain"
    write_job(runtime_root / "queue", "2026-05-08T16-00-00Z__log-site-issue-open.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = log_site_issue_job_payload("job-log-site-issue-monitoring")
    second_payload["payload"]["issue_id"] = "iss_summit_wire_drain"
    second_payload["payload"]["status"] = "monitoring"
    second_payload["payload"]["summary"] = "Client has been notified; issue is being monitored until maintenance confirms closure."
    write_job(runtime_root / "queue", "2026-05-08T16-05-00Z__log-site-issue-monitoring.json", second_payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("site_issue_iss_summit_wire_drain")
    assert "updated" in stdout
    assert "action=log-site-issue status=success" in log_text
    assert doc["issue_id"] == "iss_summit_wire_drain"
    assert doc["status"] == "monitoring"
    assert "Client has been notified; issue is being monitored" in doc["summary"]
    # Both the open and monitoring log jobs recorded on the canonical doc.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_append_to_note_still_works_after_log_site_issue_added(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    target_path = vault_root / "Journal" / "2026-05-08.md"
    payload = {
        "job_id": "job-append-after-log-site-issue",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-05-08.md",
            "content": "Existing append_to_note behavior remains available.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "2026-05-08T16-10-00Z__append-after-log-site-issue.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "updated" in stdout
    assert "action=append-to-note status=success" in log_text
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "Existing append_to_note behavior remains available." in recording_doc(store, "note_journal_2026-05-08")["content"]
    assert not (vault_root / "Journal" / "2026-05-08.md").exists()


def test_log_supply_need_writes_new_file_under_site_supplies(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_supply_need_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-need.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    supply_docs = [doc for doc in store.docs if doc.get("type") == "supply_need"]
    assert len(supply_docs) == 1
    supply = supply_docs[0]
    assert "updated" in stdout
    assert "action=log-supply-need status=success" in log_text
    assert str(supply["supply_id"]).startswith("sup_")
    assert supply["site_id"] == "7050"
    assert supply["site_name"] == "Summit Wire"
    assert supply["item_name"] == "BrightWash cleaner"
    assert supply["quantity_needed"] == "2 bottles"
    assert supply["urgency"] == "high"
    assert supply["requested_by"] == "Tom Walsh"
    # No explicit status in the payload; the canonical reader treats absent as "open".
    assert supply.get("status", "open") == "open"
    assert supply["related_capture_ids"] == ["cap-supply-summit"]
    assert supply["related_candidate_ids"] == ["ac_supply_summit"]
    assert len(supply.get("btq_job_ids", [])) == 1
    assert (runtime_root / "processed" / "2026-05-08T17-00-00Z__log-supply-need.json").exists()


def test_log_supply_need_idempotent_on_repeat_job_id(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_supply_need_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-need.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-05-08T17-01-00Z__log-supply-need-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    supply_docs = [doc for doc in store.docs if doc.get("type") == "supply_need"]
    assert len(supply_docs) == 1
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert len(supply_docs[0].get("btq_job_ids", [])) == 1


def test_log_supply_need_updates_status_field_on_re_application(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_payload = log_supply_need_job_payload("job-log-supply-open")
    first_payload["payload"]["supply_id"] = "sup_summit_brightwash"
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-open.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = log_supply_need_job_payload("job-log-supply-ordered")
    second_payload["payload"]["supply_id"] = "sup_summit_brightwash"
    second_payload["payload"]["status"] = "ordered"
    second_payload["payload"]["ordered_at"] = "2026-05-08T18:00:00+00:00"
    second_payload["payload"]["ordered_by"] = "Jordan"
    second_payload["payload"]["ordered_note"] = "Ordered from Staples."
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__log-supply-ordered.json", second_payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("supply_need_sup_summit_brightwash")
    assert "updated" in stdout
    assert "action=log-supply-need status=success" in log_text
    assert doc["status"] == "ordered"
    assert doc["ordered_by"] == "Jordan"
    assert doc["ordered_note"] == "Ordered from Staples."
    # Both the open and ordered log jobs recorded on the canonical doc.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_log_supply_need_preserves_created_at_across_updates(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_payload = log_supply_need_job_payload("job-log-supply-created-first")
    first_payload["payload"]["supply_id"] = "sup_summit_brightwash"
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-created-first.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    created_at = canonical_doc("supply_need_sup_summit_brightwash")["created_at"]

    second_payload = log_supply_need_job_payload("job-log-supply-created-second")
    second_payload["payload"]["supply_id"] = "sup_summit_brightwash"
    second_payload["payload"]["status"] = "delivered"
    write_job(runtime_root / "queue", "2026-05-08T19-00-00Z__log-supply-created-second.json", second_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("supply_need_sup_summit_brightwash")
    assert doc["created_at"] == created_at
    assert doc["supply_id"] == "sup_summit_brightwash"
    assert doc["status"] == "delivered"


def test_log_supply_need_dry_run_does_not_write(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_supply_need_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-need.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, dry_run=True)

    assert "would log supply need" in stdout
    assert "action=log-supply-need status=success" in log_text
    assert not (vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Supplies").exists()
    assert (runtime_root / "queue" / "2026-05-08T17-00-00Z__log-supply-need.json").exists()


def test_log_supply_need_records_mutation_evidence(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_supply_need_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-supply-need.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    evidence_files = sorted(runtime_root.glob("evidence/**/*.json"))
    assert evidence_files
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in evidence_files)
    assert '"handler_type": "log_supply_need"' in evidence_text
    assert '"capture_id": "cap-supply-summit"' in evidence_text
    # Evidence records the canonical mutation (doc id), not the rendered Markdown excerpt (C3-316d).
    assert '"target_doc_id": "supply_need_' in evidence_text


def test_log_equipment_request_writes_new_file_under_site_equipment(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_equipment_request_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-request.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    equipment_docs = [doc for doc in store.docs if doc.get("type") == "equipment_request"]
    assert len(equipment_docs) == 1
    equipment = equipment_docs[0]
    assert "updated" in stdout
    assert "action=log-equipment-request status=success" in log_text
    assert str(equipment["equipment_id"]).startswith("eqr_")
    assert equipment["site_id"] == "7050"
    assert equipment["site_name"] == "Summit Wire"
    assert equipment["equipment_name"] == "vacuum"
    assert equipment["reason"] == "Current vacuum will not start."
    assert equipment["priority"] == "urgent"
    assert equipment["requested_by"] == "Tom Walsh"
    # No explicit status in the payload; the canonical reader treats absent as "open".
    assert equipment.get("status", "open") == "open"
    assert equipment["related_capture_ids"] == ["cap-equipment-summit"]
    assert equipment["related_candidate_ids"] == ["ac_equipment_summit"]
    assert len(equipment.get("btq_job_ids", [])) == 1
    assert (runtime_root / "processed" / "2026-05-08T17-00-00Z__log-equipment-request.json").exists()


def test_log_equipment_request_idempotent_on_repeat_job_id(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_equipment_request_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-request.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-05-08T17-01-00Z__log-equipment-request-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    equipment_docs = [doc for doc in store.docs if doc.get("type") == "equipment_request"]
    assert len(equipment_docs) == 1
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert len(equipment_docs[0].get("btq_job_ids", [])) == 1


def test_log_equipment_request_updates_status_field_on_re_application(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_payload = log_equipment_request_job_payload("job-log-equipment-open")
    first_payload["payload"]["equipment_id"] = "eqr_summit_vacuum"
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-open.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = log_equipment_request_job_payload("job-log-equipment-approved")
    second_payload["payload"]["equipment_id"] = "eqr_summit_vacuum"
    second_payload["payload"]["status"] = "approved"
    second_payload["payload"]["approved_at"] = "2026-05-08T18:00:00+00:00"
    second_payload["payload"]["approved_by"] = "Jordan"
    second_payload["payload"]["approval_note"] = "Approved replacement vacuum."
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__log-equipment-approved.json", second_payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("equipment_request_eqr_summit_vacuum")
    assert "updated" in stdout
    assert "action=log-equipment-request status=success" in log_text
    assert doc["status"] == "approved"
    assert doc["approved_by"] == "Jordan"
    assert doc["approval_note"] == "Approved replacement vacuum."
    # Both the open and approved log jobs recorded on the canonical doc.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_log_equipment_request_preserves_created_at_across_updates(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    first_payload = log_equipment_request_job_payload("job-log-equipment-created-first")
    first_payload["payload"]["equipment_id"] = "eqr_summit_vacuum"
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-created-first.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    created_at = canonical_doc("equipment_request_eqr_summit_vacuum")["created_at"]

    second_payload = log_equipment_request_job_payload("job-log-equipment-created-second")
    second_payload["payload"]["equipment_id"] = "eqr_summit_vacuum"
    second_payload["payload"]["status"] = "ordered"
    write_job(runtime_root / "queue", "2026-05-08T19-00-00Z__log-equipment-created-second.json", second_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("equipment_request_eqr_summit_vacuum")
    assert doc["created_at"] == created_at
    assert doc["equipment_id"] == "eqr_summit_vacuum"
    assert doc["status"] == "ordered"


def test_log_equipment_request_dry_run_does_not_write(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_equipment_request_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-request.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, dry_run=True)

    assert "would log equipment request" in stdout
    assert "action=log-equipment-request status=success" in log_text
    assert not (vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Equipment").exists()
    assert (runtime_root / "queue" / "2026-05-08T17-00-00Z__log-equipment-request.json").exists()


def test_log_personnel_event_creates_structured_event_file_under_people_events_dir(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = log_personnel_event_job_payload()
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    event_docs = [doc for doc in store.docs if doc.get("type") == "personnel_event"]
    assert len(event_docs) == 1
    event = event_docs[0]
    assert "updated" in stdout
    assert "action=log-personnel-event status=success" in log_text
    assert str(event["event_id"]).startswith("evt_")
    assert event["employee"] == "Tate, Marcus"
    assert event["event_type"] == "attendance"
    assert event["severity"] == "concern"
    assert event["status"] == "open"
    assert event["reported_by"] == "Jordan"
    assert str(event["related_site"]) == "7050"
    assert event["client_notified"] is False
    assert len(event.get("btq_job_ids", [])) == 1
    assert (runtime_root / "processed" / "2026-05-18T09-00-00Z__log-personnel-event.json").exists()


def test_log_personnel_event_reprocessing_same_job_id_does_not_duplicate_history(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = log_personnel_event_job_payload()
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-05-18T09-01-00Z__log-personnel-event-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    event_docs = [doc for doc in store.docs if doc.get("type") == "personnel_event"]
    assert len(event_docs) == 1
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert len(event_docs[0].get("btq_job_ids", [])) == 1


def test_log_personnel_event_explicit_event_id_updates_existing_event(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    first_payload = log_personnel_event_job_payload("job-log-personnel-event-open")
    first_payload["payload"]["event_id"] = "evt_marcus_late_001"
    first_payload["payload"]["status"] = "open"
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event-open.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = log_personnel_event_job_payload("job-log-personnel-event-monitoring")
    second_payload["payload"]["event_id"] = "evt_marcus_late_001"
    second_payload["payload"]["status"] = "monitoring"
    second_payload["payload"]["severity"] = "verbal_warning"
    second_payload["payload"]["summary"] = "Verbal warning issued after Summit Wire opening shift was uncovered."
    write_job(runtime_root / "queue", "2026-05-18T09-15-00Z__log-personnel-event-monitoring.json", second_payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc("personnel_event_evt_marcus_late_001")
    assert "updated" in stdout
    assert "action=log-personnel-event status=success" in log_text
    assert doc["event_id"] == "evt_marcus_late_001"
    assert doc["status"] == "monitoring"
    assert doc["severity"] == "verbal_warning"
    assert "Verbal warning issued after Summit Wire" in doc["summary"]
    # Both the open and monitoring log jobs recorded on the canonical doc.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_log_personnel_event_creates_canonical_event_doc(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = log_personnel_event_job_payload()
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    event_docs = [doc for doc in store.docs if doc.get("type") == "personnel_event"]
    assert len(event_docs) == 1
    assert event_docs[0]["employee"] == "Tate, Marcus"


def test_log_personnel_event_dry_run_does_not_write(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = log_personnel_event_job_payload()
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path, dry_run=True)

    assert "would log personnel event" in stdout
    assert "action=log-personnel-event status=success" in log_text
    assert not (vault_root / "People" / "Tate, Marcus" / "Events").exists()
    assert (runtime_root / "queue" / "2026-05-18T09-00-00Z__log-personnel-event.json").exists()


def test_log_personnel_event_idempotent_across_employee_name_variations(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    first_payload = log_personnel_event_job_payload("job-log-personnel-event-first-last")
    first_payload["payload"]["employee"] = "Marcus Tate"
    write_job(runtime_root / "queue", "2026-05-18T09-00-00Z__log-personnel-event-first-last.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = log_personnel_event_job_payload("job-log-personnel-event-last-first")
    second_payload["payload"]["employee"] = "Tate, Marcus"
    write_job(runtime_root / "queue", "2026-05-18T09-05-00Z__log-personnel-event-last-first.json", second_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    event_docs = [doc for doc in store.docs if doc.get("type") == "personnel_event"]
    # Both name forms resolve to the same canonical event doc.
    assert len(event_docs) == 1
    assert len(event_docs[0].get("btq_job_ids", [])) == 2


def test_log_equipment_request_records_mutation_evidence(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = log_equipment_request_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T17-00-00Z__log-equipment-request.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    evidence_files = sorted(runtime_root.glob("evidence/**/*.json"))
    assert evidence_files
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in evidence_files)
    assert '"handler_type": "log_equipment_request"' in evidence_text
    assert '"capture_id": "cap-equipment-summit"' in evidence_text
    # Evidence records the canonical mutation (doc id), not the rendered Markdown excerpt (C3-316d).
    assert '"target_doc_id": "equipment_request_' in evidence_text


def test_process_update_site_equipment_creates_subsection_when_absent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7060")["content"]
    assert "updated" in stdout
    assert "action=update-site-equipment status=success" in log_text
    assert "### Parking / Loading\n\nUse dock.\n\n### Supplies / Equipment\n| Description | Brand | Color | Status | Notes |" in site_text
    assert "## Field Capture Reviews" in site_text
    assert (runtime_root / "processed" / "2026-05-13T17-00-00Z__update-site-equipment.json").exists()


def test_update_site_equipment_patches_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "location_7060")["content"]
    assert "| Large walk-behind scrubber | Viper | Red | operational | Used 1x/week |" in canonical_content
    assert canonical_content != shared.canonical_content_body(site_path.read_text(encoding="utf-8"))


def test_process_update_site_equipment_replaces_existing_subsection_body(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(
        vault_root,
        "# Continental Metalworks\n\n## Operational Notes\n\n### Supplies / Equipment\nOld inventory.\n\n### Zones / Sequence\nZone A first.\n",
    )
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7060")["content"]
    assert "### Supplies / Equipment\n| Description | Brand | Color | Status | Notes |" in site_text
    assert "Old inventory" not in site_text
    assert "### Zones / Sequence\nZone A first." in site_text


def test_process_update_site_equipment_appends_section_notes_when_provided(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    payload["payload"]["section_notes"] = "Repair path for blue scrubber is the current open action."
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7060")["content"]
    assert "| Large walk-behind scrubber | Viper | Red | operational | Used 1x/week |" in site_text
    assert f"Repair path for blue scrubber is the current open action.\n\n<!-- btq_job_id: {qp.compute_job_id(payload)} -->" in site_text


def test_process_update_site_equipment_omits_section_notes_when_absent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7060")["content"]
    assert "| Large walk-behind scrubber | Viper | Red | operational | Used 1x/week |" in site_text
    assert f"Inspection: 2026-05-13 (Jordan)\n\n<!-- btq_job_id: {qp.compute_job_id(payload)} -->" in site_text


def test_process_update_site_equipment_renders_inspection_line(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "Inspection: 2026-05-13 (Jordan)" in recording_doc(store, "location_7060")["content"]


def test_process_update_site_equipment_escapes_pipe_in_values(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    payload["payload"]["equipment"] = [
        {
            "description": "iMop",
            "brand": "(unknown)",
            "color": "(unknown)",
            "status": "untested",
            "notes": "Uses Type B|2 cleaner",
        }
    ]
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    row = "| iMop | (unknown) | (unknown) | untested | Uses Type B\\|2 cleaner |"
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert row in recording_doc(store, "location_7060")["content"]
    assert sum(1 for index, char in enumerate(row) if char == "|" and (index == 0 or row[index - 1] != "\\")) == 6


def test_process_update_site_equipment_idempotent_on_replay(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    first_text = recording_doc(shared._VAULT_STORE, "location_7060")["content"]

    write_job(runtime_root / "queue", "2026-05-13T17-01-00Z__update-site-equipment-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert recording_doc(shared._VAULT_STORE, "location_7060")["content"] == first_text
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text


def test_process_update_site_equipment_new_job_id_replaces_table(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    first_payload = update_site_equipment_job_payload("job-update-site-equipment-first")
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment-first.json", first_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    second_payload = update_site_equipment_job_payload("job-update-site-equipment-second")
    second_payload["payload"]["equipment"] = [
        {
            "description": "iMop",
            "brand": "(unknown)",
            "color": "(unknown)",
            "status": "untested",
        }
    ]
    write_job(runtime_root / "queue", "2026-05-13T18-00-00Z__update-site-equipment-second.json", second_payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7060")["content"]
    assert "Large walk-behind scrubber" not in site_text
    assert "| iMop | (unknown) | (unknown) | untested |  |" in site_text
    assert f"<!-- btq_job_id: {qp.compute_job_id(second_payload)} -->" in site_text
    assert f"<!-- btq_job_id: {qp.compute_job_id(first_payload)} -->" not in site_text


def test_process_update_site_equipment_raises_when_parent_heading_absent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_continental_site(vault_root, "# Continental Metalworks\n\n## Other Notes\n\n")
    payload = update_site_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "parent heading not found: ## Operational Notes" in stdout
    assert "status=failure" in log_text
    assert (runtime_root / "failed" / "2026-05-13T17-00-00Z__update-site-equipment.json").exists()
    assert not (runtime_root / "processed" / "2026-05-13T17-00-00Z__update-site-equipment.json").exists()


def test_process_update_site_equipment_resolves_site_by_name(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = write_continental_site(vault_root)
    payload = update_site_equipment_job_payload()
    payload["payload"]["site"] = "Continental Metalworks"
    del payload["payload"]["site_id"]
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    assert "| Large walk-behind scrubber | Viper | Red | operational | Used 1x/week |" in recording_doc(store, "location_7060")["content"]


def test_process_update_site_equipment_rejects_unregistered_site_name(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_continental_site(vault_root)
    other_site_path = vault_root / "Accounts" / "Contworks" / "Locations" / "1201 - Continental Annex" / "about.md"
    write_frontmatter_file(
        other_site_path,
        [
            ("type", "location"),
            ("site_id", "7061"),
            ("job", "7061"),
            ("location", "Continental Annex"),
            ("account", "Contworks"),
            ("site_aliases", "shared-continental"),
        ],
        body="# Continental Annex\n\n## Operational Notes\n\n",
    )
    first_text = write_continental_site(vault_root).read_text(encoding="utf-8")
    site_path = write_continental_site(vault_root, first_text + "")
    store = seed_projection_docs_as_canonical(vault_root)
    assert isinstance(store, RecordingVaultStore)
    site_doc = recording_doc(store, "location_7060")
    site_doc["site_aliases"] = "shared-continental"
    site_text = site_path.read_text(encoding="utf-8")
    site_path.write_text(site_text.replace("account: Contworks\n", "account: Contworks\nsite_aliases: shared-continental\n"), encoding="utf-8")
    payload = update_site_equipment_job_payload()
    payload["payload"]["site"] = "shared-continental"
    del payload["payload"]["site_id"]
    write_job(runtime_root / "queue", "2026-05-13T17-00-00Z__update-site-equipment.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "Invalid site: shared-continental" in stdout
    assert "reason=invalid-site-id" in log_text
    assert (runtime_root / "failed" / "2026-05-13T17-00-00Z__update-site-equipment.json").exists()
    assert not (runtime_root / "processed" / "2026-05-13T17-00-00Z__update-site-equipment.json").exists()


def canonical_doc(doc_id: str) -> dict[str, Any]:
    """Return the canonical CouchDB doc with ``doc_id`` from the recording store."""
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    matches = [doc for doc in store.docs if doc.get("_id") == doc_id]
    assert len(matches) == 1, f"expected exactly one canonical doc {doc_id}, found {len(matches)}"
    return matches[0]


def create_supply_need(
    project_root: Path,
    vault_root: Path,
    runtime_root: Path,
    log_path: Path,
    *,
    supply_id: str = "sup_summit_brightwash",
    status: str = "open",
) -> str:
    payload = log_supply_need_job_payload(f"job-log-{supply_id}-{status}")
    payload["payload"]["supply_id"] = supply_id
    payload["payload"]["status"] = status
    write_job(runtime_root / "queue", f"2026-05-08T17-00-00Z__log-{supply_id}-{status}.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    return f"supply_need_{supply_id}"


def create_equipment_request(
    project_root: Path,
    vault_root: Path,
    runtime_root: Path,
    log_path: Path,
    *,
    equipment_id: str = "eqr_summit_vacuum",
    status: str = "open",
) -> str:
    payload = log_equipment_request_job_payload(f"job-log-{equipment_id}-{status}")
    payload["payload"]["equipment_id"] = equipment_id
    payload["payload"]["status"] = status
    write_job(runtime_root / "queue", f"2026-05-08T17-00-00Z__log-{equipment_id}-{status}.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    return f"equipment_request_{equipment_id}"


def test_mark_supply_ordered_advances_status_from_open(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path)
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "updated" in stdout
    assert "action=mark-supply-ordered status=success" in log_text
    doc = canonical_doc(doc_id)
    assert doc["status"] == "ordered"
    # log job + mark job both recorded on the canonical doc.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_mark_supply_ordered_sets_ordered_at_ordered_by_fields(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path)
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc(doc_id)
    assert doc["ordered_at"] == "2026-05-08T18:00:00+00:00"
    assert doc["ordered_by"] == "Jordan"


def test_mark_supply_ordered_canonical_patch_failure_keeps_queue_unprocessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_markdown_writes: None,
) -> None:
    class FailingStatusStore:
        def patch_status(
            self,
            doc_id: str,
            status: str,
            extra_fields: dict | None = None,
            *,
            require_existing: bool = False,
        ) -> None:
            raise RuntimeError(f"status patch failed for {doc_id}")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path)
    original_doc = dict(canonical_doc(doc_id))
    monkeypatch.setattr(shared, "_vault_store", lambda: FailingStatusStore())
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert canonical_doc(doc_id) == original_doc
    assert not (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_mark_supply_ordered_missing_canonical_doc_fails_job(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    create_supply_need(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    store.docs = [doc for doc in store.docs if doc.get("_id") != "supply_need_sup_summit_brightwash"]
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert "job_type=mark_supply_ordered" in log_text
    assert "job_id=job-mark-supply-ordered" in log_text
    assert "entity_id=supply_need_sup_summit_brightwash" in log_text
    assert "CouchDB required document not found for canonical RMW: supply_need_sup_summit_brightwash" in log_text
    assert not (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_mark_supply_ordered_resolves_path_derived_canonical_doc_id(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    # Regression: canonical supply_need docs are stored under path-derived ids
    # (supply_need_accounts_.._<supply_id>_<slug>), not the flat
    # supply_need_<supply_id>. The mark-supply-* handlers used to construct the
    # flat id directly, so every transition failed the require-existing RMW
    # lookup and the job landed in failed/. The handler must resolve the real
    # _id by the supply_id field instead.
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    create_supply_need(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    # Rewrite the canonical doc to the production-shaped path-derived _id so the
    # flat supply_need_sup_summit_brightwash id no longer exists.
    path_derived_id = "supply_need_accounts_summitsteel_locations_7050_summit_wire_supplies_sup_summit_brightwash_brightwash"
    for doc in store.docs:
        if doc.get("type") == "supply_need" and doc.get("supply_id") == "sup_summit_brightwash":
            doc["_id"] = path_derived_id
    assert not any(doc.get("_id") == "supply_need_sup_summit_brightwash" for doc in store.docs)
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "action=mark-supply-ordered status=success" in log_text
    resolved = next(doc for doc in store.docs if doc.get("_id") == path_derived_id)
    assert resolved["status"] == "ordered"
    assert not (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()
    assert (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_mark_supply_ordered_records_note_when_present(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path)
    payload = mark_supply_job_payload()
    payload["payload"]["note"] = "Ordered from Staples."
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    assert canonical_doc(doc_id)["ordered_note"] == "Ordered from Staples."


def test_mark_supply_ordered_rejects_when_source_status_is_delivered(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path, status="delivered")
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "status=failure" in log_text
    assert "Cannot transition supply" in log_text
    assert canonical_doc(doc_id)["status"] == "delivered"
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_mark_supply_ordered_rejects_when_file_does_not_exist(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    payload = mark_supply_job_payload(supply_id="sup_missing")
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert "entity_id=supply_need_sup_missing" in log_text
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-supply-ordered.json").exists()


def test_mark_supply_ordered_is_idempotent_on_repeat_job_id(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path)
    payload = mark_supply_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-ordered.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-05-08T18-01-00Z__mark-supply-ordered-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    doc = canonical_doc(doc_id)
    assert doc["status"] == "ordered"
    # Rerun is idempotent: still only the log job + the single mark job recorded.
    assert len(doc.get("btq_job_ids", [])) == 2


def test_mark_supply_delivered_advances_status_from_ordered(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path, status="ordered")
    payload = mark_supply_job_payload("mark_supply_delivered", "job-mark-supply-delivered")
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-delivered.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc(doc_id)
    assert doc["status"] == "delivered"
    assert doc["delivered_by"] == "Jordan"


def test_mark_supply_stocked_advances_status_from_delivered(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path, status="delivered")
    payload = mark_supply_job_payload("mark_supply_stocked", "job-mark-supply-stocked")
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-supply-stocked.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc(doc_id)
    assert doc["status"] == "stocked"
    assert doc["stocked_by"] == "Jordan"


def test_mark_supply_no_action_needed_allowed_from_any_non_terminal(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    for index, status in enumerate(("open", "ordered", "delivered"), start=1):
        supply_id = f"sup_summit_brightwash_{index}"
        doc_id = create_supply_need(project_root, vault_root, runtime_root, log_path, supply_id=supply_id, status=status)
        payload = mark_supply_job_payload("mark_supply_no_action_needed", f"job-mark-supply-no-action-{index}", supply_id)
        write_job(runtime_root / "queue", f"2026-05-08T18-0{index}-00Z__mark-supply-no-action-{index}.json", payload)
        run_jobs(project_root, vault_root, runtime_root, log_path)
        assert canonical_doc(doc_id)["status"] == "no_action_needed"


def test_mark_equipment_approved_advances_status_from_open(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path)
    payload = mark_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-equipment-approved.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "updated" in stdout
    assert "action=mark-equipment-approved status=success" in log_text
    doc = canonical_doc(doc_id)
    assert doc["status"] == "approved"
    assert doc["approved_by"] == "Jordan"


def test_mark_equipment_approved_canonical_patch_failure_keeps_queue_unprocessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_markdown_writes: None,
) -> None:
    class FailingStatusStore:
        def patch_status(
            self,
            doc_id: str,
            status: str,
            extra_fields: dict | None = None,
            *,
            require_existing: bool = False,
        ) -> None:
            raise RuntimeError(f"status patch failed for {doc_id}")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path)
    original_doc = dict(canonical_doc(doc_id))
    monkeypatch.setattr(shared, "_vault_store", lambda: FailingStatusStore())
    payload = mark_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-equipment-approved.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert canonical_doc(doc_id) == original_doc
    assert not (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-equipment-approved.json").exists()
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-equipment-approved.json").exists()


def test_mark_equipment_approved_missing_canonical_doc_fails_job(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    create_equipment_request(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    store.docs = [doc for doc in store.docs if doc.get("_id") != "equipment_request_eqr_summit_vacuum"]
    payload = mark_equipment_job_payload()
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-equipment-approved.json", payload)

    _stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "canonical couchdb write failed" in log_text
    assert "job_type=mark_equipment_approved" in log_text
    assert "job_id=job-mark-equipment-approved" in log_text
    assert "entity_id=equipment_request_eqr_summit_vacuum" in log_text
    assert "CouchDB required document not found for canonical RMW: equipment_request_eqr_summit_vacuum" in log_text
    assert not (runtime_root / "processed" / "2026-05-08T18-00-00Z__mark-equipment-approved.json").exists()
    assert (runtime_root / "failed" / "2026-05-08T18-00-00Z__mark-equipment-approved.json").exists()


def test_mark_equipment_denied_allowed_from_open_or_approved(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    for index, status in enumerate(("open", "approved"), start=1):
        equipment_id = f"eqr_summit_vacuum_{index}"
        doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path, equipment_id=equipment_id, status=status)
        payload = mark_equipment_job_payload("mark_equipment_denied", f"job-mark-equipment-denied-{index}", equipment_id)
        write_job(runtime_root / "queue", f"2026-05-08T18-0{index}-00Z__mark-equipment-denied-{index}.json", payload)
        run_jobs(project_root, vault_root, runtime_root, log_path)
        doc = canonical_doc(doc_id)
        assert doc["status"] == "denied"
        assert doc["denied_by"] == "Jordan"


def test_mark_equipment_ordered_advances_status_from_approved(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path, status="approved")
    payload = mark_equipment_job_payload("mark_equipment_ordered", "job-mark-equipment-ordered")
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-equipment-ordered.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc(doc_id)
    assert doc["status"] == "ordered"
    assert doc["ordered_by"] == "Jordan"


def test_mark_equipment_provided_advances_status_from_ordered(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path, status="ordered")
    payload = mark_equipment_job_payload("mark_equipment_provided", "job-mark-equipment-provided")
    write_job(runtime_root / "queue", "2026-05-08T18-00-00Z__mark-equipment-provided.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    doc = canonical_doc(doc_id)
    assert doc["status"] == "provided"
    assert doc["provided_by"] == "Jordan"


def test_mark_equipment_no_action_needed_allowed_from_any_non_terminal(
    tmp_path: Path,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_summit_wire_site(vault_root)
    for index, status in enumerate(("open", "approved", "ordered"), start=1):
        equipment_id = f"eqr_summit_vacuum_{index}"
        doc_id = create_equipment_request(project_root, vault_root, runtime_root, log_path, equipment_id=equipment_id, status=status)
        payload = mark_equipment_job_payload("mark_equipment_no_action_needed", f"job-mark-equipment-no-action-{index}", equipment_id)
        write_job(runtime_root / "queue", f"2026-05-08T18-0{index}-00Z__mark-equipment-no-action-{index}.json", payload)
        run_jobs(project_root, vault_root, runtime_root, log_path)
        assert canonical_doc(doc_id)["status"] == "no_action_needed"


def test_flag_access_constraint_updates_site_about_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    store = use_recording_vault_store(monkeypatch)
    store.docs.append({"_id": "location_7030", "type": "location", "content": "# Western Gas Transmission\n"})
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-00Z__job-access.json",
        {
            "job_id": "job-access",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": "2026-04-19",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    site_text = recording_doc(store, "location_7030")["content"]

    assert "updated" in stdout
    assert "action=flag-access-constraint status=success" in log_text
    assert "### Access Constraints" in site_text
    assert "2026-04-19 — Only one employee has the badge." in site_text
    canonical_doc = recording_doc(store, "location_7030")
    assert "### Access Constraints" in canonical_doc["content"]
    assert "2026-04-19 — Only one employee has the badge." in canonical_doc["content"]
    assert len(canonical_doc["btq_job_ids"]) == 1
    gap_doc = recording_doc(store, "visit_gap_7030_2026-04-19")
    assert gap_doc["reason"] == "event_without_visit"
    assert "type: visit_gap" not in site_text
    assert store.patch_fields_calls == []
    assert "Only one employee has the badge." not in site_path.read_text(encoding="utf-8")


def test_flag_access_constraint_canonical_patch_failure_keeps_queue_and_projection_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingStore:
        def __init__(self) -> None:
            self.update_doc_calls: list[str] = []

        def find_visit_docs(self, site_id: str, date: str, *, limit: int = 10000) -> list[dict]:
            return [{"_id": "visit_7030_2026-04-19", "type": "visit", "site_id": site_id, "date": date}]

        def get_optional(self, doc_id: str) -> dict | None:
            return {"_id": doc_id, "type": "location", "content": "# Western Gas Transmission\n"} if doc_id == "location_7030" else None

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
            raise RuntimeError(f"missing required doc: {doc_id}")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    original_site_text = site_path.read_text(encoding="utf-8")
    store = FailingStore()
    monkeypatch.setattr(shared, "_vault_store", lambda: store)
    job_path = write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-00Z__job-access.json",
        {
            "job_id": "job-access",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": "2026-04-19",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "failed" in stdout
    assert "canonical couchdb write failed" in log_text
    assert "entity_id=location_7030" in log_text
    assert site_path.read_text(encoding="utf-8") == original_site_text
    assert not job_path.exists()
    assert (runtime_root / "failed" / "2026-04-19T23-01-00Z__job-access.json").exists()
    assert not (runtime_root / "processed" / "2026-04-19T23-01-00Z__job-access.json").exists()
    assert store.update_doc_calls == ["location_7030"]


def test_flag_access_constraint_requires_canonical_site_target(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Warehouse" / "Locations" / "4242 - Warehouse 42" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "4242"),
            ("account", "Warehouse"),
            ("location", "Warehouse 42"),
            ("type", "location"),
            ("site_aliases", "warehouse forty two"),
        ],
        body="# Warehouse 42\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-20T10-00-00Z__job-indexed-site.json",
        {
            "job_id": "job-indexed-site",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Warehouse 42",
                "details": "Gate code changed overnight.",
                "blocking": True,
                "date": "2026-04-20",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_4242")["content"]

    assert "failed" in stdout
    assert "Could not resolve canonical site target: Warehouse 42" in log_text
    assert "Gate code changed overnight." not in site_text


def test_get_active_visit_returns_key_when_canonical_visit_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    store = use_recording_vault_store(monkeypatch)
    seed_canonical_visit(store, site_id="7030", date_value="2026-04-19")
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)

    assert shared.get_active_visit(context, "Western Gas Transmission", "2026-04-19") == shared.build_visit_key(
        "Western Gas Transmission",
        "2026-04-19",
    )


def test_get_active_visit_returns_none_when_no_canonical_visit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    use_recording_vault_store(monkeypatch)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)

    assert shared.get_active_visit(context, "Western Gas Transmission", "2026-04-19") is None


def test_get_active_visit_ignores_markdown_visit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    visit_path = site_path.parent / "Visits" / "2026-04-19.md"
    visit_path.parent.mkdir(parents=True, exist_ok=True)
    visit_path.write_text(
        "---\n"
        "type: visit\n"
        "site: Western Gas Transmission\n"
        "date: 2026-04-19\n"
        "---\n",
        encoding="utf-8",
    )
    use_recording_vault_store(monkeypatch)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)

    assert shared.get_active_visit(context, "Western Gas Transmission", "2026-04-19") is None


def test_event_links_to_existing_visit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    store = use_recording_vault_store(monkeypatch)
    seed_canonical_visit(store, site_id="7030", date_value=today)

    write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-01Z__job-access-linked.json",
        {
            "job_id": "job-access-linked",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": today,
            },
        },
    )

    stdout, _log = run_jobs(project_root, vault_root, runtime_root, log_path)
    site_text = recording_doc(store, "location_7030")["content"]

    assert "updated" in stdout
    assert f'visit_key: "Western Gas Transmission:{today}"' in site_text


def test_event_without_visit(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-02Z__job-access-no-visit.json",
        {
            "job_id": "job-access-no-visit",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": today,
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7030")["content"]

    assert f'visit_key: "Western Gas Transmission:{today}"' not in site_text
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    gap_doc = recording_doc(store, f"visit_gap_7030_{today}")
    assert gap_doc["site"] == "Western Gas Transmission"
    assert gap_doc["site_id"] == "7030"
    assert gap_doc["date"] == today
    assert gap_doc["reason"] == "event_without_visit"
    assert build_visit_gap_text(today) not in site_text


def test_idempotent_event_visit_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    store = use_recording_vault_store(monkeypatch)
    seed_canonical_visit(store, site_id="7030", date_value=today)

    payload = {
        "job_type": "flag_access_constraint",
        "payload": {
            "site": "Western Gas Transmission",
            "details": "Only one employee has the badge.",
            "date": today,
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-01-04Z__job-access-1.json", {"job_id": "job-access-idempotent", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-04-19T23-01-05Z__job-access-2.json", {"job_id": "job-access-idempotent", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)

    site_text = recording_doc(store, "location_7030")["content"]
    assert site_text.count(f'visit_key: "Western Gas Transmission:{today}"') == 1


def test_trigger_recruiting_updates_site_about_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-00Z__job-recruiting.json",
        {
            "job_id": "job-recruiting",
            "job_type": "trigger_recruiting",
            "payload": {
                "site": "Western Gas Transmission",
                "priority": "emergency",
                "details": "Two openings remain on site.",
                "date": "2026-04-19",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7030")["content"]

    assert "updated" in stdout
    assert "action=trigger-recruiting status=success" in log_text
    assert "### Recruiting Triggers" in site_text
    assert "2026-04-19 — priority=emergency — Two openings remain on site." in site_text
    assert "Two openings remain on site." not in site_path.read_text(encoding="utf-8")


def test_trigger_recruiting_patches_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-00Z__job-recruiting.json",
        {
            "job_id": "job-recruiting",
            "job_type": "trigger_recruiting",
            "payload": {
                "site": "Western Gas Transmission",
                "priority": "emergency",
                "details": "Two openings remain on site.",
                "date": "2026-04-19",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "location_7030")["content"]
    assert "### Recruiting Triggers" in canonical_content
    assert "### Recruiting Triggers" not in site_path.read_text(encoding="utf-8")


def test_trigger_recruiting_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    payload = {
        "job_type": "trigger_recruiting",
        "payload": {
            "site": "Western Gas Transmission",
            "priority": "emergency",
            "details": "Two openings remain on site.",
            "date": "2026-04-19",
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-02-10Z__job-recruiting-1.json", {"job_id": "job-recruiting-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)
    write_job(runtime_root / "queue", "2026-04-19T23-02-11Z__job-recruiting-2.json", {"job_id": "job-recruiting-2", **payload})

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7030")["content"]

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert site_text.count("2026-04-19 — priority=emergency — Two openings remain on site.") == 1


def prepare_close_recruiting_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    person_path = vault_root / "People" / "Pearson, David.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    write_frontmatter_file(
        person_path,
        [
            ("name", "David Pearson"),
            ("first", "David"),
            ("last", "Pearson"),
            ("type", "employee"),
            ("status", "active"),
        ],
        body="# David Pearson\n\n## Schedule Changes\n\n",
    )
    return project_root, vault_root, runtime_root, log_path, site_path, person_path


def close_recruiting_job_payload(
    job_id: str = "job-close-recruiting",
    *,
    outcome: str = "cancelled",
    filled_by: Optional[str] = None,
    site: str = "Western Gas Transmission",
) -> dict:
    payload = {
        "site": site,
        "outcome": outcome,
        "date": "2026-04-19",
        "notes": "Coverage plan changed.",
    }
    if filled_by is not None:
        payload["filled_by"] = filled_by
    return {
        "job_id": job_id,
        "job_type": "close_recruiting",
        "payload": payload,
    }


def test_close_recruiting_appends_closure_entry_to_site_about(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, site_path, _person_path = prepare_close_recruiting_fixture(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-20Z__job-close-recruiting.json",
        close_recruiting_job_payload(),
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7030")["content"]

    assert "updated" in stdout
    assert "action=close-recruiting status=success" in log_text
    assert "### Recruiting Closed" in site_text
    assert "2026-04-19 — outcome=cancelled — Coverage plan changed." in site_text


def test_close_recruiting_filled_appends_to_people_note(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, _site_path, person_path = prepare_close_recruiting_fixture(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-21Z__job-close-filled.json",
        close_recruiting_job_payload("job-close-filled", outcome="filled", filled_by="Pearson, David"),
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    person_text = recording_doc(store, "employee_pearson_david")["content"]

    assert "## Schedule Changes" in person_text
    assert "2026-04-19 — placed at Western Gas Transmission — Coverage plan changed." in person_text


def test_close_recruiting_patches_site_and_employee_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, site_path, person_path = prepare_close_recruiting_fixture(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-21Z__job-close-filled.json",
        close_recruiting_job_payload("job-close-filled", outcome="filled", filled_by="Pearson, David"),
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_content = recording_doc(store, "location_7030")["content"]
    employee_content = recording_doc(store, "employee_pearson_david")["content"]
    assert "### Recruiting Closed" in site_content
    assert "2026-04-19 — placed at Western Gas Transmission — Coverage plan changed." in employee_content
    assert "### Recruiting Closed" not in site_path.read_text(encoding="utf-8")
    assert "placed at Western Gas Transmission" not in person_path.read_text(encoding="utf-8")


def test_close_recruiting_cancelled_skips_people_note(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, _site_path, person_path = prepare_close_recruiting_fixture(tmp_path)
    before_text = person_path.read_text(encoding="utf-8")
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-22Z__job-close-cancelled.json",
        close_recruiting_job_payload("job-close-cancelled", outcome="cancelled"),
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    assert person_path.read_text(encoding="utf-8") == before_text


def test_close_recruiting_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, site_path, person_path = prepare_close_recruiting_fixture(tmp_path)
    payload = close_recruiting_job_payload("job-close-idempotent", outcome="filled", filled_by="Pearson, David")
    write_job(runtime_root / "queue", "2026-04-19T23-02-23Z__job-close-1.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    write_job(runtime_root / "queue", "2026-04-19T23-02-24Z__job-close-2.json", payload)

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    site_text = recording_doc(store, "location_7030")["content"]
    person_text = recording_doc(store, "employee_pearson_david")["content"]

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert site_text.count("2026-04-19 — outcome=filled — filled_by=Pearson, David — Coverage plan changed.") == 1
    assert person_text.count("2026-04-19 — placed at Western Gas Transmission — Coverage plan changed.") == 1


def test_close_recruiting_rejects_filled_without_filled_by() -> None:
    assert not validate_job(close_recruiting_job_payload(outcome="filled"))


def test_close_recruiting_rejects_invalid_outcome() -> None:
    assert not validate_job(close_recruiting_job_payload(outcome="foo"))


def test_close_recruiting_site_identity_normalization_matches_trigger_recruiting(tmp_path: Path) -> None:
    site_forms = ("Western Gas Transmission", "7030", "7030 - Western Gas Transmission")
    for index, site in enumerate(site_forms):
        project_root, vault_root, runtime_root, log_path, site_path, _person_path = prepare_close_recruiting_fixture(tmp_path / f"case-{index}")
        write_job(
            runtime_root / "queue",
            f"2026-04-19T23-02-3{index}Z__job-close-site-{index}.json",
            close_recruiting_job_payload(f"job-close-site-{index}", site=site),
        )

        run_jobs(project_root, vault_root, runtime_root, log_path)

        store = shared._VAULT_STORE
        assert isinstance(store, RecordingVaultStore)
        assert "2026-04-19 — outcome=cancelled — Coverage plan changed." in recording_doc(store, "location_7030")["content"]


def test_close_recruiting_writes_evidence(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path, site_path, _person_path = prepare_close_recruiting_fixture(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-02-40Z__job-close-evidence.json",
        {
            **close_recruiting_job_payload("job-close-evidence"),
            "metadata": {"capture_id": "cap-close-recruiting"},
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    evidence_files = sorted((runtime_root / "evidence" / "cap-close-recruiting").glob("*.json"))
    assert evidence_files
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in evidence_files)
    evidence_records = [json.loads(path.read_text(encoding="utf-8")) for path in evidence_files]
    # Evidence records the canonical location doc id, not the Markdown site path (C3-316d).
    assert '"target_doc_id": "location_' in evidence_text
    assert '"handler_type": "close_recruiting"' in evidence_text
    assert any(
        record["epistemic_state"]["derived_from"] == "2026-04-19 — outcome=cancelled — Coverage plan changed."
        for record in evidence_records
    )


def test_remove_from_schedule_updates_employee_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "People" / "Nash, Peter.md",
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-03-00Z__job-remove.json",
        {
            "job_id": "job-remove",
            "job_type": "remove_from_schedule",
            "payload": {
                "employee": "Peter Nash",
                "site": "Western Gas Transmission",
                "date": "2026-04-19",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employee_text = recording_doc(store, "employee_nash_peter")["content"]

    assert "updated" in stdout
    assert "action=remove-from-schedule status=success" in log_text
    assert "## Schedule Changes" in employee_text
    assert "2026-04-19 — removed from schedule for Western Gas Transmission" in employee_text


def test_remove_from_schedule_patches_employee_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    employee_path = vault_root / "People" / "Nash, Peter.md"
    write_frontmatter_file(
        employee_path,
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-03-00Z__job-remove.json",
        {
            "job_id": "job-remove",
            "job_type": "remove_from_schedule",
            "payload": {
                "employee": "Peter Nash",
                "site": "Western Gas Transmission",
                "date": "2026-04-19",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "employee_nash_peter")["content"]
    assert "2026-04-19 — removed from schedule for Western Gas Transmission" in canonical_content
    assert "removed from schedule" not in employee_path.read_text(encoding="utf-8")


def test_remove_from_schedule_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "People" / "Nash, Peter.md",
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    payload = {
        "job_type": "remove_from_schedule",
        "payload": {
            "employee": "Peter Nash",
            "site": "Western Gas Transmission",
            "date": "2026-04-19",
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-03-10Z__job-remove-1.json", {"job_id": "job-remove-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)
    write_job(runtime_root / "queue", "2026-04-19T23-03-11Z__job-remove-2.json", {"job_id": "job-remove-2", **payload})

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employee_text = recording_doc(store, "employee_nash_peter")["content"]

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert employee_text.count("2026-04-19 — removed from schedule for Western Gas Transmission") == 1


def test_flag_retention_risk_updates_employee_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "People" / "Nash, Peter.md",
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-00Z__job-retention.json",
        {
            "job_id": "job-retention",
            "job_type": "flag_retention_risk",
            "payload": {
                "employee": "Peter Nash",
                "site": "Western Gas Transmission",
                "details": "May leave if evening load stays unchanged.",
                "date": "2026-04-19",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employee_text = recording_doc(store, "employee_nash_peter")["content"]

    assert "updated" in stdout
    assert "action=flag-retention-risk status=success" in log_text
    assert "## Retention Risks" in employee_text
    assert "2026-04-19 — Western Gas Transmission — May leave if evening load stays unchanged." in employee_text


def test_flag_retention_risk_patches_employee_canonical_content(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    employee_path = vault_root / "People" / "Nash, Peter.md"
    write_frontmatter_file(
        employee_path,
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-00Z__job-retention.json",
        {
            "job_id": "job-retention",
            "job_type": "flag_retention_risk",
            "payload": {
                "employee": "Peter Nash",
                "site": "Western Gas Transmission",
                "details": "May leave if evening load stays unchanged.",
                "date": "2026-04-19",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    canonical_content = recording_doc(store, "employee_nash_peter")["content"]
    assert "## Retention Risks" in canonical_content
    assert "May leave if evening load stays unchanged." not in employee_path.read_text(encoding="utf-8")


def test_flag_retention_risk_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "People" / "Nash, Peter.md",
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    payload = {
        "job_type": "flag_retention_risk",
        "payload": {
            "employee": "Peter Nash",
            "site": "Western Gas Transmission",
            "details": "May leave if evening load stays unchanged.",
            "date": "2026-04-19",
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-04-10Z__job-retention-1.json", {"job_id": "job-retention-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)
    write_job(runtime_root / "queue", "2026-04-19T23-04-11Z__job-retention-2.json", {"job_id": "job-retention-2", **payload})

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employee_text = recording_doc(store, "employee_nash_peter")["content"]

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert employee_text.count("2026-04-19 — Western Gas Transmission — May leave if evening load stays unchanged.") == 1


def test_observations_do_not_appear_in_people_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "People" / "Nash, Peter.md",
        [
            ("name", "Peter Nash"),
            ("first", "Peter"),
            ("last", "Nash"),
            ("type", "employee"),
            ("status", "active"),
            ("status_date", "2026-04-01"),
        ],
        body="# Peter Nash\n",
    )
    event = {
        "event_id": "evt-retention-observed-1",
        "type": "employee_retention_risk",
        "employee": "Peter Nash",
        "site": "Western Gas Transmission",
        "details": "May leave if evening load stays unchanged.",
        "confidence": "medium",
        "timestamp": "2026-04-20T08:00:00Z",
        "source_excerpt": "May leave if evening load stays unchanged.",
        "observations": [
            {
                "type": "Candidate volunteered denial of drug and alcohol issues without prompting",
                "confidence": "observed",
            }
        ],
    }
    write_job(runtime_root / "queue", "2026-04-20T23-04-30Z__job-retention-observed.json", event_to_job(event))

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    employee_text = recording_doc(store, "employee_nash_peter")["content"]

    assert "updated" in stdout
    assert "action=flag-retention-risk status=success" in log_text
    assert "2026-04-20 — Western Gas Transmission — May leave if evening load stays unchanged." in employee_text
    assert "Observation:" not in employee_text
    assert "drug and alcohol issues" not in employee_text


def test_crash_after_write_before_move_reruns_safely(tmp_path: Path, monkeypatch) -> None:
    store = use_recording_vault_store(monkeypatch)
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    job_path = write_job(
        runtime_root / "queue",
        "2026-04-20T23-34-00Z__job-crash.json",
        {
            "job_id": "job-crash-before-move",
            "job_type": "append_to_note",
            "payload": {
                "path": "Journal/2026-04-20.md",
                "content": "Crash-safe queue note.",
                "destination": "journal",
            },
        },
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    processed_dir = runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    job = qp.load_job(job_path)
    original_move_job_file = shared.move_job_file

    def crash_after_write(_job_path: Path, _destination_dir: Path) -> Path:
        raise RuntimeError("simulated crash before move")

    monkeypatch.setattr(shared, "move_job_file", crash_after_write)
    try:
        try:
            qp.process_append_to_note_job(job_path, job, context, processed_dir)
        except RuntimeError as exc:
            assert str(exc) == "simulated crash before move"
    finally:
        monkeypatch.setattr(shared, "move_job_file", original_move_job_file)

    assert job_path.exists()
    assert recording_doc(store, "note_journal_2026-04-20")["content"].count("Crash-safe queue note.") == 1
    assert store.job_id_applied_doc_id(job.job_id) == "note_journal_2026-04-20"

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id marker already present" in stdout
    assert "reason=job-id-marker-present" in log_text
    assert recording_doc(store, "note_journal_2026-04-20")["content"].count("Crash-safe queue note.") == 1


def test_visit_create_crash_after_write_before_move_reruns_safely(
    tmp_path: Path,
    monkeypatch,
    legacy_markdown_writes: None,
) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    store = use_recording_vault_store(monkeypatch)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    store.docs.append(
        {
            "_id": "location_7030",
            "type": "location",
            "site_id": "7030",
            "job": "7030",
            "location": "Western Gas Transmission",
            "account": "Wgtco",
            "vault_path": str(site_path),
        }
    )
    job_path = write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-29Z__job-visit-crash.json",
        {
            "job_id": "job-visit-crash",
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "high",
                "source": "ingestion",
                "evidence": "I was at Western Gas Transmission.",
            },
        },
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    processed_dir = runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    job = qp.load_job(job_path)
    original_move_job_file = shared.move_job_file

    def crash_after_write(_job_path: Path, _destination_dir: Path) -> Path:
        raise RuntimeError("simulated crash before move")

    monkeypatch.setattr(shared, "move_job_file", crash_after_write)
    try:
        try:
            qp.process_visit_create_job(job_path, job, context, processed_dir)
        except RuntimeError as exc:
            assert str(exc) == "simulated crash before move"
    finally:
        monkeypatch.setattr(shared, "move_job_file", original_move_job_file)

    assert job_path.exists()
    assert store.job_id_applied_doc_id(job.job_id) is not None
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert len(visit_docs) == 1
    assert visit_docs[0]["evidence"] == "I was at Western Gas Transmission."

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id marker already present" in stdout
    assert "reason=job-id-marker-present" in log_text
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert len(visit_docs) == 1


def test_visit_create_basic(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-30Z__job-visit-create.json",
        {
            "job_id": "job-visit-create",
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "high",
                "source": "ingestion",
                "evidence": "I was at Western Gas Transmission.",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    # Visit doc id suffix is the computed job_id[:8] (a hash), so locate by type+site_id.
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert len(visit_docs) == 1
    visit_doc = visit_docs[0]

    assert "action=visit-create status=success" in log_text
    assert visit_doc["type"] == "visit"
    assert visit_doc["site"] == "Western Gas Transmission"
    assert visit_doc["site_id"] == "7030"
    assert visit_doc["date"] == datetime.utcnow().date().isoformat()
    assert visit_doc["visit_key"] == f"Western Gas Transmission:{datetime.utcnow().date().isoformat()}"
    assert visit_doc["source"] == "ingestion"
    assert visit_doc["confidence"] == "high"
    assert visit_doc["evidence"] == "I was at Western Gas Transmission."


def test_visit_create_writes_visit_type_to_vault(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-30Z__job-visit-create-qc.json",
        {
            "job_id": "job-visit-create-qc",
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "medium",
                "source": "fca_qc",
                "visit_type": "qc_inspection",
                "evidence": "QC inspection walk-through completed.",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert len(visit_docs) == 1

    assert visit_docs[0]["visit_type"] == "qc_inspection"


def test_visit_create_writes_visited_by_to_vault(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-30Z__job-visit-create-person.json",
        {
            "job_id": "job-visit-create-person",
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "medium",
                "source": "fca_person",
                "visited_by": "per_test006",
                "evidence": "Service completion visit.",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert len(visit_docs) == 1

    assert visit_docs[0]["visited_by"] == "per_test006"


def test_visit_create_without_visited_by_still_validates() -> None:
    assert validate_job(
        {
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "medium",
                "source": "fca_visit",
                "evidence": "Service completion visit.",
            },
        }
    )


def test_visit_create_idempotent(tmp_path: Path, legacy_markdown_writes: None) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    payload = {
        "job_type": "visit_create",
        "payload": {
            "site": "Western Gas Transmission",
            "confidence": "medium",
            "source": "ingestion",
            "evidence": "I was at Western Gas Transmission.",
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-04-31Z__job-visit-create-1.json", {"job_id": "job-visit-create-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-04-19T23-04-32Z__job-visit-create-2.json", {"job_id": "job-visit-create-2", **payload})
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    visit_docs = [doc for doc in store.docs if doc.get("type") == "visit" and doc.get("site_id") == "7030"]
    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert len(visit_docs) == 1
    assert visit_docs[0]["evidence"] == "I was at Western Gas Transmission."


def test_visit_create_invalid_payload(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-04-33Z__job-visit-invalid.json",
        {
            "job_id": "job-visit-invalid",
            "job_type": "visit_create",
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "high",
                "source": "ingestion",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "Job does not match queue_spec" in stdout
    assert "Job does not match queue_spec" in log_text
    assert (runtime_root / "failed" / "2026-04-19T23-04-33Z__job-visit-invalid.json").exists()


def test_parse_supply_email_creates_canonical_supply_order_with_operator(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_apex_site(vault_root)
    write_supply_email(project_root)
    store = RmwRecordingVaultStore()
    shared._VAULT_STORE = store
    processed_dir, failed_dir = make_processor_dirs(runtime_root, log_path)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    payload = parse_supply_email_payload()
    queue_file = write_job(runtime_root / "queue", "2026-04-20T23-10-00Z__job-supply-canonical.json", {"job_id": "job-supply-canonical", **payload})

    # Site resolution now reads canonical location docs (C3-313), not the Markdown vault.
    store.docs.append({
        "_id": "location_7080",
        "type": "location",
        "job": "7080",
        "account": "Apexco",
        "location": "Apex Powdered Metals",
        "address": "700 Martha St, Springfield, PA 00000",
        "site_aliases": ["Apex Powdered Metals", "Apex"],
        "monthly_supply_budget": "250.00",
        "budget_basis": "monthly_actual",
    })

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = recording_doc(store, "supply_order_99887766")
    assert doc["type"] == "supply_order"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["order_number"] == "99887766"
    assert doc["site_id"] == "7080"
    assert doc["account"] == "Apexco"
    assert doc["total"] == 65.7
    assert doc["btq_job_ids"] == [qp.compute_job_id(payload)]
    assert (processed_dir / queue_file.name).exists()
    assert not (failed_dir / queue_file.name).exists()


def test_parse_supply_email_unresolved_order_still_writes_canonical_doc(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    unresolved_html = (
        SUPPLY_EMAIL_HTML
        .replace("99887766", "11223344")
        .replace("Apex Powdered Metals", "Unknown Facility")
        .replace("700 Martha St", "1 Unknown Rd")
        .replace("Springfield, PA 00000", "Nowhere, PA 00000")
    )
    write_supply_email(project_root, unresolved_html, filename="staples-order-11223344.html")
    store = RmwRecordingVaultStore()
    shared._VAULT_STORE = store
    processed_dir, failed_dir = make_processor_dirs(runtime_root, log_path)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    payload = parse_supply_email_payload("staples-order-11223344.html", "Staples order confirmation 11223344")
    queue_file = write_job(runtime_root / "queue", "2026-04-20T23-10-00Z__job-supply-unresolved.json", {"job_id": "job-supply-unresolved", **payload})

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    doc = recording_doc(store, "supply_order_11223344")
    assert doc["type"] == "supply_order"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["site_id"] is None
    assert "unresolved_site" in str(doc["unresolved_reason"])
    quarantine_path = project_root.parent / "data" / "supply_orders" / "quarantine" / "2026" / "04" / "11223344.json"
    assert quarantine_path.exists()
    assert (processed_dir / queue_file.name).exists()
    assert not (failed_dir / queue_file.name).exists()


def test_parse_supply_email_replay_skips_without_repersist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_apex_site(vault_root)
    write_supply_email(project_root)
    store = RmwRecordingVaultStore()
    store.docs = [
        {
            "_id": "supply_order_99887766",
            "type": "supply_order",
            "operator": OPERATOR_ID_GREG,
            "order_number": "99887766",
            "btq_job_ids": ["job-one"],
        }
    ]
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    processed_dir = runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    queue_file = runtime_root / "queue" / "job-one.json"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text("{}\n", encoding="utf-8")

    qp.process_parse_supply_email_job(
        queue_file,
        qp.QueueJob(job_id="job-one", job_type="parse_supply_email", payload=parse_supply_email_payload()["payload"], metadata={}, intent={}),
        context,
        processed_dir,
    )

    assert (processed_dir / queue_file.name).exists()
    assert store.get_optional("supply_order_99887766") == store.docs[0]
    assert not (project_root.parent / "data" / "supply_orders" / "2026" / "04" / "99887766.json").exists()


def test_parse_supply_email_store_error_fails_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingSupplyOrderStore(RmwRecordingVaultStore):
        def get_optional(self, doc_id: str) -> dict[str, Any] | None:
            raise RuntimeError("couchdb unavailable")

    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_apex_site(vault_root)
    write_supply_email(project_root)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingSupplyOrderStore())
    processed_dir, failed_dir = make_processor_dirs(runtime_root, log_path)
    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    payload = parse_supply_email_payload()
    queue_file = write_job(runtime_root / "queue", "2026-04-20T23-10-00Z__job-supply-store-error.json", {"job_id": "job-supply-store-error", **payload})

    qp.process_job(queue_file, context, processed_dir, failed_dir)

    assert (failed_dir / queue_file.name).exists()
    assert not (processed_dir / queue_file.name).exists()
    log_text = log_path.read_text(encoding="utf-8")
    assert "canonical couchdb write failed job_type=parse_supply_email job_id=" in log_text
    assert "entity_id=supply_order_99887766" in log_text


def test_parse_supply_email_job_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_apex_site(vault_root)
    write_supply_email(project_root)

    payload = parse_supply_email_payload()
    write_job(runtime_root / "queue", "2026-04-20T23-10-00Z__job-supply-1.json", {"job_id": "job-supply-1", **payload})
    stdout_one, log_text_one = run_jobs(project_root, vault_root, runtime_root, log_path)

    data_path = project_root.parent / "data" / "supply_orders" / "2026" / "04" / "99887766.json"

    assert "updated" in stdout_one
    assert "action=parse-supply-email status=success" in log_text_one
    assert data_path.exists()
    # Supply-order Markdown projection retired in C3-313 (it derived its path from the removed
    # SiteMetadata.about_path); the canonical supply_order doc + JSON record are the record now.
    stored = json.loads(data_path.read_text(encoding="utf-8"))
    assert stored["site_id"] == "7080"
    assert stored["account"] == "Apexco"
    assert stored["total"] == 65.7

    write_job(runtime_root / "queue", "2026-04-20T23-10-01Z__job-supply-2.json", {"job_id": "job-supply-2", **payload})
    stdout_two, log_text_two = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id already processed" in stdout_two
    assert "reason=job-id-already-processed" in log_text_two
    assert len(list((project_root.parent / "data" / "supply_orders" / "2026" / "04").glob("*.json"))) == 1


def test_mixed_job_order_produces_same_final_state(tmp_path: Path) -> None:
    def prepare_case(root: Path) -> tuple[Path, Path, Path, Path]:
        project_root, vault_root, runtime_root, log_path = make_roots(root)
        write_frontmatter_file(
            vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
            [
                ("job", "7030"),
                ("account", "Wgtco"),
                ("location", "Western Gas Transmission"),
                ("type", "location"),
            ],
            body="# Western Gas Transmission\n\n## Operational Notes\n",
        )
        write_frontmatter_file(
            vault_root / "People" / "Nash, Peter.md",
            [
                ("name", "Peter Nash"),
                ("first", "Peter"),
                ("last", "Nash"),
                ("type", "employee"),
                ("status", "active"),
                ("status_date", "2026-04-01"),
            ],
            body="# Peter Nash\n",
        )
        return project_root, vault_root, runtime_root, log_path

    case_a = prepare_case(tmp_path / "case-a")
    case_b = prepare_case(tmp_path / "case-b")

    jobs = [
        (
            "2026-04-19T23-20-00Z__job-recruiting.json",
            {
                "job_id": "case-recruiting",
                "job_type": "trigger_recruiting",
                "payload": {
                    "site": "Western Gas Transmission",
                    "priority": "high",
                    "details": "Coverage gap remains open.",
                    "date": "2026-04-19",
                },
            },
        ),
        (
            "2026-04-19T23-20-01Z__job-retention.json",
            {
                "job_id": "case-retention",
                "job_type": "flag_retention_risk",
                "payload": {
                    "employee": "Peter Nash",
                    "site": "Western Gas Transmission",
                    "details": "May leave if evening load stays unchanged.",
                    "date": "2026-04-19",
                },
            },
        ),
    ]

    for filename, payload in jobs:
        write_job(case_a[2] / "queue", filename, payload)
    run_jobs(*case_a)

    for filename, payload in reversed(jobs):
        write_job(case_b[2] / "queue", filename, payload)
    run_jobs(*case_b)

    site_a = (case_a[1] / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md").read_text(encoding="utf-8")
    site_b = (case_b[1] / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md").read_text(encoding="utf-8")
    employee_a = (case_a[1] / "People" / "Nash, Peter.md").read_text(encoding="utf-8")
    employee_b = (case_b[1] / "People" / "Nash, Peter.md").read_text(encoding="utf-8")

    assert site_a == site_b
    assert employee_a == employee_b


def test_reclassify_unknown_job_processes_single_unknown_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    write_unknown_capture(
        unknown_path,
        timestamp="2026-04-19T10:00:00+00:00",
        audio_file="targeted.m4a",
        normalized_text="Diamond textured metal stalls are difficult to clean and show marks.",
        notes="#unknown #needs-review\n#site: Western Gas Transmission",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp="2026-04-19T10:00:00+00:00",
        audio_file="targeted.m4a",
        normalized_text="Diamond textured metal stalls are difficult to clean and show marks.",
        notes="#unknown #needs-review\n#site: Western Gas Transmission",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-05-00Z__job-reclassify.json",
        {
            "job_id": "job-reclassify",
            "job_type": "reclassify_unknown",
            "payload": {
                "path": "Journal/2026-04-19-unknown.md",
            },
        },
    )

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)
    queue_jobs = sorted((runtime_root / "queue").glob("*.json"))

    assert "action=reclassify-unknown status=success" in log_text
    assert (runtime_root / "processed" / "2026-04-19T23-05-00Z__job-reclassify.json").exists()
    assert len(queue_jobs) == 1
    derived_job = json.loads(queue_jobs[0].read_text(encoding="utf-8"))
    assert derived_job["payload"]["source_unknown_id"]
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["status"] == "resolved"


def test_reclassify_unknown_job_is_idempotent(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    payload = {
        "job_type": "reclassify_unknown",
        "payload": {
            "path": "Journal/2026-04-19-unknown.md",
        },
    }
    write_unknown_capture(
        unknown_path,
        timestamp="2026-04-19T10:00:00+00:00",
        audio_file="targeted.m4a",
        normalized_text="Diamond textured metal stalls are difficult to clean and show marks.",
        notes="#unknown #needs-review\n#site: Western Gas Transmission",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp="2026-04-19T10:00:00+00:00",
        audio_file="targeted.m4a",
        normalized_text="Diamond textured metal stalls are difficult to clean and show marks.",
        notes="#unknown #needs-review\n#site: Western Gas Transmission",
    )
    write_job(runtime_root / "queue", "2026-04-19T23-05-10Z__job-reclassify-1.json", {"job_id": "job-reclassify-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)
    write_job(runtime_root / "queue", "2026-04-19T23-05-11Z__job-reclassify-2.json", {"job_id": "job-reclassify-2", **payload})

    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    matching_jobs = []
    for directory_name in ("queue", "processed", "failed"):
        for candidate in sorted((runtime_root / directory_name).glob("*.json")):
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if payload.get("payload", {}).get("source_unknown_id"):
                matching_jobs.append(candidate)
    assert len(matching_jobs) == 1
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["status"] == "resolved"


def test_process_unknowns_retry_triggers_without_edit(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    timestamp = "2026-04-19T10:00:00+00:00"
    write_unknown_capture(
        unknown_path,
        timestamp=timestamp,
        audio_file="missed.m4a",
        normalized_text="This remains an unclassified walkthrough note.",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp=timestamp,
        audio_file="missed.m4a",
        normalized_text="This remains an unclassified walkthrough note.",
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    created_jobs = qp.process_unknowns(context)

    assert created_jobs == 0
    assert sorted((runtime_root / "queue").glob("*.json")) == []
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["status"] == "unresolved"
    assert canonical_doc["retry_count"] == 1


def test_process_unknowns_does_not_treat_own_retry_update_as_edit(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    timestamp = "2026-04-19T10:00:00+00:00"
    write_unknown_capture(
        unknown_path,
        timestamp=timestamp,
        audio_file="missed.m4a",
        normalized_text="This remains an unclassified walkthrough note.",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp=timestamp,
        audio_file="missed.m4a",
        normalized_text="This remains an unclassified walkthrough note.",
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    first_created = qp.process_unknowns(context)
    second_created = qp.process_unknowns(context)

    assert first_created == 0
    assert second_created == 0
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["retry_count"] == 1
    assert sorted((runtime_root / "queue").glob("*.json")) == []


def test_process_unknowns_backoff_is_respected(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    timestamp = "2026-04-19T10:00:00+00:00"
    recent_attempt = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
    write_unknown_capture(
        unknown_path,
        timestamp=timestamp,
        audio_file="backoff.m4a",
        normalized_text="This remains ambiguous.",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp=timestamp,
        audio_file="backoff.m4a",
        normalized_text="This remains ambiguous.",
        retry_count=1,
        last_attempted=recent_attempt,
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    created_jobs = qp.process_unknowns(context)

    assert created_jobs == 0
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["last_attempted"] == recent_attempt
    assert canonical_doc["retry_count"] == 1
    assert sorted((runtime_root / "queue").glob("*.json")) == []


def test_process_unknowns_retry_after_delay(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    timestamp = "2026-04-19T10:00:00+00:00"
    stale_attempt = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    write_unknown_capture(
        unknown_path,
        timestamp=timestamp,
        audio_file="delayed.m4a",
        normalized_text="Still ambiguous after first attempt.",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp=timestamp,
        audio_file="delayed.m4a",
        normalized_text="Still ambiguous after first attempt.",
        retry_count=1,
        last_attempted=stale_attempt,
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    created_jobs = qp.process_unknowns(context)

    assert created_jobs == 0
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["retry_count"] == 2
    assert canonical_doc["last_attempted"] != stale_attempt


def test_process_unknowns_successful_delayed_resolution(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    write_frontmatter_file(
        vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md",
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n",
    )
    unknown_path = vault_root / "Journal" / "2026-04-19-unknown.md"
    timestamp = "2026-04-19T10:00:00+00:00"
    write_unknown_capture(
        unknown_path,
        timestamp=timestamp,
        audio_file="partial.m4a",
        normalized_text="A note with no extractable event yet.",
    )
    store = seed_projection_docs_as_canonical(vault_root)
    doc_id = seed_unknown_capture_doc(
        store,
        journal_path="Journal/2026-04-19-unknown.md",
        timestamp=timestamp,
        audio_file="partial.m4a",
        normalized_text="A note with no extractable event yet.",
    )

    context = build_context(project_root, vault_root, runtime_root, log_path, dry_run=False)
    first_created = qp.process_unknowns(context)
    first_doc = store.get_optional(doc_id)

    assert first_created == 0
    assert first_doc is not None
    assert first_doc["status"] == "unresolved"
    assert first_doc["retry_count"] == 1

    stale_attempt = (datetime.utcnow() - timedelta(hours=3)).isoformat()

    def update_unknown_doc(current: dict[str, Any] | None) -> dict[str, Any] | None:
        assert current is not None
        outgoing = dict(current)
        outgoing["normalized_transcript"] = "Diamond textured metal stalls are difficult to clean and show marks."
        outgoing["notes"] = "#unknown #needs-review\n#site: Western Gas Transmission"
        outgoing["last_attempted"] = stale_attempt
        return outgoing

    store.update_doc(doc_id, update_unknown_doc, require_existing=True)

    second_created = qp.process_unknowns(context)
    queue_jobs = sorted((runtime_root / "queue").glob("*.json"))

    assert second_created >= 1
    assert len(queue_jobs) == second_created
    canonical_doc = store.get_optional(doc_id)
    assert canonical_doc is not None
    assert canonical_doc["status"] == "resolved"
    assert canonical_doc["resolved_at"]
    assert canonical_doc["resolved_site"] == "Western Gas Transmission"
    assert any(json.loads(path.read_text(encoding="utf-8"))["payload"]["source_unknown_id"] for path in queue_jobs)

    third_created = qp.process_unknowns(context)
    assert third_created == 0
    assert len(sorted((runtime_root / "queue").glob("*.json"))) == second_created


def build_visit_gap_text(date_value: str) -> str:
    return (
        "---\n"
        "type: visit_gap\n"
        "site: Western Gas Transmission\n"
        f"date: {date_value}\n"
        'reason: "event_without_visit"\n'
        "---\n"
    )


def test_visit_gap_created_when_event_without_visit(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-06Z__job-gap.json",
        {
            "job_id": "job-gap",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": today,
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    site_text = site_path.read_text(encoding="utf-8")

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    gap_doc = recording_doc(store, f"visit_gap_7030_{today}")
    assert gap_doc["site"] == "Western Gas Transmission"
    assert gap_doc["site_id"] == "7030"
    assert gap_doc["date"] == today
    assert gap_doc["reason"] == "event_without_visit"
    assert build_visit_gap_text(today) not in site_text


def test_append_to_site_note_writes_canonical_visit_gap_without_markdown_block(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    field_capture_content = (
        "---\n\n"
        "## Field Capture Reviews\n\n"
        "### Field Capture Review - 2026-05-04T23:33:00+00:00\n"
        "- field_capture_timestamp: 2026-05-04T23:33:00+00:00\n"
        "- site_id: 7030\n"
        "- area: Unknown\n\n"
        "Summary: Reference photo should be preserved.\n"
    )
    write_job(
        runtime_root / "queue",
        "2026-05-04T23-33-00Z__job-field-capture.json",
        {
            "job_id": "job-field-capture",
            "job_type": "append_to_note",
            "metadata": {"source": "approved_job_draft", "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca.json"},
            "payload": {
                "path": "Accounts/Wgtco/Locations/7030 - Western Gas Transmission/about.md",
                "content": field_capture_content,
                "destination": "site_note",
            },
        },
    )

    run_jobs(project_root, vault_root, runtime_root, log_path)
    site_text = site_path.read_text(encoding="utf-8")

    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    gap_doc = recording_doc(store, f"visit_gap_7030_{today}")
    assert gap_doc["site"] == "Western Gas Transmission"
    assert gap_doc["site_id"] == "7030"
    assert gap_doc["date"] == today
    assert gap_doc["operator"] == OPERATOR_ID_GREG
    assert build_visit_gap_text(today) not in site_text
    location_doc = recording_doc(store, "location_7030")
    assert "\n\n---\n\n## Field Capture Reviews" in location_doc["content"]


def test_no_duplicate_visit_gap(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    payload = {
        "job_type": "flag_access_constraint",
        "payload": {
            "site": "Western Gas Transmission",
            "details": "Only one employee has the badge.",
            "date": today,
        },
    }
    write_job(runtime_root / "queue", "2026-04-19T23-01-07Z__job-gap-1.json", {"job_id": "job-gap-1", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)

    write_job(runtime_root / "queue", "2026-04-19T23-01-08Z__job-gap-2.json", {"job_id": "job-gap-2", **payload})
    run_jobs(project_root, vault_root, runtime_root, log_path)

    site_text = site_path.read_text(encoding="utf-8")
    store = shared._VAULT_STORE
    assert isinstance(store, RecordingVaultStore)
    gap_docs = [doc for doc in store.docs if doc.get("_id") == f"visit_gap_7030_{today}"]
    assert len(gap_docs) == 1
    assert len(gap_docs[0]["btq_job_ids"]) == 1
    assert site_text.count(build_visit_gap_text(today)) == 0


def test_no_visit_gap_when_visit_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    today = datetime.utcnow().date().isoformat()
    site_path = vault_root / "Accounts" / "Wgtco" / "Locations" / "7030 - Western Gas Transmission" / "about.md"
    write_frontmatter_file(
        site_path,
        [
            ("job", "7030"),
            ("account", "Wgtco"),
            ("location", "Western Gas Transmission"),
            ("type", "location"),
        ],
        body="# Western Gas Transmission\n\n## Operational Notes\n",
    )
    store = use_recording_vault_store(monkeypatch)
    seed_canonical_visit(store, site_id="7030", date_value=today)

    write_job(
        runtime_root / "queue",
        "2026-04-19T23-01-10Z__job-gap-no-gap.json",
        {
            "job_id": "job-gap-no-gap",
            "job_type": "flag_access_constraint",
            "payload": {
                "site": "Western Gas Transmission",
                "details": "Only one employee has the badge.",
                "date": today,
            },
        },
    )
    run_jobs(project_root, vault_root, runtime_root, log_path)

    site_text = site_path.read_text(encoding="utf-8")
    assert build_visit_gap_text(today) not in site_text
    assert recording_doc(store, "visit_7030_" + today)
    assert not [doc for doc in store.docs if doc.get("_id") == f"visit_gap_7030_{today}"]
