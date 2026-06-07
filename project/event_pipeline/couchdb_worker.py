from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from event_pipeline import couchdb_config
from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError
from queue_processor import metrics
from vps.couchdb_ops import temporary_env
from vps.ssh import VpsError, couchdb_tunnel


MARK_PROCESSING_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def configure_worker_logger(log_path: Path, logger_name: str) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def couchdb_url_needs_remote_tunnel(couchdb_url: str, *, timeout_seconds: float = 1.0) -> bool:
    parsed = urlparse(couchdb_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout_seconds):
            return False
    except OSError:
        return True


class _OnceListener:
    def __init__(self, listener: Any) -> None:
        self._listener = listener

    def listen(self) -> Any:
        for doc in self._listener.listen():
            yield doc
            return

    def mark_processing(self, doc: dict[str, Any], state: str = "processing") -> dict[str, Any]:
        return _call_mark_processing(self._listener, doc, state)

    def mark_complete(self, doc: dict[str, Any], state: str = "complete") -> None:
        _call_mark_complete(self._listener, doc, state)

    def mark_failed(self, doc: dict[str, Any], reason: str, state: str = "failed") -> None:
        _call_mark_failed(self._listener, doc, reason, state)

    def stop(self) -> None:
        self._listener.stop()


def _call_mark_processing(listener: Any, doc: dict[str, Any], state: str) -> dict[str, Any]:
    try:
        return listener.mark_processing(doc, state)
    except TypeError as exc:
        try:
            return listener.mark_processing(doc)
        except TypeError:
            raise exc


def _call_mark_complete(listener: Any, doc: dict[str, Any], state: str) -> None:
    try:
        listener.mark_complete(doc, state)
    except TypeError as exc:
        try:
            listener.mark_complete(doc)
        except TypeError:
            raise exc


def _call_mark_failed(listener: Any, doc: dict[str, Any], reason: str, state: str) -> None:
    try:
        listener.mark_failed(doc, reason, state)
    except TypeError as exc:
        try:
            listener.mark_failed(doc, reason=reason)
        except TypeError:
            raise exc


def _run_change_loop(
    *,
    listener: Any,
    runtime_root: Path,
    stage: str,
    process_doc: Callable[[dict[str, Any], logging.Logger], dict[str, Any]],
    id_for_doc: Callable[[dict[str, Any]], str],
    id_label: str,
    worker_label: str,
    state_processing: str,
    state_done: str,
    state_failed: str,
    dry_run: bool,
    dry_run_updates_state: bool,
    json_output: bool,
    logger: logging.Logger,
    default_failure_reason: str,
    exhausted_result: Callable[[str, str], dict[str, Any]],
    log_processed: Callable[[logging.Logger, dict[str, Any], str], None],
    mark_processing_backoff_seconds: Sequence[float],
) -> None:
    for doc in listener.listen():
        doc_identifier = id_for_doc(doc)
        processing_doc: dict[str, Any] | None = None
        final_mark_processing_error = ""
        should_update_state = (not dry_run) or dry_run_updates_state
        latency_started = time.monotonic()
        if should_update_state:
            for attempt, delay_seconds in enumerate(mark_processing_backoff_seconds, start=1):
                try:
                    processing_doc = _call_mark_processing(listener, doc, state_processing)
                    break
                except CouchDBListenerError as exc:
                    final_mark_processing_error = str(exc)
                    logger.warning(
                        "%s mark_processing failed %s=%s attempt=%s error=%s",
                        worker_label,
                        id_label,
                        doc_identifier,
                        attempt,
                        exc,
                    )
                    if attempt < len(mark_processing_backoff_seconds):
                        time.sleep(delay_seconds)
        else:
            processing_doc = doc

        if processing_doc is None:
            result = exhausted_result(doc_identifier, final_mark_processing_error)
            logger.error(
                "%s mark_processing exhausted %s=%s rev=%s error=%s",
                worker_label,
                id_label,
                doc_identifier,
                doc.get("_rev"),
                final_mark_processing_error,
            )
            if json_output:
                print(json.dumps(result, sort_keys=True), flush=True)
            continue

        result = process_doc(processing_doc, logger)
        if should_update_state:
            try:
                if result["ok"]:
                    _call_mark_complete(listener, processing_doc, state_done)
                    metrics.record_latency(runtime_root, stage, (time.monotonic() - latency_started) * 1000)
                else:
                    reason = str(result.get("error") or default_failure_reason)
                    result["error"] = reason
                    _call_mark_failed(listener, processing_doc, reason, state_failed)
            except CouchDBListenerError:
                logger.exception(
                    "%s state update failed %s=%s ok=%s",
                    worker_label,
                    id_label,
                    result.get(id_label, doc_identifier),
                    result.get("ok"),
                )
                raise

        log_processed(logger, result, doc_identifier)
        if json_output:
            print(json.dumps(result, sort_keys=True), flush=True)


