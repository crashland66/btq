from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error as urlerror

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError
from event_pipeline.couchdb_worker import configure_worker_logger, couchdb_url_needs_remote_tunnel, run_change_worker
from vps.couchdb_ops import temporary_env
from vps.ssh import couchdb_tunnel


DEFAULT_DATABASE = "btq_queue"
DEFAULT_STATE_FIELD = "btq_state"
DEFAULT_REMOTE_HOST = "btq-vps"
MARK_PROCESSING_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
PROCESSING_STARTED_AT_FIELD = "btq_processing_started_at"
LAST_STALE_PROCESSING_STARTED_AT_FIELD = "btq_last_stale_processing_started_at"
STALE_RESET_AT_FIELD = "btq_stale_reset_at"
STALE_RESET_COUNT_FIELD = "btq_stale_reset_count"
DEFAULT_STALE_PROCESSING_TTL_SECONDS = 6 * 60 * 60
DEFAULT_STALE_PROCESSING_SWEEP_INTERVAL_SECONDS = 15 * 60
DEFAULT_STALE_PROCESSING_SWEEP_LIMIT = 100
RESET_APPLIED = "reset"
RESET_CONFLICT = "conflict"
RESET_IGNORED = "ignored"
COUCHDB_METADATA_KEYS = frozenset(
    {
        "_id",
        "_rev",
        "_deleted",
        "btq_state",
        "created_at",
        "created_by",
        PROCESSING_STARTED_AT_FIELD,
        LAST_STALE_PROCESSING_STARTED_AT_FIELD,
        STALE_RESET_AT_FIELD,
        STALE_RESET_COUNT_FIELD,
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat_utc(value: datetime) -> str:
    active = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return active.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class QueueCouchDBChangesListener(CouchDBChangesListener):
    def mark_processing(self, doc: dict[str, Any], state: str = "processing") -> dict[str, Any]:
        return self._update_state(doc, state, {PROCESSING_STARTED_AT_FIELD: _isoformat_utc(_utc_now())})

    def find_stale_processing_docs(self, *, cutoff: datetime, limit: int = DEFAULT_STALE_PROCESSING_SWEEP_LIMIT) -> list[dict[str, Any]]:
        payload = {
            "selector": {
                self.state_field: "processing",
                PROCESSING_STARTED_AT_FIELD: {"$lt": _isoformat_utc(cutoff)},
            },
            "limit": max(1, int(limit)),
        }
        result = self._request_json("POST", "_find", payload)
        docs = result.get("docs")
        if not isinstance(docs, list):
            return []
        return [doc for doc in docs if isinstance(doc, dict)]

    def reset_stale_processing_doc(self, doc: dict[str, Any], *, now: datetime) -> str:
        if doc.get(self.state_field) != "processing":
            return RESET_IGNORED
        if not doc.get("_id") or not doc.get("_rev"):
            raise CouchDBListenerError("Cannot reset CouchDB queue document without _id and _rev")
        previous_started_at = str(doc.get(PROCESSING_STARTED_AT_FIELD) or "")
        updated = dict(doc)
        updated[self.state_field] = "pending"
        updated.pop(PROCESSING_STARTED_AT_FIELD, None)
        updated[LAST_STALE_PROCESSING_STARTED_AT_FIELD] = previous_started_at
        updated[STALE_RESET_AT_FIELD] = _isoformat_utc(now)
        try:
            updated[STALE_RESET_COUNT_FIELD] = int(updated.get(STALE_RESET_COUNT_FIELD) or 0) + 1
        except (TypeError, ValueError):
            updated[STALE_RESET_COUNT_FIELD] = 1
        try:
            result = self._request_json("PUT", str(updated["_id"]), updated)
        except urlerror.HTTPError as exc:
            if exc.code == 409:
                return RESET_CONFLICT
            raise CouchDBListenerError(f"CouchDB stale queue reset failed with HTTP {exc.code}") from exc
        rev = result.get("rev")
        if rev:
            updated["_rev"] = rev
        return RESET_APPLIED


def default_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "queue_couchdb_watch.log"


def configure_logger(log_path: Path) -> logging.Logger:
    return configure_worker_logger(log_path, "queue.couchdb_watch")


def _safe_doc_id(doc_id: str, *, max_len: int = 64) -> str:
    """Render `doc_id` as a safe filename component.

    Two CouchDB docs with the same first `max_len` filesystem-safe
    characters used to materialize to the same filename (e.g. two
    site_note jobs under the same Account/Locations prefix). When that
    collision happened, the second `materialize_queue_job` call raised
    FileExistsError, the run_listener swallowed it as
    "queue job already materialized", and the doc was acked as processed
    without its content ever reaching the runtime queue. See
    Journal/2026-05-28.md (Stage 0 tail closed entry).

    Appending a short content hash of the full doc_id makes the
    filename collision-resistant regardless of the readable prefix
    length, while keeping the prefix readable for human ops.
    """
    import hashlib

    safe_id = "".join(char if (char.isascii() and char.isalnum()) or char in "_-" else "-" for char in doc_id)
    truncated = safe_id[:max_len] or "unknown"
    digest = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:8]
    return f"{truncated}-{digest}"


def materialize_queue_job(doc: dict[str, Any], runtime_root: Path, logger: logging.Logger, *, dry_run: bool = False) -> Path:
    job_type = str(doc.get("type") or "unknown_job")
    doc_id = str(doc.get("_id") or "")
    payload = {key: value for key, value in doc.items() if key not in COUCHDB_METADATA_KEYS}
    queue_dir = runtime_root / "queue"
    destination = queue_dir / f"{job_type}.{_safe_doc_id(doc_id)}.json"
    if destination.exists():
        raise FileExistsError(str(destination))
    if dry_run:
        logger.info("queue dry-run doc_id=%s path=%s", doc_id, destination)
        return destination
    queue_dir.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, destination)
    except Exception:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    return destination


