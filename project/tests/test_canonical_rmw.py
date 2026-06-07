from __future__ import annotations

import io
import logging
from typing import Any
from urllib import error

import pytest

from btq_vault.couch_store import CouchDBEntityStore, CouchDBEntityStoreError
from event_pipeline.sites import SITES
from queue_processor.canonical_rmw import (
    CanonicalMutation,
    CanonicalTarget,
    apply_canonical_rmw,
    resolve_employee_target,
    resolve_employee_target_by_person_id,
    resolve_site_context,
    resolve_site_target,
)
from queue_processor.handlers._shared import QueueProcessorError


class FakeStore(CouchDBEntityStore):
    def __init__(
        self,
        docs: dict[str, dict[str, Any]] | None = None,
        *,
        employee_docs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.docs = {doc_id: dict(doc) for doc_id, doc in (docs or {}).items()}
        self.employee_docs = [dict(doc) for doc in (employee_docs or [])]
        self.get_optional_calls: list[str] = []
        self.put_with_rev_calls: list[tuple[dict[str, Any], str | None]] = []
        self.conflicts_remaining = 0
        self.rev_counter = 10

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        self.get_optional_calls.append(doc_id)
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None else None

    def put_with_rev(self, doc: dict[str, Any], *, expected_rev: str | None) -> dict[str, Any]:
        self.put_with_rev_calls.append((dict(doc), expected_rev))
        if self.conflicts_remaining:
            self.conflicts_remaining -= 1
            raise _http_error(409)
        self.rev_counter += 1
        stored = dict(doc)
        stored["_rev"] = f"{self.rev_counter}-fake"
        self.docs[str(stored["_id"])] = dict(stored)
        return stored

    def find_employee_docs(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.employee_docs]


def _site(site_id: str = "7020") -> dict[str, Any]:
    return next(site for site in SITES if str(site["site_id"]) == site_id)


def test_update_doc_creates_when_missing_and_allowed() -> None:
    store = FakeStore()

    result = store.update_doc(
        "employee_prs_1",
        lambda doc: {**(doc or {}), "name": "Alex"},
        create=lambda: {"_id": "employee_prs_1", "type": "employee"},
        require_existing=False,
    )

    assert result["_id"] == "employee_prs_1"
    assert result["name"] == "Alex"
    assert store.put_with_rev_calls == [
        ({"_id": "employee_prs_1", "type": "employee", "name": "Alex"}, None)
    ]


def test_update_doc_missing_required_raises() -> None:
    store = FakeStore()

    with pytest.raises(CouchDBEntityStoreError, match="employee_missing"):
        store.update_doc("employee_missing", lambda doc: doc)

    assert store.put_with_rev_calls == []


def test_update_doc_transform_none_is_noop() -> None:
    current = {"_id": "employee_prs_1", "_rev": "1-old", "type": "employee", "name": "Alex"}
    store = FakeStore({"employee_prs_1": current})

    result = store.update_doc("employee_prs_1", lambda _doc: None)

    assert result == current
    assert store.put_with_rev_calls == []


def test_update_doc_retries_once_on_conflict() -> None:
    store = FakeStore({"employee_prs_1": {"_id": "employee_prs_1", "_rev": "1-old", "type": "employee"}})
    store.conflicts_remaining = 1
    transform_calls = 0

    def transform(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        nonlocal transform_calls
        transform_calls += 1
        assert doc is not None
        outgoing = dict(doc)
        outgoing["name"] = "Alex"
        return outgoing

    result = store.update_doc("employee_prs_1", transform)

    assert result["name"] == "Alex"
    assert transform_calls == 2
    assert store.get_optional_calls == ["employee_prs_1", "employee_prs_1"]
    assert len(store.put_with_rev_calls) == 2


def test_apply_canonical_rmw_skips_when_job_id_already_applied() -> None:
    store = FakeStore({
        "employee_prs_1": {"_id": "employee_prs_1", "_rev": "1-old", "type": "employee", "btq_job_ids": ["job-1"]}
    })

    result = apply_canonical_rmw(
        store,
        CanonicalTarget("employee_prs_1", "employee"),
        "job-1",
        lambda state: CanonicalMutation(dict(state.doc or {})),
    )

    assert result is None
    assert store.put_with_rev_calls == []


def test_apply_canonical_rmw_appends_job_id_and_puts() -> None:
    store = FakeStore({
        "employee_prs_1": {
            "_id": "employee_prs_1",
            "_rev": "1-old",
            "type": "employee",
            "content": "Current body",
            "btq_job_ids": ["job-0"],
        }
    })

    def transform(state):
        assert state.doc_id == "employee_prs_1"
        assert state.rev == "1-old"
        assert state.content == "Current body"
        outgoing = dict(state.doc or {})
        outgoing["name"] = "Alex"
        return CanonicalMutation(outgoing, evidence_text="updated name")

    result = apply_canonical_rmw(
        store,
        CanonicalTarget("employee_prs_1", "employee"),
        "job-1",
        transform,
    )

    assert result is not None
    put_doc, expected_rev = store.put_with_rev_calls[-1]
    assert expected_rev == "1-old"
    assert put_doc["_id"] == "employee_prs_1"
    assert put_doc["type"] == "employee"
    assert put_doc["name"] == "Alex"
    assert put_doc["btq_job_ids"] == ["job-0", "job-1"]


def test_apply_canonical_rmw_missing_required_doc_raises() -> None:
    store = FakeStore()

    with pytest.raises(CouchDBEntityStoreError, match="employee_missing"):
        apply_canonical_rmw(
            store,
            CanonicalTarget("employee_missing", "employee"),
            "job-1",
            lambda state: CanonicalMutation(dict(state.doc or {})),
        )


def test_resolve_site_target_uses_site_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    target = resolve_site_target("summit")

    assert target.doc_id == "location_7050"
    assert target.doc_type == "location"
    with pytest.raises(QueueProcessorError):
        resolve_site_target("definitely unknown site")


def test_resolve_site_context_uses_canonical_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    site = _site()
    store = FakeStore(
        {
            f"location_{site['site_id']}": {
                "_id": f"location_{site['site_id']}",
                "type": "location",
                "location": "Custom Canonical Name",
                "account": "ACCT",
            }
        }
    )

    context = resolve_site_context(store, str(site["site_id"]))

    assert context.site_id == str(site["site_id"])
    assert context.name == "Custom Canonical Name"
    assert context.account == "ACCT"


def test_resolve_site_context_falls_back_to_registry_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    site = _site()

    context = resolve_site_context(FakeStore(), str(site["site_id"]))

    assert context.site_id == str(site["site_id"])
    assert context.name == site["canonical"]
    assert context.account is None


def test_resolve_site_context_unresolvable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    with pytest.raises(QueueProcessorError, match="Could not resolve canonical site target: not-a-real-site"):
        resolve_site_context(FakeStore(), "not-a-real-site")


def test_resolve_site_context_mismatch_logs_and_canonical_wins(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    site = _site()
    store = FakeStore(
        {
            f"location_{site['site_id']}": {
                "_id": f"location_{site['site_id']}",
                "type": "location",
                "location": "Different Canonical Name",
                "account": "ACCT",
            }
        }
    )

    with caplog.at_level(logging.WARNING):
        context = resolve_site_context(store, str(site["site_id"]))

    assert context.name == "Different Canonical Name"
    assert f"site_id={site['site_id']}" in caplog.text
    assert "Different Canonical Name" in caplog.text
    assert str(site["canonical"]) in caplog.text


def test_resolve_employee_target_by_person_id() -> None:
    target = resolve_employee_target_by_person_id(" prs_123 ")

    assert target.doc_id == "employee_prs_123"
    assert target.doc_type == "employee"
    assert target.allow_create is False
    assert target.require_existing is True


def test_resolve_employee_target_by_person_id_field() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_123", "type": "employee", "person_id": "prs_123", "name": "Eric Dalton"}
    ])

    target = resolve_employee_target(store, " prs_123 ")

    assert target.doc_id == "employee_prs_123"
    assert target.doc_type == "employee"
    assert target.allow_create is False
    assert target.require_existing is True


def test_resolve_employee_target_by_doc_id() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_456", "type": "employee", "person_id": "different", "name": "Eric Dalton"}
    ])

    target = resolve_employee_target(store, "prs_456")

    assert target.doc_id == "employee_prs_456"
    assert target.doc_type == "employee"


