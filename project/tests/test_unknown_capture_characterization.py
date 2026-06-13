from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from queue_processor.handlers import _shared
from queue_processor.handlers import unknowns
from queue_processor.idempotency import upsert_job_id_frontmatter
from queue_spec import JOB_RECLASSIFY_UNKNOWN


def context_for(root: Path, *, dry_run: bool = False) -> _shared.RunContext:
    vault_root = root / "vault"
    runtime_root = root / "runtime"
    (vault_root / "Journal").mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    context = _shared.RunContext(
        project_root=root,
        runtime_root=runtime_root,
        log_path=runtime_root / "queue.log",
        dry_run=dry_run,
    )
    # vault_root was removed from the production RunContext; the test still uses
    # a throwaway temp dir to assert no markdown projection is written.
    object.__setattr__(context, "vault_root", vault_root)
    return context


def unknown_body(text: str = "Visited #site: 7060 today.") -> str:
    return (
        "## Normalized Transcript\n"
        f"{text}\n"
        "\n"
        "## Notes\n"
        "#unknown #needs-review\n"
    )


def unknown_block(
    *,
    timestamp: str,
    audio_file: str,
    status: str | None = "unresolved",
    retry_count: str | None = None,
    last_attempted: str | None = None,
    capture_id: str | None = None,
    body: str | None = None,
) -> str:
    fields: list[tuple[str, Any]] = [
        ("type", "unknown_capture"),
        ("timestamp", timestamp),
        ("audio_file", audio_file),
    ]
    if status is not None:
        fields = _shared.set_frontmatter_value(fields, "status", status)
    if retry_count is not None:
        fields = _shared.set_frontmatter_value(fields, "retry_count", retry_count)
    if last_attempted is not None:
        fields = _shared.set_frontmatter_value(fields, "last_attempted", last_attempted)
    if capture_id is not None:
        fields = _shared.set_frontmatter_value(fields, "capture_id", capture_id)
    return _shared.frontmatter_to_text(fields, body or unknown_body())


def write_unknown_file(journal_dir: Path, name: str, *blocks: str) -> Path:
    path = journal_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(blocks), encoding="utf-8")
    return path


def fields_for(path: Path) -> list[tuple[str, Any]]:
    fields, _body = _shared.parse_frontmatter(path.read_text(encoding="utf-8"))
    return fields


def get_field(path: Path, key: str) -> str | None:
    return _shared.get_frontmatter_value(fields_for(path), key)


class RecordingUnknownCaptureStore:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = [dict(doc) for doc in (docs or [])]
        self.update_doc_calls: list[str] = []
        self.find_unknown_capture_docs_calls: list[str | None] = []

    def find_unknown_capture_docs(self, status: str | None = "unresolved", *, limit: int = 10000) -> list[dict[str, Any]]:
        self.find_unknown_capture_docs_calls.append(status)
        docs = [
            doc
            for doc in self.docs
            if doc.get("type") == "unknown_capture" and (status is None or doc.get("status") == status)
        ]
        return [dict(doc) for doc in docs[:limit]]

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
        index = next((index for index, doc in enumerate(self.docs) if doc.get("_id") == doc_id), None)
        current = dict(self.docs[index]) if index is not None else None
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
        if index is None:
            self.docs.append(stored)
        else:
            self.docs[index] = stored
        return dict(stored)

    def doc(self, doc_id: str) -> dict[str, Any]:
        for doc in self.docs:
            if doc.get("_id") == doc_id:
                return dict(doc)
        raise AssertionError(f"missing doc: {doc_id}")


class ExplodingUnknownCaptureStore(RecordingUnknownCaptureStore):
    def find_unknown_capture_docs(self, status: str | None = "unresolved", *, limit: int = 10000) -> list[dict[str, Any]]:
        raise RuntimeError("couchdb unavailable")

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


def install_store(monkeypatch: Any, store: RecordingUnknownCaptureStore) -> RecordingUnknownCaptureStore:
    monkeypatch.setattr(_shared, "_VAULT_STORE", store)
    return store


def canonical_unknown_doc(
    context: _shared.RunContext,
    *,
    timestamp: str = "2026-01-01T10:00:00+00:00",
    audio_file: str = "a.m4a",
    status: str = "unresolved",
    retry_count: int = 0,
    last_attempted: str | None = None,
    capture_id: str | None = None,
    normalized_transcript: str = "Visited #site: 7060 today.",
    notes: str = "#unknown #needs-review",
) -> dict[str, Any]:
    legacy_path = context.vault_root / "Journal" / "2026-01-01-unknown.md"
    source_unknown_id = unknowns.derive_source_unknown_id(legacy_path, timestamp, audio_file)
    doc: dict[str, Any] = {
        "_id": f"unknown_capture_{source_unknown_id}",
        "type": "unknown_capture",
        "source_unknown_id": source_unknown_id,
        "timestamp": timestamp,
        "audio_file": audio_file,
        "status": status,
        "retry_count": retry_count,
        "last_attempted": last_attempted,
        "normalized_transcript": normalized_transcript,
        "notes": notes,
        "btq_job_ids": [],
    }
    if capture_id is not None:
        doc["capture_id"] = capture_id
    return doc


