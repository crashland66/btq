from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import (
    CouchDBCandidateWriterError,
    get_action_candidate_document,
    upsert_action_candidate,
)
from field_capture.action_candidates import default_candidate_dir, iter_candidate_artifacts
from processing_core.results import result_counts


LOGGER = logging.getLogger(__name__)


def backfill_action_candidates(
    candidate_dir: Path,
    *,
    cdb_config: couchdb_config.CouchDBConfig | None,
    database: str,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Scan filesystem candidates and upsert each one into btq_field_captures."""
    counts = result_counts("found", "written", "updated", "skipped", "errors", "dry_run")
    candidate_root = candidate_dir.expanduser().resolve(strict=False)
    if not candidate_root.exists():
        LOGGER.warning("candidate dir does not exist: %s", candidate_root)
        return counts

    processed = 0
    for path, payload in iter_candidate_artifacts(candidate_root):
        if limit is not None and processed >= limit:
            counts["skipped"] += 1
            continue
        counts["found"] += 1
        processed += 1

        candidate_id = str(payload.get("candidate_id") or "").strip()
        if not candidate_id:
            LOGGER.warning("skipping candidate artifact without candidate_id: %s", path)
            counts["errors"] += 1
            continue

        try:
            existing = None
            if not dry_run:
                if cdb_config is None:
                    raise CouchDBCandidateWriterError("CouchDB config is required for live backfill")
                existing = get_action_candidate_document(cdb_config, database, candidate_id)
                upsert_action_candidate(cdb_config, database, payload)
            if dry_run:
                counts["dry_run"] += 1
            elif existing is None:
                counts["written"] += 1
            else:
                counts["updated"] += 1
        except (couchdb_config.CouchDBConfigError, CouchDBCandidateWriterError) as exc:
            LOGGER.warning("action candidate backfill failed for %s (%s): %s", candidate_id, path, exc)
            counts["errors"] += 1

    return counts


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(
        description="Backfill filesystem action candidates into CouchDB btq_field_captures.",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=default_candidate_dir(config.runtime_root),
        help="Directory containing field-capture action candidate JSON files.",
    )
    parser.add_argument(
        "--database",
        default=couchdb_config.field_captures_database(),
        help="CouchDB database for action candidate documents.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Scan candidates but do not write to CouchDB.")
    parser.add_argument("--limit", type=int, help="Maximum number of candidates to process.")
    parser.add_argument("--json", action="store_true", help="Print result counts as JSON.")
    return parser


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    cdb_config = None
    if not args.dry_run:
        url = (os.environ.get("BTQ_COUCHDB_URL") or "").strip()
        if not url:
            print("error: BTQ_COUCHDB_URL is not set; use --dry-run or set the env var")
            return 1
        cdb_config = couchdb_config.from_env()

    counts = backfill_action_candidates(
        args.candidate_dir,
        cdb_config=cdb_config,
        database=args.database,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(counts, indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "live"
        parts = " ".join(f"{key}={value}" for key, value in counts.items())
        print(f"action-candidate CouchDB backfill ({mode}): {parts}")
    return 0 if counts["errors"] == 0 else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
