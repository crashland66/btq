from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from btq_vault.entity_types import current_operator_id
from queue_processor.handlers import _shared
from queue_processor.handlers.unknowns import (
    derive_source_unknown_id,
    extract_section,
    parse_last_attempted,
    parse_retry_count,
)


@dataclass
class BackfillReport:
    files_scanned: int = 0
    blocks_found: int = 0
    created: int = 0
    would_create: int = 0
    skipped_existing: int = 0
    note_journal_reconciled: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "blocks_found": self.blocks_found,
            "created": self.created,
            "would_create": self.would_create,
            "skipped_existing": self.skipped_existing,
            "note_journal_reconciled": self.note_journal_reconciled,
            "errors": [{"id": doc_id, "message": message} for doc_id, message in self.errors],
        }


def _frontmatter_value(fields: list[tuple[str, Any]], key: str) -> str | None:
    return _shared.get_frontmatter_value(fields, key)


def iter_all_unknown_blocks(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    marker = "---\ntype: unknown_capture\n"
    blocks: list[dict[str, Any]] = []

    for piece in text.split(marker)[1:]:
        block = f"{marker}{piece}".rstrip()
        try:
            fields, body = _shared.parse_frontmatter(block)
        except _shared.QueueProcessorError:
            continue
        if _frontmatter_value(fields, "type") != "unknown_capture":
            continue
        timestamp = _frontmatter_value(fields, "timestamp")
        audio_file = _frontmatter_value(fields, "audio_file")
        if timestamp is None or audio_file is None:
            continue

        item: dict[str, Any] = {
            "source_unknown_id": derive_source_unknown_id(path, timestamp, audio_file),
            "timestamp": timestamp,
            "audio_file": audio_file,
            "status": _frontmatter_value(fields, "status") or "unresolved",
            "retry_count": parse_retry_count(_frontmatter_value(fields, "retry_count")),
            "last_attempted": parse_last_attempted(_frontmatter_value(fields, "last_attempted")),
            "original_transcript": extract_section(body, "Original Transcript"),
            "normalized_transcript": extract_section(body, "Normalized Transcript"),
            "notes": extract_section(body, "Notes"),
        }
        for key in ("capture_id", "resolved_at", "resolved_site"):
            value = _frontmatter_value(fields, key)
            if value is not None:
                item[key] = value
        blocks.append(item)

    return blocks


def _canonical_doc(block: dict[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": f"unknown_capture_{block['source_unknown_id']}",
        "type": "unknown_capture",
        "operator": current_operator_id(),
        "source_unknown_id": block["source_unknown_id"],
        "timestamp": block["timestamp"],
        "audio_file": block["audio_file"],
        "status": block["status"],
        "retry_count": block["retry_count"],
        "last_attempted": block["last_attempted"],
        "original_transcript": block["original_transcript"],
        "normalized_transcript": block["normalized_transcript"],
        "notes": block["notes"],
        "btq_job_ids": [],
    }
    for key in ("capture_id", "resolved_at", "resolved_site"):
        if key in block:
            doc[key] = block[key]
    return doc


def _find_note_journal_unknown_ids(store: Any) -> list[str]:
    query = {
        "selector": {"type": "note"},
        "fields": ["_id", "type"],
        "limit": 10000,
    }
    if hasattr(store, "_request_json"):
        response = store._request_json("POST", "_find", query)
    else:
        response = store._find(query)
    docs = response.get("docs")
    if not isinstance(docs, list):
        return []
    doc_ids: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        doc_id = str(doc.get("_id") or "")
        if re.fullmatch(r"note_journal_.*-unknown", doc_id):
            doc_ids.append(doc_id)
    return sorted(doc_ids)


def _reconcile_note_journal_unknowns(store: Any, report: BackfillReport, *, dry_run: bool) -> None:
    try:
        doc_ids = _find_note_journal_unknown_ids(store)
    except Exception as exc:
        report.errors.append(("note_journal_*-unknown", str(exc)))
        return

    for doc_id in doc_ids:
        try:
            if not dry_run:
                doc = store.get_optional(doc_id)
                if doc is None:
                    continue
                rev = doc.get("_rev")
                if not rev:
                    raise ValueError("missing _rev")
                store.put_with_rev({"_id": doc_id, "_rev": rev, "_deleted": True}, expected_rev=str(rev))
            report.note_journal_reconciled += 1
        except Exception as exc:
            report.errors.append((doc_id, str(exc)))


def backfill(store: Any, vault_root: Path, *, dry_run: bool) -> BackfillReport:
    report = BackfillReport()
    journal_dir = vault_root / "Journal"
    paths = sorted(journal_dir.glob("*-unknown.md")) if journal_dir.exists() else []
    report.files_scanned = len(paths)

    for path in paths:
        try:
            blocks = iter_all_unknown_blocks(path)
        except Exception as exc:
            report.errors.append((str(path), str(exc)))
            continue
        report.blocks_found += len(blocks)

        for block in blocks:
            doc_id = f"unknown_capture_{block['source_unknown_id']}"
            try:
                if store.get_optional(doc_id) is not None:
                    report.skipped_existing += 1
                    continue
                doc = _canonical_doc(block)
                if dry_run:
                    report.would_create += 1
                else:
                    store.put_with_rev(doc, expected_rev=None)
                    report.created += 1
            except Exception as exc:
                report.errors.append((doc_id, str(exc)))

    _reconcile_note_journal_unknowns(store, report, dry_run=dry_run)
    return report