def _start_periodic_task(
    *,
    task: Callable[[logging.Logger], None] | None,
    interval_seconds: float | None,
    logger: logging.Logger,
) -> tuple[threading.Event, threading.Thread] | None:
    if task is None or interval_seconds is None or interval_seconds <= 0:
        return None

    stop_event = threading.Event()

    def run_periodically() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                task(logger)
            except Exception:  # noqa: BLE001 - periodic observability/recovery must not stop the watcher.
                logger.exception("CouchDB worker periodic task failed")

    thread = threading.Thread(target=run_periodically, name="couchdb-worker-periodic-task", daemon=True)
    thread.start()
    return stop_event, thread


def run_change_worker(
    *,
    database: str,
    state_field: str,
    runtime_root: Path,
    stage: str,
    log_path: Path | None,
    default_log_path: Callable[[Path], Path],
    logger_name: str,
    worker_label: str,
    missing_url_message: str,
    config_failed_message: str,
    listener_error_message: str,
    tunnel_failed_message: str,
    process_doc: Callable[[dict[str, Any], logging.Logger], dict[str, Any]],
    id_for_doc: Callable[[dict[str, Any]], str],
    id_label: str,
    default_failure_reason: str,
    exhausted_result: Callable[[str, str], dict[str, Any]],
    log_processed: Callable[[logging.Logger, dict[str, Any], str], None],
    remote_host: str = "btq-vps",
    dry_run: bool = False,
    dry_run_updates_state: bool = True,
    once: bool = False,
    json_output: bool = False,
    state_processing: str = "processing",
    state_done: str = "complete",
    state_failed: str = "failed",
    mark_processing_backoff_seconds: Sequence[float] | None = None,
    listener: Any | None = None,
    logger: logging.Logger | None = None,
    configure_logger: Callable[[Path], logging.Logger] | None = None,
    listener_class: Callable[..., Any] = CouchDBChangesListener,
    setup_context: Callable[[], None] | None = None,
    couchdb_url_env: str = "BTQ_COUCHDB_URL",
    tunnel_needed: Callable[[str], bool] = couchdb_url_needs_remote_tunnel,
    tunnel_factory: Callable[[str], Any] = couchdb_tunnel,
    temporary_env_func: Callable[[str, str], Any] = temporary_env,
    config_from_env: Callable[[], Any] = couchdb_config.from_env,
    on_tunnel_url: Callable[[logging.Logger, str], None] | None = None,
    startup_task: Callable[[logging.Logger], None] | None = None,
    periodic_task: Callable[[logging.Logger], None] | None = None,
    periodic_interval_seconds: float | None = None,
) -> int:
    runtime_root = runtime_root.expanduser().resolve(strict=False)
    resolved_log_path = (log_path or default_log_path(runtime_root)).expanduser().resolve(strict=False)
    active_logger = logger or (configure_logger(resolved_log_path) if configure_logger else configure_worker_logger(resolved_log_path, logger_name))
    active_backoff = MARK_PROCESSING_BACKOFF_SECONDS if mark_processing_backoff_seconds is None else mark_processing_backoff_seconds

    if listener is not None:
        active_listener = _OnceListener(listener) if once else listener
        periodic_handle: tuple[threading.Event, threading.Thread] | None = None
        try:
            if startup_task is not None:
                try:
                    startup_task(active_logger)
                except Exception:  # noqa: BLE001 - startup observability/recovery must not stop the watcher.
                    active_logger.exception("CouchDB worker startup task failed")
            periodic_handle = _start_periodic_task(
                task=periodic_task,
                interval_seconds=periodic_interval_seconds,
                logger=active_logger,
            )
            _run_change_loop(
                listener=active_listener,
                runtime_root=runtime_root,
                stage=stage,
                process_doc=process_doc,
                id_for_doc=id_for_doc,
                id_label=id_label,
                worker_label=worker_label,
                state_processing=state_processing,
                state_done=state_done,
                state_failed=state_failed,
                dry_run=dry_run,
                dry_run_updates_state=dry_run_updates_state,
                json_output=json_output,
                logger=active_logger,
                default_failure_reason=default_failure_reason,
                exhausted_result=exhausted_result,
                log_processed=log_processed,
                mark_processing_backoff_seconds=active_backoff,
            )
        finally:
            if periodic_handle is not None:
                periodic_handle[0].set()
                periodic_handle[1].join(timeout=1.0)
            if once:
                listener.stop()
        return 0

    couchdb_url = os.environ.get(couchdb_url_env)
    if not couchdb_url:
        active_logger.error(missing_url_message)
        return 1

    tunnel_context: Any = contextlib.nullcontext(None)
    if tunnel_needed(couchdb_url):
        active_logger.warning(
            "Configured CouchDB URL is unavailable; opening SSH tunnel remote_host=%s original_url=%s",
            remote_host,
            couchdb_url,
        )
        tunnel_context = tunnel_factory(remote_host)

    try:
        with tunnel_context as tunnel_url:
            env_context: Any = contextlib.nullcontext()
            if tunnel_url:
                if on_tunnel_url:
                    on_tunnel_url(active_logger, tunnel_url)
                env_context = temporary_env_func(couchdb_url_env, tunnel_url)
            with env_context:
                try:
                    config_from_env().assert_can_authenticate()
                except couchdb_config.CouchDBConfigError as exc:
                    active_logger.error("CouchDB startup auth check failed: %s", exc)
                    raise SystemExit(2)
                try:
                    active_listener = listener_class(database=database, state_field=state_field)
                    if setup_context is not None:
                        setup_context()
                except CouchDBListenerError as exc:
                    active_logger.error(config_failed_message, exc)
                    return 1

                def stop_listener(_signum: int, _frame: Any) -> None:
                    active_listener.stop()

                signal.signal(signal.SIGTERM, stop_listener)
                signal.signal(signal.SIGINT, stop_listener)

                loop_listener: Any = _OnceListener(active_listener) if once else active_listener
                periodic_handle: tuple[threading.Event, threading.Thread] | None = None
                try:
                    if startup_task is not None:
                        try:
                            startup_task(active_logger)
                        except Exception:  # noqa: BLE001 - startup observability/recovery must not stop the watcher.
                            active_logger.exception("CouchDB worker startup task failed")
                    periodic_handle = _start_periodic_task(
                        task=periodic_task,
                        interval_seconds=periodic_interval_seconds,
                        logger=active_logger,
                    )
                    _run_change_loop(
                        listener=loop_listener,
                        runtime_root=runtime_root,
                        stage=stage,
                        process_doc=process_doc,
                        id_for_doc=id_for_doc,
                        id_label=id_label,
                        worker_label=worker_label,
                        state_processing=state_processing,
                        state_done=state_done,
                        state_failed=state_failed,
                        dry_run=dry_run,
                        dry_run_updates_state=dry_run_updates_state,
                        json_output=json_output,
                        logger=active_logger,
                        default_failure_reason=default_failure_reason,
                        exhausted_result=exhausted_result,
                        log_processed=log_processed,
                        mark_processing_backoff_seconds=active_backoff,
                    )
                finally:
                    if periodic_handle is not None:
                        periodic_handle[0].set()
                        periodic_handle[1].join(timeout=1.0)
                    if once:
                        active_listener.stop()
    except VpsError as exc:
        active_logger.error(tunnel_failed_message, exc)
        return 1
    except (StopIteration, CouchDBListenerError) as exc:
        if once and isinstance(exc, StopIteration):
            return 0
        active_logger.error(listener_error_message, exc)
        return 1
    return 0
