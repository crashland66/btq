"""INDEPENDENT VERIFIER tests for the record_shift_report queue job.

Authored against the contract for the (uncommitted) `record_shift_report` job
type. These tests were NOT written by the implementer. They drive the real
handler with an in-memory recording vault store (the same `RunContext` +
`QueueJob` harness used by the rest of the queue-processor suite), exercise
`validate_job`, the registry dispatch, and the ops-dashboard summary surface,
and probe beyond spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import queue_processor.main as qp
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import misc
from queue_processor.main import QueueJob, QueueJobError, RunContext
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_RECORD_SHIFT_REPORT, validate_job
from ops_dashboard.common import KNOWN_JOB_SUMMARY_TYPES, render_job_summary

# Reuse the production-faithful in-memory RMW store from the main couchdb-write
# suite. It backs apply_canonical_rmw via update_doc with create /
# require_existing / no-op semantics and dedup-by-job-id, exactly like the real
# CouchDBEntityStore contract the handler depends on.
from tests.test_queue_processor_couchdb_write import RecordingRmwVaultStore


SHIFT_MARKDOWN = (
    "# Shift Report — 2026-06-12\n\n"
    "## Open positions\n"
    "- 7060 Continental Metalworks: 2 open\n\n"
    "## Notes\n"
    "Covered the dock walk-through; **bold** _italic_ `code` 100% done.\n"
)


def context_for(root: Path) -> RunContext:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return RunContext(
        project_root=root,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=False,
        valid_site_ids={"7060"},
        site_id_to_opportunities_dir={},
    )


def make_queue_file(context: RunContext, job_id: str) -> Path:
    queue_file = context.runtime_root / f"{job_id}.json"
    queue_file.write_text("{}\n", encoding="utf-8")
    return queue_file


def shift_job(payload: dict[str, Any], job_id: str = "shift-job-1") -> QueueJob:
    return QueueJob(
        job_id=job_id,
        job_type="record_shift_report",
        payload=payload,
        metadata={},
        intent={},
    )


def shift_payload(**overrides: Any) -> dict[str, Any]:
    payload = {"date": "2026-06-12", "content": SHIFT_MARKDOWN}
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
    # queue_name lets a caller replay the SAME job_id from a distinct queue
    # file, so the handler's "Destination already exists" first-line guard does
    # not pre-empt the apply_canonical_rmw job-id no-op we want to exercise.
    queue_file = make_queue_file(context, queue_name or job_id)
    processed_dir = context.runtime_root / "processed"
    misc.process_record_shift_report_job(queue_file, shift_job(payload, job_id), context, processed_dir)


# --------------------------------------------------------------------------- #
# Contract 1: validate_job accept / reject
# --------------------------------------------------------------------------- #


def test_validate_accepts_minimal_payload() -> None:
    assert validate_job(
        {"job_type": "record_shift_report", "payload": {"date": "2026-06-12", "content": "# Shift Report\n..."}}
    )


def test_validate_accepts_optional_prepared_by_and_source() -> None:
    assert validate_job(
        {
            "job_type": "record_shift_report",
            "payload": {
                "date": "2026-06-12",
                "content": "# Shift Report",
                "prepared_by": "Sarah",
                "source": "closeday",
            },
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"content": "x"}, id="missing-date"),
        pytest.param({"date": "", "content": "x"}, id="empty-date"),
        pytest.param({"date": "   ", "content": "x"}, id="whitespace-date"),
        pytest.param({"date": "2026-06-12"}, id="missing-content"),
        pytest.param({"date": "2026-06-12", "content": ""}, id="empty-content"),
        pytest.param({"date": "2026-06-12", "content": "   "}, id="whitespace-content"),
        pytest.param({"date": "06/12/2026", "content": "x"}, id="slash-date"),
        pytest.param({"date": "2026-06-12", "content": "x", "stray": "y"}, id="stray-key"),
        pytest.param({"date": 20260612, "content": "x"}, id="int-date"),
        pytest.param({"date": "2026-06-12", "content": 42}, id="int-content"),
    ],
)
def test_validate_rejects(payload: dict[str, Any]) -> None:
    assert not validate_job({"job_type": "record_shift_report", "payload": payload})


def test_validate_rejects_non_padded_date() -> None:
    # Beyond-spec: a YYYY-M-D date must be rejected by the zero-padded regex.
    assert not validate_job(
        {"job_type": "record_shift_report", "payload": {"date": "2026-6-2", "content": "x"}}
    )


def test_validate_is_shape_only_accepts_semantically_invalid_month() -> None:
    # OBSERVED (not a defect per spec): the YYYY-MM-DD check is shape-only, so a
    # syntactically well-formed but semantically impossible date like 2026-13-01
    # passes validation. The contract only requires the regex shape; documenting
    # the boundary so a future tightening is a deliberate, visible change.
    assert validate_job(
        {"job_type": "record_shift_report", "payload": {"date": "2026-13-01", "content": "x"}}
    )


# --------------------------------------------------------------------------- #
# Contract 2: processing writes the canonical shift_report doc
# --------------------------------------------------------------------------- #


def test_processing_writes_canonical_shift_report_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, shift_payload(), "shift-job-1", monkeypatch)

    doc = store.get_optional("shift_report_journal_2026_06_12_shift_report")
    assert doc is not None
    assert doc["_id"] == "shift_report_journal_2026_06_12_shift_report"
    assert doc["type"] == "shift_report"
    assert doc["date"] == "2026-06-12"
    assert doc["operator"] == OPERATOR_ID_GREG == "op_greg"
    assert doc["content"] == SHIFT_MARKDOWN  # markdown preserved verbatim
    assert doc["prepared_by"] == "Greg"  # default when omitted
    assert doc["btq_job_ids"] == ["shift-job-1"]
    assert (context.runtime_root / "processed" / "shift-job-1.json").exists()


def test_markdown_special_chars_preserved_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    tricky = "# H1\n\n- [ ] task <tag> & 50% \"quote\" \\backslash\n\n```py\nprint('x')\n```\n"
    run_handler(store, context, shift_payload(content=tricky), "shift-job-tricky", monkeypatch)

    doc = store.get_optional("shift_report_journal_2026_06_12_shift_report")
    assert doc is not None
    assert doc["content"] == tricky


def test_prepared_by_explicit_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, shift_payload(prepared_by="Sarah"), "shift-job-sarah", monkeypatch)

    doc = store.get_optional("shift_report_journal_2026_06_12_shift_report")
    assert doc is not None
    assert doc["prepared_by"] == "Sarah"


# --------------------------------------------------------------------------- #
# Contract 3: idempotent upsert
# --------------------------------------------------------------------------- #


def test_replay_same_job_id_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, shift_payload(), "shift-job-1", monkeypatch)
    after_first = store.get_optional("shift_report_journal_2026_06_12_shift_report")
    assert after_first is not None

    # Replay the SAME job_id with different content; apply_canonical_rmw must
    # short-circuit on the btq_job_ids marker and leave the doc untouched.
    # Use a distinct queue file so the earlier destination-exists guard does
    # not pre-empt the RMW-level no-op under test.
    run_handler(
        store,
        context,
        shift_payload(content="# REPLAYED — should NOT overwrite\n"),
        "shift-job-1",
        monkeypatch,
        queue_name="shift-job-1-replay",
    )

    docs = [d for d in store.docs if d.get("type") == "shift_report"]
    assert len(docs) == 1
    assert docs[0] == after_first
    assert docs[0]["content"] == SHIFT_MARKDOWN


def test_new_job_same_date_upserts_same_id_and_updates_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, shift_payload(), "shift-job-1", monkeypatch)

    revised = "# Shift Report — REVISED 2026-06-12\n\nUpdated open positions.\n"
    run_handler(store, context, shift_payload(content=revised), "shift-job-2", monkeypatch)

    docs = [d for d in store.docs if d.get("type") == "shift_report"]
    assert len(docs) == 1  # same _id, no duplicate
    doc = docs[0]
    assert doc["_id"] == "shift_report_journal_2026_06_12_shift_report"
    assert doc["content"] == revised
    assert doc["btq_job_ids"] == ["shift-job-1", "shift-job-2"]


# --------------------------------------------------------------------------- #
# Contract 4: findable by nightly-digest-style selector
# --------------------------------------------------------------------------- #


def test_doc_findable_by_type_and_date_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, shift_payload(), "shift-job-1", monkeypatch)

    selector = {"type": "shift_report", "date": "2026-06-12"}
    matches = [
        d for d in store.docs if all(d.get(k) == v for k, v in selector.items())
    ]
    assert len(matches) == 1
    assert matches[0]["_id"] == "shift_report_journal_2026_06_12_shift_report"


# --------------------------------------------------------------------------- #
# Beyond-spec: pre-existing unrelated field survives the upsert
# --------------------------------------------------------------------------- #


def test_preexisting_unrelated_field_survives_upsert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    seeded = {
        "_id": "shift_report_journal_2026_06_12_shift_report",
        "type": "shift_report",
        "date": "2026-06-12",
        "operator": OPERATOR_ID_GREG,
        "prepared_by": "Greg",
        "content": "# Old report\n",
        "safetyculture_audit_id": "audit-xyz-123",  # unrelated field set elsewhere
        "btq_job_ids": ["old-job"],
    }
    store = RecordingRmwVaultStore([seeded])
    run_handler(store, context, shift_payload(content="# New report\n"), "shift-job-new", monkeypatch)

    doc = store.get_optional("shift_report_journal_2026_06_12_shift_report")
    assert doc is not None
    assert doc["content"] == "# New report\n"
    assert doc["safetyculture_audit_id"] == "audit-xyz-123"  # MUST survive
    assert doc["btq_job_ids"] == ["old-job", "shift-job-new"]


# --------------------------------------------------------------------------- #
# Contract 5: registry dispatch
# --------------------------------------------------------------------------- #


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_RECORD_SHIFT_REPORT] is misc.process_record_shift_report_job


# --------------------------------------------------------------------------- #
# Contract 6: ops-dashboard summary
# --------------------------------------------------------------------------- #


def test_record_shift_report_is_known_summary_type() -> None:
    assert "record_shift_report" in KNOWN_JOB_SUMMARY_TYPES


def test_render_job_summary_non_empty_with_date_and_prepared_by() -> None:
    summary = render_job_summary("record_shift_report", {"date": "2026-06-12", "prepared_by": "Sarah"})
    assert summary
    assert "2026-06-12" in summary
    assert "Sarah" in summary


def test_render_job_summary_non_empty_without_prepared_by() -> None:
    summary = render_job_summary("record_shift_report", {"date": "2026-06-12", "content": SHIFT_MARKDOWN})
    assert summary
    assert "2026-06-12" in summary


# --------------------------------------------------------------------------- #
# Beyond-spec: store failure fails closed (queue file not moved to processed)
# --------------------------------------------------------------------------- #


class _ExplodingStore(RecordingRmwVaultStore):
    def update_doc(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("couchdb unavailable")


def test_store_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = _ExplodingStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, "shift-job-boom")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        misc.process_record_shift_report_job(
            queue_file, shift_job(shift_payload(), "shift-job-boom"), context, processed_dir
        )

    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()
