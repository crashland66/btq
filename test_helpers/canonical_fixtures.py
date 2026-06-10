"""Canonical SANDBOX fixture — the ONE fake site + employee.

This is the single, reusable fake identity used across tests AND as the live demo
persona on fc.gregstoltz.com. Use these builders instead of inventing a new
person/site per test (which is how synthetic data scattered into prod before).

The IDs/names here are kept in sync with the REAL canonical records created for the
demo token:
  - employee ``employee_sandbox-user`` in ``btq_vault`` (type ``employee``)
  - site ``SANDBOX`` in ``btq_sites`` (type ``site``)
so a test fixture and the live tokenized link describe the same identity.
"""

from __future__ import annotations

from typing import Any

# --- stable identity (do not churn — baked into tests + the live demo token) -----
SANDBOX_SITE_ID = "SANDBOX"
SANDBOX_SITE_NAME = "Sandbox Site"
SANDBOX_ACCOUNT = "Sandbox"
SANDBOX_PERSON_ID = "sandbox-user"
SANDBOX_PERSON_NAME = "Sandy Sandbox"
SANDBOX_EMPLOYEE_DOC_ID = f"employee_{SANDBOX_PERSON_ID}"


def sandbox_employee_doc(**overrides: Any) -> dict[str, Any]:
    """Canonical employee doc (``btq_vault`` ``employee_<person_id>``, type ``employee``)."""
    doc: dict[str, Any] = {
        "_id": SANDBOX_EMPLOYEE_DOC_ID,
        "type": "employee",
        "operator": "op_greg",
        "person_id": SANDBOX_PERSON_ID,
        "name": SANDBOX_PERSON_NAME,
        "first": "Sandy",
        "last": "Sandbox",
        "status": "active",
        "role": "cleaner",
        "job": SANDBOX_SITE_ID,
        "site_ids": [SANDBOX_SITE_ID],
    }
    doc.update(overrides)
    return doc


def sandbox_site_doc(**overrides: Any) -> dict[str, Any]:
    """Canonical site doc (``btq_sites``, ``_id`` == ``site_id``, type ``site``).

    ``active: True`` (boolean) is REQUIRED for the site to be submittable: the
    btq_sites ``by_site_id`` view gates on ``doc.active === true`` and emits
    ``canonical: doc.account`` — so without it the fc app's /api/session returns an
    empty ``sites`` list and disables capture (mis-reported as "cannot submit").
    """
    doc: dict[str, Any] = {
        "_id": SANDBOX_SITE_ID,
        "type": "site",
        "site_id": SANDBOX_SITE_ID,
        "account": SANDBOX_SITE_NAME,  # fc app uses account as the site's display name
        "location": SANDBOX_SITE_NAME,
        "active": True,
        "status": "active",
    }
    doc.update(overrides)
    return doc


def sandbox_site_registry_entry(**overrides: Any) -> dict[str, Any]:
    """Site-registry list entry (``{canonical, site_id, note_path, aliases}``)."""
    entry: dict[str, Any] = {
        "canonical": SANDBOX_SITE_NAME,
        "site_id": SANDBOX_SITE_ID,
        "note_path": f"Accounts/{SANDBOX_ACCOUNT}/Locations/{SANDBOX_SITE_ID} - {SANDBOX_SITE_NAME}/about.md",
        "aliases": ["sandbox site", "sandbox", SANDBOX_SITE_ID.lower()],
    }
    entry.update(overrides)
    return entry


def sandbox_employee_view_row(**overrides: Any) -> dict[str, Any]:
    """An ``employees_by_site`` view row wrapping the sandbox employee doc."""
    doc = sandbox_employee_doc(**overrides)
    return {"key": [SANDBOX_SITE_ID, doc["person_id"]], "value": None, "doc": doc}
