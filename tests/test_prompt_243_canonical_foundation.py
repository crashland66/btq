from __future__ import annotations

from pathlib import Path

import canonical_domain
import vault_errors


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
