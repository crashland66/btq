from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.couchdb_registry import CouchDBRegistryError, CouchDBSiteRegistry
from event_pipeline.sites import SITES


ACTIVE_LOCATION_STATUSES = {"active", "contracted"}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _location_doc(row: dict[str, Any]) -> dict[str, Any]:
    doc = row.get("doc")
    return doc if isinstance(doc, dict) else row


def _location_value(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("value")
    return value if isinstance(value, dict) else {}


def _location_site_id(row: dict[str, Any]) -> str:
    doc = _location_doc(row)
    value = _location_value(row)
    return _clean(doc.get("site_id") or doc.get("job") or value.get("site_id") or value.get("job"))


def _location_status(row: dict[str, Any]) -> str:
    doc = _location_doc(row)
    value = _location_value(row)
    return _clean(doc.get("status") or value.get("status")).lower()


def _location_canonical(row: dict[str, Any]) -> str:
    doc = _location_doc(row)
    value = _location_value(row)
    account = _clean(doc.get("account") or value.get("account"))
    name = _clean(doc.get("canonical") or doc.get("name") or doc.get("location") or value.get("name") or value.get("location"))
    if account and name:
        return f"{account} - {name}"
    return name or account or _clean(doc.get("_id") or row.get("id"))


def missing_sites(registered_ids: set[str], location_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    registered = {_clean(site_id) for site_id in registered_ids if _clean(site_id)}
    missing: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in location_rows:
        site_id = _location_site_id(row)
        if not site_id or site_id in registered or site_id in seen:
            continue
        status = _location_status(row)
        if status not in ACTIVE_LOCATION_STATUSES:
            continue
        missing.append({
            "site_id": site_id,
            "canonical": _location_canonical(row),
            "status": status,
        })
        seen.add(site_id)
    return sorted(missing, key=lambda item: (item["site_id"], item["canonical"]))


def registered_site_ids() -> set[str]:
    if os.environ.get("BTQ_COUCHDB_URL"):
        try:
            registry = CouchDBSiteRegistry()
            return {row.site_id for row in registry._get_alias_rows() if row.site_id}
        except CouchDBRegistryError:
            raise
    return {str(site["site_id"]) for site in SITES}


def _request_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise CouchDBRegistryError(f"CouchDB request failed with HTTP {exc.code}: {url}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBRegistryError(f"CouchDB request failed: {url}") from exc
    if not 200 <= status < 300:
        raise CouchDBRegistryError(f"CouchDB request failed with HTTP {status}: {url}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBRegistryError(f"CouchDB returned invalid JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise CouchDBRegistryError(f"CouchDB returned non-object JSON: {url}")
    return payload


def vault_location_rows() -> list[dict[str, Any]]:
    config = couchdb_config.from_env()
    db = parse.quote(couchdb_config.vault_database(), safe="")
    query = parse.urlencode({"include_docs": "true"})
    url = f"{config.base_url}/{db}/_design/btq_vault/_view/locations_all?{query}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    payload = _request_json(url, headers, config.timeout)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise CouchDBRegistryError("CouchDB locations_all view returned no rows list")
    return [row for row in rows if isinstance(row, dict)]


def run() -> int:
    missing = missing_sites(registered_site_ids(), vault_location_rows())
    if missing:
        print("Missing registered active vault locations:")
        for item in missing:
            print(f"- {item['site_id']} | {item['canonical']} | status={item['status']}")
    else:
        print("No missing registered active vault locations.")
    print(f"Summary: {len(missing)} missing active/contracted vault locations")
    return 0


def main() -> int:
    return run()
