from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.sites import SITES, normalize_for_match
from event_pipeline.site_registry_data import load_vision_contexts


# Real labels live in the gitignored site_registry.json (synthetic example committed).
SITE_VISION_CONTEXTS: dict[str, dict[str, str]] = load_vision_contexts()


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


def site_document(site: dict[str, Any], contexts: dict[str, dict[str, str]]) -> dict[str, Any]:
    site_id = str(site["site_id"])
    aliases = sorted({normalize_for_match(str(alias)) for alias in site.get("aliases", []) if str(alias).strip()})
    context = contexts.get(site_id)
    if context is None:
        context_keys = [normalize_for_match(str(site["canonical"])), *aliases]
        context = next((contexts[key] for key in context_keys if key in contexts), None)
    return {
        "_id": f"site_{site_id}",
        "type": "site",
        "site_id": site_id,
        "account": str(site["canonical"]),
        "location": str(site["canonical"]),
        "aliases": aliases,
        "active": True,
        "vision_context": context,
        "note_path": str(site["note_path"]),
    }


def merge_doc(existing: dict[str, Any] | None, desired: dict[str, Any]) -> tuple[dict[str, Any], bool, bool]:
    if existing is None:
        return desired, True, False
    merged = dict(existing)
    changed = False
    for key, value in desired.items():
        if key in {"aliases", "vision_context"}:
            if merged.get(key) != value:
                merged[key] = value
                changed = True
        elif key not in merged or merged.get(key) in (None, "", []):
            merged[key] = value
            changed = True
    return merged, False, changed


def main() -> int:
    database = couchdb_config.sites_database()
    contexts = normalize_contexts()
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for site in SITES:
        desired = site_document(site, contexts)
        existing = get_existing_doc(database, str(desired["_id"]))
        merged, is_created, changed = merge_doc(existing, desired)
        if is_created:
            put_doc(database, merged)
            created.append(str(desired["_id"]))
        elif changed:
            put_doc(database, merged)
            updated.append(str(desired["_id"]))
        else:
            skipped.append(str(desired["_id"]))

    print(f"created: {len(created)} {created}")
    print(f"updated: {len(updated)} {updated}")
    print(f"skipped: {len(skipped)} {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