def make_entry(
    status: str = "unresolved",
    retry_count: int = 0,
    last_attempted: str | None = None,
    timestamp: str = "2999-01-01T00:00:00+00:00",
    audio_file: str = "memo.m4a",
) -> dict[str, Any]:
    return {
        "type": "unknown_capture",
        "timestamp": timestamp,
        "audio_file": audio_file,
        "status": status,
        "retry_count": retry_count,
        "last_attempted": last_attempted,
    }


def install_successful_event_pipeline(
    monkeypatch: Any,
    *,
    event_id: str = "event-one",
    site: str = "7060",
    job_id: str = "generated-job",
) -> None:
    def fake_extract_events(transcript_path: Path, raw_dir: Path, *, transcript_text: str) -> list[Path]:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "raw.json"
        raw_path.write_text(json.dumps({"transcript": transcript_text, "source": str(transcript_path)}), encoding="utf-8")
        return [raw_path]

    def fake_enrich_events(raw_dir: Path, enriched_dir: Path, raw_paths: list[Path]) -> list[Path]:
        enriched_dir.mkdir(parents=True, exist_ok=True)
        enriched_path = enriched_dir / "enriched.json"
        enriched_path.write_text(json.dumps({"raw_paths": [str(path) for path in raw_paths]}), encoding="utf-8")
        return [enriched_path]

    def fake_validate_events(
        enriched_dir: Path,
        valid_dir: Path,
        failed_dir: Path,
        enriched_paths: list[Path],
    ) -> tuple[list[Path], list[Path]]:
        valid_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)
        event_path = valid_dir / f"{event_id}.json"
        event_path.write_text(
            json.dumps({"event_id": event_id, "site": site, "enriched_paths": [str(path) for path in enriched_paths]}),
            encoding="utf-8",
        )
        return [event_path], []

    def fake_event_to_job(event: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": job_id, "job_type": "log_visit", "payload": {"site": event["site"]}}

    monkeypatch.setattr(unknowns, "extract_events", fake_extract_events)
    monkeypatch.setattr(unknowns, "enrich_events", fake_enrich_events)
    monkeypatch.setattr(unknowns, "validate_events", fake_validate_events)
    monkeypatch.setattr(unknowns, "event_to_job", fake_event_to_job)


def install_no_valid_event_pipeline(monkeypatch: Any) -> None:
    def fake_extract_events(transcript_path: Path, raw_dir: Path, *, transcript_text: str) -> list[Path]:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "raw.json"
        raw_path.write_text(transcript_text, encoding="utf-8")
        return [raw_path]

    def fake_enrich_events(raw_dir: Path, enriched_dir: Path, raw_paths: list[Path]) -> list[Path]:
        enriched_dir.mkdir(parents=True, exist_ok=True)
        return raw_paths

    def fake_validate_events(
        enriched_dir: Path,
        valid_dir: Path,
        failed_dir: Path,
        enriched_paths: list[Path],
    ) -> tuple[list[Path], list[Path]]:
        valid_dir.mkdir(parents=True, exist_ok=True)
        failed_dir.mkdir(parents=True, exist_ok=True)
        return [], []

    def fake_event_to_job(event: dict[str, Any]) -> None:
        raise AssertionError("event_to_job should not be called when validation returns no valid events")

    monkeypatch.setattr(unknowns, "extract_events", fake_extract_events)
    monkeypatch.setattr(unknowns, "enrich_events", fake_enrich_events)
    monkeypatch.setattr(unknowns, "validate_events", fake_validate_events)
    monkeypatch.setattr(unknowns, "event_to_job", fake_event_to_job)


def install_failing_event_pipeline(monkeypatch: Any, reason: str) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError(reason)

    monkeypatch.setattr(unknowns, "extract_events", fail)
    monkeypatch.setattr(unknowns, "enrich_events", fail)
    monkeypatch.setattr(unknowns, "validate_events", fail)
    monkeypatch.setattr(unknowns, "event_to_job", fail)


def make_reclassify_job(job_id: str = "reclassify-one") -> _shared.QueueJob:
    return _shared.QueueJob(
        job_id=job_id,
        job_type=JOB_RECLASSIFY_UNKNOWN,
        payload={"path": "Journal/2026-01-01-unknown.md"},
        metadata={},
        intent={},
    )


def write_job_file(runtime_root: Path, job: _shared.QueueJob) -> Path:
    queue_dir = runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_path = queue_dir / f"{job.job_id}.json"
    job_path.write_text(
        json.dumps(
            {
                "job_id": job.job_id,
                "job_type": job.job_type,
                "payload": job.payload,
                "metadata": job.metadata,
                "intent": job.intent,
            }
        ),
        encoding="utf-8",
    )
    return job_path


def test_parse_retry_count_and_last_attempted_current_markdown_values() -> None:
    assert [unknowns.parse_retry_count(value) for value in [None, "", "-1", "abc", "3"]] == [0, 0, 0, 0, 3]
    assert [
        unknowns.parse_last_attempted(value)
        for value in [None, "", "null", "NULL", " 2026-01-02T03:04:05+00:00 "]
    ] == [None, None, None, None, "2026-01-02T03:04:05+00:00"]


def test_should_retry_current_backoff_rules() -> None:
    just_now = datetime.now(timezone.utc).isoformat()
    far_past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    assert unknowns.should_retry(make_entry(retry_count=3, last_attempted=None)) is False
    assert unknowns.should_retry(make_entry(retry_count=0, last_attempted=None)) is True
    assert unknowns.should_retry(make_entry(retry_count=1, last_attempted=just_now)) is False
    assert unknowns.should_retry(make_entry(retry_count=1, last_attempted=far_past)) is True


def test_site_signal_current_rules(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    alias = next(alias for site in unknowns.SITES for alias in site["aliases"])

    assert unknowns.unknown_entry_contains_site_signal("Please revisit #site: 7060") is True
    assert unknowns.unknown_entry_contains_site_signal(f"Please revisit {alias}") is True
    assert unknowns.canonical_unknown_contains_site_signal(
        canonical_unknown_doc(context, normalized_transcript="#site: 7060", notes="")
    ) is True
    assert unknowns.canonical_unknown_contains_site_signal(
        canonical_unknown_doc(context, normalized_transcript=f"Please revisit {alias}", notes="")
    ) is True


def test_reclassify_unknown_success_creates_queue_job_and_resolves_doc(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    install_successful_event_pipeline(monkeypatch, event_id="event-success", site="7060")
    doc = canonical_unknown_doc(context, capture_id="capture-a")
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))

    assert unknowns.reclassify_unknown(doc, context) == 1

    job_payload = json.loads((context.runtime_root / "queue" / "job_event-success.json").read_text(encoding="utf-8"))
    assert job_payload["payload"]["source_unknown_id"] == doc["source_unknown_id"]
    assert job_payload["metadata"]["capture_id"] == "capture-a"
    updated = store.doc(doc["_id"])
    assert updated["status"] == "resolved"
    assert updated["resolved_at"] is not None
    assert updated["resolved_site"] == "7060"
    assert updated["resolution"] == "Reclassified and routed to structured events."
    assert updated["retry_count"] == 1
    assert updated["last_attempted"] is not None


def test_reclassify_unknown_dedups_existing_source_and_resolves_doc(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))
    processed_dir = context.runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    (processed_dir / "existing.json").write_text(
        json.dumps({"job_id": "existing", "job_type": "log_visit", "payload": {"source_unknown_id": doc["source_unknown_id"]}}),
        encoding="utf-8",
    )

    install_failing_event_pipeline(monkeypatch, "dedup should resolve before invoking event extraction")

    assert unknowns.reclassify_unknown(doc, context) == 0
    assert not (context.runtime_root / "queue").exists()
    updated = store.doc(doc["_id"])
    assert updated["status"] == "resolved"
    assert updated["resolved_site"] == "unknown"
    assert updated["resolution"] == "Reclassified and routed to structured events."


