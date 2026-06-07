from __future__ import annotations

from pathlib import Path
from typing import Any

from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.backfill_unknown_captures import backfill, iter_all_unknown_blocks
from queue_processor.handlers import _shared
from queue_processor.handlers.unknowns import derive_source_unknown_id


class RecordingStore:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.puts: list[tuple[dict[str, Any], str | None]] = []
        self.find_payloads: list[dict[str, Any]] = []
        for doc in docs or []:
            self.docs[str(doc["_id"])] = dict(doc)

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(doc_id)
        if doc is None or doc.get("_deleted") is True:
            return None
        return dict(doc)

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        outgoing = dict(doc)
        self.puts.append((outgoing, expected_rev))
        doc_id = str(outgoing["_id"])
        if expected_rev is None:
            outgoing["_rev"] = "1-created"
        else:
            outgoing["_rev"] = f"{expected_rev}-next"
        self.docs[doc_id] = dict(outgoing)
        return dict(outgoing)

    def _request_json(self, method: str, doc_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        assert method == "POST"
        assert doc_id == "_find"
        self.find_payloads.append(dict(payload or {}))
        return {
            "docs": [
                {"_id": doc["_id"], "type": doc.get("type")}
                for doc in self.docs.values()
                if doc.get("type") == "note" and doc.get("_deleted") is not True
            ]
        }


def unknown_body(
    *,
    original: str = "Raw transcript.",
    normalized: str = "Normalized transcript.",
    notes: str = "#unknown #needs-review",
) -> str:
    return (
        "## Original Transcript\n"
        f"{original}\n"
        "\n"
        "## Normalized Transcript\n"
        f"{normalized}\n"
        "\n"
        "## Notes\n"
        f"{notes}\n"
    )


def unknown_block(
    *,
    timestamp: str,
    audio_file: str,
    status: str | None = "unresolved",
    retry_count: str | None = None,
    last_attempted: str | None = None,
    capture_id: str | None = None,
    resolved_at: str | None = None,
    resolved_site: str | None = None,
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
    if resolved_at is not None:
        fields = _shared.set_frontmatter_value(fields, "resolved_at", resolved_at)
    if resolved_site is not None:
        fields = _shared.set_frontmatter_value(fields, "resolved_site", resolved_site)
    return _shared.frontmatter_to_text(fields, body or unknown_body())


def write_unknown_file(vault_root: Path, name: str, *blocks: str) -> Path:
    path = vault_root / "Journal" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(blocks), encoding="utf-8")
    return path


def test_iter_all_unknown_blocks_returns_all_blocks_with_transcripts_and_retry_state(tmp_path: Path) -> None:
    path = write_unknown_file(
        tmp_path,
        "2026-05-08-unknown.md",
        unknown_block(
            timestamp="2026-05-08T10:00:00+00:00",
            audio_file="memo-a.m4a",
            retry_count="2",
            last_attempted="2026-05-08T11:00:00+00:00",
            capture_id="capture-a",
            body=unknown_body(original="Original A.", normalized="Normalized A.", notes="Needs review A."),
        ),
        unknown_block(
            timestamp="2026-05-08T12:00:00+00:00",
            audio_file="memo-b.m4a",
            status="resolved",
            resolved_at="2026-05-08T13:00:00+00:00",
            resolved_site="7060",
            body=unknown_body(original="Original B.", normalized="Normalized B.", notes="Resolved B."),
        ),
    )

    blocks = iter_all_unknown_blocks(path)

    assert len(blocks) == 2
    assert blocks[0]["source_unknown_id"] == derive_source_unknown_id(path, "2026-05-08T10:00:00+00:00", "memo-a.m4a")
    assert blocks[0]["status"] == "unresolved"
    assert blocks[0]["retry_count"] == 2
    assert blocks[0]["last_attempted"] == "2026-05-08T11:00:00+00:00"
    assert blocks[0]["capture_id"] == "capture-a"
    assert blocks[0]["original_transcript"] == "Original A."
    assert blocks[0]["normalized_transcript"] == "Normalized A."
    assert blocks[0]["notes"] == "Needs review A."
    assert blocks[1]["status"] == "resolved"
    assert blocks[1]["resolved_at"] == "2026-05-08T13:00:00+00:00"
    assert blocks[1]["resolved_site"] == "7060"


def test_backfill_creates_canonical_docs_and_preserves_resolved_status(tmp_path: Path) -> None:
    path = write_unknown_file(
        tmp_path,
        "2026-05-08-unknown.md",
        unknown_block(timestamp="2026-05-08T10:00:00+00:00", audio_file="memo-a.m4a"),
        unknown_block(
            timestamp="2026-05-08T12:00:00+00:00",
            audio_file="memo-b.m4a",
            status="resolved",
            resolved_at="2026-05-08T13:00:00+00:00",
            resolved_site="7060",
        ),
    )
    store = RecordingStore()

    report = backfill(store, tmp_path, dry_run=False)

    assert report.files_scanned == 1
    assert report.blocks_found == 2
    assert report.created == 2
    assert report.skipped_existing == 0
    assert report.blocks_found == report.created + report.skipped_existing
    first_id = f"unknown_capture_{derive_source_unknown_id(path, '2026-05-08T10:00:00+00:00', 'memo-a.m4a')}"
    second_id = f"unknown_capture_{derive_source_unknown_id(path, '2026-05-08T12:00:00+00:00', 'memo-b.m4a')}"
    assert store.docs[first_id]["type"] == "unknown_capture"
    assert store.docs[first_id]["operator"] == OPERATOR_ID_GREG
    assert store.docs[first_id]["btq_job_ids"] == []
    assert store.docs[second_id]["status"] == "resolved"
    assert store.docs[second_id]["resolved_at"] == "2026-05-08T13:00:00+00:00"
    assert store.docs[second_id]["resolved_site"] == "7060"


def test_backfill_is_no_clobber_and_idempotent(tmp_path: Path) -> None:
    path = write_unknown_file(
        tmp_path,
        "2026-05-08-unknown.md",
        unknown_block(timestamp="2026-05-08T10:00:00+00:00", audio_file="memo-a.m4a"),
        unknown_block(timestamp="2026-05-08T12:00:00+00:00", audio_file="memo-b.m4a"),
    )
    existing_id = f"unknown_capture_{derive_source_unknown_id(path, '2026-05-08T10:00:00+00:00', 'memo-a.m4a')}"
    existing = {
        "_id": existing_id,
        "_rev": "3-existing",
        "type": "unknown_capture",
        "status": "resolved",
        "retry_count": 2,
    }
    store = RecordingStore([existing])

    report = backfill(store, tmp_path, dry_run=False)

    assert report.blocks_found == 2
    assert report.created == 1
    assert report.skipped_existing == 1
    assert store.docs[existing_id] == existing

    second_report = backfill(store, tmp_path, dry_run=False)

    assert second_report.blocks_found == 2
    assert second_report.created == 0
    assert second_report.skipped_existing == 2


def test_backfill_dry_run_counts_would_create_without_writes(tmp_path: Path) -> None:
    write_unknown_file(
        tmp_path,
        "2026-05-08-unknown.md",
        unknown_block(timestamp="2026-05-08T10:00:00+00:00", audio_file="memo-a.m4a"),
        unknown_block(timestamp="2026-05-08T12:00:00+00:00", audio_file="memo-b.m4a"),
    )
    store = RecordingStore()

    report = backfill(store, tmp_path, dry_run=True)

    assert report.blocks_found == 2
    assert report.would_create == 2
    assert report.created == 0
    assert report.blocks_found == report.would_create + report.skipped_existing
    assert store.puts == []


def test_backfill_reconciles_note_journal_unknown_docs_only(tmp_path: Path) -> None:
    write_unknown_file(
        tmp_path,
        "2026-05-08-unknown.md",
        unknown_block(timestamp="2026-05-08T10:00:00+00:00", audio_file="memo-a.m4a"),
    )
    store = RecordingStore(
        [
            {"_id": "note_journal_2026-05-08-unknown", "_rev": "2-note", "type": "note"},
            {"_id": "note_journal_2026-05-08", "_rev": "1-note", "type": "note"},
        ]
    )

    report = backfill(store, tmp_path, dry_run=False)

    assert report.note_journal_reconciled == 1
    tombstone, expected_rev = store.puts[-1]
    assert tombstone["_id"] == "note_journal_2026-05-08-unknown"
    assert tombstone["_deleted"] is True
    assert expected_rev == "2-note"
    assert store.docs["note_journal_2026-05-08-unknown"]["_deleted"] is True
    assert store.docs["note_journal_2026-05-08"].get("_deleted") is not True
