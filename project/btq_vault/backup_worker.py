from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.couchdb_listener import CouchDBChangesListener, CouchDBListenerError


DEFAULT_DEBOUNCE_SECONDS = 5.0
DEFAULT_BACKUP_DIR_ENV = "BTQ_VAULT_BACKUP_DIR"
DEFAULT_RETAIN_COUNT = 10


class BackupError(Exception):
    pass


def write_backup(
    output_dir: Path,
    base_url: str,
    auth_headers: dict,
    database: str,
    *,
    timeout: float = 10.0,
) -> Path:
    """
    Fetch all documents via _all_docs?include_docs=true and write a timestamped
    JSON dump to output_dir. Retains only the last DEFAULT_RETAIN_COUNT files.
    """
    db = parse.quote(database, safe="")
    url = f"{base_url.rstrip('/')}/{db}/_all_docs?include_docs=true"
    headers = {"Accept": "application/json"}
    headers.update(auth_headers)
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
        if not 200 <= status < 300:
            raise BackupError(f"CouchDB backup returned HTTP {status}")
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"btq_vault_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except BackupError:
        raise
    except error.HTTPError as exc:
        raise BackupError(f"CouchDB backup failed with HTTP {exc.code}") from exc
    except (error.URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("CouchDB backup failed") from exc

    try:
        backups = sorted(output_dir.glob("btq_vault_*.json"))
        for old_backup in backups[:-DEFAULT_RETAIN_COUNT]:
            old_backup.unlink()
    except OSError as exc:
        logging.getLogger(__name__).warning("btq_vault backup cleanup failed: %s", exc)
    return target


def check_freshness(
    output_dir: Path,
    *,
    max_age_hours: float = 24.0,
) -> dict:
    """
    Check whether the newest backup in output_dir is younger than max_age_hours.
    """
    backups = sorted(output_dir.glob("btq_vault_*.json"))
    if not backups:
        return {"fresh": False, "newest_backup_age_hours": None}
    newest = backups[-1]
    timestamp = _timestamp_from_backup_name(newest)
    if timestamp is None:
        timestamp = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
    age_hours = (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600.0
    return {"fresh": age_hours <= max_age_hours, "newest_backup_age_hours": age_hours}


def watch_and_backup(
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
    Subscribe to the btq_vault _changes feed and write a backup after each
    debounced burst of changes.
    """
    output_dir = output_dir.expanduser().resolve(strict=False)
    changes: queue.Queue[dict[str, Any]] = queue.Queue()
    stop_event = threading.Event()
    reader_error: queue.Queue[BaseException] = queue.Queue(maxsize=1)
    listener = CouchDBChangesListener(database=database, state_field="_btq_backup_unused", base_url=base_url, timeout=timeout)
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

    thread = threading.Thread(target=read_changes, name="btq-vault-backup-changes", daemon=True)
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
                write_backup(output_dir, base_url, auth_headers, database, timeout=timeout)
                pending = False
    except KeyboardInterrupt:
        stop_event.set()
        listener.stop()
        thread.join(timeout=2.0)


def main() -> int:
    """
    Entry point for `btq backup-vault`.
    """
    output_dir_raw = os.environ.get(DEFAULT_BACKUP_DIR_ENV, "").strip()
    if not output_dir_raw:
        print("error: BTQ_VAULT_BACKUP_DIR is required", file=sys.stderr)
        return 1
    try:
        config = couchdb_config.from_env()
        output_dir = Path(output_dir_raw)
        # Write an initial backup immediately on startup so iCloud always has
        # a current snapshot, then hand off to the changes-feed watcher.
        target = write_backup(
            output_dir,
            config.base_url,
            dict(config.auth_header()),
            couchdb_config.vault_database(),
            timeout=config.timeout,
        )
        print(f"btq backup-vault: initial backup → {target}", flush=True)
        watch_and_backup(
            output_dir,
            config.base_url,
            dict(config.auth_header()),
            couchdb_config.vault_database(),
            timeout=config.timeout,
        )
    except (couchdb_config.CouchDBConfigError, CouchDBListenerError, BackupError) as exc:
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


def _timestamp_from_backup_name(path: Path) -> datetime | None:
    stem = path.stem
    prefix = "btq_vault_"
    if not stem.startswith(prefix):
        return None
    try:
        return datetime.strptime(stem[len(prefix) :], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
