"""INDEPENDENT VERIFIER tests for the shift_report_note queue job (prompt 412a).

Authored against the contract for the (uncommitted) `shift_report_note` job type.
NOT written by the implementer. Drives the real handler with the production-faithful
in-memory recording vault store used by the rest of the queue-processor suite,
exercises `validate_job`, registry dispatch, the ops-dashboard summary surface, and
the `write_shift_report_note_job` staging helper.

Run from repo root:
    .venv/bin/python -m pytest project/tests/test_shift_report_note_job_412a.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from queue_processor.handlers import _shared as shared
from queue_processor.handlers import misc
from queue_processor.main import QueueJob, QueueJobError, RunContext
from queue_processor.registry import JOB_HANDLERS
from queue_spec import (
    JOB_SHIFT_REPORT_NOTE,
    ALLOWED_JOB_TYPES,
    validate_job,
)
from ops_dashboard.common import (
    KNOWN_JOB_SUMMARY_TYPES,
    render_job_summary,
    write_shift_report_note_job,
)

from tests.test_queue_processor_couchdb_write import RecordingRmwVaultStore


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

CONTENT = "Deep analysis found a damaged threshold at the west entrance."


def context_for(root: Path) -> RunContext:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return RunContext(
        project_root=root,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7050"},
        site_id_to_opportunities_dir={},
    )


def make_queue_file(context: RunContext, name: str) -> Path:
    queue_file = context.runtime_root / f"{name}.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    return queue_file


def note_job(payload: dict[str, Any], job_id: str = "note-job-1") -> QueueJob:
    return QueueJob(
        job_id=job_id,
        job_type="shift_report_note",
        payload=payload,
        metadata={},
        intent={},
    )


def note_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "date": "2026-06-12",
        "content": CONTENT,
        "actor": "Jordan Avery",
        "capture_id": "cap-photo-1",
        "photo_asset_id": "photo-west-1",
    }
    payload.update(overrides)
    return payload


def run_handler(
    store: RecordingRmwVaultStore,
    context: RunContext,
    payload: dict[str, Any],
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    queue_name: str | None = None,
) -> None:
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, queue_name or job_id)
    processed_dir = context.runtime_root / "processed"
    misc.process_shift_report_note_job(queue_file, note_job(payload, job_id), context, processed_dir)


def _job(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_type": "shift_report_note", "payload": payload}


# --------------------------------------------------------------------------- #
# Contract 1: validate_job accept / reject table
# --------------------------------------------------------------------------- #

def test_validate_accepts_complete_payload() -> None:
    assert validate_job(_job(note_payload())) is True


def test_validate_accepts_with_optional_fields() -> None:
    assert validate_job(
        _job(note_payload(site_id="7050", prompt_id="damage_hazard", prompt_label="Damage / hazard"))
    ) is True


@pytest.mark.parametrize("missing", ["date", "content", "actor", "capture_id", "photo_asset_id"])
def test_validate_rejects_missing_required(missing: str) -> None:
    payload = note_payload()
    del payload[missing]
    assert validate_job(_job(payload)) is False


@pytest.mark.parametrize("blank", ["date", "content", "actor", "capture_id", "photo_asset_id"])
def test_validate_rejects_empty_required(blank: str) -> None:
    assert validate_job(_job(note_payload(**{blank: ""}))) is False


@pytest.mark.parametrize("blank", ["content", "actor", "capture_id", "photo_asset_id"])
def test_validate_rejects_whitespace_required(blank: str) -> None:
    assert validate_job(_job(note_payload(**{blank: "   "}))) is False


def test_validate_rejects_non_string_required() -> None:
    assert validate_job(_job(note_payload(content=42))) is False
    assert validate_job(_job(note_payload(actor=7))) is False


def test_validate_rejects_bad_date_shape() -> None:
    assert validate_job(_job(note_payload(date="06/12/2026"))) is False
    assert validate_job(_job(note_payload(date="2026-6-2"))) is False


def test_validate_rejects_stray_field() -> None:
    assert validate_job(_job(note_payload(stray="nope"))) is False


def test_validate_rejects_non_string_optional() -> None:
    assert validate_job(_job(note_payload(site_id=7050))) is False
    assert validate_job(_job(note_payload(prompt_id={"x": 1}))) is False


# --------------------------------------------------------------------------- #
# Contract 2: allowed type + registry dispatch
# --------------------------------------------------------------------------- #

def test_type_is_allowed() -> None:
    assert JOB_SHIFT_REPORT_NOTE == "shift_report_note"
    assert JOB_SHIFT_REPORT_NOTE in ALLOWED_JOB_TYPES


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_SHIFT_REPORT_NOTE] is misc.process_shift_report_note_job


# --------------------------------------------------------------------------- #
# Contract 3: handler writes a canonical content-aware doc
# --------------------------------------------------------------------------- #

def test_handler_writes_canonical_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(site_id="7050", prompt_id="damage_hazard", prompt_label="Damage / hazard"), "note-job-1", monkeypatch)

    docs = [d for d in store.docs if d.get("type") == "shift_report_note"]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["type"] == "shift_report_note"
    assert doc["_id"].startswith("shift_report_note_2026_06_12_")
    assert doc["content"] == CONTENT
    assert doc["date"] == "2026-06-12"
    assert doc["capture_id"] == "cap-photo-1"
    assert doc["photo_asset_id"] == "photo-west-1"
    assert doc["actor"] == "Jordan Avery"
    assert doc["site_id"] == "7050"
    assert doc["prompt_id"] == "damage_hazard"
    assert doc["prompt_label"] == "Damage / hazard"
    assert doc["operator"]  # operator resolved by the handler
    assert doc["captured_at"]  # captured_at resolved by the handler
    assert doc["btq_job_ids"] == ["note-job-1"]
    assert (context.runtime_root / "processed" / "note-job-1.json").exists()


def test_handler_routes_through_apply_canonical_rmw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The store records every update_doc (RMW) call by doc_id.
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(), "note-job-rmw", monkeypatch)
    assert len(store.update_doc_calls) == 1
    assert store.update_doc_calls[0].startswith("shift_report_note_2026_06_12_")


def test_two_distinct_notes_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONTENT-AWARE id: two distinct notes for the same date -> two docs."""
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(content="First finding: dock leak."), "note-a", monkeypatch)
    run_handler(store, context, note_payload(content="Second finding: cracked tile."), "note-b", monkeypatch)

    docs = [d for d in store.docs if d.get("type") == "shift_report_note"]
    assert len(docs) == 2
    ids = {d["_id"] for d in docs}
    assert len(ids) == 2  # distinct doc_ids
    contents = {d["content"] for d in docs}
    assert contents == {"First finding: dock leak.", "Second finding: cracked tile."}


