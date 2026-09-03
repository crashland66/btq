from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from typing import Any

from btq_vault.employee_assignments import employee_assigned_site_ids
from event_pipeline import couchdb_config
from event_pipeline import btq_client


Doc = Mapping[str, Any]


def resolve_account(
    query: object,
    *,
    docs: Iterable[Doc] | Mapping[str, Iterable[Doc]] | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Resolve an account/site query to one canonical CouchDB-backed account shape.

    `docs` is the test seam: pass synthetic site documents to avoid live CouchDB.
    With no docs, the function reads `type == "location"` docs from the configured
    sites database through the existing read-only btq_client helper.
    """
    site_docs = _site_docs_from_injection(docs) if docs is not None else _load_site_docs(config=config)
    accounts = _account_records(site_docs)
    needle = _clean_string(query)
    matches = _match_accounts(needle, accounts)
    if not matches:
        return {"kind": "not_found", "query": needle}
    if len(matches) > 1:
        return {
            "kind": "ambiguous",
            "query": needle,
            "candidates": [_account_shape(account) for account in matches],
        }
    return _account_shape(matches[0])


def resolve_person(
    query: object,
    *,
    docs: Iterable[Doc] | Mapping[str, Iterable[Doc]] | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Resolve an employee/person query to one canonical CouchDB-backed person shape."""
    employee_docs = _employee_docs_from_injection(docs) if docs is not None else _load_employee_docs(config=config)
    people = _person_records(employee_docs)
    needle = _clean_string(query)
    matches = _match_people(needle, people)
    if not matches:
        return {"kind": "not_found", "query": needle}
    if len(matches) > 1:
        return {
            "kind": "ambiguous",
            "query": needle,
            "candidates": [_person_shape(person) for person in matches],
        }
    return _person_shape(matches[0])


def operator_context_snapshot(
    operator: object,
    *,
    account_docs: Iterable[Doc] | None = None,
    person_docs: Iterable[Doc] | None = None,
    docs: Mapping[str, Iterable[Doc]] | None = None,
    now: dt.datetime | str | None = None,
    config: object | None = None,
) -> dict[str, Any]:
    """Return accounts and people related to the given operator identity.

    Accounts come from the operator employee doc's `site_ids`. People are the
    union of direct manager matches and assignment to those account site_ids,
    with relationship labeled as `direct`, `assigned`, or `both`.
    """
    if docs is not None:
        account_docs = account_docs if account_docs is not None else docs.get("accounts") or docs.get("sites")
        person_docs = person_docs if person_docs is not None else docs.get("people") or docs.get("employees")

    site_docs = list(account_docs) if account_docs is not None else _load_site_docs(config=config)
    employee_docs = list(person_docs) if person_docs is not None else _load_employee_docs(config=config)

    accounts_by_site_id = _account_records(site_docs)
    people_by_id = _person_records(employee_docs)
    operator_query = _clean_string(operator)
    operator_matches = _match_people(operator_query, people_by_id)
    generated_at = _generated_at(now)

    if len(operator_matches) != 1:
        return {
            "operator": operator_query,
            "accounts": [],
            "people": [],
            "generated_at": generated_at,
            "resolution": {
                "kind": "ambiguous" if len(operator_matches) > 1 else "not_found",
                "query": operator_query,
                "candidates": [_person_shape(person) for person in operator_matches],
            },
        }

    operator_record = operator_matches[0]
    operator_site_ids = set(operator_record["assigned_job_numbers"])
    operator_names = _operator_name_keys(operator_record)

    account_shapes = [
        _account_shape(accounts_by_site_id[site_id])
        for site_id in sorted(operator_site_ids, key=_site_sort_key)
        if site_id in accounts_by_site_id
    ]

    person_shapes: list[dict[str, Any]] = []
    for person in people_by_id.values():
        if person["doc_id"] == operator_record["doc_id"]:
            continue
        direct = _manager_matches(person.get("manager"), operator_names)
        assigned = bool(operator_site_ids & set(person["assigned_job_numbers"]))
        if not direct and not assigned:
            continue
        shape = _person_shape(person)
        shape["relationship"] = "both" if direct and assigned else "direct" if direct else "assigned"
        person_shapes.append(shape)

    person_shapes.sort(
        key=lambda person: _normalize_text(str(person.get("display_name") or person.get("canonical_name") or ""))
    )

    return {
        "operator": operator_record["canonical_name"],
        "accounts": account_shapes,
        "people": person_shapes,
        "generated_at": generated_at,
    }


def normalize_query(value: object) -> str:
    """Normalize text for exact safe matching: lowercase, punctuation collapsed."""
    return _normalize_text(_clean_string(value))


def _load_site_docs(*, config: object | None = None) -> list[dict[str, Any]]:
    injected = _docs_from_config(config, ("accounts", "sites", "site_docs"))
    if injected is not None:
        return injected
    return btq_client.find(couchdb_config.vault_database(), {"type": "location"}, limit=10000)


def _load_employee_docs(*, config: object | None = None) -> list[dict[str, Any]]:
    injected = _docs_from_config(config, ("people", "employees", "employee_docs"))
    if injected is not None:
        return injected
    return btq_client.find(couchdb_config.vault_database(), {"type": "employee"}, limit=10000)


def _docs_from_config(config: object | None, names: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        for name in names:
            if name in config:
                return list(config[name])
        return None
    for name in names:
        value = getattr(config, name, None)
        if value is not None:
            return list(value)
    return None


def _site_docs_from_injection(docs: Iterable[Doc] | Mapping[str, Iterable[Doc]]) -> list[Doc]:
    if isinstance(docs, Mapping):
        injected = docs.get("accounts") or docs.get("sites") or docs.get("site_docs") or []
        return list(injected)
    return list(docs)


def _employee_docs_from_injection(docs: Iterable[Doc] | Mapping[str, Iterable[Doc]]) -> list[Doc]:
    if isinstance(docs, Mapping):
        injected = docs.get("people") or docs.get("employees") or docs.get("employee_docs") or []
        return list(injected)
    return list(docs)


def _account_records(docs: Iterable[Doc]) -> dict[str, dict[str, Any]]:
    by_site_id: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if str(doc.get("type") or "") not in ("location", "site"):
            continue
        site_id = _clean_string(doc.get("site_id") or _site_id_from_doc_id(doc.get("_id")))
        if not site_id:
            continue
        record = _build_account_record(doc, site_id)
        existing = by_site_id.get(site_id)
        if existing is None or _account_doc_score(record["doc"]) > _account_doc_score(existing["doc"]):
            by_site_id[site_id] = record
    return by_site_id


def _build_account_record(doc: Doc, site_id: str) -> dict[str, Any]:
    # Canonical location docs put the display name in `location` and the short
    # account code in `account`; legacy site-shaped docs used `account` for the
    # display name (and mirrored it in `location`), so this order serves both.
    canonical = _clean_string(doc.get("location") or doc.get("account") or doc.get("canonical") or doc.get("name") or site_id)
    aliases = _account_aliases(doc, canonical, site_id)
    return {
        "doc": doc,
        "site_id": site_id,
        "canonical_name": canonical,
        "aliases": aliases,
        "match_keys": _account_match_keys(site_id, canonical, aliases),
    }


def _account_aliases(doc: Doc, canonical: str, site_id: str) -> list[str]:
    aliases: list[str] = []
    aliases.extend(_string_list(doc.get("aliases")))
    for value in (canonical, doc.get("name"), doc.get("location")):
        cleaned = _clean_string(value)
        if cleaned:
            aliases.append(cleaned)
    normalized = _normalize_text(canonical)
    if normalized:
        aliases.append(normalized)
        aliases.append(normalized.replace(" - ", " "))
    tokens = normalized.split()
    if tokens:
        aliases.append(tokens[0])
    return _dedupe_strings(aliases)


def _account_match_keys(site_id: str, canonical: str, aliases: list[str]) -> set[str]:
    keys = {site_id, _normalize_text(canonical)}
    keys.update(_normalize_text(alias) for alias in aliases)
    return {key for key in keys if key}


def _account_doc_score(doc: Doc) -> tuple[int, int, int]:
    has_account = 1 if _clean_string(doc.get("account")) else 0
    has_aliases = 1 if _string_list(doc.get("aliases")) else 0
    rich_id = 1 if str(doc.get("_id") or "").startswith("site_") else 0
    return has_account, has_aliases, rich_id


def _match_accounts(query: str, accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _normalize_text(query)
    if not normalized:
        return []

    exact = [
        account
        for account in accounts.values()
        if query == account["site_id"] or normalized in account["match_keys"]
    ]
    if exact:
        return _sort_accounts(_unique_records(exact, "site_id"))

    query_words = set(normalized.split())
    partial: list[dict[str, Any]] = []
    for account in accounts.values():
        for key in account["match_keys"]:
            key_words = set(key.split())
            if normalized in key or (query_words and query_words <= key_words):
                partial.append(account)
                break
    return _sort_accounts(_unique_records(partial, "site_id"))


def _account_shape(account: Mapping[str, Any]) -> dict[str, Any]:
    doc = account["doc"]
    active = doc.get("active")
    status = str(doc.get("status") or ("active" if active is True else "inactive" if active is False else "")).strip()
    shape: dict[str, Any] = {
        "kind": "account",
        "job_number": account["site_id"],
        "canonical_name": account["canonical_name"],
        "aliases": list(account["aliases"]),
        "status": status,
        "source": _source(doc, couchdb_config.DEFAULT_VAULT_DB),
    }
    manager = _clean_string(doc.get("manager") or doc.get("owner"))
    if manager:
        shape["manager"] = manager
    return shape


def _person_records(docs: Iterable[Doc]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if str(doc.get("type") or "") != "employee":
            continue
        doc_id = _clean_string(doc.get("_id"))
        if not doc_id:
            continue
        by_id[doc_id] = _build_person_record(doc)
    return by_id


def _build_person_record(doc: Doc) -> dict[str, Any]:
    canonical = _clean_string(doc.get("name") or _display_name_from_fields(doc) or doc.get("_id"))
    display = _display_name(doc, canonical)
    assigned_job_numbers = employee_assigned_site_ids(doc)
    manager = _clean_string(doc.get("manager"))
    match_keys = _person_match_keys(doc, canonical, display)
    return {
        "doc": doc,
        "doc_id": _clean_string(doc.get("_id")),
        "canonical_name": canonical,
        "display_name": display,
        "assigned_job_numbers": assigned_job_numbers,
        "manager": manager,
        "match_keys": match_keys,
    }


def _person_match_keys(doc: Doc, canonical: str, display: str) -> set[str]:
    keys = {
        _normalize_text(canonical),
        _normalize_text(display),
        _normalize_text(_last_first_name(doc, canonical)),
        _normalize_text(_display_name_from_fields(doc)),
        _clean_string(doc.get("_id")),
        _clean_string(doc.get("ehub_id")),
        _clean_string(doc.get("employee_id")),
        _clean_string(doc.get("person_id")),
    }
    return {key for key in keys if key}


def _match_people(query: str, people: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = _normalize_text(query)
    raw = _clean_string(query)
    if not normalized and not raw:
        return []

    exact = [
        person
        for person in people.values()
        if normalized in person["match_keys"] or raw in person["match_keys"]
    ]
    if exact:
        return _sort_people(_unique_records(exact, "doc_id"))

    query_words = set(normalized.split())
    partial: list[dict[str, Any]] = []
    for person in people.values():
        for key in person["match_keys"]:
            key_words = set(_normalize_text(key).split())
            if normalized and normalized in _normalize_text(key):
                partial.append(person)
                break
            if query_words and query_words <= key_words:
                partial.append(person)
                break
    return _sort_people(_unique_records(partial, "doc_id"))


def _person_shape(person: Mapping[str, Any]) -> dict[str, Any]:
    doc = person["doc"]
    return {
        "kind": "person",
        "canonical_name": person["canonical_name"],
        "display_name": person["display_name"],
        "role": _clean_string(doc.get("role")),
        "assigned_job_numbers": list(person["assigned_job_numbers"]),
        "manager": person["manager"],
        "source": _source(doc, couchdb_config.DEFAULT_VAULT_DB),
    }


def _operator_name_keys(operator: Mapping[str, Any]) -> set[str]:
    doc = operator["doc"]
    keys = {
        _normalize_text(operator["canonical_name"]),
        _normalize_text(operator["display_name"]),
        _normalize_text(_display_name_from_fields(doc)),
        _normalize_text(_first_name(doc, operator["display_name"])),
    }
    return {key for key in keys if key}


def _manager_matches(manager: object, operator_keys: set[str]) -> bool:
    normalized = _normalize_text(_clean_string(manager))
    if not normalized:
        return False
    if normalized in operator_keys:
        return True
    manager_words = set(normalized.split())
    if any(manager_words and manager_words <= set(key.split()) for key in operator_keys):
        return True
    if len(manager_words) == 1 and len(normalized) >= 4:
        return any(word.startswith(normalized) for key in operator_keys for word in key.split())
    return False


def _display_name(doc: Doc, canonical: str) -> str:
    from_fields = _display_name_from_fields(doc)
    if from_fields:
        return from_fields
    if "," in canonical:
        last, rest = canonical.split(",", 1)
        first_parts = rest.strip().split()
        if first_parts and last.strip():
            return f"{first_parts[0]} {last.strip()}".strip()
    return canonical


def _display_name_from_fields(doc: Doc) -> str:
    first = _clean_string(doc.get("first"))
    last = _clean_string(doc.get("last"))
    return f"{first} {last}".strip()


def _last_first_name(doc: Doc, fallback: str) -> str:
    first = _clean_string(doc.get("first"))
    last = _clean_string(doc.get("last"))
    if first and last:
        return f"{last}, {first}"
    return fallback


def _first_name(doc: Doc, display: str) -> str:
    first = _clean_string(doc.get("first"))
    if first:
        return first
    return display.split()[0] if display.split() else ""


def _site_id_from_doc_id(doc_id: object) -> str:
    raw = _clean_string(doc_id)
    for prefix in ("location_", "site_"):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _source(doc: Mapping[str, Any], default_database: str) -> dict[str, str]:
    source = {
        "database": _clean_string(doc.get("_database") or doc.get("database") or default_database),
        "doc_id": _clean_string(doc.get("_id")),
        "rev": _clean_string(doc.get("_rev")),
    }
    return source


def _generated_at(now: dt.datetime | str | None) -> str:
    if isinstance(now, str):
        return now
    value = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_string(item) for item in value if _clean_string(item)]


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = _clean_string(value)
        key = _normalize_text(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _unique_records(records: Iterable[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        by_id.setdefault(str(record[id_key]), record)
    return list(by_id.values())


def _sort_accounts(accounts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(accounts, key=lambda account: _site_sort_key(account["site_id"]))


def _sort_people(people: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(people, key=lambda person: _normalize_text(person["display_name"]))


def _site_sort_key(site_id: object) -> tuple[int, int | str]:
    text = _clean_string(site_id)
    return (0, int(text)) if text.isdigit() else (1, text)


def _normalize_text(value: str) -> str:
    lowered = value.lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()


def _clean_string(value: object) -> str:
    return str(value or "").strip()