def test_resolve_employee_target_by_doc_id_accepts_hyphen_underscore_variants() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_keller-bruce", "type": "employee", "person_id": "keller-bruce", "name": "Bruce Keller"},
        {"_id": "employee_mills_carol", "type": "employee", "person_id": "mills_carol", "name": "Carol Mills"},
    ])

    hyphen_target = resolve_employee_target(store, "keller_bruce")
    underscore_target = resolve_employee_target(store, "mills-carol")

    assert hyphen_target.doc_id == "employee_keller-bruce"
    assert underscore_target.doc_id == "employee_mills_carol"


def test_resolve_employee_target_by_employee_id() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_123", "type": "employee", "person_id": "prs_123", "employee_id": "BTQ-001"}
    ])

    target = resolve_employee_target(store, "BTQ-001")

    assert target.doc_id == "employee_prs_123"
    assert target.doc_type == "employee"


def test_resolve_employee_target_by_normalized_name() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_123", "type": "employee", "person_id": "prs_123", "name": "Eric Dalton"}
    ])

    target = resolve_employee_target(store, "Dalton, Eric")

    assert target.doc_id == "employee_prs_123"
    assert target.doc_type == "employee"


def test_resolve_employee_target_loose_first_name_requires_last_name_anchor() -> None:
    store = FakeStore(employee_docs=[
        {
            "_id": "employee_mills-carol",
            "type": "employee",
            "person_id": "mills-carol",
            "name": "Carol Mills",
            "first": "Carol",
            "last": "Mills",
            "preferred_name": "",
        },
        {
            "_id": "employee_beveridge-carol",
            "type": "employee",
            "person_id": "beveridge-carol",
            "name": "Carol Beveridge",
            "first": "Carol",
            "last": "Beveridge",
        },
    ])

    target = resolve_employee_target(store, "Deb Mills")

    assert target.doc_id == "employee_mills-carol"