def test_distinct_capture_same_content_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doc id folds capture_id in too: same text from two captures -> two docs."""
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(capture_id="cap-A"), "note-capA", monkeypatch)
    run_handler(store, context, note_payload(capture_id="cap-B"), "note-capB", monkeypatch)
    docs = [d for d in store.docs if d.get("type") == "shift_report_note"]
    assert len({d["_id"] for d in docs}) == 2


def test_identical_resend_idempotent_via_job_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same content + same capture re-sent under the SAME job_id -> same doc_id,
    handler short-circuits on the btq_job_ids marker, one doc only."""
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(), "note-dup", monkeypatch)
    after_first = [d for d in store.docs if d.get("type") == "shift_report_note"][0]

    run_handler(
        store,
        context,
        note_payload(content=CONTENT),
        "note-dup",
        monkeypatch,
        queue_name="note-dup-replay",
    )
    docs = [d for d in store.docs if d.get("type") == "shift_report_note"]
    assert len(docs) == 1
    assert docs[0]["_id"] == after_first["_id"]
    assert docs[0]["btq_job_ids"] == ["note-dup"]


def test_identical_content_same_doc_id_distinct_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same content+capture from two DIFFERENT job_ids -> same doc_id (one doc),
    second job_id appended to the marker list (RMW upsert)."""
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(), "note-j1", monkeypatch)
    run_handler(store, context, note_payload(), "note-j2", monkeypatch)
    docs = [d for d in store.docs if d.get("type") == "shift_report_note"]
    assert len(docs) == 1
    assert docs[0]["btq_job_ids"] == ["note-j1", "note-j2"]


def test_doc_id_helper_is_content_aware() -> None:
    a = misc._shift_report_note_doc_id("2026-06-12", "x content", "cap-1")
    b = misc._shift_report_note_doc_id("2026-06-12", "y content", "cap-1")
    same = misc._shift_report_note_doc_id("2026-06-12", "x content", "cap-1")
    assert a != b  # different content -> different id
    assert a == same  # deterministic
    assert a.startswith("shift_report_note_2026_06_12_")


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    object.__setattr__(context, "dry_run", True)
    store = RecordingRmwVaultStore()
    run_handler(store, context, note_payload(), "note-dry", monkeypatch)
    assert [d for d in store.docs if d.get("type") == "shift_report_note"] == []
    assert store.update_doc_calls == []
    assert not (context.runtime_root / "processed" / "note-dry.json").exists()


def test_store_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)

    class _ExplodingStore(RecordingRmwVaultStore):
        def update_doc(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("couchdb unavailable")

    store = _ExplodingStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "note-boom")
    processed_dir = context.runtime_root / "processed"
    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        misc.process_shift_report_note_job(queue_file, note_job(note_payload(), "note-boom"), context, processed_dir)
    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()


# --------------------------------------------------------------------------- #
# Contract 4: drift guards (authoring guide + summary branch)
# --------------------------------------------------------------------------- #

def test_shift_report_note_is_known_summary_type() -> None:
    assert "shift_report_note" in KNOWN_JOB_SUMMARY_TYPES


def test_render_job_summary_non_empty() -> None:
    summary = render_job_summary("shift_report_note", {"capture_id": "cap-9", "prompt_label": "Damage / hazard"})
    assert summary
    assert "cap-9" in summary
    assert "Damage / hazard" in summary


def test_render_job_summary_without_prompt_label() -> None:
    summary = render_job_summary("shift_report_note", {"capture_id": "cap-9"})
    assert summary
    assert "cap-9" in summary


# --------------------------------------------------------------------------- #
# Contract 5: write_shift_report_note_job staging helper
# --------------------------------------------------------------------------- #

def test_write_job_validates(tmp_path: Path) -> None:
    path = write_shift_report_note_job(
        tmp_path,
        date="2026-06-12",
        content=CONTENT,
        actor="Greg",
        capture_id="cap-1",
        photo_asset_id="photo-1",
    )
    assert path.exists()
    assert path.parent == tmp_path
    assert path.name.startswith("shift-report-note-") and path.suffix == ".json"
    job = json.loads(path.read_text())
    assert job["job_type"] == "shift_report_note"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["date"] == "2026-06-12"
    assert payload["content"] == CONTENT
    assert payload["actor"] == "Greg"
    assert payload["capture_id"] == "cap-1"
    assert payload["photo_asset_id"] == "photo-1"
    assert "site_id" not in payload  # omitted optional not injected
    assert job["idempotency_key"].startswith("shift-report-note:")


def test_write_job_includes_optional_fields(tmp_path: Path) -> None:
    path = write_shift_report_note_job(
        tmp_path,
        date="2026-06-12",
        content=CONTENT,
        actor="Greg",
        capture_id="cap-1",
        photo_asset_id="photo-1",
        site_id="7050",
        prompt_id="damage_hazard",
        prompt_label="Damage / hazard",
    )
    job = json.loads(path.read_text())
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["site_id"] == "7050"
    assert payload["prompt_id"] == "damage_hazard"
    assert payload["prompt_label"] == "Damage / hazard"


def test_write_job_idempotency_key_content_aware(tmp_path: Path) -> None:
    p1 = write_shift_report_note_job(tmp_path, date="2026-06-12", content="A", actor="g", capture_id="c", photo_asset_id="p")
    p2 = write_shift_report_note_job(tmp_path, date="2026-06-12", content="B", actor="g", capture_id="c", photo_asset_id="p")
    p3 = write_shift_report_note_job(tmp_path, date="2026-06-12", content="A", actor="g", capture_id="c", photo_asset_id="p")
    k1 = json.loads(p1.read_text())["idempotency_key"]
    k2 = json.loads(p2.read_text())["idempotency_key"]
    k3 = json.loads(p3.read_text())["idempotency_key"]
    assert k1 != k2  # distinct content -> distinct key
    assert k1 == k3  # same content/capture -> same key (dedupe)
