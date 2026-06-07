from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from config import get_config
from field_capture import photo_vision as field_photo_vision
from field_capture.photo_vision_couchdb import (
    PhotoVisionCouchDBError,
    build_photo_vision_document,
    put_photo_vision_document,
)
from processing_core.results import result_counts

logger = logging.getLogger(__name__)


def backfill_photo_vision(
    photo_vision_dir: Path,
    *,
    cdb_config: object,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Scan photo_vision_dir and PUT each sidecar to CouchDB. Idempotent."""
    counts = result_counts("found", "put", "skipped", "failed", "dry_run")
    if not photo_vision_dir.exists():
        logger.warning("photo vision dir does not exist: %s", photo_vision_dir)
        return counts

    paths = sorted(photo_vision_dir.glob("*.json"))
    counts["found"] = len(paths)

    processed = 0
    for path in paths:
        if limit is not None and processed >= limit:
            counts["skipped"] += len(paths) - processed
            break
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping malformed sidecar %s: %s", path.name, exc)
            counts["failed"] += 1
            processed += 1
            continue

        if not isinstance(payload, dict):
            counts["failed"] += 1
            processed += 1
            continue

        # Compute quality block lazily for pre-Stage-1 sidecars
        if not isinstance(payload.get("quality"), dict):
            payload = dict(payload)
            payload["quality"] = field_photo_vision.derive_quality(payload)

        try:
            doc = build_photo_vision_document(payload)
        except PhotoVisionCouchDBError as exc:
            logger.warning("skipping sidecar %s: %s", path.name, exc)
            counts["failed"] += 1
            processed += 1
            continue

        if dry_run:
            logger.info("dry-run: would PUT %s", doc.get("_id"))
            counts["dry_run"] += 1
            processed += 1
            continue

        try:
            put_photo_vision_document(cdb_config, doc)
            counts["put"] += 1
        except PhotoVisionCouchDBError as exc:
            logger.warning("PUT failed for %s: %s", doc.get("_id"), exc)
            counts["failed"] += 1
        processed += 1

    return counts


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(
        description="Backfill existing photo vision sidecars into CouchDB btq_photo_vision.",
    )
    parser.add_argument(
        "--photo-vision-dir",
        type=Path,
        default=field_photo_vision.default_photo_vision_dir(config.runtime_root),
        help="Directory containing photo vision sidecar JSON files.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan and build documents but do not write to CouchDB.")
    parser.add_argument("--limit", type=int, help="Maximum number of sidecars to process.")
    parser.add_argument("--json", action="store_true", help="Print result counts as JSON.")
    return parser


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    photo_vision_dir = args.photo_vision_dir.expanduser()

    cdb_config = None
    if not args.dry_run:
        url = (os.environ.get("BTQ_COUCHDB_URL") or "").strip()
        if not url:
            print("error: BTQ_COUCHDB_URL is not set; use --dry-run or set the env var")
            return 1
        from event_pipeline import couchdb_config as _cdb
        cdb_config = _cdb.from_env()

    counts = backfill_photo_vision(
        photo_vision_dir,
        cdb_config=cdb_config,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "live"
        parts = " ".join(f"{k}={v}" for k, v in counts.items())
        print(f"photo-vision CouchDB backfill ({mode}): {parts}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
