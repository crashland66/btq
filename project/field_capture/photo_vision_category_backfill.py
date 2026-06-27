from __future__ import annotations

import argparse
import json
import logging
import os
from urllib import error, parse, request

from event_pipeline import couchdb_config
from field_capture.photo_vision_categories import derive_vision_category_fields
from field_capture.photo_vision_couchdb import PhotoVisionCouchDBError, query_photo_vision
from processing_core.results import result_counts

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 250
_MISSING = object()


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


def _needs_update(doc: dict[str, object], derived: dict[str, object]) -> bool:
    return (
        doc.get("vision_category", _MISSING) != derived["vision_category"]
        or doc.get("category_agreement", _MISSING) != derived["category_agreement"]
    )


def backfill_photo_vision_categories(
    config: couchdb_config.CouchDBConfig,
    *,
    dry_run: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
) -> dict[str, int]:
    """Derive vision_category/category_agreement on btq_photo_vision sidecars.

    The derivation uses only each sidecar's own area_guess and qc_category.
    No field-capture join is needed, so re-running is idempotent.
    """
    page_size = max(1, batch_size)
    remaining = limit if limit is not None else None
    counts = result_counts("scanned", "updated", "dry_run", "unchanged", "failed")
    bookmark: object = None

    while True:
        current_limit = page_size if remaining is None else min(page_size, remaining)
        if current_limit <= 0:
            break
        mango: dict[str, object] = {
            "selector": {"doc_type": "photo_vision_sidecar"},
            "limit": current_limit,
        }
        if bookmark:
            mango["bookmark"] = bookmark

        response = query_photo_vision(config, mango, database=couchdb_config.photo_vision_database())
        docs = response.get("docs")
        if not isinstance(docs, list) or not docs:
            break

        counts["scanned"] += len(docs)
        for doc in docs:
            if not isinstance(doc, dict):
                counts["failed"] += 1
                continue
            doc_id = str(doc.get("_id") or "").strip()
            derived = derive_vision_category_fields(doc.get("area_guess"), doc.get("qc_category"))
            if not _needs_update(doc, derived):
                counts["unchanged"] += 1
                continue
            updated_doc = dict(doc)
            updated_doc.update(derived)
            if dry_run:
                counts["dry_run"] += 1
                continue
            try:
                _put_couchdb_document(config, couchdb_config.photo_vision_database(), updated_doc)
                counts["updated"] += 1
            except PhotoVisionCouchDBError as exc:
                logger.warning("photo vision category backfill failed for sidecar %s: %s", doc_id, exc)
                counts["failed"] += 1

        if remaining is not None:
            remaining -= len(docs)
        bookmark = response.get("bookmark")
        if len(docs) < current_limit or not bookmark:
            break

    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill vision_category and category_agreement on btq_photo_vision sidecars.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan sidecars without writing to CouchDB.")
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
    counts = backfill_photo_vision_categories(
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
        print(f"photo-vision category backfill ({mode}): {parts}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
