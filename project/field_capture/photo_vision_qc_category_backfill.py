from __future__ import annotations

import argparse
import json
import logging
import os
from urllib import error, parse, request

from event_pipeline import couchdb_config
from field_capture.photo_vision_couchdb import PhotoVisionCouchDBError, query_photo_vision
from processing_core.results import result_counts
from voice_memo.couchdb import query_couchdb_find

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 250


def _put_couchdb_document(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc: dict[str, object],
) -> None:
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise PhotoVisionCouchDBError("document is missing _id")
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    url = f"{config.base_url}/{db}/{quoted_id}"
    body = json.dumps(doc, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(url, data=body, headers=headers, method="PUT")
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            response.read()
    except error.HTTPError as exc:
        raise PhotoVisionCouchDBError(f"CouchDB PUT failed for {doc_id}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise PhotoVisionCouchDBError(f"CouchDB PUT failed for {doc_id}: {exc}") from exc
    if not 200 <= status < 300:
        raise PhotoVisionCouchDBError(f"CouchDB PUT failed for {doc_id}: HTTP {status}")


def _capture_qc_categories(
    config: couchdb_config.CouchDBConfig,
    capture_ids: list[str],
) -> dict[str, str]:
    cleaned = sorted({str(capture_id).strip() for capture_id in capture_ids if str(capture_id).strip()})
    if not cleaned:
        return {}
    mango = {
        "selector": {
            "type": "field_capture",
            "capture_id": {"$in": cleaned},
        },
        "fields": ["_id", "capture_id", "qc_category"],
        "limit": max(100, len(cleaned) + 10),
    }
    response = query_couchdb_find(config, couchdb_config.field_captures_database(), mango)
    docs = response.get("docs")
    if not isinstance(docs, list):
        return {}
    by_capture: dict[str, str] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
        qc_category = str(doc.get("qc_category") or "").strip()
        if capture_id and qc_category:
            by_capture[capture_id] = qc_category
    return by_capture


def backfill_photo_vision_qc_categories(
    config: couchdb_config.CouchDBConfig,
    *,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> dict[str, int]:
    """Copy field_capture.qc_category onto existing btq_photo_vision sidecars.

    The selector only reads sidecars where qc_category is absent or blank, so
    re-running after a successful pass is a no-op except for sidecars whose
    source capture is still missing or has no category.
    """
    page_size = max(1, batch_size)
    remaining = limit if limit is not None else None
    counts = result_counts("scanned", "updated", "dry_run", "missing_capture", "missing_qc_category", "failed")
    bookmark: object = None

    while True:
        current_limit = page_size if remaining is None else min(page_size, remaining)
        if current_limit <= 0:
            break
        mango: dict[str, object] = {
            "selector": {
                "$and": [
                    {"doc_type": "photo_vision_sidecar"},
                    {"$or": [{"qc_category": {"$exists": False}}, {"qc_category": ""}]},
                ],
            },
            "limit": current_limit,
        }
        if bookmark:
            mango["bookmark"] = bookmark

        response = query_photo_vision(config, mango, database=couchdb_config.photo_vision_database())
        docs = response.get("docs")
        if not isinstance(docs, list) or not docs:
            break

        counts["scanned"] += len(docs)
        capture_map = _capture_qc_categories(
            config,
            [str(doc.get("capture_id") or "").strip() for doc in docs if isinstance(doc, dict)],
        )

        for doc in docs:
            if not isinstance(doc, dict):
                counts["failed"] += 1
                continue
            doc_id = str(doc.get("_id") or "").strip()
            capture_id = str(doc.get("capture_id") or "").strip()
            if not capture_id or capture_id not in capture_map:
                logger.info("photo vision sidecar %s has no matching field_capture for capture_id=%s", doc_id, capture_id)
                counts["missing_capture"] += 1
                continue
            qc_category = capture_map.get(capture_id, "")
            if not qc_category:
                logger.info("field_capture %s has no qc_category; leaving sidecar %s unchanged", capture_id, doc_id)
                counts["missing_qc_category"] += 1
                continue
            updated_doc = dict(doc)
            updated_doc["qc_category"] = qc_category
            if dry_run:
                counts["dry_run"] += 1
                continue
            try:
                _put_couchdb_document(config, couchdb_config.photo_vision_database(), updated_doc)
                counts["updated"] += 1
            except PhotoVisionCouchDBError as exc:
                logger.warning("qc_category backfill failed for sidecar %s: %s", doc_id, exc)
                counts["failed"] += 1

        if remaining is not None:
            remaining -= len(docs)
        bookmark = response.get("bookmark")
        if len(docs) < current_limit or not bookmark:
            break

    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill qc_category onto existing btq_photo_vision sidecars from btq_field_captures.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan and join records without writing to CouchDB.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Sidecars to process per Mango page.")
    parser.add_argument("--limit", type=int, help="Maximum number of sidecars to scan.")
    parser.add_argument("--json", action="store_true", help="Print result counts as JSON.")
    return parser


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if not (os.environ.get("BTQ_COUCHDB_URL") or "").strip():
        print("error: BTQ_COUCHDB_URL is not set")
        return 1
    config = couchdb_config.from_env()
    counts = backfill_photo_vision_qc_categories(
        config,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "live"
        parts = " ".join(f"{key}={value}" for key, value in counts.items())
        print(f"photo-vision qc_category backfill ({mode}): {parts}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
