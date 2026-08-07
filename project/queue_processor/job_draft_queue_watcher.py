from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import AlreadyDecided, CouchDBCandidateWriterError, _put_document
from event_pipeline.couchdb_job_draft_writer import (
    CouchDBJobDraftWriterError,
    get_job_draft,
    set_job_draft_queue_no_action_at,
    set_job_draft_queue_materialized_at,
)
from field_capture.approval_routes import APPROVAL_ROUTE_NO_ACTION
from event_pipeline.couchdb_worker import configure_worker_logger
from queue_processor.couchdb_queue_watcher import _safe_doc_id
from queue_spec import validate_job


DEFAULT_DATABASE = couchdb_config.DEFAULT_FIELD_CAPTURES_DB
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_LIMIT = 100


def default_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "job_draft_queue_watch.log"


def configure_logger(log_path: Path) -> logging.Logger:
    return configure_worker_logger(log_path, "queue.job_draft_queue_watch")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_approved_unmaterialized_drafts(
    config: couchdb_config.CouchDBConfig,
    db: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    selector = {
        "type": "job_draft",
        "review_status": "approved",
        "queue_materialize_error": {"$exists": False},
        "$or": [
            {"queue_materialized_at": {"$exists": False}},
            {"queue_materialized_at": ""},
            {"queue_materialized_at": None},
        ],
    }
    payload = {"selector": selector, "limit": max(1, int(limit))}
    result = _request_json(config, db, "POST", "_find", payload)
    docs = result.get("docs")
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def build_queue_job_from_draft(doc: dict[str, Any]) -> dict[str, Any]:
    payload = doc.get("payload")
    return {
        "job_type": str(doc["job_type"]),
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "metadata": {
            "source": "job_draft",
            "draft_id": str(doc["draft_id"]),
            "source_capture_id": str(doc.get("source_capture_id", "")),
            "group_id": str(doc.get("group_id", "")),
        },
    }


def queue_doc_id_for_draft_job(job: dict[str, Any], draft_id: str) -> str:
    return f"{job['job_type']}.{_safe_doc_id(draft_id)}"


def materialize_draft_job(doc: dict[str, Any], runtime_root: Path, *, dry_run: bool = False) -> str:
    """Author the approved draft as a btq_queue CouchDB doc (unified transport).

    The doc id is deterministic per draft, so re-materializing the same
    draft is an idempotent 409 no-op on CouchDB. Returns the queue doc id.
    """
    draft_id = str(doc.get("draft_id") or "").strip()
    if not draft_id:
        raise CouchDBJobDraftWriterError("approved job_draft is missing draft_id")
    job = build_queue_job_from_draft(doc)
    if not validate_job(job):
        raise ValueError(f"invalid queue job for draft_id={draft_id}")
    queue_doc_id = queue_doc_id_for_draft_job(job, draft_id)
    if dry_run:
        return queue_doc_id
    from event_pipeline import btq_client

    enqueue_job = dict(job)
    enqueue_job["job_id"] = queue_doc_id
    btq_client.enqueue(enqueue_job, created_by="job_draft_queue_watcher")
    return queue_doc_id


def mark_job_draft_queue_materialize_error(
    config: couchdb_config.CouchDBConfig,
    db: str,
    draft_id: str,
    *,
    error_message: str,
    expected_rev: str | None = None,
) -> dict[str, Any]:
    doc = get_job_draft(config, db, draft_id)
    if doc is None:
        raise CouchDBJobDraftWriterError(f"job_draft not found: {draft_id}")
    current_rev = str(doc.get("_rev") or "")
    if not current_rev:
        raise CouchDBJobDraftWriterError(f"job_draft has no _rev: {draft_id}")
    if expected_rev is not None and str(expected_rev) != current_rev:
        raise AlreadyDecided(f"job_draft _rev changed before queue materialization error mark: {draft_id}")
    updated = dict(doc)
    updated["queue_materialize_error"] = str(error_message or "")
    updated["queue_materialize_error_at"] = utc_now_iso()
    try:
        return _put_document(config, db, str(updated["_id"]), updated, conflict_as_already_decided=True)
    except AlreadyDecided:
        raise
    except CouchDBCandidateWriterError as exc:
        raise CouchDBJobDraftWriterError(str(exc)) from exc


def process_one(
    *,
    config: couchdb_config.CouchDBConfig,
    db: str,
    doc: dict[str, Any],
    runtime_root: Path,
    logger: logging.Logger,
    dry_run: bool = False,
) -> dict[str, Any]:
    draft_id = str(doc.get("draft_id") or "").strip()
    doc_rev = str(doc.get("_rev") or "")
    if not draft_id:
        return {"draft_id": "", "ok": False, "materialized": False, "error": "draft_id is required"}
    if doc.get("queue_materialized_at"):
        return {"draft_id": draft_id, "ok": True, "materialized": False, "error": "", "skipped": "already materialized"}
    if doc.get("queue_materialize_error"):
        return {"draft_id": draft_id, "ok": True, "materialized": False, "error": "", "skipped": "materialization previously failed"}
    if doc.get("route") == APPROVAL_ROUTE_NO_ACTION:
        if dry_run:
            return {
                "draft_id": draft_id,
                "ok": True,
                "materialized": False,
                "error": "",
                "skipped": "no_action",
                "dry_run": True,
            }
        materialized_at = utc_now_iso()
        try:
            updated = set_job_draft_queue_no_action_at(
                config,
                db,
                draft_id,
                materialized_at=materialized_at,
                expected_rev=doc_rev or None,
            )
        except AlreadyDecided:
            latest = get_job_draft(config, db, draft_id)
            if latest and latest.get("queue_materialized_at"):
                return {
                    "draft_id": draft_id,
                    "ok": True,
                    "materialized": False,
                    "error": "",
                    "skipped": "already materialized after conflict",
                    "queue_materialized_as": str(latest.get("queue_materialized_as") or ""),
                }
            raise
        return {
            "draft_id": draft_id,
            "ok": True,
            "materialized": False,
            "error": "",
            "skipped": "no_action",
            "new_rev": str(updated.get("_rev") or ""),
            "queue_materialized_at": materialized_at,
            "queue_materialized_as": "no_action",
        }
    try:
        try:
            queue_doc_id = materialize_draft_job(doc, runtime_root, dry_run=dry_run)
        except ValueError as exc:
            logger.exception("job_draft queue validation failed draft_id=%s", draft_id)
            if not dry_run:
                try:
                    updated = mark_job_draft_queue_materialize_error(
                        config,
                        db,
                        draft_id,
                        error_message=str(exc),
                        expected_rev=doc_rev or None,
                    )
                except AlreadyDecided:
                    latest = get_job_draft(config, db, draft_id)
                    if latest and latest.get("queue_materialize_error"):
                        return {
                            "draft_id": draft_id,
                            "ok": True,
                            "materialized": False,
                            "error": "",
                            "skipped": "already errored after conflict",
                        }
                    raise
                return {
                    "draft_id": draft_id,
                    "ok": False,
                    "materialized": False,
                    "error": str(exc),
                    "queue_materialize_error": True,
                    "new_rev": str(updated.get("_rev") or ""),
                }
            return {"draft_id": draft_id, "ok": False, "materialized": False, "error": str(exc), "dry_run": True}

        if dry_run:
            return {
                "draft_id": draft_id,
                "ok": True,
                "materialized": False,
                "error": "",
                "dry_run": True,
                "queue_doc_id": queue_doc_id,
            }

        materialized_at = utc_now_iso()
        try:
            updated = set_job_draft_queue_materialized_at(
                config,
                db,
                draft_id,
                materialized_at=materialized_at,
                expected_rev=doc_rev or None,
            )
        except AlreadyDecided:
            latest = get_job_draft(config, db, draft_id)
            if latest and latest.get("queue_materialized_at"):
                return {
                    "draft_id": draft_id,
                    "ok": True,
                    "materialized": False,
                    "error": "",
                    "skipped": "already materialized after conflict",
                    "queue_doc_id": queue_doc_id,
                }
            raise
    except Exception as exc:  # noqa: BLE001 - fail soft per draft.
        logger.exception("job_draft queue materialization failed draft_id=%s", draft_id)
        return {"draft_id": draft_id, "ok": False, "materialized": False, "error": str(exc)}
    return {
        "draft_id": draft_id,
        "ok": True,
        "materialized": True,
        "error": "",
        "queue_doc_id": queue_doc_id,
        "new_rev": str(updated.get("_rev") or ""),
        "queue_materialized_at": materialized_at,
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
        docs = find_approved_unmaterialized_drafts(config, db, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.exception("approved job_draft query failed")
        return [{"draft_id": "", "ok": False, "materialized": False, "error": str(exc)}]
    results: list[dict[str, Any]] = []
    for doc in docs:
        result = process_one(config=config, db=db, doc=doc, runtime_root=runtime_root, logger=logger, dry_run=dry_run)
        results.append(result)
        logger.info(
            "job_draft queue materialization draft_id=%s ok=%s materialized=%s error=%s",
            result.get("draft_id", ""),
            result.get("ok"),
            result.get("materialized"),
            result.get("error", ""),
        )
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CouchDB approved job_drafts and materialize queue jobs locally.")
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
        logger.error("job_draft queue watcher configuration failed: %s", exc)
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
        raise CouchDBJobDraftWriterError(f"CouchDB job_draft query failed with HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise CouchDBJobDraftWriterError(f"CouchDB job_draft query failed: {exc}") from exc
    if not 200 <= status < 300:
        raise CouchDBJobDraftWriterError(f"CouchDB job_draft query failed with HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBJobDraftWriterError("CouchDB job_draft query returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise CouchDBJobDraftWriterError("CouchDB job_draft query returned non-object JSON")
    return parsed


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
