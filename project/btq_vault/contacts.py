from __future__ import annotations

from typing import Any


CONTACT_ROLES: frozenset[str] = frozenset(
    {
        "account_escalation",
        "site_contact",
        "access_contact",
        "practice_manager",
        "facilities",
        "billing",
        "safety",
        "regional_manager",
        "client_admin",
        "emergency_access",
        "other",
    }
)
CONTACT_ACTIONS: frozenset[str] = frozenset({"upsert", "remove"})
CONTACT_SCOPES: frozenset[str] = frozenset({"account", "site"})
CONTACT_COLLECTIONS: frozenset[str] = frozenset({"account_contacts", "site_contacts"})
CONTACT_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "title",
        "phone",
        "email",
        "role",
        "scope",
        "source",
        "source_date",
        "notes",
    }
)


class ContactError(ValueError):
    """Raised when a contact entry does not match the canonical shape."""


def normalize_contact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ContactError("contact entry must be an object")
    if set(entry) - CONTACT_FIELDS:
        raise ContactError("contact entry contains unsupported fields")

    contact_id = _required_string(entry.get("id"), "contact entries require an id")
    role = _required_string(entry.get("role"), "contact entries require a role")
    scope = _required_string(entry.get("scope"), "contact entries require a scope")
    if role not in CONTACT_ROLES:
        raise ContactError("contact entries require an allowed role")
    if scope not in CONTACT_SCOPES:
        raise ContactError("contact entries require an allowed scope")

    return {
        "id": contact_id,
        "name": _string(entry.get("name")),
        "title": _string(entry.get("title")),
        "phone": _string(entry.get("phone")),
        "email": _string(entry.get("email")),
        "role": role,
        "scope": scope,
        "source": _string(entry.get("source")),
        "source_date": _nullable_string(entry.get("source_date")),
        "notes": _string(entry.get("notes")),
    }


def contacts_from_doc(doc: dict[str, Any] | None, collection: str) -> list[dict[str, Any]]:
    if collection not in CONTACT_COLLECTIONS:
        raise ContactError("unsupported contact collection")
    raw_contacts = (doc or {}).get(collection)
    if raw_contacts is None:
        return []
    if not isinstance(raw_contacts, list):
        return []

    contacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in raw_contacts:
        if not isinstance(raw_entry, dict):
            continue
        try:
            entry = normalize_contact_entry(raw_entry)
        except ContactError:
            continue
        if entry["id"] in seen:
            continue
        contacts.append(entry)
        seen.add(entry["id"])
    return contacts


def upsert_contact_by_id(
    doc: dict[str, Any] | None,
    collection: str,
    contact: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = normalize_contact_entry(contact)
    contacts = contacts_from_doc(doc, collection)
    match_index = next((index for index, entry in enumerate(contacts) if entry["id"] == normalized["id"]), None)
    if match_index is None:
        contacts.append(normalized)
    else:
        contacts[match_index] = normalized
    return contacts


def remove_contact_by_id(doc: dict[str, Any] | None, collection: str, contact_id: str) -> list[dict[str, Any]]:
    normalized_id = str(contact_id or "").strip()
    if not normalized_id:
        raise ContactError("contact removal requires an id")
    return [entry for entry in contacts_from_doc(doc, collection) if entry["id"] != normalized_id]


def _required_string(value: object, message: str) -> str:
    text = _string(value)
    if not text:
        raise ContactError(message)
    return text


def _string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _nullable_string(value: object) -> str | None:
    text = _string(value)
    return text or None