def test_reclassify_unknown_no_valid_events_leaves_doc_unresolved_after_attempt(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    install_no_valid_event_pipeline(monkeypatch)
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))

    assert unknowns.reclassify_unknown(doc, context) == 0
    assert not (context.runtime_root / "queue").exists()
    updated = store.doc(doc["_id"])
    assert updated["status"] == "unresolved"
    assert updated["retry_count"] == 1
    assert updated["last_attempted"] is not None


def test_process_unknowns_scans_canonical_unresolved_docs_and_reclassifies(
    tmp_path: Path, monkeypatch: Any
) -> None:
    context = context_for(tmp_path)
    install_successful_event_pipeline(monkeypatch, event_id="event-from-scanner")
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))

    assert unknowns.process_unknowns(context) == 1

    assert store.find_unknown_capture_docs_calls == ["unresolved"]
    assert (context.runtime_root / "queue" / "job_event-from-scanner.json").exists()
    updated = store.doc(doc["_id"])
    assert updated["status"] == "resolved"
    assert updated["retry_count"] == 1


def test_process_reclassify_unknown_job_happy_path_moves_job_without_marking_file(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    install_successful_event_pipeline(monkeypatch, event_id="event-from-handler")
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))
    path = write_unknown_file(
        context.vault_root / "Journal",
        "2026-01-01-unknown.md",
        unknown_block(timestamp="2026-01-01T10:00:00+00:00", audio_file="a.m4a"),
    )
    original_text = path.read_text(encoding="utf-8")
    job = make_reclassify_job("reclassify-happy")
    job_path = write_job_file(context.runtime_root, job)
    processed_dir = context.runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    unknowns.process_reclassify_unknown_job(job_path, job, context, processed_dir)

    assert path.read_text(encoding="utf-8") == original_text
    assert store.find_unknown_capture_docs_calls == ["unresolved"]
    assert store.doc(doc["_id"])["status"] == "resolved"
    assert (processed_dir / job_path.name).exists()
    assert not job_path.exists()
    assert "action=reclassify-unknown status=success" in context.log_path.read_text(encoding="utf-8")


