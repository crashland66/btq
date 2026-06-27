from __future__ import annotations

import json
import logging
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config

logger = logging.getLogger(__name__)

DOC_TYPE = "photo_vision_sidecar"
SCHEMA_VERSION = 1


class PhotoVisionCouchDBError(Exception):
    pass


def build_photo_vision_document(sidecar_payload: dict[str, object]) -> dict[str, object]:
    """Map a sidecar payload to the btq_photo_vision CouchDB document schema."""
    photo_asset_id = str(sidecar_payload.get("photo_asset_id") or "").strip()
    if not photo_asset_id:
        raise PhotoVisionCouchDBError("sidecar_payload is missing photo_asset_id")

    provenance = sidecar_payload.get("provenance") if isinstance(sidecar_payload.get("provenance"), dict) else {}

    description = str(sidecar_payload.get("description") or "")
    area_guess = str(sidecar_payload.get("area_guess") or "")
    visible_objects = sidecar_payload.get("visible_objects")
    possible_conditions = sidecar_payload.get("possible_conditions")
    possible_issues = sidecar_payload.get("possible_issues")

    def _str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    qc_category = str(sidecar_payload.get("qc_category") or "")
    raw_vision_category = sidecar_payload.get("vision_category")
    vision_category = str(raw_vision_category).strip() if raw_vision_category is not None else None
    category_agreement = str(sidecar_payload.get("category_agreement") or "").strip()

    search_parts = [description, area_guess, qc_category, vision_category or "", category_agreement]
    search_parts.extend(_str_list(visible_objects))
    search_parts.extend(_str_list(possible_conditions))
    search_parts.extend(_str_list(possible_issues))
    search_text = " ".join(part for part in search_parts if part).lower()

    quality = sidecar_payload.get("quality") if isinstance(sidecar_payload.get("quality"), dict) else None

    doc: dict[str, object] = {
        "_id": photo_asset_id,
        "doc_type": DOC_TYPE,
        "schema_version": SCHEMA_VERSION,
        "status": str(sidecar_payload.get("status") or ""),
        "generated_at": str(sidecar_payload.get("generated_at") or ""),
        "site_id": str(sidecar_payload.get("site_id") or ""),
        "capture_id": str(sidecar_payload.get("capture_id") or provenance.get("capture_id") or ""),
        "photo_asset_id": photo_asset_id,
        "photo_id": str(sidecar_payload.get("photo_id") or provenance.get("photo_id") or ""),
        "submitter_id": str(sidecar_payload.get("submitter_id") or ""),
        "qc_category": qc_category,
        "vision_category": vision_category,
        "category_agreement": category_agreement,
        "description": description,
        "area_guess": area_guess,
        "submitted_area": str(sidecar_payload.get("submitted_area") or ""),
        "submitted_phase": str(sidecar_payload.get("submitted_phase") or ""),
        "visible_objects": _str_list(visible_objects),
        "possible_conditions": _str_list(possible_conditions),
        "possible_issues": _str_list(possible_issues),
        "confidence": sidecar_payload.get("confidence"),
        "needs_human_review": bool(sidecar_payload.get("needs_human_review", True)),
        "warnings": _str_list(sidecar_payload.get("warnings")),
        "quality": quality,
        "search_text": search_text,
        "model_name": str(sidecar_payload.get("model_name") or ""),
        "model_provider": str(sidecar_payload.get("model_provider") or ""),
        "source_image_hash": str(sidecar_payload.get("source_image_hash") or ""),
        "provenance": provenance,
    }

    error_payload = sidecar_payload.get("error")
    if isinstance(error_payload, dict):
        doc["error"] = error_payload

    deep_analysis = sidecar_payload.get("deep_analysis")
    if isinstance(deep_analysis, list):
        doc["deep_analysis"] = deep_analysis

    return doc


