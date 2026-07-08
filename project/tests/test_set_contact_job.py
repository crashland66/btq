from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import queue_processor.main as qp
from btq_vault.contacts import contacts_from_doc
from btq_vault.entity_types import OPERATOR_ID_GREG
from queue_processor.handlers import _shared as shared
from queue_processor.handlers import contacts
from queue_processor.registry import JOB_HANDLERS
from queue_spec import JOB_SET_CONTACT, validate_job

from test_queue_processor_couchdb_write import RecordingRmwVaultStore, context_for, job, make_queue_file


def _location_doc(*, contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "location_7060",
        "type": "location",
        "operator": OPERATOR_ID_GREG,
        "site_id": "7060",
        "location": "Continental Metalworks",
        "account": "Contworks",
        "content": "Keep the operational notes intact.",
        "customer_portal_contact": "legacy customer data",
        "btq_job_ids": [],
    }
    if contacts is not None:
        doc["site_contacts"] = contacts
    return doc


def _account_doc(*, contacts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "account_phn",
        "type": "account",
        "account": "PHN",
        "name": "Public Health Network",
        "aliases": ["PHN Clinics"],
        "customer_relationship_owner": "legacy customer data",
        "btq_job_ids": [],
    }
    if contacts is not None:
        doc["account_contacts"] = contacts
    return doc


def _contact(**overrides: Any) -> dict[str, Any]:
    contact: dict[str, Any] = {
        "id": "contact_jeremy",
        "name": "Jeremy Synthetic",
        "title": "Operations Manager",
        "phone": "555-0101",
        "email": "jeremy@example.com",
        "role": "account_escalation",
        "scope": "account",
        "source": "operator_verified",
        "source_date": "2026-07-07",
        "notes": "Synthetic fixture.",
    }
    contact.update(overrides)
    return contact


def _site_contact(**overrides: Any) -> dict[str, Any]:
    contact = _contact(
        id="contact_jackie",
        name="Jackie Synthetic",
        role="site_contact",
        scope="site",
        source="field_capture",
    )
    contact.update(overrides)
    return contact


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "upsert",
        "target": {"type": "site", "id": "7060"},
        "actor": "Greg",
        "source": "ops_dashboard_site_detail",
        "contact": _site_contact(),
    }
    payload.update(overrides)
    return payload


def _remove_payload(target: dict[str, str] | None = None, contact_id: str = "contact_jackie") -> dict[str, Any]:
    return {
        "action": "remove",
        "target": target or {"type": "site", "id": "7060"},
        "actor": "Greg",
        "contact": {"id": contact_id},
    }


def _validatable(payload: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": "job-one", "job_type": JOB_SET_CONTACT, "payload": payload}


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
    qp.process_set_contact_job(queue_file, job(JOB_SET_CONTACT, payload, job_id=job_id), context, processed_dir)
    return queue_file, processed_dir


def test_registry_dispatch_points_at_handler() -> None:
    assert JOB_HANDLERS[JOB_SET_CONTACT] is contacts.process_set_contact_job


def test_contacts_from_doc_reads_known_collections() -> None:
    assert contacts_from_doc(_location_doc(contacts=[_site_contact(name=" Jackie ")]), "site_contacts")[0]["name"] == "Jackie"


def test_validate_accepts_upsert_and_remove() -> None:
    assert validate_job(_validatable(_payload())) is True
    assert validate_job(_validatable(_remove_payload())) is True


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload(contact=_site_contact(scope="account")), id="scope-mismatch"),
        pytest.param(_payload(contact=_site_contact(role="primary")), id="unknown-role"),
        pytest.param(_payload(contact=_site_contact(name="")), id="missing-name"),
        pytest.param(_payload(contact=_site_contact(phone="", email="", notes="")), id="missing-contact-method"),
        pytest.param(_payload(contact=_site_contact(email="jackie.example.com")), id="bad-email"),
        pytest.param(_payload(contact=_site_contact(source_date="07/07/2026")), id="bad-source-date"),
        pytest.param(_payload(extra=True), id="unknown-top-level"),
        pytest.param(_payload(contact={**_site_contact(), "extra": "x"}), id="unknown-contact-field"),
        pytest.param(_remove_payload() | {"contact": {"id": "contact_jackie", "name": "Jackie Synthetic"}}, id="remove-extra-field"),
    ],
)
def test_validate_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    assert validate_job(_validatable(payload)) is False


def test_site_upsert_adds_and_replaces_by_id_without_touching_customer_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_location_doc()])

    _run(store, context, _payload(), "contact-site-upsert-one", monkeypatch)
    _run(
        store,
        context,
        _payload(contact=_site_contact(name="Jackie Updated", phone="555-0199", notes="Replacement.")),
        "contact-site-upsert-two",
        monkeypatch,
    )

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert len(doc["site_contacts"]) == 1
    assert doc["site_contacts"][0]["id"] == "contact_jackie"
    assert doc["site_contacts"][0]["name"] == "Jackie Updated"
    assert doc["site_contacts"][0]["phone"] == "555-0199"
    assert doc["customer_portal_contact"] == "legacy customer data"
    assert doc["btq_job_ids"] == ["contact-site-upsert-one", "contact-site-upsert-two"]


def test_account_upsert_accepts_canonical_account_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_account_doc()])

    _run(
        store,
        context,
        _payload(target={"type": "account", "id": "account_phn"}, contact=_contact()),
        "contact-account-upsert",
        monkeypatch,
    )

    doc = store.get_optional("account_phn")
    assert doc is not None
    assert doc["account_contacts"][0]["id"] == "contact_jeremy"
    assert doc["account_contacts"][0]["scope"] == "account"
    assert doc["customer_relationship_owner"] == "legacy customer data"


def test_account_upsert_resolves_unambiguous_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_account_doc()])

    _run(
        store,
        context,
        _payload(target={"type": "account", "id": "PHN Clinics"}, contact=_contact(email="jeremy.updated@example.com")),
        "contact-account-alias",
        monkeypatch,
    )

    doc = store.get_optional("account_phn")
    assert doc is not None
    assert doc["account_contacts"][0]["email"] == "jeremy.updated@example.com"


def test_remove_deletes_by_id_and_absent_remove_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([_location_doc(contacts=[_site_contact()])])

    _run(store, context, _remove_payload(), "contact-remove", monkeypatch)
    _run(store, context, _remove_payload(contact_id="contact_missing"), "contact-remove-missing", monkeypatch)

    doc = store.get_optional("location_7060")
    assert doc is not None
    assert doc["site_contacts"] == []
    assert doc["customer_portal_contact"] == "legacy customer data"


def test_missing_target_doc_errors_without_creating_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = context_for(tmp_path)
    store = RecordingRmwVaultStore([])

    with pytest.raises(qp.QueueJobError):
        _run(store, context, _payload(), "contact-missing-target", monkeypatch)

    assert store.get_optional("location_7060") is None