def test_process_reclassify_unknown_job_ignores_stale_markdown_job_marker(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))
    install_successful_event_pipeline(monkeypatch, event_id="event-from-stale-marker")
    job = make_reclassify_job("reclassify-already-applied")
    original_text = unknown_block(timestamp="2026-01-01T10:00:00+00:00", audio_file="a.m4a")
    path = write_unknown_file(
        context.vault_root / "Journal",
        "2026-01-01-unknown.md",
        upsert_job_id_frontmatter(original_text, job.job_id),
    )
    job_path = write_job_file(context.runtime_root, job)
    processed_dir = context.runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    unknowns.process_reclassify_unknown_job(job_path, job, context, processed_dir)

    assert path.read_text(encoding="utf-8") == upsert_job_id_frontmatter(original_text, job.job_id)
    assert store.find_unknown_capture_docs_calls == ["unresolved"]
    assert store.doc(doc["_id"])["status"] == "resolved"
    assert (processed_dir / job_path.name).exists()
    assert not job_path.exists()
    assert "action=reclassify-unknown status=success" in context.log_path.read_text(encoding="utf-8")


def test_process_reclassify_unknown_job_dry_run_logs_without_mutation_or_move(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    context = context_for(tmp_path, dry_run=True)
    doc = canonical_unknown_doc(context)
    store = install_store(monkeypatch, RecordingUnknownCaptureStore([doc]))
    path = write_unknown_file(
        context.vault_root / "Journal",
        "2026-01-01-unknown.md",
        unknown_block(timestamp="2026-01-01T10:00:00+00:00", audio_file="a.m4a"),
    )
    original_text = path.read_text(encoding="utf-8")
    job = make_reclassify_job("reclassify-dry-run")
    job_path = write_job_file(context.runtime_root, job)
    processed_dir = context.runtime_root / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    install_failing_event_pipeline(monkeypatch, "dry run should not invoke reclassification")

    unknowns.process_reclassify_unknown_job(job_path, job, context, processed_dir)
    output = capsys.readouterr().out

    assert path.read_text(encoding="utf-8") == original_text
    assert store.find_unknown_capture_docs_calls == []
    assert store.update_doc_calls == []
    assert job_path.exists()
    assert not (processed_dir / job_path.name).exists()
    assert "would reclassify unknowns" in output
    assert "action=reclassify-unknown status=success" in context.log_path.read_text(encoding="utf-8")


def test_reclassify_unknown_fails_closed_when_canonical_attempt_update_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    context = context_for(tmp_path)
    doc = canonical_unknown_doc(context)
    install_store(monkeypatch, ExplodingUnknownCaptureStore([doc]))
    install_failing_event_pipeline(monkeypatch, "canonical update failure should stop before extraction")

    try:
        unknowns.reclassify_unknown(doc, context)
    except _shared.QueueJobError as exc:
        assert "canonical unknown_capture attempt update failed" in str(exc)
    else:
        raise AssertionError("expected QueueJobError")


def test_process_unknowns_fails_closed_when_canonical_scan_fails(tmp_path: Path, monkeypatch: Any) -> None:
    context = context_for(tmp_path)
    install_store(monkeypatch, ExplodingUnknownCaptureStore())

    try:
        unknowns.process_unknowns(context)
    except _shared.QueueJobError as exc:
        assert "canonical unknown_capture scan failed" in str(exc)
    else:
        raise AssertionError("expected QueueJobError")