def _get_current_rev(config: couchdb_config.CouchDBConfig, database: str, doc_id: str) -> str | None:
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
        raise PhotoVisionCouchDBError(f"CouchDB GET failed for {doc_id}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise PhotoVisionCouchDBError(f"CouchDB GET failed for {doc_id}: {exc}") from exc


def put_photo_vision_document(
    config: couchdb_config.CouchDBConfig,
    doc: dict[str, object],
    *,
    database: str | None = None,
) -> dict[str, Any]:
    """Idempotent PUT — fetches current _rev before writing; retries once on 409."""
    db = database or couchdb_config.photo_vision_database()
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise PhotoVisionCouchDBError("photo vision document is missing _id")

    db_quoted = parse.quote(db, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db_quoted}/{quoted_id}"

    for attempt in range(1, 3):
        rev = _get_current_rev(config, db, doc_id)
        doc_to_put = dict(doc)
        if rev:
            doc_to_put["_rev"] = rev

        body = json.dumps(doc_to_put, sort_keys=True).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        headers.update(config.auth_header())
        req = request.Request(url, data=body, headers=headers, method="PUT")
        try:
            with request.urlopen(req, timeout=config.timeout) as response:
                status = int(getattr(response, "status", getattr(response, "code", 200)))
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code == 409 and attempt < 2:
                logger.debug("CouchDB 409 conflict on put for %s; retrying", doc_id)
                continue
            raise PhotoVisionCouchDBError(f"CouchDB photo vision PUT failed with HTTP {exc.code} for {doc_id}") from exc
        except (error.URLError, OSError) as exc:
            raise PhotoVisionCouchDBError(f"CouchDB photo vision PUT failed for {doc_id}: {exc}") from exc

        if not 200 <= status < 300:
            raise PhotoVisionCouchDBError(f"CouchDB photo vision PUT failed with HTTP {status} for {doc_id}")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PhotoVisionCouchDBError("CouchDB photo vision PUT returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise PhotoVisionCouchDBError("CouchDB photo vision PUT returned non-object JSON")
        return parsed

    raise PhotoVisionCouchDBError(f"CouchDB photo vision PUT failed after retries for {doc_id}")


def query_photo_vision(
    config: couchdb_config.CouchDBConfig,
    selector: dict[str, object],
    *,
    database: str | None = None,
) -> dict[str, Any]:
    """Mango _find query against btq_photo_vision."""
    db = database or couchdb_config.photo_vision_database()
    db_quoted = parse.quote(db, safe="")
    url = f"{config.base_url}/{db_quoted}/_find"
    body = json.dumps(selector, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, data=body, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
    except error.HTTPError as exc:
        raise PhotoVisionCouchDBError(f"CouchDB photo vision _find failed: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise PhotoVisionCouchDBError(f"CouchDB photo vision _find failed: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhotoVisionCouchDBError("CouchDB photo vision _find returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise PhotoVisionCouchDBError("CouchDB photo vision _find returned non-object JSON")
    return parsed


def query_photo_vision_by_capture_ids(
    config: couchdb_config.CouchDBConfig,
    capture_ids: list[str],
    *,
    database: str | None = None,
) -> dict[str, list[dict]]:
    """Return photo vision docs grouped by capture_id, ordered by photo_id."""
    cleaned_ids = sorted({str(capture_id).strip() for capture_id in capture_ids if str(capture_id).strip()})
    if not cleaned_ids:
        return {}

    mango = {
        "selector": {"capture_id": {"$in": cleaned_ids}},
        "limit": max(100, len(cleaned_ids) * 10),
    }
    response = query_photo_vision(config, mango, database=database)
    docs_raw = response.get("docs")
    if not isinstance(docs_raw, list):
        return {}

    grouped: dict[str, list[dict]] = {}
    for doc in docs_raw:
        if not isinstance(doc, dict):
            continue
        capture_id = str(doc.get("capture_id") or "").strip()
        if not capture_id:
            continue
        grouped.setdefault(capture_id, []).append(doc)

    for docs in grouped.values():
        docs.sort(key=lambda doc: (str(doc.get("photo_id") or ""), str(doc.get("_id") or "")))
    return grouped