def process_one(
    *,
    doc: dict[str, Any],
    runtime_root: Path,
    logger: logging.Logger,
    dry_run: bool = False,
) -> dict[str, Any]:
    doc_id = str(doc.get("_id") or "")
    try:
        path = materialize_queue_job(doc, runtime_root, logger, dry_run=dry_run)
    except FileExistsError as exc:
        logger.info("queue job already materialized doc_id=%s path=%s", doc_id, exc)
        return {"doc_id": doc_id, "ok": True, "path": str(exc), "error": ""}
    except OSError as exc:
        logger.exception("CouchDB queue materialization failed doc_id=%s", doc_id)
        return {"doc_id": doc_id, "ok": False, "error": str(exc)}
    return {"doc_id": doc_id, "ok": True, "path": str(path), "error": ""}


def sweep_stale_processing_docs(
    *,
    listener: Any,
    ttl_seconds: int,
    logger: logging.Logger,
    now: datetime | None = None,
    limit: int = DEFAULT_STALE_PROCESSING_SWEEP_LIMIT,
) -> int:
    if not hasattr(listener, "find_stale_processing_docs") or not hasattr(listener, "reset_stale_processing_doc"):
        logger.debug("queue stale processing sweep skipped: listener does not support stale reset")
        return 0
    active_now = now or _utc_now()
    cutoff = active_now - timedelta(seconds=ttl_seconds)
    try:
        docs = listener.find_stale_processing_docs(cutoff=cutoff, limit=limit)
    except (CouchDBListenerError, urlerror.HTTPError) as exc:
        logger.warning("queue stale processing sweep skipped error=%s", exc)
        return 0
    reset_count = 0
    state_field = str(getattr(listener, "state_field", DEFAULT_STATE_FIELD) or DEFAULT_STATE_FIELD)
    for doc in docs:
        doc_id = str(doc.get("_id") or "")
        previous_started_at = str(doc.get(PROCESSING_STARTED_AT_FIELD) or "")
        started_at = _parse_timestamp(previous_started_at)
        if doc.get(state_field) != "processing" or started_at is None or started_at >= cutoff:
            continue
        age_seconds = max(0, int((active_now - started_at).total_seconds()))
        try:
            outcome = listener.reset_stale_processing_doc(doc, now=active_now)
        except CouchDBListenerError as exc:
            logger.warning(
                "queue stale processing reset skipped doc_id=%s previous_claim_timestamp=%s age_seconds=%s error=%s",
                doc_id,
                previous_started_at,
                age_seconds,
                exc,
            )
            continue
        if outcome == RESET_APPLIED:
            reset_count += 1
            logger.info(
                "queue stale processing reset metric=queue_stale_processing_reset doc_id=%s previous_claim_timestamp=%s age_seconds=%s",
                doc_id,
                previous_started_at,
                age_seconds,
            )
        elif outcome == RESET_CONFLICT:
            logger.warning(
                "queue stale processing reset conflict doc_id=%s previous_claim_timestamp=%s age_seconds=%s",
                doc_id,
                previous_started_at,
                age_seconds,
            )
    return reset_count


def _build_stale_processing_sweep_task(
    *,
    database: str,
    state_field: str,
    ttl_seconds: int,
    limit: int = DEFAULT_STALE_PROCESSING_SWEEP_LIMIT,
) -> Any:
    def sweep_task(active_logger: logging.Logger) -> None:
        listener = QueueCouchDBChangesListener(database=database, state_field=state_field)
        sweep_stale_processing_docs(listener=listener, ttl_seconds=ttl_seconds, logger=active_logger, limit=limit)

    return sweep_task


