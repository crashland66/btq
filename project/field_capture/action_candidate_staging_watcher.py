from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import (
    AlreadyDecided,
    CouchDBCandidateWriterError,
    get_action_candidate,
    set_action_candidate_staged_at,
)
from event_pipeline.couchdb_worker import configure_worker_logger
from field_capture import action_candidates
from field_capture.candidate_staging import stage_candidate_job_after_approval
from processing_core.action_candidates import action_candidate_review_path
from processing_core.artifacts import read_json_object, write_json_object


DEFAULT_DATABASE = couchdb_config.DEFAULT_FIELD_CAPTURES_DB
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_LIMIT = 100


def default_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "action_candidate_staging_watch.log"


def configure_logger(log_path: Path) -> logging.Logger:
    return configure_worker_logger(log_path, "field_capture.action_candidate_staging_watch")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_approved_unstaged_candidates(
    config: couchdb_config.CouchDBConfig,
    db: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    selector = {
        "type": "action_candidate",
        "status": "approved",
        "$or": [
            {"staged_at": {"$exists": False}},
            {"staged_at": ""},
            {"staged_at": None},
        ],
    }
    payload = {"selector": selector, "limit": max(1, int(limit))}
    result = _request_json(config, db, "POST", "_find", payload)
    docs = result.get("docs")
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def materialize_candidate_for_existing_staging(doc: dict[str, Any], runtime_root: Path) -> Path:
    candidate_id = str(doc.get("candidate_id") or "").strip()
    if not candidate_id:
        raise CouchDBCandidateWriterError("approved candidate is missing candidate_id")
    candidate_dir = action_candidates.default_candidate_dir(runtime_root)
    candidate_path = action_candidate_review_path(candidate_dir, candidate_id)
    existing = read_json_object(candidate_path)
    from_couch = action_candidates.couchdb_action_candidate_to_review_payload(doc)
    if isinstance(existing, dict):
        payload = {
            **existing,
            "status": "approved",
            "reviewer": str(doc.get("reviewer") or doc.get("reviewed_by") or existing.get("reviewer") or ""),
            "reviewed_by": str(doc.get("reviewed_by") or doc.get("reviewer") or existing.get("reviewed_by") or ""),
            "reviewed_at": str(doc.get("reviewed_at") or existing.get("reviewed_at") or ""),
            "review_rationale": str(doc.get("review_rationale") or existing.get("review_rationale") or ""),
            "prior_status": str(doc.get("prior_status") or existing.get("prior_status") or "pending_review"),
        }
        if "review_history" in doc:
            payload["review_history"] = doc["review_history"]
    else:
        payload = from_couch
    candidate_dir.mkdir(parents=True, exist_ok=True)
    write_json_object(candidate_path, payload)
    return candidate_path


def process_one(
    *,
    config: couchdb_config.CouchDBConfig,
    db: str,
    doc: dict[str, Any],
    runtime_root: Path,
    logger: logging.Logger,
    dry_run: bool = False,
) -> dict[str, Any]:
    candidate_id = str(doc.get("candidate_id") or "").strip()
    doc_rev = str(doc.get("_rev") or "")
    if not candidate_id:
        return {"candidate_id": "", "ok": False, "staged": False, "error": "candidate_id is required"}
    if doc.get("staged_at"):
        return {"candidate_id": candidate_id, "ok": True, "staged": False, "error": "", "skipped": "already staged"}
    try:
        if dry_run:
            return {"candidate_id": candidate_id, "ok": True, "staged": False, "error": "", "dry_run": True}
        candidate_path = materialize_candidate_for_existing_staging(doc, runtime_root)
        stage_summary = stage_candidate_job_after_approval(runtime_root, candidate_id)
        staged_at = utc_now_iso()
        try:
            updated = set_action_candidate_staged_at(config, db, candidate_id, staged_at=staged_at, expected_rev=doc_rev or None)
        except AlreadyDecided:
            latest = get_action_candidate(config, db, candidate_id)
            if latest and latest.get("staged_at"):
                return {
                    "candidate_id": candidate_id,
                    "ok": True,
                    "staged": False,
                    "error": "",
                    "skipped": "already staged after conflict",
                }
            raise
    except Exception as exc:  # noqa: BLE001 - fail soft per candidate.
        logger.exception("action candidate staging failed candidate_id=%s", candidate_id)
        return {"candidate_id": candidate_id, "ok": False, "staged": False, "error": str(exc)}
    return {
        "candidate_id": candidate_id,
        "ok": True,
        "staged": True,
        "error": "",
        "candidate_path": str(candidate_path),
        "stage_summary": stage_summary,
        "new_rev": str(updated.get("_rev") or ""),
        "staged_at": staged_at,
    }


def process_pass(
    *,
    config: couchdb_config.CouchDBConfig,
    db: str,
    runtime_root: Path,
    logger: logging.Logger,
    dry_run: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    try:
        docs = find_approved_unstaged_candidates(config, db, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("approved action candidate query failed")
        return [{"candidate_id": "", "ok": False, "staged": False, "error": str(exc)}]
    results: list[dict[str, Any]] = []
    for doc in docs:
        result = process_one(config=config, db=db, doc=doc, runtime_root=runtime_root, logger=logger, dry_run=dry_run)
        results.append(result)
        logger.info(
            "action candidate staging candidate_id=%s ok=%s staged=%s error=%s",
            result.get("candidate_id", ""),
            result.get("ok"),
            result.get("staged"),
            result.get("error", ""),
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CouchDB approved action candidates and stage queue jobs locally.")
    parser.add_argument("--runtime-root", type=Path, default=get_config().runtime_root)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-path", type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve(strict=False)
    log_path = args.log_path.expanduser() if args.log_path else default_log_path(runtime_root)
    logger = configure_logger(log_path)
    try:
        config = couchdb_config.from_env()
    except couchdb_config.CouchDBConfigError as exc:
        logger.error("action candidate staging watcher configuration failed: %s", exc)
        return 2

    while True:
        results = process_pass(
            config=config,
            db=args.database,
            runtime_root=runtime_root,
            logger=logger,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        if args.json:
            for result in results:
                print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 0 if all(result.get("ok") for result in results) else 1
        time.sleep(max(0.1, float(args.poll_seconds)))


def _request_json(
    config: couchdb_config.CouchDBConfig,
    db: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db_quoted = parse.quote(db, safe="")
    quoted_path = "/".join(parse.quote(part, safe="") for part in path.split("/"))
    url = f"{config.base_url}/{db_quoted}/{quoted_path}"
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(config.auth_header())
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate query failed with HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate query failed: {exc}") from exc
    if not 200 <= status < 300:
        raise CouchDBCandidateWriterError(f"CouchDB action candidate query failed with HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBCandidateWriterError("CouchDB action candidate query returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise CouchDBCandidateWriterError("CouchDB action candidate query returned non-object JSON")
    return parsed


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
