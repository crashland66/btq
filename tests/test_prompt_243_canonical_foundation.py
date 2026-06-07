from __future__ import annotations

from pathlib import Path
from typing import Any

import canonical_domain
import vault_errors
from queue_processor.canonical_rmw import resolve_person_vault_path, resolve_site_vault_path


class RecordingPathStore:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self.docs = docs
        self.requested_doc_ids: list[str] = []

    def get_optional(self, doc_id: str) -> dict[str, Any] | None:
        self.requested_doc_ids.append(doc_id)
        doc = self.docs.get(doc_id)
        return dict(doc) if doc is not None else None


def test_vault_errors_define_repository_exceptions() -> None:
    assert issubclass(vault_errors.NotFoundError, vault_errors.VaultRepositoryError)
    assert issubclass(vault_errors.AmbiguousResolutionError, vault_errors.VaultRepositoryError)
    assert issubclass(vault_errors.ValidationError, vault_errors.VaultRepositoryError)


def test_canonical_domain_defines_vault_entities() -> None:
    site = canonical_domain.Site(
        site_id=canonical_domain.SiteId("7050"),
        canonical_name="Summit Wire",
        account="Accounts",
        aliases=("Summit",),
        address="1 Main St",
        monthly_supply_budget=125.50,
        budget_basis="monthly",
        about_path=Path("Accounts/Summit Wire/about.md"),
    )
    person = canonical_domain.Person(
        person_id=canonical_domain.PersonId("p42"),
        canonical_name="Ada Lovelace",
        aliases=("Ada",),
        status="active",
        site_ids=(canonical_domain.SiteId("7050"),),
        note_path=Path("People/Lovelace, Ada.md"),
    )

    assert site == canonical_domain.Site(
        site_id=canonical_domain.SiteId("7050"),
        canonical_name="Summit Wire",
        account="Accounts",
        aliases=("Summit",),
        address="1 Main St",
        monthly_supply_budget=125.50,
        budget_basis="monthly",
        about_path=Path("Accounts/Summit Wire/about.md"),
    )
    assert person == canonical_domain.Person(
        person_id=canonical_domain.PersonId("p42"),
        canonical_name="Ada Lovelace",
        aliases=("Ada",),
        status="active",
        site_ids=(canonical_domain.SiteId("7050"),),
        note_path=Path("People/Lovelace, Ada.md"),
    )


def test_resolve_site_vault_path_reads_canonical_doc() -> None:
    store = RecordingPathStore(
        {
            "location_7050": {
                "_id": "location_7050",
                "type": "location",
                "vault_path": "Accounts/Summit Wire/7050 - Summit Wire/about.md",
            }
        }
    )

    assert resolve_site_vault_path(store, "7050") == Path(
        "Accounts/Summit Wire/7050 - Summit Wire/about.md"
    )
    assert store.requested_doc_ids == ["location_7050"]


def test_resolve_site_vault_path_missing_doc_or_path_raises_not_found() -> None:
    missing_store = RecordingPathStore({})
    empty_path_store = RecordingPathStore(
        {"location_7050": {"_id": "location_7050", "type": "location", "vault_path": "   "}}
    )

    try:
        resolve_site_vault_path(missing_store, "7050")
    except vault_errors.NotFoundError:
        pass
    else:
        raise AssertionError("missing canonical site doc did not raise NotFoundError")

    try:
        resolve_site_vault_path(empty_path_store, "7050")
    except vault_errors.NotFoundError:
        pass
    else:
        raise AssertionError("empty canonical site vault_path did not raise NotFoundError")


def test_resolve_person_vault_path_reads_canonical_doc() -> None:
    store = RecordingPathStore(
        {
            "employee_p42": {
                "_id": "employee_p42",
                "type": "employee",
                "vault_path": "People/Lovelace, Ada.md",
            }
        }
    )

    assert resolve_person_vault_path(store, "p42") == Path("People/Lovelace, Ada.md")
    assert store.requested_doc_ids == ["employee_p42"]
