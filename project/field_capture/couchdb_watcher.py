from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

from config import get_config
from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_adapter import CaptureAdapterError, import_couchdb_capture, run_command
from event_pipeline.couchdb_listener import CouchDBChangesListener
from event_pipeline.couchdb_worker import configure_worker_logger, couchdb_url_needs_remote_tunnel, run_change_worker
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from vps.couchdb_ops import temporary_env
from vps.ssh import couchdb_tunnel


DEFAULT_DATABASE = "btq_field_captures"
DEFAULT_STATE_FIELD = "processing_state"
DEFAULT_REMOTE_HOST = "btq-vps"
MARK_PROCESSING_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


def default_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "field_capture_couchdb_watch.log"


def configure_logger(log_path: Path) -> logging.Logger:
    return configure_worker_logger(log_path, "field_capture.couchdb_watch")


def process_one(
    *,
    doc: dict[str, Any],
    runtime_root: Path,
    remote_host: str,
    registry: CouchDBSiteRegistry,
    runner: Any,
    dry_run: bool,
    logger: logging.Logger,
) -> dict[str, Any]:
    capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
    try:
        result = import_couchdb_capture(
            doc=doc,
            runtime_root=runtime_root,
            remote_host=remote_host,
            registry=registry,
            runner=runner,
            dry_run=dry_run,
        )
    except CaptureAdapterError as exc:
        logger.exception("CouchDB field-capture import failed capture_id=%s", capture_id)
        return {"capture_id": capture_id, "ok": False, "counts": {}, "error": str(exc)}
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    return {
        "capture_id": str(result.get("capture_id") or capture_id),
        "ok": bool(result.get("ok", True)),
        "counts": dict(counts),
        "error": str(result.get("error") or ""),
    }


def run_listener(
    *,
    listener: CouchDBChangesListener,
    runtime_root: Path,
    remote_host: str,
    registry: CouchDBSiteRegistry,
    runner: Any,
    dry_run: bool,
    json_output: bool,
    logger: logging.Logger,
) -> None:
    def process_doc(doc: dict[str, Any], active_logger: logging.Logger) -> dict[str, Any]:
        return process_one(
            doc=doc,
            runtime_root=runtime_root,
            remote_host=remote_host,
            registry=registry,
            runner=runner,
            dry_run=dry_run,
            logger=active_logger,
        )

    run_change_worker(
        database=DEFAULT_DATABASE,
        state_field=DEFAULT_STATE_FIELD,
        runtime_root=runtime_root,
        stage="field_capture",
        log_path=None,
        default_log_path=default_log_path,
        logger_name="field_capture.couchdb_watch",
        worker_label="CouchDB field-capture",
        missing_url_message="BTQ_COUCHDB_URL must be set for CouchDB field-capture watcher",
        config_failed_message="CouchDB field-capture watcher configuration failed: %s",
        listener_error_message="CouchDB field-capture watcher stopped with listener error: %s",
        tunnel_failed_message="CouchDB field-capture watcher tunnel failed: %s",
        process_doc=process_doc,
        id_for_doc=lambda doc: str(doc.get("capture_id") or doc.get("_id") or "").strip(),
        id_label="capture_id",
        default_failure_reason="CouchDB field-capture import failed",
        exhausted_result=lambda capture_id, error: {
            "capture_id": capture_id,
            "ok": False,
            "counts": {},
            "error": f"mark_processing exhausted: {error}",
        },
        log_processed=lambda active_logger, result, fallback_id: active_logger.info(
            "CouchDB field-capture processed capture_id=%s ok=%s counts=%s",
            result.get("capture_id", fallback_id),
            result.get("ok"),
            result.get("counts", {}),
        ),
        dry_run=dry_run,
        dry_run_updates_state=True,
        json_output=json_output,
        mark_processing_backoff_seconds=MARK_PROCESSING_BACKOFF_SECONDS,
        listener=listener,
        logger=logger,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Watch CouchDB field-capture changes and import claimed docs into local intake.")
    parser.add_argument("--runtime-root", type=Path, default=get_config().runtime_root)
    parser.add_argument("--remote-host", default=os.environ.get("BTQ_COUCHDB_REMOTE_HOST", DEFAULT_REMOTE_HOST))
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--state-field", default=DEFAULT_STATE_FIELD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-path", type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser().resolve(strict=False)
    registry_holder: dict[str, CouchDBSiteRegistry] = {}

    def registry() -> CouchDBSiteRegistry:
        if "registry" not in registry_holder:
            registry_holder["registry"] = CouchDBSiteRegistry()
        return registry_holder["registry"]

    def setup_registry() -> None:
        registry()

    def process_doc(doc: dict[str, Any], active_logger: logging.Logger) -> dict[str, Any]:
        return process_one(
            doc=doc,
            runtime_root=runtime_root,
            remote_host=args.remote_host,
            registry=registry(),
            runner=run_command,
            dry_run=args.dry_run,
            logger=active_logger,
        )

    return run_change_worker(
        database=args.database,
        state_field=args.state_field,
        runtime_root=runtime_root,
        stage="field_capture",
        log_path=args.log_path,
        default_log_path=default_log_path,
        logger_name="field_capture.couchdb_watch",
        worker_label="CouchDB field-capture",
        missing_url_message="BTQ_COUCHDB_URL must be set for CouchDB field-capture watcher",
        config_failed_message="CouchDB field-capture watcher configuration failed: %s",
        listener_error_message="CouchDB field-capture watcher stopped with listener error: %s",
        tunnel_failed_message="CouchDB field-capture watcher tunnel failed: %s",
        process_doc=process_doc,
        id_for_doc=lambda doc: str(doc.get("capture_id") or doc.get("_id") or "").strip(),
        id_label="capture_id",
        default_failure_reason="CouchDB field-capture import failed",
        exhausted_result=lambda capture_id, error: {
            "capture_id": capture_id,
            "ok": False,
            "counts": {},
            "error": f"mark_processing exhausted: {error}",
        },
        log_processed=lambda active_logger, result, fallback_id: active_logger.info(
            "CouchDB field-capture processed capture_id=%s ok=%s counts=%s",
            result.get("capture_id", fallback_id),
            result.get("ok"),
            result.get("counts", {}),
        ),
        remote_host=args.remote_host,
        dry_run=args.dry_run,
        dry_run_updates_state=True,
        once=args.once,
        json_output=args.json,
        state_processing="processing",
        state_done="complete",
        state_failed="failed",
        mark_processing_backoff_seconds=MARK_PROCESSING_BACKOFF_SECONDS,
        configure_logger=configure_logger,
        listener_class=CouchDBChangesListener,
        setup_context=setup_registry,
        tunnel_needed=couchdb_url_needs_remote_tunnel,
        tunnel_factory=couchdb_tunnel,
        temporary_env_func=temporary_env,
        config_from_env=couchdb_config.from_env,
        on_tunnel_url=lambda active_logger, tunnel_url: active_logger.info(
            "Using tunneled CouchDB URL for field-capture watcher: %s",
            tunnel_url,
        ),
    )


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
