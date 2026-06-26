from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import queue_processor.main as qp
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import site_urls
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_SET_SITE_URL, validate_job

from test_queue_processor_couchdb_write import RecordingRmwVaultStore, context_for, job, make_queue_file


def _location_doc(*, urls: list[dict[str, Any]] | None = None, job_ids: list[str] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "location_7060",
        "type": "location",
        "operator": OPERATOR_ID_GREG,
        "site_id": "7060",
        "location": "Continental Metalworks",
        "account": "Contworks",
        "content": "Keep the operational notes intact.",
        "btq_job_ids": list(job_ids or []),
    }
    if urls is not None:
        doc["urls"] = urls
    return doc


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": "7060",
        "action": "add",
        "url": "https://example.com/sites/continental",
        "label": "Location page",
        "kind": "official_location_page",
        "actor": "Greg",
    }
    payload.update(overrides)
    return payload


def _remove_payload() -> dict[str, Any]:
    return {
        "site_id": "7060",
        "action": "remove",
        "url": "https://example.com/sites/continental",
        "actor": "Greg",
    }


def _validatable(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": "job-one", "job_type": JOB_SET_SITE_URL, "payload": payload}


def _run(
    store: RecordingRmwVaultStore,
    context,
    payload: dict[str, Any],
    job_id: str,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(shared, "_VAULT_STORE", store)
    queue_file = make_queue_file(context, job_id)
    processed_dir = context.runtime_root / "processed"
    qp.process_set_site_url_job(queue_file, job(JOB_SET_SITE_URL, payload, job_id=job_id), context, processed_dir)
    return queue_file, processed_dir


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_SET_SITE_URL] is site_urls.process_set_site_url_job


def test_validate_accepts_add_edit_remove() -> None:
    assert validate_job(_validatable(_payload())) is True
    assert validate_job(
        _validatable(
            _payload(
                action="edit",
                new_url="https://example.com/sites/continental-updated",
                status="verified",
                last_verified_at="2026-06-26",
                last_verified_by="Greg",
                verification_note="Operator checked.",
            )
        )
    ) is True
    assert validate_job(_validatable(_remove_payload())) is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload(url="ftp://example.com/site"), id="bad-url-scheme"),
        pytest.param(_payload(kind="directory"), id="bad-kind"),
        pytest.param(_payload(status="trusted"), id="bad-status"),
        pytest.param(_payload(actor=""), id="blank-actor"),
        pytest.param({"site_id": "7060", "action": "edit", "url": "https://example.com/sites/continental", "actor": "Greg"}, id="edit-no-fields"),
        pytest.param({**_remove_payload(), "status": "deprecated"}, id="remove-with-edit-field"),
    ],
)
def test_validate_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    assert validate_job(_validatable(payload)) is False


def test_missing_urls_reads_as_empty_and_add_appends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_location_doc()])

    queue_file, processed_dir = _run(store, context, _payload(), "url-add", monkeypatch)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["urls"] == [
        {
            "url": "https://example.com/sites/continental",
            "label": "Location page",
            "kind": "official_location_page",
            "status": "reference",
            "last_verified_at": None,
            "last_verified_by": None,
            "verification_note": "",
        }
    ]
    assert doc["content"] == "Keep the operational notes intact."
    assert doc["btq_job_ids"] == ["url-add"]
    assert store.update_doc_calls == ["location_7060"]
    assert (processed_dir / queue_file.name).exists()


def test_edit_updates_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore(
        [
            _location_doc(
                urls=[
                    {
                        "url": "https://example.com/sites/continental",
                        "label": "Location page",
                        "kind": "official_location_page",
                        "status": "reference",
                    },
                    {
                        "url": "https://maps.example.com/?q=continental",
                        "label": "Map",
                        "kind": "maps",
                        "status": "reference",
                    },
                ]
            )
        ]
    )

    _run(
        store,
        context,
        _payload(
            action="edit",
            new_url="https://example.com/sites/continental-updated",
            label="Updated page",
            kind="official_location_page",
            status="verified",
            last_verified_at="2026-06-26",
            last_verified_by="Greg",
            verification_note="Checked by operator.",
        ),
        "url-edit",
        monkeypatch,
    )

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert [entry["url"] for entry in doc["urls"]] == [
        "https://example.com/sites/continental-updated",
        "https://maps.example.com/?q=continental",
    ]
    assert doc["urls"][0]["status"] == "verified"
    assert doc["urls"][0]["last_verified_by"] == "Greg"


def test_remove_drops_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore(
        [
            _location_doc(
                urls=[
                    {"url": "https://example.com/sites/continental", "kind": "official_location_page"},
                    {"url": "https://maps.example.com/?q=continental", "kind": "maps"},
                ]
            )
        ]
    )

    _run(store, context, _remove_payload(), "url-remove", monkeypatch)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["urls"] == [
        {
            "url": "https://maps.example.com/?q=continental",
            "label": "",
            "kind": "maps",
            "status": "reference",
            "last_verified_at": None,
            "last_verified_by": None,
            "verification_note": "",
        }
    ]
