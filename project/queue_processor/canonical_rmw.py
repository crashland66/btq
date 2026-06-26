from __future__ import annotations

"""Shared canonical CouchDB read-modify-write helpers.

Projection-path resolution is deferred until the required CouchDB views exist.
The dedicated employees_by_lookup_key view remains deferred; find_employee_docs
suffices for current employee canonical target resolution.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from btq_vault.couch_store import CouchDBEntityStore, CouchDBEntityStoreError
from btq_vault.entity_types import CANONICAL_ENTITY_TYPES
from btq_vault.location_urls import location_urls_from_doc
from event_pipeline.sites import SITES, resolve_site_id
from queue_processor.handlers._shared import QueueProcessorError


logger = logging.getLogger(__name__)


__all__ = [
    "CanonicalEntityState",
    "CanonicalMutation",
    "CanonicalTarget",
    "SiteContext",
    "append_job_id",
    "apply_canonical_rmw",
    "canonical_job_already_applied",
    "resolve_employee_target",
    "resolve_employee_target_by_person_id",
    "resolve_site_context",
    "resolve_site_target",
]


@dataclass(frozen=True)
class CanonicalTarget:
    doc_id: str
    doc_type: str
    projection_path: Path | None = None
    allow_create: bool = False
    require_existing: bool = True


@dataclass(frozen=True)
class SiteContext:
    site_id: str
    name: str
    account: str | None
    urls: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CanonicalEntityState:
    doc: dict[str, Any] | None
    doc_id: str
    rev: str | None
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CanonicalMutation:
    doc: dict[str, Any]
    evidence_text: str = ""
    skip: bool = False


def canonical_job_already_applied(doc: dict[str, Any] | None, job_id: str) -> bool:
    return job_id in _string_list((doc or {}).get("btq_job_ids"))


def append_job_id(doc: dict[str, Any], job_id: str) -> dict[str, Any]:
    outgoing = dict(doc)
    job_ids = _string_list(outgoing.get("btq_job_ids"))
    if job_id not in job_ids:
        job_ids.append(job_id)
    outgoing["btq_job_ids"] = job_ids
    return outgoing


def apply_canonical_rmw(
    store: CouchDBEntityStore,
    target: CanonicalTarget,
    job_id: str,
    transform: Callable[[CanonicalEntityState], CanonicalMutation],
) -> dict[str, Any] | None:
    """
    Apply a canonical CouchDB read-modify-write mutation.

    Markdown projection and evidence writes intentionally remain caller-side
    side effects after a non-None canonical write result.
    """
    _validate_target(target)
    current = store.get_optional(target.doc_id)
    if current is None:
        if target.require_existing:
            raise CouchDBEntityStoreError(f"CouchDB required document not found for canonical RMW: {target.doc_id}")
        if not target.allow_create:
            return None
    if canonical_job_already_applied(current, job_id):
        return None

    def create_doc() -> dict[str, Any]:
        return {"_id": target.doc_id, "type": target.doc_type}

    def update(current_or_seed: dict[str, Any] | None) -> dict[str, Any] | None:
        if current_or_seed is None:
            raise _CanonicalSkip()
        if canonical_job_already_applied(current_or_seed, job_id):
            raise _CanonicalSkip()
        state = _entity_state(current_or_seed, target.doc_id)
        mutation = transform(state)
        if mutation.skip:
            raise _CanonicalSkip()
        outgoing = append_job_id(mutation.doc, job_id)
        outgoing["_id"] = target.doc_id
        outgoing["type"] = target.doc_type
        return outgoing

    try:
        return store.update_doc(
            target.doc_id,
            update,
            create=create_doc if target.allow_create else None,
            require_existing=target.require_existing,
        )
    except _CanonicalSkip:
        return None


def resolve_site_target(value: str, *, allow_create: bool = False) -> CanonicalTarget:
    try:
        site_id = resolve_site_id(value)
    except Exception as exc:
        raise QueueProcessorError(f"Could not resolve canonical site target: {value}") from exc
    if site_id is None:
        raise QueueProcessorError(f"Could not resolve canonical site target: {value}")
    return CanonicalTarget(
        doc_id=f"location_{site_id}",
        doc_type="location",
        allow_create=allow_create,
        require_existing=not allow_create,
    )


def resolve_site_context(store: CouchDBEntityStore, value: str, *, include_deprecated_urls: bool = False) -> SiteContext:
    try:
        site_id = resolve_site_id(value)
        if site_id is None and " - " in value:
            site_id = resolve_site_id(value.split(" - ", 1)[0].strip())
    except Exception as exc:
        raise QueueProcessorError(f"Could not resolve canonical site target: {value}") from exc
    if site_id is None:
        raise QueueProcessorError(f"Could not resolve canonical site target: {value}")

    doc = store.get_optional(f"location_{site_id}")
    doc_name = (doc or {}).get("location") or (doc or {}).get("name")
    reg_name = next((s["canonical"] for s in SITES if str(s["site_id"]) == str(site_id)), None)

    if doc_name and reg_name and doc_name != reg_name:
        logger.warning(
            "Canonical site name mismatch for site_id=%s: doc_name=%r reg_name=%r; using canonical doc",
            site_id,
            doc_name,
            reg_name,
        )

    name = doc_name or reg_name
    if not name:
        raise QueueProcessorError(f"Could not resolve site name for {value} (site_id={site_id})")

    account = (doc or {}).get("account")
    urls = tuple(location_urls_from_doc(doc, include_deprecated=include_deprecated_urls))
    return SiteContext(site_id=str(site_id), name=str(name), account=account, urls=urls)


def resolve_employee_target_by_person_id(person_id: str, *, allow_create: bool = False) -> CanonicalTarget:
    normalized = person_id.strip()
    if not normalized:
        raise QueueProcessorError("Cannot resolve employee target without person_id")
    return CanonicalTarget(
        doc_id=f"employee_{normalized}",
        doc_type="employee",
        allow_create=allow_create,
        require_existing=not allow_create,
    )


def resolve_employee_target(
    store: "CouchDBEntityStore",
    value: str,
    *,
    allow_create: bool = False,
) -> CanonicalTarget:
    """
    Resolve an employee reference (person_id, employee_id, or display name) to a
    CanonicalTarget for employee_<person_id>. Precedence: exact person_id, then
    exact employee_id, then unique normalized-name match. Raises QueueProcessorError
    on no match or on an ambiguous name match.
    """
    normalized = value.strip()
    if not normalized:
        raise QueueProcessorError("Cannot resolve employee target without value")

    docs = store.find_employee_docs()

    normalized_doc_ids = set(_employee_doc_id_variants(normalized))
    normalized_person_ids = {normalized}
    normalized_person_ids.update(doc_id.removeprefix("employee_") for doc_id in normalized_doc_ids)
    for doc in docs:
        person_id = str(doc.get("person_id") or "").strip()
        doc_id = str(doc.get("_id") or "").strip()
        if person_id in normalized_person_ids or doc_id in normalized_doc_ids:
            return _employee_target_from_doc(doc, allow_create=allow_create)

    employee_id_matches = []
    for doc in docs:
        employee_id = _normalized_employee_id(doc.get("employee_id"))
        if employee_id and employee_id == normalized:
            employee_id_matches.append(doc)
    if len(employee_id_matches) == 1:
        return _employee_target_from_doc(employee_id_matches[0], allow_create=allow_create)
    if len(employee_id_matches) > 1:
        raise QueueProcessorError(f"Ambiguous canonical employee target for employee_id: {value}")

    normalized_names = _employee_name_lookup_keys(normalized)
    name_matches = [
        doc for doc in docs if _normalize_employee_name(_canonical_employee_name(doc)) in normalized_names
    ]
    if len(name_matches) == 1:
        return _employee_target_from_doc(name_matches[0], allow_create=allow_create)
    if len(name_matches) > 1:
        raise QueueProcessorError(f"Ambiguous canonical employee target for name: {value}")

    loose_matches = [doc for doc in docs if _employee_loose_name_matches(doc, normalized)]
    if len(loose_matches) == 1:
        return _employee_target_from_doc(loose_matches[0], allow_create=allow_create)
    if len(loose_matches) > 1:
        raise QueueProcessorError(f"Ambiguous canonical employee target for name: {value}")

    if _employee_first_name_ambiguous(docs, normalized):
        raise QueueProcessorError(f"Ambiguous canonical employee target for name: {value}")

    raise QueueProcessorError(f"Could not resolve canonical employee target: {value}")


def _entity_state(doc: dict[str, Any] | None, doc_id: str) -> CanonicalEntityState:
    metadata: dict[str, Any] = {}
    content = ""
    rev = None
    if doc is not None:
        rev_value = doc.get("_rev")
        rev = str(rev_value) if rev_value else None
        content_value = doc.get("content", "")
        content = content_value if isinstance(content_value, str) else str(content_value)
        metadata = {key: value for key, value in doc.items() if key not in {"_id", "_rev", "content"}}
    return CanonicalEntityState(doc=doc, doc_id=doc_id, rev=rev, content=content, metadata=metadata)


def _validate_target(target: CanonicalTarget) -> None:
    if target.doc_type not in CANONICAL_ENTITY_TYPES:
        raise QueueProcessorError(f"Unsupported canonical entity type: {target.doc_type}")
    if not target.doc_id.strip():
        raise QueueProcessorError("Cannot apply canonical RMW without doc_id")


class _CanonicalSkip(Exception):
    pass


def _employee_target_from_doc(doc: dict[str, Any], *, allow_create: bool) -> CanonicalTarget:
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise QueueProcessorError("Resolved employee document is missing _id")
    return CanonicalTarget(
        doc_id=doc_id,
        doc_type="employee",
        allow_create=allow_create,
        require_existing=not allow_create,
    )


def _normalized_employee_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _employee_doc_id_variants(value: str) -> list[str]:
    suffix = str(value).strip().removeprefix("employee_")
    if not suffix:
        return []
    variants = [f"employee_{suffix}"]
    for alternate in (suffix.replace("_", "-"), suffix.replace("-", "_")):
        doc_id = f"employee_{alternate}"
        if doc_id not in variants:
            variants.append(doc_id)
    return variants


def _canonical_employee_name(doc: dict[str, Any]) -> str:
    name = str(doc.get("name") or "").strip()
    if name:
        return name
    first = str(doc.get("first") or "").strip()
    last = str(doc.get("last") or "").strip()
    return f"{first} {last}".strip()


_EMPLOYEE_NICKNAMES: dict[str, tuple[str, ...]] = {
    "deb": ("carol",),
    "debbie": ("carol",),
    "mike": ("michael",),
    "jordan": ("jordan",),
    "joe": ("joseph",),
    "tony": ("anthony",),
}


def _normalize_employee_name(value: str) -> str:
    # Mirrors queue_processor.handlers.people.normalize_employee_name without importing handlers.
    return " ".join(value.strip().lower().replace(",", " ").split())


def _employee_name_lookup_keys(value: str) -> set[str]:
    keys = {_normalize_employee_name(value)}
    if "," in value:
        last, first = value.split(",", 1)
        reversed_value = f"{first.strip()} {last.strip()}"
        keys.add(_normalize_employee_name(reversed_value))
    return {key for key in keys if key}


def _employee_query_name_parts(value: str) -> tuple[str, str]:
    normalized = _normalize_employee_name(value)
    if not normalized:
        return "", ""
    if "," in value:
        last, first = value.split(",", 1)
        normalized_first = _normalize_employee_name(first)
        query_first = normalized_first.split()[0] if normalized_first else ""
        return query_first, _normalize_employee_name(last)
    parts = normalized.split()
    if len(parts) < 2:
        return parts[0], ""
    return parts[0], parts[-1]


def _employee_doc_name_parts(doc: dict[str, Any]) -> tuple[list[str], str]:
    first_names: list[str] = []
    for key in ("first", "preferred_name"):
        value = _normalize_employee_name(str(doc.get(key) or ""))
        if value:
            first_names.append(value.split()[0])

    name = _canonical_employee_name(doc)
    last = _normalize_employee_name(str(doc.get("last") or ""))
    normalized_name = _normalize_employee_name(name)
    if normalized_name:
        name_parts = normalized_name.split()
        if "," in name:
            parsed_first, parsed_last = _employee_query_name_parts(name)
            if parsed_first:
                first_names.append(parsed_first)
            if parsed_last:
                last = last or parsed_last
        elif len(name_parts) >= 2:
            first_names.append(name_parts[0])
            last = last or name_parts[-1]

    deduped_first_names: list[str] = []
    for first_name in first_names:
        if first_name and first_name not in deduped_first_names:
            deduped_first_names.append(first_name)
    return deduped_first_names, last


def _employee_first_name_matches(query_first: str, candidate_first: str) -> bool:
    query = _normalize_employee_name(query_first)
    candidate = _normalize_employee_name(candidate_first)
    if not query or not candidate:
        return False
    if query == candidate or candidate.startswith(query):
        return True
    return candidate in _EMPLOYEE_NICKNAMES.get(query, ())


def _employee_loose_name_matches(doc: dict[str, Any], value: str) -> bool:
    query_first, query_last = _employee_query_name_parts(value)
    if not query_first or not query_last:
        return False
    candidate_first_names, candidate_last = _employee_doc_name_parts(doc)
    if candidate_last != query_last:
        return False
    return any(_employee_first_name_matches(query_first, candidate_first) for candidate_first in candidate_first_names)


def _employee_first_name_ambiguous(docs: list[dict[str, Any]], value: str) -> bool:
    query_first, query_last = _employee_query_name_parts(value)
    if not query_first or query_last:
        return False
    matches = 0
    for doc in docs:
        candidate_first_names, _candidate_last = _employee_doc_name_parts(doc)
        if any(_employee_first_name_matches(query_first, candidate_first) for candidate_first in candidate_first_names):
            matches += 1
            if matches > 1:
                return True
    return False


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
