from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from queue_processor import couchdb_queue_watcher


def logger() -> logging.Logger:
    return logging.getLogger("test.queue_materializer_job_type")


def sandbox_doc(**overrides: Any) -> dict[str, Any]:
    """Queue doc fixture with sandbox-safe identities.

    Deliberately carries NO type field by default; tests add job_type
    and/or legacy type explicitly so each case states its contract.
    """
    doc: dict[str, Any] = {
        "_id": "sandbox-doc-001",
        "_rev": "1-s",
        "btq_state": "claimed",
        "site_id": "site-sandbox",
        "summary": "Sandbox queue materializer probe",
        "created_at": "2026-08-30T08:00:00Z",
        "created_by": "sandbox-suite",
    }
    doc.update(overrides)
    return doc


def materialized_prefix(path: Path) -> str:
    """The job-type prefix of a materialized filename (text before first dot)."""
    return path.name.split(".", 1)[0]


def expected_payload(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key not in couchdb_queue_watcher.COUCHDB_METADATA_KEYS}


def test_job_type_only_names_file_with_job_type_prefix(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type="draft_shift_note")

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert path.parent == tmp_path / "queue"
    assert materialized_prefix(path) == "draft_shift_note"
    assert path.name.startswith("draft_shift_note.sandbox-doc-001-")
    assert path.exists()


def test_legacy_type_only_still_names_file_with_that_prefix(tmp_path: Path) -> None:
    doc = sandbox_doc(type="log_site_issue")

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "log_site_issue"
    assert path.exists()


def test_neither_field_yields_unknown_job_prefix(tmp_path: Path) -> None:
    doc = sandbox_doc()

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "unknown_job"
    assert path.exists()


def test_disagreeing_fields_prefer_job_type(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type="draft_shift_note", type="log_site_issue")

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "draft_shift_note"
    assert "log_site_issue" not in path.name


def test_payload_is_doc_minus_couchdb_metadata_regardless_of_naming_field(tmp_path: Path) -> None:
    docs = {
        "job_type_only": sandbox_doc(_id="sandbox-doc-jt", job_type="draft_shift_note"),
        "legacy_type_only": sandbox_doc(_id="sandbox-doc-legacy", type="log_site_issue"),
        "both_disagreeing": sandbox_doc(_id="sandbox-doc-both", job_type="draft_shift_note", type="log_site_issue"),
        "neither": sandbox_doc(_id="sandbox-doc-none"),
    }

    for label, doc in docs.items():
        path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload == expected_payload(doc), label
        for metadata_key in couchdb_queue_watcher.COUCHDB_METADATA_KEYS:
            assert metadata_key not in payload, (label, metadata_key)


def test_empty_string_job_type_falls_back_to_legacy_type(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type="", type="log_site_issue")

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "log_site_issue"


def test_empty_string_both_fields_fall_back_to_unknown_job(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type="", type="")

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "unknown_job"


def test_non_string_job_type_is_stringified(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type=1234)

    path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())

    assert materialized_prefix(path) == "1234"


def test_dry_run_returns_job_type_path_without_writing(tmp_path: Path) -> None:
    doc = sandbox_doc(job_type="draft_shift_note", type="log_site_issue")

    dry_path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger(), dry_run=True)

    assert materialized_prefix(dry_path) == "draft_shift_note"
    assert not dry_path.exists()
    assert not (tmp_path / "queue").exists()

    # A real run afterwards computes the same destination and writes it.
    wet_path = couchdb_queue_watcher.materialize_queue_job(doc, tmp_path, logger())
    assert wet_path == dry_path
    assert wet_path.exists()
