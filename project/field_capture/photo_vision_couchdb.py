from __future__ import annotations

import json
import logging
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config

logger = logging.getLogger(__name__)

DOC_TYPE = "photo_vision_sidecar"
SCHEMA_VERSION = 2


class PhotoVisionCouchDBError(Exception):
    pass


def build_photo_vision_document(sidecar_payload: dict[str, object]) -> dict[str, object]:
    """Map a sidecar payload to the btq_photo_vision CouchDB document.

    Schema 2 stores the FULL sidecar payload instead of a hand-picked subset —
    the subset mapping silently dropped every field added after it was written
    (summary, quality_flags, source_image_path, site context), which is how
    the dashboard's CouchDB reads drifted from disk. Only derived doc plumbing
    is layered on top: _id, doc_type, schema_version, and search_text.
    """
    photo_asset_id = str(sidecar_payload.get("photo_asset_id") or "").strip()
    if not photo_asset_id:
        raise PhotoVisionCouchDBError("sidecar_payload is missing photo_asset_id")

    doc: dict[str, object] = {
        str(key): value
        for key, value in sidecar_payload.items()
        if not str(key).startswith("_")
    }
    doc["_id"] = photo_asset_id
    doc["doc_type"] = DOC_TYPE
    doc["schema_version"] = SCHEMA_VERSION
    # Pre-lane sidecars lack vision_lane; consumers filter on it, so the
    # normalized default survives the passthrough rewrite.
    lane = str(sidecar_payload.get("vision_lane") or "").strip()
    doc["vision_lane"] = lane if lane in {"qc", "pow"} else "pow"
    # deep_analysis consumers iterate a list; a malformed shape is dropped
    # rather than propagated (pinned by test_build_doc_ignores_non_list_deep_analysis).
    if "deep_analysis" in doc and not isinstance(doc["deep_analysis"], list):
        del doc["deep_analysis"]

    def _str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    search_parts = [
        str(sidecar_payload.get("description") or ""),
        str(sidecar_payload.get("summary") or ""),
        str(sidecar_payload.get("area_guess") or ""),
        str(sidecar_payload.get("qc_category") or ""),
        str(sidecar_payload.get("vision_category") or ""),
        str(sidecar_payload.get("category_agreement") or ""),
    ]
    search_parts.extend(_str_list(sidecar_payload.get("visible_objects")))
    search_parts.extend(_str_list(sidecar_payload.get("possible_conditions")))
    search_parts.extend(_str_list(sidecar_payload.get("possible_issues")))
    doc["search_text"] = " ".join(part for part in search_parts if part).lower()
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


def fetch_all_photo_vision_docs(
    config: couchdb_config.CouchDBConfig,
    *,
    database: str | None = None,
    page_size: int = 5000,
    max_pages: int = 400,
) -> list[dict[str, Any]]:
    """Every photo_vision_sidecar doc, bookmark-paginated (the corpus is ~16k
    and growing; a fixed limit would silently truncate)."""
    docs: list[dict[str, Any]] = []
    bookmark: object = None
    for _ in range(max_pages):
        mango: dict[str, object] = {
            "selector": {"doc_type": DOC_TYPE},
            "limit": page_size,
        }
        if bookmark:
            mango["bookmark"] = bookmark
        payload = query_photo_vision(config, mango, database=database)
        page = [doc for doc in payload.get("docs", []) if isinstance(doc, dict)]
        docs.extend(page)
        if len(page) < page_size:
            break
        bookmark = payload.get("bookmark")
        if not bookmark:
            break
    return docs


def _bulk_revs(config: couchdb_config.CouchDBConfig, database: str, doc_ids: list[str]) -> dict[str, str]:
    db_quoted = parse.quote(database, safe="")
    url = f"{config.base_url}/{db_quoted}/_all_docs"
    body = json.dumps({"keys": doc_ids}).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=config.timeout) as response:
        rows = json.loads(response.read().decode("utf-8")).get("rows", [])
    revs: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("value"), dict):
            revs[str(row.get("id"))] = str(row["value"].get("rev") or "")
    return revs


def _bulk_put(config: couchdb_config.CouchDBConfig, database: str, docs: list[dict[str, Any]]) -> int:
    db_quoted = parse.quote(database, safe="")
    url = f"{config.base_url}/{db_quoted}/_bulk_docs"
    body = json.dumps({"docs": docs}).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=max(config.timeout, 60.0)) as response:
        results = json.loads(response.read().decode("utf-8"))
    return sum(1 for item in results if isinstance(item, dict) and item.get("ok"))


def reconcile_photo_vision_from_disk(
    config: couchdb_config.CouchDBConfig,
    photo_vision_dir: "Path",
    *,
    database: str | None = None,
    batch_size: int = 500,
) -> dict[str, int]:
    """Re-put every disk sidecar through the full-payload mapping.

    Idempotent one-shot: upgrades subset-schema docs to schema 2 and closes
    any write-through gaps, making CouchDB the complete read source.
    """
    from pathlib import Path

    db = database or couchdb_config.photo_vision_database()
    paths = sorted(Path(photo_vision_dir).glob("*.json"))
    written = 0
    skipped = 0
    for start in range(0, len(paths), batch_size):
        batch_docs: list[dict[str, Any]] = []
        for path in paths[start:start + batch_size]:
            try:
                sidecar = json.loads(path.read_text(encoding="utf-8"))
                batch_docs.append(build_photo_vision_document(sidecar))
            except Exception:  # noqa: BLE001 - unreadable sidecars are counted, not fatal.
                skipped += 1
        if not batch_docs:
            continue
        revs = _bulk_revs(config, db, [str(doc["_id"]) for doc in batch_docs])
        for doc in batch_docs:
            rev = revs.get(str(doc["_id"]))
            if rev:
                doc["_rev"] = rev
        written += _bulk_put(config, db, batch_docs)
    return {"disk_sidecars": len(paths), "written": written, "unreadable": skipped}
