"""cowork_drop_watcher — drain Cowork's file-drop bridge into CouchDB.

Cowork runs in a Linux sandbox that cannot reach CouchDB (no tailnet route,
egress allowlist). The only channel that crosses the sandbox boundary is an
iCloud-synced directory. Cowork writes a queue job as JSON into

    <pipeline_dir>/cowork_drop/incoming/<id>.json

and this watcher — running on the Pro, where CouchDB is local — validates it
against queue_spec, enqueues it via the existing btq-enqueue (so the job-doc
shape is never forked), and writes an acknowledgement back into the drop dir
so Cowork (which cannot see CouchDB) can confirm the job landed.

Design guarantees (see ai-methodology .../cowork-couchdb-unreachable-file-drop-bridge.md):
  1. Ack back into the mount — processed/<id>.ack.json or failed/<id>.ack.json,
     carrying the CouchDB _id/rev/status. Without this Cowork's "Staged:" line
     is unverifiable.
  2. Idempotent enqueue — a deterministic _id (explicit job_id, else a content
     hash) so an iCloud re-sync is a CouchDB 409 no-op, not a duplicate.
  3. Server-side re-validation — defense in depth even though Cowork validates
     before dropping.
  4. Scoped as the Cowork bridge only — voice memos and Code still author
     straight to CouchDB.

Transient failures (CouchDB unreachable) leave the file in incoming/ for retry;
only validation / permanent enqueue failures move it to failed/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from event_pipeline import sites
from event_pipeline.couchdb_registry import CouchDBRegistryError
from config import get_config
from queue_spec import SITE_ROUTABILITY_JOB_TYPES, validate_job

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENQUEUE_SCRIPT = REPO_ROOT / "scripts" / "btq-enqueue"

DEFAULT_POLL_SECONDS = 5.0
# Skip files modified within this window so we don't read a half-synced file.
STABLE_SECONDS = 3.0
CREATED_BY = "cowork"

_should_stop = False


def _handle_signal(signum, _frame):
    global _should_stop
    _should_stop = True


def configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cowork.drop_watch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deterministic_job_id(job: dict) -> str:
    """Stable id from job content so re-syncs collide at CouchDB (409 no-op)."""
    canonical = json.dumps(
        {"job_type": job.get("job_type"), "payload": job.get("payload")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
    return f"cw-{digest}"


def _write_ack(dest_dir: Path, stem: str, ack: dict) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    ack_path = dest_dir / f"{stem}.ack.json"
    ack_path.write_text(json.dumps(ack, indent=2, ensure_ascii=False), encoding="utf-8")


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    # Overwrite any stale same-named original from a prior round.
    if dest.exists():
        dest.unlink()
    src.rename(dest)
    return dest


def _enqueue(job: dict, python_bin: str, dry_run: bool = False) -> tuple[bool, bool, dict, str]:
    """Run btq-enqueue on the job. Returns (ok, transient, result, message).

    transient=True means a retryable failure (CouchDB unreachable) — caller
    should leave the file in incoming/ and try again next poll.
    """
    payload = json.dumps(job, ensure_ascii=False).encode("utf-8")
    cmd = [python_bin, str(ENQUEUE_SCRIPT), "--created-by", CREATED_BY]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(
        cmd,
        input=payload,
        capture_output=True,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            return False, False, {"raw": stdout}, "btq-enqueue returned non-JSON on success"
        return True, False, result, "enqueued"
    transient = "cannot reach CouchDB" in stderr
    return False, transient, {}, stderr or stdout or f"btq-enqueue exited {proc.returncode}"


def _site_routability_target(job: dict) -> str | None:
    if job.get("job_type") not in SITE_ROUTABILITY_JOB_TYPES:
        return None
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return None
    site_name = payload.get("site") or payload.get("site_id")
    if not isinstance(site_name, str):
        return None
    site_name = site_name.strip()
    return site_name or None


def process_file(path: Path, dirs: dict, python_bin: str, logger: logging.Logger, dry_run: bool = False) -> None:
    stem = path.stem
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read %s yet (%s); will retry", path.name, exc)
        return
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("invalid JSON in %s: %s", path.name, exc)
        _move(path, dirs["failed"])
        _write_ack(dirs["failed"], stem, {
            "ok": False, "status": "invalid_json", "reasons": [str(exc)], "at": _now_iso(),
        })
        return

    if not validate_job(job):
        logger.error("job %s failed queue_spec validation", path.name)
        _move(path, dirs["failed"])
        _write_ack(dirs["failed"], stem, {
            "ok": False, "status": "invalid", "job_type": job.get("job_type") if isinstance(job, dict) else None,
            "reasons": ["does not match queue_spec; run btq-validate for detail"], "at": _now_iso(),
        })
        return

    site_name = _site_routability_target(job)
    if site_name is not None:
        try:
            registered = sites.is_site_registered(site_name)
        except CouchDBRegistryError as exc:
            logger.warning(
                "site registry unavailable for %s (%s); leaving for retry",
                path.name,
                exc,
            )
            return
        if not registered:
            logger.error("job %s names unregistered site: %s", path.name, site_name)
            _move(path, dirs["failed"])
            _write_ack(dirs["failed"], stem, {
                "ok": False,
                "status": "unregistered_site",
                "job_type": job.get("job_type"),
                "reasons": [f"site not registered: {site_name}"],
                "at": _now_iso(),
            })
            return

    if not job.get("job_id"):
        job["job_id"] = _deterministic_job_id(job)

    ok, transient, result, message = _enqueue(job, python_bin, dry_run=dry_run)
    if ok:
        couch = result.get("couchdb", {})
        duplicate = bool(couch.get("duplicate"))
        logger.info("enqueued %s -> %s%s", path.name, result.get("job_id"), " (duplicate)" if duplicate else "")
        _move(path, dirs["processed"])
        _write_ack(dirs["processed"], stem, {
            "ok": True,
            "status": "duplicate" if duplicate else "enqueued",
            "job_id": result.get("job_id"),
            "couchdb": couch,
            "at": _now_iso(),
        })
        return

    if transient:
        logger.warning("transient enqueue failure for %s (%s); leaving for retry", path.name, message)
        return

    logger.error("permanent enqueue failure for %s: %s", path.name, message)
    _move(path, dirs["failed"])
    _write_ack(dirs["failed"], stem, {
        "ok": False, "status": "enqueue_error", "job_id": job.get("job_id"),
        "reasons": [message], "at": _now_iso(),
    })


def run(poll_seconds: float, once: bool, logger: logging.Logger,
        dry_run: bool = False, drop_root_override: Path | None = None) -> int:
    cfg = get_config()
    drop_root = drop_root_override or (cfg.pipeline_dir / "cowork_drop")
    dirs = {
        "incoming": drop_root / "incoming",
        "processed": drop_root / "processed",
        "failed": drop_root / "failed",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    python_bin = sys.executable
    logger.info("cowork_drop_watcher watching %s (poll=%.1fs)", dirs["incoming"], poll_seconds)

    while not _should_stop:
        now = time.time()
        for path in sorted(dirs["incoming"].glob("*.json")):
            if path.name.endswith(".ack.json"):
                continue
            try:
                if now - path.stat().st_mtime < STABLE_SECONDS:
                    continue  # possibly mid-sync; let it settle
            except OSError:
                continue
            try:
                process_file(path, dirs, python_bin, logger, dry_run=dry_run)
            except Exception:  # never let one bad file kill the watcher
                logger.exception("unexpected error processing %s", path.name)
        if once:
            break
        time.sleep(poll_seconds)
    logger.info("cowork_drop_watcher stopping")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain the Cowork file-drop bridge into CouchDB.")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Process the current backlog once and exit (for testing).")
    parser.add_argument("--dry-run", action="store_true", help="Pass --dry-run to btq-enqueue (no CouchDB write).")
    parser.add_argument("--drop-root", type=Path, default=None, help="Override the cowork_drop root (for testing).")
    args = parser.parse_args(argv)

    cfg = get_config()
    log_path = cfg.logs_dir / "cowork_drop_watch.log"
    logger = configure_logger(log_path)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run(args.poll_seconds, args.once, logger, dry_run=args.dry_run, drop_root_override=args.drop_root)


if __name__ == "__main__":
    raise SystemExit(main())
