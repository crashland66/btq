from __future__ import annotations

"""Seed site-registration fields onto btq_vault location docs.

Bootstrap seeder for fresh nodes: folds the static SITES table (gitignored
site_registry.json, synthetic example committed) into the canonical
``location_<site_id>`` docs so the sites_by_alias / sites_by_site_id views
resolve captures. The old sites database is retired — registration lives on the location
docs (fields: site_id, aliases, capture_active, vision_context, note_path).

STRICTLY FILL-ONLY: the vault is canonical and operator-editable (/sites
dashboard), so this seeder never overwrites an existing value — it only fills
fields that are missing/empty. Overwriting here would let a stale static
table clobber dashboard edits on every ``btq setup-couchdb`` run.

SANDBOX is skipped: the demo persona is code-canonical
(couchdb_registry builtin row) and must not enter btq_vault.
"""

import json
from typing import Any
from urllib import error, parse, request

from btq_vault.entity_types import current_operator_id
from event_pipeline import couchdb_config
from event_pipeline.site_registry_data import BUILTIN_SANDBOX_SITE, load_vision_contexts
from event_pipeline.sites import SITES, normalize_for_match


# Real labels live in the gitignored site_registry.json (synthetic example committed).
SITE_VISION_CONTEXTS: dict[str, dict[str, str]] = load_vision_contexts()

# Registration fields this seeder may fill on a location doc.
REGISTRATION_FIELDS = (
    "site_id",
    "location",
    "aliases",
    "capture_active",
    "vision_context",
    "note_path",
)


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
    config = couchdb_config.from_env()
    url = f"{config.base_url}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    body = None
    if payload is not None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        raise
    if not raw:
        return status, None
    return status, json.loads(raw.decode("utf-8"))


def get_existing_doc(database: str, doc_id: str) -> dict[str, Any] | None:
    _status, payload = request_json("GET", f"{parse.quote(database, safe='')}/{parse.quote(doc_id, safe='')}")
    return payload


def put_doc(database: str, doc: dict[str, Any]) -> None:
    request_json("PUT", f"{parse.quote(database, safe='')}/{parse.quote(str(doc['_id']), safe='')}", doc)


def normalize_contexts() -> dict[str, dict[str, str]]:
    contexts: dict[str, dict[str, str]] = {}
    for raw_key, raw_context in SITE_VISION_CONTEXTS.items():
        key = normalize_for_match(str(raw_key))
        contexts[key] = {
            "context_id": str(raw_context.get("context_id") or key),
            "label": str(raw_context.get("label") or ""),
            "summary": str(raw_context.get("summary") or ""),
            "environment": str(raw_context.get("environment") or ""),
        }
    return contexts


def registration_fields(site: dict[str, Any], contexts: dict[str, dict[str, str]]) -> dict[str, Any]:
    site_id = str(site["site_id"])
    aliases = sorted({normalize_for_match(str(alias)) for alias in site.get("aliases", []) if str(alias).strip()})
    context = contexts.get(site_id)
    if context is None:
        context_keys = [normalize_for_match(str(site["canonical"])), *aliases]
        context = next((contexts[key] for key in context_keys if key in contexts), None)
    return {
        "site_id": site_id,
        "location": str(site["canonical"]),
        "aliases": aliases,
        "capture_active": True,
        "vision_context": context,
        "note_path": str(site["note_path"]),
    }


def minimal_location_doc(site_id: str) -> dict[str, Any]:
    return {
        "_id": f"location_{site_id}",
        "type": "location",
        "operator": current_operator_id(),
        "status": "active",
    }


def fill_missing(existing: dict[str, Any] | None, desired: dict[str, Any], site_id: str) -> tuple[dict[str, Any], bool, bool]:
    """Fill-only merge. Returns (doc, is_created, changed)."""
    is_created = existing is None
    merged = dict(existing) if existing is not None else minimal_location_doc(site_id)
    changed = False
    for key, value in desired.items():
        if merged.get(key) in (None, "", []):
            merged[key] = value
            changed = True
    return merged, is_created, changed


def main() -> int:
    database = couchdb_config.vault_database()
    contexts = normalize_contexts()
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for site in SITES:
        site_id = str(site["site_id"])
        if site_id == str(BUILTIN_SANDBOX_SITE["site_id"]):
            continue
        desired = registration_fields(site, contexts)
        doc_id = f"location_{site_id}"
        existing = get_existing_doc(database, doc_id)
        merged, is_created, changed = fill_missing(existing, desired, site_id)
        if is_created:
            put_doc(database, merged)
            created.append(doc_id)
        elif changed:
            put_doc(database, merged)
            updated.append(doc_id)
        else:
            skipped.append(doc_id)

    print(f"created: {len(created)} {created}")
    print(f"updated: {len(updated)} {updated}")
    print(f"skipped: {len(skipped)} {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
