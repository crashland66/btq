from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import queue_processor.main as qp
from queue_processor.canonical_rmw import SiteContext
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import supply_requests
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_CREATE_SUPPLY_REQUEST, JOB_SCHEMAS, validate_job
from tests.test_queue_processor_couchdb_write import (
    RecordingRmwVaultStore,
    context_for,
    job,
    make_queue_file,
)


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": "public-site-001",
        "requested_by": " Public Worker ",
        "observed_at": "2026-07-22T09:15:00-04:00",
        "items": [
            {"item_name": " Paper products ", "quantity": " 2 cases ", "note": " Upstairs "},
            {"item_name": " Hand soap ", "quantity": 4, "unit": " containers "},
        ],
        "notes": " Restock request ",
        "related_capture_ids": [" capture-public-001 ", "capture-public-002"],
        "source": " field_capture_audio ",
    }
    payload.update(overrides)
    return payload


def _validatable(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_type": JOB_CREATE_SUPPLY_REQUEST, "payload": payload}


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: RecordingRmwVaultStore,
    payload: dict[str, Any],
    *,
    job_id: str = "request-public-001",
    filename: str | None = None,
    dry_run: bool = False,
) -> tuple[qp.RunContext, Path]:
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    monkeypatch.setattr(
        supply_requests,
        "resolve_site_context",
        lambda _store, value: SiteContext(str(value), "Public Test Site", "Public Account"),
    )
    context = context_for(tmp_path)
    if dry_run:
        context = replace(context, dry_run=True)
    queue_file = make_queue_file(context, filename or job_id)
    processed_dir = context.runtime_root / "processed"
    supply_requests.process_create_supply_request_job(
        queue_file,
        job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id=job_id),
        context,
        processed_dir,
    )
    return context, queue_file


def test_registry_and_required_fields() -> None:
    assert JOB_HANDLERS[JOB_CREATE_SUPPLY_REQUEST] is supply_requests.process_create_supply_request_job
    assert JOB_SCHEMAS[JOB_CREATE_SUPPLY_REQUEST] == [
        "site_id",
        "requested_by",
        "items",
        "observed_at",
    ]


def test_valid_multi_item_request_writes_one_canonical_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    context, queue_file = _run(tmp_path, monkeypatch, store, _payload())

    docs = [doc for doc in store.docs if doc.get("type") == "supply_request"]
    assert len(docs) == 1
    doc = docs[0]
    assert doc["_id"] == f"supply_request_{doc['supply_request_id']}"
    assert doc["site_id"] == "public-site-001"
    assert doc["requested_by"] == "Public Worker"
    assert doc["observed_at"] == "2026-07-22T09:15:00-04:00"
    assert doc["items"] == [
        {"item_name": "Paper products", "quantity": "2 cases", "note": "Upstairs"},
        {"item_name": "Hand soap", "quantity": 4, "unit": "containers"},
    ]
    assert doc["related_capture_ids"] == ["capture-public-001", "capture-public-002"]
    assert doc["notes"] == "Restock request"
    assert doc["source"] == "field_capture_audio"
    assert doc["status"] == "open"
    assert doc["btq_job_ids"] == ["request-public-001"]
    assert len(store.update_doc_calls) == 1
    assert (context.runtime_root / "processed" / queue_file.name).exists()


def test_single_item_and_duplicate_names_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    duplicate_items = [
        {"item_name": "Cleaner", "note": "North restroom"},
        {"item_name": "Cleaner", "note": "South restroom"},
    ]
    _run(tmp_path, monkeypatch, store, _payload(items=duplicate_items))
    assert store.docs[0]["items"] == duplicate_items

    single_store = RecordingRmwVaultStore()
    _run(
        tmp_path / "single",
        monkeypatch,
        single_store,
        _payload(items=[{"item_name": "Liners"}]),
        job_id="request-public-single",
    )
    assert single_store.docs[0]["items"] == [{"item_name": "Liners"}]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"site_id": "public-site-001", "requested_by": "Public Worker"}, id="missing-items"),
        pytest.param(
            _payload(observed_at=None),
            id="missing-observed-at",
        ),
        pytest.param(_payload(items=[]), id="empty-items"),
        pytest.param(_payload(items=[{"item_name": "  "}]), id="blank-item-name"),
        pytest.param(_payload(estimated_cost=None), id="financial-top-level-field"),
        pytest.param(
            _payload(items=[{"item_name": "Paper products", "unit_price": None}]),
            id="financial-item-field",
        ),
    ],
)
def test_invalid_item_lists_are_rejected_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    assert validate_job(_validatable(payload)) is False
    store = RecordingRmwVaultStore()
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context_for(tmp_path), "invalid-request")
    with pytest.raises(qp.QueueJobError, match="non-empty site_id, requested_by, observed_at, and items"):
        supply_requests.process_create_supply_request_job(
            queue_file,
            job(JOB_CREATE_SUPPLY_REQUEST, payload, job_id="invalid-request"),
            context_for(tmp_path),
            tmp_path / "processed",
        )
    assert store.update_doc_calls == []


def test_same_job_replay_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RecordingRmwVaultStore()
    payload = _payload()
    _run(tmp_path, monkeypatch, store, payload, filename="first")
    _run(tmp_path, monkeypatch, store, payload, filename="replay")

    assert len(store.docs) == 1
    assert store.docs[0]["items"] == [
        {"item_name": "Paper products", "quantity": "2 cases", "note": "Upstairs"},
        {"item_name": "Hand soap", "quantity": 4, "unit": "containers"},
    ]
    assert store.docs[0]["btq_job_ids"] == ["request-public-001"]


def test_resubmission_preserves_provenance_and_explicit_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RecordingRmwVaultStore()
    payload = _payload(request_id="request-public-explicit")
    _run(tmp_path, monkeypatch, store, payload, job_id="request-public-a", filename="first")
    _run(tmp_path, monkeypatch, store, payload, job_id="request-public-b", filename="second")

    assert len(store.docs) == 1
    assert store.docs[0]["supply_request_id"] == "request-public-explicit"
    assert store.docs[0]["btq_job_ids"] == ["request-public-a", "request-public-b"]


def test_dry_run_reports_target_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = RecordingRmwVaultStore()
    payload = _payload()
    expected_target = f"supply_request_{supply_requests.supply_request_id(payload)}"
    context, queue_file = _run(tmp_path, monkeypatch, store, payload, dry_run=True)

    output = capsys.readouterr().out
    assert f"target {expected_target}" in output
    assert "would create supply request" in output
    assert store.update_doc_calls == []
    assert queue_file.exists()
    assert not (context.runtime_root / "processed" / queue_file.name).exists()


def test_existing_log_supply_need_schema_is_unchanged() -> None:
    assert JOB_SCHEMAS[qp.JOB_LOG_SUPPLY_NEED] == ["site_id", "item_name", "requested_by"]
