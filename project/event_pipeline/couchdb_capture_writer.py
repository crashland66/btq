from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


class CouchDBCaptureWriterError(Exception):
    pass


def _get_current_rev(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc_id: str,
) -> str | None:
    """Return the current _rev of a document, or None if it does not exist."""
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
        parsed = json.loads(raw.decode("utf-8"))
        return str(parsed.get("_rev") or "") or None
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CouchDBCaptureWriterError(f"CouchDB capture GET failed for {doc_id}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCaptureWriterError(f"CouchDB capture GET failed for {doc_id}") from exc


def get_field_capture_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc_id: str,
) -> dict[str, Any] | None:
    """
    Fetch a field-capture document by _id. Returns the parsed
    document dict, or None if the document does not exist
    (HTTP 404). Raises CouchDBCaptureWriterError on any other
    transport or HTTP failure.

    Used by /api/submit for capture_id idempotency: if a doc
    already exists, the handler returns the original response
    shape without writing media or PUTing a new revision.
    """
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise CouchDBCaptureWriterError(
            f"CouchDB capture GET failed for {doc_id}: HTTP {exc.code}"
        ) from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCaptureWriterError(
            f"CouchDB capture GET failed for {doc_id}"
        ) from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBCaptureWriterError(
            "CouchDB capture GET returned invalid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise CouchDBCaptureWriterError(
            "CouchDB capture GET returned non-object JSON"
        )
    return parsed


def put_field_capture_document(
    config: couchdb_config.CouchDBConfig,
    doc: dict[str, Any],
    *,
    database: str,
) -> dict[str, Any]:
    """
    PUT a field-capture document to CouchDB. Document _id is required.
    Fetches current _rev before writing; retries once on 409 conflict.
    Returns the parsed CouchDB response on success.
    Raises CouchDBCaptureWriterError on any failure.
    """
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise CouchDBCaptureWriterError("CouchDB capture document is missing _id")
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())

    for attempt in range(1, 3):
        rev = _get_current_rev(config, database, doc_id)
        doc_to_put = dict(doc)
        if rev:
            doc_to_put["_rev"] = rev

        body = json.dumps(doc_to_put, sort_keys=True).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with request.urlopen(req, timeout=config.timeout) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code == 409 and attempt < 2:
                continue
            raise CouchDBCaptureWriterError(
                f"CouchDB capture PUT failed with HTTP {exc.code} for {doc_id}"
            ) from exc
        except (error.URLError, OSError) as exc:
            raise CouchDBCaptureWriterError(f"CouchDB capture PUT failed for {doc_id}") from exc

        if not 200 <= status < 300:
            raise CouchDBCaptureWriterError(f"CouchDB capture PUT failed with HTTP {status} for {doc_id}")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CouchDBCaptureWriterError("CouchDB capture PUT returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise CouchDBCaptureWriterError("CouchDB capture PUT returned non-object JSON")
        return parsed

    raise CouchDBCaptureWriterError(f"CouchDB capture PUT exhausted retries for {doc_id}")
