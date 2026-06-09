from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import queue_processor.main as qp
from btq_vault.couch_store import CouchDBEntityStore
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import unknowns
from queue_spec import validate_job
from transcription_pipeline.main import emit_missed_capture_job


class RecordingRmwVaultStore:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = [dict(doc) for doc in (docs or [])]

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


class RecordingFindStore(CouchDBEntityStore):
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert payload is not None
        self.requests.append((method, path, payload))
        selector = payload["selector"]
        docs = [
            doc
            for doc in self.docs
            if doc.get("type") == selector.get("type")
            and ("status" not in selector or doc.get("status") == selector["status"])
        ]
        return {"docs": docs[: payload["limit"]]}


def context_for(root: Path) -> qp.RunContext:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return qp.RunContext(
        project_root=root,
        vault_root=root / "vault",
        personal_vault_root=root / "personal",
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids=set(),
        site_id_to_opportunities_dir={},
    )


def unknown_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": "Journal/2026-04-19-unknown.md",
        "content": "---\ntype: unknown_capture\ntimestamp: 2026-04-19T12:00:00+00:00\naudio_file: memo.webm\n---\nbody\n",
        "timestamp": "2026-04-19T12:00:00+00:00",
        "audio_file": "memo.webm",
        "original_transcript": "raw words",
        "normalized_transcript": "clean words",
        "capture_id": "cap-123",
        "capture_status": "#unknown #needs-review",
        "events_created": 0,
        "reason_heading": "Why Unknown",
        "reasons": ["No site detected", "No strong event pattern matched"],
    }
    payload.update(overrides)
    return payload