def run_listener(
    *,
    listener: CouchDBChangesListener,
    runtime_root: Path,
    dry_run: bool,
    json_output: bool,
    logger: logging.Logger,
    stale_processing_ttl_seconds: int = DEFAULT_STALE_PROCESSING_TTL_SECONDS,
) -> None:
    def process_doc(doc: dict[str, Any], active_logger: logging.Logger) -> dict[str, Any]:
        return process_one(doc=doc, runtime_root=runtime_root, dry_run=dry_run, logger=active_logger)

    run_change_worker(
        database=DEFAULT_DATABASE,
        state_field=DEFAULT_STATE_FIELD,
        runtime_root=runtime_root,
        stage="queue",
        log_path=None,
        default_log_path=default_log_path,
        logger_name="queue.couchdb_watch",
        worker_label="CouchDB queue",
        missing_url_message="BTQ_COUCHDB_URL must be set for CouchDB queue watcher",
        config_failed_message="CouchDB queue watcher configuration failed: %s",
        listener_error_message="CouchDB queue watcher stopped with listener error: %s",
        tunnel_failed_message="CouchDB queue watcher tunnel failed: %s",
        process_doc=process_doc,
        id_for_doc=lambda doc: str(doc.get("_id") or "").strip(),
        id_label="doc_id",
        default_failure_reason="CouchDB queue materialization failed",
        exhausted_result=lambda doc_id, error: {
            "doc_id": doc_id,
            "ok": False,
            "error": f"mark_processing exhausted: {error}",
        },
        log_processed=lambda active_logger, result, fallback_id: active_logger.info(
            "CouchDB queue processed doc_id=%s ok=%s path=%s",
            result.get("doc_id", fallback_id),
            result.get("ok"),
            result.get("path", ""),
        ),
        dry_run=dry_run,
        dry_run_updates_state=False,
        json_output=json_output,
        mark_processing_backoff_seconds=MARK_PROCESSING_BACKOFF_SECONDS,
        listener=listener,
        logger=logger,
        startup_task=None
        if dry_run
        else lambda active_logger: sweep_stale_processing_docs(
            listener=listener,
            ttl_seconds=stale_processing_ttl_seconds,
            logger=active_logger,
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CouchDB queue changes and materialize claimed docs into the local runtime queue.")
    parser.add_argument("--runtime-root", type=Path, default=get_config().runtime_root)
    parser.add_argument("--remote-host", default=os.environ.get("BTQ_COUCHDB_REMOTE_HOST", DEFAULT_REMOTE_HOST))
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--state-field", default=DEFAULT_STATE_FIELD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-path", type=Path)
    parser.add_argument(
        "--stale-processing-ttl-seconds",
        type=int,
        default=_env_int("BTQ_QUEUE_STALE_PROCESSING_TTL_SECONDS", DEFAULT_STALE_PROCESSING_TTL_SECONDS),
    )
    parser.add_argument(
        "--stale-processing-sweep-interval-seconds",
        type=int,
        default=_env_int("BTQ_QUEUE_STALE_PROCESSING_SWEEP_INTERVAL_SECONDS", DEFAULT_STALE_PROCESSING_SWEEP_INTERVAL_SECONDS),
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve(strict=False)

    def process_doc(doc: dict[str, Any], active_logger: logging.Logger) -> dict[str, Any]:
        return process_one(doc=doc, runtime_root=runtime_root, dry_run=args.dry_run, logger=active_logger)

    stale_sweep_task = None
    if not args.dry_run:
        stale_sweep_task = _build_stale_processing_sweep_task(
            database=args.database,
            state_field=args.state_field,
            ttl_seconds=args.stale_processing_ttl_seconds,
        )

    return run_change_worker(
        database=args.database,
        state_field=args.state_field,
        runtime_root=runtime_root,
        stage="queue",
        log_path=args.log_path,
        default_log_path=default_log_path,
        logger_name="queue.couchdb_watch",
        worker_label="CouchDB queue",
        missing_url_message="BTQ_COUCHDB_URL must be set for CouchDB queue watcher",
        config_failed_message="CouchDB queue watcher configuration failed: %s",
        listener_error_message="CouchDB queue watcher stopped with listener error: %s",
        tunnel_failed_message="CouchDB queue watcher tunnel failed: %s",
        process_doc=process_doc,
        id_for_doc=lambda doc: str(doc.get("_id") or "").strip(),
        id_label="doc_id",
        default_failure_reason="CouchDB queue materialization failed",
        exhausted_result=lambda doc_id, error: {
            "doc_id": doc_id,
            "ok": False,
            "error": f"mark_processing exhausted: {error}",
        },
        log_processed=lambda active_logger, result, fallback_id: active_logger.info(
            "CouchDB queue processed doc_id=%s ok=%s path=%s",
            result.get("doc_id", fallback_id),
            result.get("ok"),
            result.get("path", ""),
        ),
        remote_host=args.remote_host,
        dry_run=args.dry_run,
        dry_run_updates_state=False,
        once=args.once,
        json_output=args.json,
        state_processing="processing",
        state_done="complete",
        state_failed="failed",
        mark_processing_backoff_seconds=MARK_PROCESSING_BACKOFF_SECONDS,
        configure_logger=configure_logger,
        listener_class=QueueCouchDBChangesListener,
        tunnel_needed=couchdb_url_needs_remote_tunnel,
        tunnel_factory=couchdb_tunnel,
        temporary_env_func=temporary_env,
        config_from_env=couchdb_config.from_env,
        on_tunnel_url=lambda active_logger, tunnel_url: active_logger.info(
            "Using tunneled CouchDB URL for queue watcher: %s",
            tunnel_url,
        ),
        startup_task=stale_sweep_task,
        periodic_task=stale_sweep_task,
        periodic_interval_seconds=args.stale_processing_sweep_interval_seconds,
    )


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
