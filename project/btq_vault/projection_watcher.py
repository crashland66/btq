from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib import error

from btq_vault.projector import ProjectorError, build_all_pages
from event_pipeline import couchdb_config
from event_pipeline.couchdb.push_design_doc import push_design_doc
from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError


DEFAULT_DEBOUNCE_SECONDS = 3.0
DEFAULT_OUTPUT_DIR_ENV = "BTQ_VAULT_PROJECTION_DIR"


def watch_and_project(
    output_dir: Path,
    base_url: str,
    auth_headers: dict,
    database: str,
    *,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    timeout: float = 10.0,
    since: str = "now",
) -> None:
    """
    Subscribe to the btq_vault _changes feed (uses CouchDBChangesListener).
    On each batch of changes, debounce by debounce_seconds, then call
    build_all_pages() and write the results to output_dir.
    Runs until interrupted (KeyboardInterrupt).
    """
    output_dir = output_dir.expanduser().resolve(strict=False)
    changes: queue.Queue[dict[str, Any]] = queue.Queue()
    stop_event = threading.Event()
    reader_error: queue.Queue[BaseException] = queue.Queue(maxsize=1)
    listener = CouchDBChangesListener(database=database, state_field="_btq_projection_unused", base_url=base_url, timeout=timeout)
    listener._selector_available = False
    listener._last_seq = since
    listener._auth_header = dict(auth_headers)

    def read_changes() -> None:
        try:
            _read_changes(listener, changes, stop_event)
        except BaseException as exc:
            try:
                reader_error.put_nowait(exc)
            except queue.Full:
                pass

    thread = threading.Thread(target=read_changes, name="btq-vault-projection-changes", daemon=True)
    thread.start()

    pending = False
    last_change_at = 0.0
    try:
        while True:
            if not reader_error.empty():
                exc = reader_error.get_nowait()
                if isinstance(exc, CouchDBListenerError):
                    raise exc
                raise CouchDBListenerError("CouchDB changes feed failed") from exc
            try:
                changes.get(timeout=1.0)
                pending = True
                last_change_at = time.monotonic()
                while True:
                    changes.get_nowait()
                    last_change_at = time.monotonic()
            except queue.Empty:
                pass
            if pending and time.monotonic() - last_change_at >= debounce_seconds:
                pages = build_all_pages(base_url, auth_headers, database, timeout)
                _write_pages(output_dir, pages)
                pending = False
    except KeyboardInterrupt:
        stop_event.set()
        listener.stop()
        thread.join(timeout=2.0)


def main() -> int:
    """
    Entry point for `btq project-vault`.
    Reads BTQ_VAULT_PROJECTION_DIR (required) and BTQ_COUCHDB_* vars.
    Pushes the vault design doc (to ensure views exist), then calls
    watch_and_project().
    """
    output_dir_raw = os.environ.get(DEFAULT_OUTPUT_DIR_ENV, "").strip()
    if not output_dir_raw:
        print(f"error: {DEFAULT_OUTPUT_DIR_ENV} is required", file=sys.stderr)
        return 1
    try:
        config = couchdb_config.from_env()
        push_design_doc("vault")
        output_dir = Path(output_dir_raw)
        # Build once immediately so the projection is populated on startup,
        # then hand off to the changes-feed watcher for incremental updates.
        pages = build_all_pages(
            config.base_url,
            dict(config.auth_header()),
            couchdb_config.vault_database(),
            config.timeout,
        )
        _write_pages(output_dir, pages)
        print(
            f"btq project-vault: initial build complete ({len(pages)} pages) → {output_dir}",
            flush=True,
        )
        watch_and_project(
            output_dir,
            config.base_url,
            dict(config.auth_header()),
            couchdb_config.vault_database(),
            timeout=config.timeout,
        )
    except (couchdb_config.CouchDBConfigError, CouchDBListenerError, ProjectorError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _read_changes(
    listener: CouchDBChangesListener,
    changes: queue.Queue[dict[str, Any]],
    stop_event: threading.Event,
) -> None:
    reconnect_attempts = 0
    while not stop_event.is_set():
        try:
            with listener._open_changes_feed() as response:
                reconnect_attempts = 0
                while not stop_event.is_set():
                    line = response.readline()
                    if line == b"":
                        raise error.URLError("changes feed ended")
                    if not line.strip():
                        continue
                    change = listener._decode_change(line)
                    if "seq" in change:
                        listener._last_seq = change["seq"]
                    if change.get("id"):
                        changes.put(change)
        except error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise CouchDBListenerError(f"CouchDB changes feed failed with HTTP {exc.code}") from exc
            reconnect_attempts = listener._next_reconnect_attempt(reconnect_attempts, exc)
        except (error.URLError, TimeoutError, OSError) as exc:
            reconnect_attempts = listener._next_reconnect_attempt(reconnect_attempts, exc)


def _write_pages(output_dir: Path, pages: dict[str, str]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, html in pages.items():
        target = output_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_text(html, encoding="utf-8")
        temp.replace(target)


if __name__ == "__main__":
    raise SystemExit(main())
