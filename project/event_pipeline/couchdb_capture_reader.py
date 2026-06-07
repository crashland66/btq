from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


class CouchDBCaptureReaderError(Exception):
    pass


def query_captures_by_site_id(
    config: couchdb_config.CouchDBConfig,
    site_id: str,
    *,
    database: str,
) -> list[dict[str, Any]]:
    normalized = str(site_id).strip()
    if not normalized:
        return []
    query = parse.urlencode(
        {
            "descending": "true",
            "startkey": json.dumps([normalized, {}]),
            "endkey": json.dumps([normalized, ""]),
        }
    )
    payload = _request_json(config, _view_url(config, database, "by_site_id", query))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise CouchDBCaptureReaderError("CouchDB by_site_id view returned no rows list")
    captures: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("value")
        if isinstance(value, dict):
            captures.append(value)
    return captures


def query_captures_by_target(
    config: couchdb_config.CouchDBConfig,
    target_type: str,
    target_id: str,
    *,
    database: str,
) -> list[dict[str, Any]]:
    target_type = str(target_type).strip()
    target_id = str(target_id).strip()
    if not target_type or not target_id:
        return []
    selector = {
        "type": "field_capture",
        "target_type": target_type,
        "target_id": target_id,
    }
    # No `sort` here: CouchDB rejects mango sorts without a matching
    # index ("no_usable_index"). Sort in Python after fetching — the
    # selector keeps the result set small enough that an O(n log n)
    # pass is fine, and we avoid the design-doc churn of declaring
    # an index just for this read.
    body = json.dumps({
        "selector": selector,
        "limit": 500,
    }).encode("utf-8")
    payload = _request_json_post(config, _find_url(config, database), body)
    docs = payload.get("docs")
    if not isinstance(docs, list):
        raise CouchDBCaptureReaderError("CouchDB _find returned no docs list")
    matched = [
        doc
        for doc in docs
        if isinstance(doc, dict)
        and str(doc.get("target_type") or "").strip() == target_type
        and str(doc.get("target_id") or "").strip() == target_id
    ]
    matched.sort(key=lambda doc: str(doc.get("captured_at") or ""), reverse=True)
    return matched


def query_site_id_by_upload_id(
    config: couchdb_config.CouchDBConfig,
    upload_id: str,
    *,
    database: str,
) -> str | None:
    """Back-compat reader. Returns the upload's site_id if the
    target is a location, None for prospect targets or missing.

    Prefer query_capture_target_by_upload_id for new callers."""
    target = query_capture_target_by_upload_id(config, upload_id, database=database)
    if target is None:
        return None
    if target["target_type"] != "location":
        return None
    return target["site_id"] or None


def query_capture_target_by_upload_id(
    config: couchdb_config.CouchDBConfig,
    upload_id: str,
    *,
    database: str,
) -> dict[str, str] | None:
    """
    Resolve the target context of a media upload by upload_id.
    Returns a dict {target_type, target_id, site_id} or None if
    the upload is unknown. Reads the by_upload_id view; for
    post-prompt-155 design docs the value is an object, and for
    pre-155 docs it's a bare site_id string (back-compat shim
    promotes the string to a location target).
    """
    normalized = str(upload_id).strip()
    if not normalized:
        return None
    query = parse.urlencode({"key": json.dumps(normalized), "limit": "1"})
    payload = _request_json(config, _view_url(config, database, "by_upload_id", query))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise CouchDBCaptureReaderError("CouchDB by_upload_id view returned no rows list")
    if not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    value = first.get("value")
    # Post-155 shape: object
    if isinstance(value, dict):
        target_type = str(value.get("target_type") or "location")
        target_id = str(value.get("target_id") or value.get("site_id") or "").strip()
        site_id = str(value.get("site_id") or "").strip()
        if not target_id:
            return None
        return {"target_type": target_type, "target_id": target_id, "site_id": site_id}
    # Pre-155 shape: bare site_id string. Promote to a location target.
    if isinstance(value, str):
        site_id = value.strip()
        if not site_id:
            return None
        return {"target_type": "location", "target_id": site_id, "site_id": site_id}
    return None


def _view_url(config: couchdb_config.CouchDBConfig, database: str, view_name: str, query: str) -> str:
    db = parse.quote(database, safe="")
    view = parse.quote(view_name, safe="")
    return f"{config.base_url}/{db}/_design/btq_field_captures/_view/{view}?{query}"


def _find_url(config: couchdb_config.CouchDBConfig, database: str) -> str:
    return f"{config.base_url}/{parse.quote(database, safe='')}/_find"


def _request_json(config: couchdb_config.CouchDBConfig, url: str) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            body = response.read()
    except error.HTTPError as exc:
        raise CouchDBCaptureReaderError(f"CouchDB capture view failed with HTTP {exc.code}: {url}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCaptureReaderError(f"CouchDB capture view failed: {url}") from exc
    if not 200 <= status < 300:
        raise CouchDBCaptureReaderError(f"CouchDB capture view failed with HTTP {status}: {url}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBCaptureReaderError(f"CouchDB capture view returned invalid JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise CouchDBCaptureReaderError(f"CouchDB capture view returned non-object JSON: {url}")
    return payload


def _request_json_post(config: couchdb_config.CouchDBConfig, url: str, body: bytes) -> dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json", **config.auth_header()}
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (error.URLError, error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        raise CouchDBCaptureReaderError(f"CouchDB _find request failed: {exc}") from exc