def write_queue_file(path: Path, *, job_type: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_type": job_type, "payload": payload}, indent=2), encoding="utf-8")


def test_record_unknown_capture_handler_creates_canonical_doc_without_projection(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    job_path = context.runtime_root / "queue" / "job_unknown.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text("{}\n", encoding="utf-8")
    payload = unknown_payload()
    job = qp.QueueJob("job-record-1", "record_unknown_capture", payload, {"capture_id": "cap-123"}, {})

    unknowns.process_record_unknown_capture_job(job_path, job, context, context.runtime_root / "processed")

    source_id = unknowns.derive_source_unknown_id(Path(payload["path"]), payload["timestamp"], payload["audio_file"])
    [doc] = store.docs
    assert doc["_id"] == f"unknown_capture_{source_id}"
    assert doc["type"] == "unknown_capture"
    assert doc["operator"] == OPERATOR_ID_GREG
    assert doc["status"] == "unresolved"
    assert doc["retry_count"] == 0
    assert doc["source_unknown_id"] == source_id
    assert doc["original_transcript"] == "raw words"
    assert doc["normalized_transcript"] == "clean words"
    assert doc["capture_id"] == "cap-123"
    assert doc["btq_job_ids"] == ["job-record-1"]
    assert not (context.vault_root / payload["path"]).exists()
    assert (context.runtime_root / "processed" / job_path.name).exists()


def test_record_unknown_capture_replay_skips_when_job_id_already_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    payload = unknown_payload()
    source_id = unknowns.derive_source_unknown_id(Path(payload["path"]), payload["timestamp"], payload["audio_file"])
    store = RecordingRmwVaultStore([
        {"_id": f"unknown_capture_{source_id}", "type": "unknown_capture", "btq_job_ids": ["job-record-1"]}
    ])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    job_path = context.runtime_root / "queue" / "job_unknown.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text("{}\n", encoding="utf-8")
    job = qp.QueueJob("job-record-1", "record_unknown_capture", payload, {}, {})

    unknowns.process_record_unknown_capture_job(job_path, job, context, context.runtime_root / "processed")

    assert store.docs == [{"_id": f"unknown_capture_{source_id}", "type": "unknown_capture", "btq_job_ids": ["job-record-1"]}]
    assert (context.runtime_root / "processed" / job_path.name).exists()
    assert not (context.vault_root / payload["path"]).exists()


def test_record_unknown_capture_existing_doc_only_unions_job_ids(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    payload = unknown_payload()
    source_id = unknowns.derive_source_unknown_id(Path(payload["path"]), payload["timestamp"], payload["audio_file"])
    existing = {
        "_id": f"unknown_capture_{source_id}",
        "type": "unknown_capture",
        "status": "resolved",
        "retry_count": 2,
        "resolved_at": "2026-04-20T00:00:00+00:00",
        "btq_job_ids": ["job-old"],
    }
    store = RecordingRmwVaultStore([existing])
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    job_path = context.runtime_root / "queue" / "job_unknown.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    job_path.write_text("{}\n", encoding="utf-8")
    job = qp.QueueJob("job-new", "record_unknown_capture", payload, {}, {})

    unknowns.process_record_unknown_capture_job(job_path, job, context, context.runtime_root / "processed")

    [doc] = store.docs
    assert doc["status"] == "resolved"
    assert doc["retry_count"] == 2
    assert doc["resolved_at"] == "2026-04-20T00:00:00+00:00"
    assert doc["btq_job_ids"] == ["job-old", "job-new"]
    assert not (context.vault_root / payload["path"]).exists()


def test_record_unknown_capture_store_error_moves_job_to_failed(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    monkeypatch.setattr(shared, "_VAULT_STORE", ExplodingRmwVaultStore())
    job_path = context.runtime_root / "queue" / "job_unknown.json"
    write_queue_file(job_path, job_type="record_unknown_capture", payload=unknown_payload())

    qp.process_job(job_path, context, context.runtime_root / "processed", context.runtime_root / "failed")

    assert (context.runtime_root / "failed" / job_path.name).exists()
    assert not (context.runtime_root / "processed" / job_path.name).exists()


def test_producer_redirects_unknown_capture_to_record_job(tmp_path: Path) -> None:
    queue_dir = tmp_path / "queue_jobs"
    processed_at = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)

    job_path = emit_missed_capture_job(
        queue_dir,
        "memo.webm",
        "unmatched operational note",
        "unmatched operational note",
        [],
        processed_at,
        capture_id="cap-123",
    )

    assert job_path is not None
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "record_unknown_capture"
    assert job["metadata"] == {"capture_id": "cap-123"}
    assert job["payload"]["path"] == "Journal/2026-04-19-unknown.md"
    assert "type: unknown_capture" in job["payload"]["content"]
    assert job["payload"]["timestamp"] == "2026-04-19T12:00:00+00:00"
    assert job["payload"]["audio_file"] == "memo.webm"
    assert job["payload"]["capture_id"] == "cap-123"
    assert "destination" not in job["payload"]


def test_append_to_note_rejects_unknown_files_without_note_doc(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    job_path = context.runtime_root / "queue" / "job_append_unknown.json"
    write_queue_file(
        job_path,
        job_type="append_to_note",
        payload={
            "path": "Journal/2026-04-19-unknown.md",
            "content": "---\ntype: unknown_capture\n---\nbody\n",
            "destination": "journal_unknown",
        },
    )

    qp.process_job(job_path, context, context.runtime_root / "processed", context.runtime_root / "failed")

    assert (context.runtime_root / "failed" / job_path.name).exists()
    assert not store.docs


def test_queue_spec_accepts_record_unknown_capture_and_requires_core_fields() -> None:
    assert validate_job({"job_type": "record_unknown_capture", "payload": unknown_payload()})
    missing_audio = unknown_payload()
    missing_audio.pop("audio_file")
    assert not validate_job({"job_type": "record_unknown_capture", "payload": missing_audio})


def test_find_unknown_capture_docs_records_selector_and_filters_status() -> None:
    unresolved = {"_id": "unknown_capture_one", "type": "unknown_capture", "status": "unresolved"}
    resolved = {"_id": "unknown_capture_two", "type": "unknown_capture", "status": "resolved"}
    store = RecordingFindStore([unresolved, resolved])

    assert store.find_unknown_capture_docs() == [unresolved]
    assert store.requests[-1][2]["selector"] == {"type": "unknown_capture", "status": "unresolved"}
    assert store.find_unknown_capture_docs(None) == [unresolved, resolved]
    assert store.requests[-1][2]["selector"] == {"type": "unknown_capture"}
    assert store.find_unknown_capture_docs("resolved") == [resolved]