def test_resolve_employee_target_first_name_only_ambiguity_raises() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_mills-carol", "type": "employee", "first": "Carol", "last": "Mills"},
        {"_id": "employee_beveridge-carol", "type": "employee", "first": "Carol", "last": "Beveridge"},
    ])

    with pytest.raises(QueueProcessorError, match="Ambiguous canonical employee target"):
        resolve_employee_target(store, "Carol")


def test_resolve_employee_target_ambiguous_name_raises() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_123", "type": "employee", "person_id": "prs_123", "name": "Eric Dalton"},
        {"_id": "employee_prs_456", "type": "employee", "person_id": "prs_456", "first": "Eric", "last": "Dalton"},
    ])

    with pytest.raises(QueueProcessorError):
        resolve_employee_target(store, "seth dalton")


def test_resolve_employee_target_not_found_raises() -> None:
    store = FakeStore(employee_docs=[
        {"_id": "employee_prs_123", "type": "employee", "person_id": "prs_123", "name": "Eric Dalton"}
    ])

    with pytest.raises(QueueProcessorError, match="Could not resolve canonical employee target"):
        resolve_employee_target(store, "Unknown Person")


def test_resolve_employee_target_empty_value_raises() -> None:
    store = FakeStore()

    with pytest.raises(QueueProcessorError):
        resolve_employee_target(store, "   ")


def _http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://example.test", code, "error", {}, io.BytesIO(b""))
