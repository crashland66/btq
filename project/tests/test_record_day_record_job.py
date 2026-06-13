"""INDEPENDENT VERIFIER tests for the record_day_record queue job.

Authored against the contract for the (uncommitted) `record_day_record` job
type — the structural twin of `record_shift_report`. These tests were NOT
written by the implementer. They drive the real handler with an in-memory
recording vault store (the same RunContext + QueueJob harness used by the rest
of the queue-processor suite), exercise validate_job, the registry dispatch,
the canonical-type registration (Python set AND CouchDB design doc), and the
ops-dashboard summary surface, and probe beyond spec.

Notably distinct from the shift_report twin: a day_record doc has NO
`prepared_by` field, and its _id is plain `day_record_<date>` (no journal /
suffix segments).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from btq_vault.entity_types import CANONICAL_ENTITY_TYPES, OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import misc
from queue_processor.main import QueueJob, QueueJobError, RunContext
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_RECORD_DAY_RECORD, validate_job
from ops_dashboard.common import KNOWN_JOB_SUMMARY_TYPES, render_job_summary

from tests.test_queue_processor_couchdb_write import RecordingRmwVaultStore


DAY_MARKDOWN = (
    "## Day Record — 2026-06-12\n\n"
    "### Highlights\n"
    "- 7060 Continental Metalworks: dock walk-through complete\n\n"
    "### Notes\n"
    "Covered the **bold** _italic_ `code` 100% recap.\n"
)

DOC_ID = "day_record_2026_06_12"
DESIGN_DOC_PATH = (
    Path(__file__).resolve().parent.parent
    / "event_pipeline"
    / "couchdb"
    / "design_btq_vault.json"
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


def day_job(payload: dict[str, Any], job_id: str = "day-job-1") -> QueueJob:
    return QueueJob(
        job_id=job_id,
        job_type="record_day_record",
        payload=payload,
        metadata={},
        intent={},
    )


def day_payload(**overrides: Any) -> dict[str, Any]:
    payload = {"date": "2026-06-12", "content": DAY_MARKDOWN}
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
    # A distinct queue_name lets a caller replay the SAME job_id from a separate
    # queue file so the handler's "Destination already exists" first-line guard
    # does not pre-empt the apply_canonical_rmw job-id no-op we want to exercise.
    queue_file = make_queue_file(context, queue_name or job_id)
    processed_dir = context.runtime_root / "processed"
    misc.process_record_day_record_job(queue_file, day_job(payload, job_id), context, processed_dir)


def _design_validator_string() -> str:
    # The whole file must parse as JSON (guard against a botched edit); the
    # validate_doc_update validator function lives at the design-doc top level.
    raw = json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))
    return raw["validate_doc_update"]


# --------------------------------------------------------------------------- #
# Contract 1: validate_job accept / reject
# --------------------------------------------------------------------------- #


def test_validate_accepts_minimal_payload() -> None:
    assert validate_job(
        {
            "job_type": "record_day_record",
            "payload": {"date": "2026-06-12", "content": "## Day Record — 2026-06-12\n…"},
        }
    )


def test_validate_accepts_optional_source() -> None:
    assert validate_job(
        {
            "job_type": "record_day_record",
            "payload": {"date": "2026-06-12", "content": "## Day Record", "source": "closeday"},
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
        # prepared_by is a shift_report-only field — it is NOT in the
        # record_day_record allowlist and must be rejected as a stray key.
        pytest.param({"date": "2026-06-12", "content": "x", "prepared_by": "Greg"}, id="shift-only-prepared_by"),
    ],
)
def test_validate_rejects(payload: dict[str, Any]) -> None:
    assert not validate_job({"job_type": "record_day_record", "payload": payload})


def test_validate_rejects_non_padded_date() -> None:
    # Beyond-spec: a YYYY-M-D date must be rejected by the zero-padded regex.
    assert not validate_job(
        {"job_type": "record_day_record", "payload": {"date": "2026-6-2", "content": "x"}}
    )


# --------------------------------------------------------------------------- #
# Contract 2: processing writes the canonical day_record doc
# --------------------------------------------------------------------------- #


def test_processing_writes_canonical_day_record_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, day_payload(), "day-job-1", monkeypatch)

    doc = store.get_optional(DOC_ID)
    assert doc is not None
    assert doc["_id"] == DOC_ID
    assert doc["type"] == "day_record"
    assert doc["date"] == "2026-06-12"
    assert doc["operator"] == OPERATOR_ID_GREG == "op_greg"
    assert doc["content"] == DAY_MARKDOWN  # markdown preserved verbatim
    assert doc["btq_job_ids"] == ["day-job-1"]
    # day_record carries no prepared_by (distinguishes it from shift_report).
    assert "prepared_by" not in doc
    assert (context.runtime_root / "processed" / "day-job-1.json").exists()


def test_markdown_special_chars_preserved_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    tricky = "## Day\n\n- [ ] task <tag> & 50% \"quote\" \\backslash\n\n```py\nprint('x')\n```\n— em–dash ✓\n"
    run_handler(store, context, day_payload(content=tricky), "day-job-tricky", monkeypatch)

    doc = store.get_optional(DOC_ID)
    assert doc is not None
    assert doc["content"] == tricky


# --------------------------------------------------------------------------- #
# Contract 3: idempotent upsert
# --------------------------------------------------------------------------- #


def test_replay_same_job_id_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, day_payload(), "day-job-1", monkeypatch)
    after_first = store.get_optional(DOC_ID)
    assert after_first is not None

    # Replay the SAME job_id with different content; apply_canonical_rmw must
    # short-circuit on the btq_job_ids marker and leave the doc untouched. Use a
    # distinct queue file so the destination-exists guard does not pre-empt the
    # RMW-level no-op under test.
    run_handler(
        store,
        context,
        day_payload(content="## REPLAYED — should NOT overwrite\n"),
        "day-job-1",
        monkeypatch,
        queue_name="day-job-1-replay",
    )

    docs = [d for d in store.docs if d.get("type") == "day_record"]
    assert len(docs) == 1
    assert docs[0] == after_first
    assert docs[0]["content"] == DAY_MARKDOWN


def test_new_job_same_date_upserts_same_id_and_updates_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, day_payload(), "day-job-1", monkeypatch)

    revised = "## Day Record — REVISED 2026-06-12\n\nUpdated recap.\n"
    run_handler(store, context, day_payload(content=revised), "day-job-2", monkeypatch)

    docs = [d for d in store.docs if d.get("type") == "day_record"]
    assert len(docs) == 1  # same _id, no duplicate
    doc = docs[0]
    assert doc["_id"] == DOC_ID
    assert doc["content"] == revised
    assert doc["btq_job_ids"] == ["day-job-1", "day-job-2"]


# --------------------------------------------------------------------------- #
# Contract 4: findable by nightly-digest-style selector
# --------------------------------------------------------------------------- #


def test_doc_findable_by_type_and_date_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore()
    run_handler(store, context, day_payload(), "day-job-1", monkeypatch)

    selector = {"type": "day_record", "date": "2026-06-12"}
    matches = [d for d in store.docs if all(d.get(k) == v for k, v in selector.items())]
    assert len(matches) == 1
    assert matches[0]["_id"] == DOC_ID


# --------------------------------------------------------------------------- #
# Beyond-spec: pre-existing unrelated field survives the upsert
# --------------------------------------------------------------------------- #


def test_preexisting_unrelated_field_survives_upsert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    seeded = {
        "_id": DOC_ID,
        "type": "day_record",
        "date": "2026-06-12",
        "operator": OPERATOR_ID_GREG,
        "content": "## Old record\n",
        "client_pinned": "keep-me-9000",  # unrelated field set elsewhere
        "btq_job_ids": ["old-job"],
    }
    store = RecordingRmwVaultStore([seeded])
    run_handler(store, context, day_payload(content="## New record\n"), "day-job-new", monkeypatch)

    doc = store.get_optional(DOC_ID)
    assert doc is not None
    assert doc["content"] == "## New record\n"
    assert doc["client_pinned"] == "keep-me-9000"  # MUST survive
    assert doc["btq_job_ids"] == ["old-job", "day-job-new"]


# --------------------------------------------------------------------------- #
# Contract 5: canonical-type registration — Python set AND design doc
# --------------------------------------------------------------------------- #


def test_day_record_in_python_canonical_entity_types() -> None:
    assert "day_record" in CANONICAL_ENTITY_TYPES
    # Guard: nothing else dropped from the Python set.
    for required in ("shift_report", "journal", "note"):
        assert required in CANONICAL_ENTITY_TYPES


def test_day_record_in_design_doc_canonical_types() -> None:
    validator = _design_validator_string()
    assert '"day_record": true' in validator


def test_design_doc_validator_is_valid_json_and_other_types_intact() -> None:
    # The whole design doc must still parse as JSON (guard against a botched
    # edit), and the OTHER canonical types must still be present in the
    # validate_doc_update string — i.e. the day_record insertion did not drop a
    # sibling type.
    validator = _design_validator_string()
    for required in ("account", "location", "employee", "shift_report", "journal", "note", "operator"):
        assert f'"{required}": true' in validator, f"design doc dropped canonical type {required!r}"


def test_python_set_and_design_doc_agree() -> None:
    # Cross-check: every canonical type the Python set declares is also present
    # in the design-doc validator string (the two are documented as kept in
    # sync). This catches drift in either direction for the registration pair.
    validator = _design_validator_string()
    for entity_type in CANONICAL_ENTITY_TYPES:
        assert f'"{entity_type}": true' in validator, f"{entity_type!r} missing from design doc"


# --------------------------------------------------------------------------- #
# Contract 6: registry dispatch
# --------------------------------------------------------------------------- #


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_RECORD_DAY_RECORD] is misc.process_record_day_record_job


# --------------------------------------------------------------------------- #
# Contract 7: ops-dashboard summary
# --------------------------------------------------------------------------- #


def test_record_day_record_is_known_summary_type() -> None:
    assert "record_day_record" in KNOWN_JOB_SUMMARY_TYPES


def test_render_job_summary_non_empty_with_date() -> None:
    summary = render_job_summary("record_day_record", {"date": "2026-06-12", "content": DAY_MARKDOWN})
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
    queue_file = make_queue_file(context, "day-job-boom")
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueJobError, match="canonical couchdb write failed"):
        misc.process_record_day_record_job(
            queue_file, day_job(day_payload(), "day-job-boom"), context, processed_dir
        )

    assert not (processed_dir / queue_file.name).exists()
    assert queue_file.exists()
