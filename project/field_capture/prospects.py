from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

from btq_vault.entity_types import current_operator_id
from event_pipeline import couchdb_config


PROSPECT_STATUSES = ("open", "quoted", "won", "lost", "withdrawn")
TERMINAL_PROSPECT_STATUSES = ("won", "lost", "withdrawn")


class CouchDBProspectError(Exception):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_prospect_document(
    *,
    prospect_id: str,
    name: str,
    address: str = "",
    account: str = "",
    area_manager: str = "",
    lead_source: str = "",
    status: str = "open",
    notes: str = "",
    created_at: str | None = None,
) -> dict:
    """Construct a CouchDB prospect doc."""
    normalized_id = prospect_id.strip()
    normalized_name = name.strip()
    normalized_status = status.strip()
    if not normalized_id:
        raise ValueError("prospect_id is required")
    if not normalized_name:
        raise ValueError("name is required")
    if normalized_status not in PROSPECT_STATUSES:
        raise ValueError(f"invalid prospect status: {status}")
    return {
        "_id": f"prospect_{normalized_id}",
        "doc_type": "prospect",
        "type": "prospect",
        "operator": current_operator_id(),
        "prospect_id": normalized_id,
        "name": normalized_name,
        "address": address,
        "account": account,
        "area_manager": area_manager,
        "lead_source": lead_source,
        "status": normalized_status,
        "created_at": created_at or _utc_now_iso(),
        "promoted_to_site_id": None,
        "promoted_at": None,
        "notes": notes,
    }


def write_prospect(
    cdb_config: couchdb_config.CouchDBConfig,
    *,
    prospect: dict,
) -> dict:
    """Upsert a prospect doc into the vault DB."""
    doc_id = str(prospect.get("_id") or "").strip()
    if not doc_id:
        raise CouchDBProspectError("prospect document is missing _id")
    database = couchdb_config.sites_database()
    current = _get_doc(cdb_config, database, doc_id)
    doc_to_put = dict(prospect)
    if current and current.get("_rev"):
        doc_to_put["_rev"] = current["_rev"]
    return _put_doc(cdb_config, database, doc_id, doc_to_put)


def load_prospect(cdb_config: couchdb_config.CouchDBConfig, prospect_id: str) -> dict | None:
    """GET prospect_<id> from the vault DB; return None on 404."""
    normalized_id = prospect_id.strip()
    if not normalized_id:
        return None
    return _get_doc(cdb_config, couchdb_config.sites_database(), f"prospect_{normalized_id}")


def list_active_prospects(cdb_config: couchdb_config.CouchDBConfig) -> list[dict]:
    payload = {
        "selector": {
            "doc_type": "prospect",
            "status": {"$in": ["open", "quoted"]},
        }
    }
    data = _post_json(cdb_config, couchdb_config.sites_database(), "_find", payload)
    docs = data.get("docs")
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def _doc_url(config: couchdb_config.CouchDBConfig, database: str, doc_id: str) -> str:
    return f"{config.base_url}/{parse.quote(database, safe='')}/{parse.quote(doc_id, safe='')}"


def _get_doc(config: couchdb_config.CouchDBConfig, database: str, doc_id: str) -> dict | None:
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(_doc_url(config, database, doc_id), headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CouchDBProspectError(f"CouchDB prospect GET failed for {doc_id}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBProspectError(f"CouchDB prospect GET failed for {doc_id}") from exc
    return _decode_object(raw, f"CouchDB prospect GET returned invalid JSON for {doc_id}")


def _put_doc(config: couchdb_config.CouchDBConfig, database: str, doc_id: str, doc: dict) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    body = json.dumps(doc, sort_keys=True).encode("utf-8")
    req = request.Request(_doc_url(config, database, doc_id), data=body, headers=headers, method="PUT")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise CouchDBProspectError(f"CouchDB prospect PUT failed for {doc_id}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBProspectError(f"CouchDB prospect PUT failed for {doc_id}") from exc
    if not 200 <= status < 300:
        raise CouchDBProspectError(f"CouchDB prospect PUT failed for {doc_id}: HTTP {status}")
    return _decode_object(raw, f"CouchDB prospect PUT returned invalid JSON for {doc_id}") if raw else {}


def _post_json(config: couchdb_config.CouchDBConfig, database: str, path: str, payload: dict[str, Any]) -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    url = f"{config.base_url}/{parse.quote(database, safe='')}/{path}"
    req = request.Request(url, data=json.dumps(payload, sort_keys=True).encode("utf-8"), headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise CouchDBProspectError(f"CouchDB prospect query failed: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBProspectError("CouchDB prospect query failed") from exc
    if not 200 <= status < 300:
        raise CouchDBProspectError(f"CouchDB prospect query failed: HTTP {status}")
    return _decode_object(raw, "CouchDB prospect query returned invalid JSON")


def _decode_object(raw: bytes, message: str) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBProspectError(message) from exc
    if not isinstance(parsed, dict):
        raise CouchDBProspectError(message)
    return parsed
