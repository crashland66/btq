"""Vault-aware authorization on top of the dependency-free token store.

The token store itself (``TokenStore``, ``TokenRecord``, and the SQLite/crypto
helpers) lives in the top-level ``token_store`` module so it can be imported
without the vault toolchain. This module adds the parts that need the vault —
turning an authenticated token into a session with a resolved person and the
sites they may act on — and re-exports the token-store names so existing
``from field_capture.auth import ...`` callers keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any, Iterable

from btq_vault.couch_store import CouchDBEntityStore
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from canonical_domain import Person, PersonId, Site, SiteId
from field_capture.person_slugs import employee_slug_candidates, person_slug
from vault_errors import NotFoundError

from token_store import (
    CreatedToken,
    TokenRecord,
    TokenStore,
    UNIVERSAL_SITE_SCOPE,
    ensure_token_columns,
    isoformat,
    normalize_site_ids,
    parse_site_ids,
    parse_timestamp,
    row_to_record,
    token_hash,
    token_records_as_rows,
    utc_now,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AuthorizedSession",
    "CreatedToken",
    "TokenRecord",
    "TokenStore",
    "UNIVERSAL_SITE_SCOPE",
    "allowed_sites_for_person",
    "authorize_token",
    "load_person_from_canonical",
    "site_from_registry",
    "ensure_token_columns",
    "isoformat",
    "normalize_site_ids",
    "parse_site_ids",
    "parse_timestamp",
    "row_to_record",
    "token_hash",
    "token_records_as_rows",
    "utc_now",
]


@dataclass(frozen=True)
class AuthorizedSession:
    record: TokenRecord
    person: Person
    sites: tuple[Site, ...]


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return (text,) if text else ()


def _employee_site_ids(doc: dict[str, Any]) -> tuple[SiteId, ...]:
    site_ids = _string_list(doc.get("site_ids"))
    if not site_ids:
        site_ids = _string_list(doc.get("job")) + _string_list(doc.get("additional_jobs"))
    if not site_ids:
        site_ids = _string_list(doc.get("sites"))
    seen: set[str] = set()
    deduped: list[SiteId] = []
    for site_id in site_ids:
        if site_id not in seen:
            deduped.append(SiteId(site_id))
            seen.add(site_id)
    return tuple(deduped)


def _employee_alias_slugs(doc: dict[str, Any]) -> set[str]:
    slugs = employee_slug_candidates(
        first=doc.get("first"),
        last=doc.get("last"),
    )
    aliases = doc.get("aliases")
    if isinstance(aliases, (list, tuple, set)):
        for alias in aliases:
            text = str(alias).strip()
            if text:
                slugs.add(person_slug(text))
    return slugs


def _doc_matches_person_id(doc: dict[str, Any], person_id: str) -> bool:
    if str(doc.get("type") or "") != "employee":
        return False
    if str(doc.get("person_id") or "").strip() == person_id:
        return True
    if str(doc.get("_id") or "").strip() == f"employee_{person_id}":
        return True
    return person_slug(person_id) in _employee_alias_slugs(doc)


def _first_matching_employee_doc(docs: object, person_id: str) -> dict[str, Any] | None:
    if not isinstance(docs, list):
        return None
    for doc in docs:
        if isinstance(doc, dict) and _doc_matches_person_id(doc, person_id):
            return doc
    return None


def _find_employee_fallback(store: object, person_id: str) -> dict[str, Any] | None:
    finder = getattr(store, "find_employee_docs_by_person_id", None)
    if callable(finder):
        doc = _first_matching_employee_doc(finder(person_id), person_id)
        if doc is not None:
            return doc

    finder = getattr(store, "find_employee_docs", None)
    if callable(finder):
        return _first_matching_employee_doc(finder(), person_id)

    return None


def load_person_from_canonical(store: object, person_id: str) -> Person:
    normalized = str(person_id).strip()
    if not normalized:
        raise NotFoundError("Unknown person: empty person_id")
    doc = store.get_optional(f"employee_{normalized}")  # type: ignore[attr-defined]
    if doc is None:
        doc = _find_employee_fallback(store, normalized)
    if doc is None:
        raise NotFoundError(f"Unknown person: {normalized}")
    if not isinstance(doc, dict):
        raise NotFoundError(f"Canonical employee document is malformed: {normalized}")
    canonical_name = str(doc.get("name") or doc.get("location") or doc.get("canonical") or normalized).strip()
    resolved_person_id = str(doc.get("person_id") or normalized).strip() or normalized
    return Person(
        person_id=PersonId(resolved_person_id),
        canonical_name=canonical_name,
        aliases=(),
        status=str(doc.get("status")).strip() if doc.get("status") is not None else None,
        site_ids=_employee_site_ids(doc),
        note_path=Path(f"employee_{normalized}"),
    )


def _site_from_values(site_id: str, canonical_name: str, *, about_path: Path | None = None) -> Site:
    return Site(
        site_id=SiteId(str(site_id)),
        canonical_name=str(canonical_name),
        account=None,
        aliases=(),
        address=None,
        monthly_supply_budget=None,
        budget_basis=None,
        about_path=about_path or Path(f"location_{site_id}"),
    )


def site_from_registry(site_registry: object, site_id: str) -> Site | None:
    normalized = str(site_id).strip()
    if not normalized:
        return None
    canonical = site_registry.resolve_canonical(normalized)  # type: ignore[attr-defined]
    if canonical is None:
        return None
    canonical_name = str(canonical).strip()
    if not canonical_name:
        return None
    return _site_from_values(normalized, canonical_name)


def _site_from_registry_row(row: object) -> Site | None:
    if not isinstance(row, dict):
        return None
    site_id = str(row.get("site_id") or "").strip()
    canonical_name = str(row.get("canonical") or row.get("canonical_name") or row.get("location") or row.get("name") or "").strip()
    if not site_id or not canonical_name:
        return None
    return _site_from_values(site_id, canonical_name)


def allowed_sites_for_person(site_registry: object, person: Person, explicit_site_ids: Iterable[str] | None = None) -> tuple[Site, ...]:
    allowed_ids = tuple(explicit_site_ids or person.site_ids)
    if UNIVERSAL_SITE_SCOPE in allowed_ids:
        sites = tuple(
            site
            for row in site_registry.list_sites()  # type: ignore[attr-defined]
            if (site := _site_from_registry_row(row)) is not None
        )
        return tuple(sorted(sites, key=lambda site: site.canonical_name.lower()))
    sites: list[Site] = []
    for site_id in allowed_ids:
        try:
            site = site_from_registry(site_registry, str(site_id))
        except Exception as exc:
            logger.warning(
                "site lookup failed during field-capture auth person_id=%s site_id=%s: %s",
                person.person_id,
                site_id,
                exc,
            )
            site = None
        if site is not None:
            sites.append(site)
    return tuple(sorted(sites, key=lambda site: site.canonical_name.lower()))


def authorize_token(token_store: TokenStore, token_value: str) -> AuthorizedSession | None:
    record = token_store.authenticate(token_value)
    if record is None:
        return None
    try:
        person_store = CouchDBEntityStore.from_env()
        site_registry = CouchDBSiteRegistry()
        person = load_person_from_canonical(person_store, record.person_id)
        sites = allowed_sites_for_person(site_registry, person, record.site_ids or None)
    except Exception as exc:
        logger.warning(
            "field-capture auth lookup failed person_id=%s token_id=%s: %s",
            record.person_id,
            record.token_id,
            exc,
        )
        return None
    return AuthorizedSession(record=record, person=person, sites=sites)
