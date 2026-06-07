"""cowork_read_watcher - answer Cowork queue read requests via file drop.

Cowork's sandbox cannot reach CouchDB or the tailnet-hosted read MCP server.
It can only exchange files through the shared pipeline mount. This watcher runs
on the Pro, reads JSON requests from

    <pipeline_dir>/cowork_read/requests/<request_id>.json

dispatches the read-only request to event_pipeline.couchdb_queue_reader, writes
a JSON response into responses/, and moves the request to processed/ or failed/.

Transient CouchDB failures leave the request in requests/ for retry. This module
is intentionally read-only by construction: it imports only the queue reader and
does not import or call enqueue/write bridge code.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from config import get_config
from event_pipeline import couchdb_queue_reader
from event_pipeline.couchdb_queue_reader import CouchDBQueueReaderError


DEFAULT_POLL_SECONDS = 5.0
# Skip files modified within this window so we don't read a half-synced file.
STABLE_SECONDS = 3.0
ALLOWED_TOOLS = frozenset({"queue_state", "list_queue_jobs", "get_job"})
TRANSIENT_ERROR_CODES = frozenset({"couchdb_unavailable"})
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_should_stop = False


class BadRequestError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _handle_signal(signum, _frame):
    global _should_stop
    _should_stop = True


def configure_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cowork.read_watch")
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


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest.unlink()
    src.rename(dest)
    return dest


def _write_response(response_dir: Path, request_id: str, response: dict[str, Any]) -> Path:
    response_dir.mkdir(parents=True, exist_ok=True)
    response_path = response_dir / f"{request_id}.json"
    tmp_path = response_dir / f".{request_id}.json.tmp"
    tmp_path.write_text(json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(response_path)
    return response_path


def _safe_response_id(path: Path, request: object | None = None) -> str:
    if isinstance(request, dict) and isinstance(request.get("request_id"), str) and REQUEST_ID_RE.fullmatch(request["request_id"]):
        return request["request_id"]
    if REQUEST_ID_RE.fullmatch(path.stem):
        return path.stem
    return "invalid_request"


def _validate_request(path: Path, request: object) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(request, dict):
        raise BadRequestError("invalid_request", "request must be a JSON object")

    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise BadRequestError("invalid_request", "request_id must be a safe non-empty string")
    if request_id != path.stem:
        raise BadRequestError("invalid_request", "request_id must match the request filename stem")

    tool = request.get("tool")
    if not isinstance(tool, str) or tool not in ALLOWED_TOOLS:
        raise BadRequestError("invalid_tool", f"unknown read-only tool: {tool!r}")

    args = request.get("args", {})
    if not isinstance(args, dict):
        raise BadRequestError("invalid_request", "args must be a JSON object")

    return request_id, tool, args


def _bool_arg(args: dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise BadRequestError("invalid_request", f"{key} must be a boolean")
    return value


def _int_arg(args: dict[str, Any], key: str, default: int | None = None) -> int | None:
    value = args.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequestError("invalid_request", f"{key} must be an integer")
    return value


def _str_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequestError("invalid_request", f"{key} must be a string")
    return value


def _states_arg(args: dict[str, Any]) -> tuple[str, ...]:
    value = args.get("states")
    if value is None:
        return ("failed", "pending", "processing", "claimed")
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise BadRequestError("invalid_request", "states must be a list of non-empty strings")
    return tuple(value)


def _dispatch(tool: str, args: dict[str, Any], config: couchdb_queue_reader.QueueReaderConfig) -> Any:
    if tool == "queue_state":
        recent_limit = _int_arg(args, "recent_limit", 10)
        assert recent_limit is not None
        return couchdb_queue_reader.queue_state(states=_states_arg(args), recent_limit=recent_limit, config=config)
    if tool == "list_queue_jobs":
        limit = _int_arg(args, "limit")
        return couchdb_queue_reader.list_queue_jobs(
            date=_str_arg(args, "date"),
            since=_str_arg(args, "since"),
            state=_str_arg(args, "state"),
            all_dates=_bool_arg(args, "all_dates", False),
            limit=limit,
            config=config,
        )
    if tool == "get_job":
        job_id = _str_arg(args, "job_id")
        if not job_id:
            raise BadRequestError("invalid_request", "get_job requires a non-empty job_id")
        return couchdb_queue_reader.get_job(job_id, config=config)
    raise BadRequestError("invalid_tool", f"unknown read-only tool: {tool!r}")


def process_request(path: Path, dirs: dict[str, Path], logger: logging.Logger) -> None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read %s yet (%s); will retry", path.name, exc)
        return

    request: object | None = None
    try:
        request = json.loads(raw)
        request_id, tool, args = _validate_request(path, request)
        reader_config = couchdb_queue_reader.load_reader_config()
        result = _dispatch(tool, args, reader_config)
    except json.JSONDecodeError as exc:
        request_id = _safe_response_id(path)
        logger.error("invalid JSON in %s: %s", path.name, exc)
        _write_response(dirs["responses"], request_id, {
            "request_id": request_id,
            "ok": False,
            "error": {"code": "invalid_json", "message": str(exc)},
            "completed_at": _now_iso(),
        })
        _move(path, dirs["failed"])
        return
    except BadRequestError as exc:
        request_id = _safe_response_id(path, request)
        tool_value = request.get("tool") if isinstance(request, dict) else None
        logger.error("bad cowork_read request %s: %s", path.name, exc.message)
        _write_response(dirs["responses"], request_id, {
            "request_id": request_id,
            "ok": False,
            "tool": tool_value,
            "error": {"code": exc.code, "message": exc.message},
            "completed_at": _now_iso(),
        })
        _move(path, dirs["failed"])
        return
    except CouchDBQueueReaderError as exc:
        if exc.code in TRANSIENT_ERROR_CODES:
            logger.warning("transient queue read failure for %s (%s); leaving for retry", path.name, exc.message)
            return
        request_id = _safe_response_id(path, request)
        tool_value = request.get("tool") if isinstance(request, dict) else None
        logger.error("queue read error for %s: %s", path.name, exc.message)
        _write_response(dirs["responses"], request_id, {
            "request_id": request_id,
            "ok": False,
            "tool": tool_value,
            "error": {"code": exc.code, "message": exc.message},
            "completed_at": _now_iso(),
        })
        _move(path, dirs["failed"])
        return

    logger.info("answered cowork_read request %s (%s)", path.name, tool)
    _write_response(dirs["responses"], request_id, {
        "request_id": request_id,
        "ok": True,
        "tool": tool,
        "result": result,
        "completed_at": _now_iso(),
    })
    _move(path, dirs["processed"])


def run(poll_seconds: float, once: bool, logger: logging.Logger, drop_root_override: Path | None = None) -> int:
    cfg = get_config()
    drop_root = drop_root_override or (cfg.pipeline_dir / "cowork_read")
    dirs = {
        "requests": drop_root / "requests",
        "responses": drop_root / "responses",
        "processed": drop_root / "processed",
        "failed": drop_root / "failed",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    logger.info("cowork_read_watcher watching %s (poll=%.1fs)", dirs["requests"], poll_seconds)

    while not _should_stop:
        now = time.time()
        for path in sorted(dirs["requests"].glob("*.json")):
            try:
                if now - path.stat().st_mtime < STABLE_SECONDS:
                    continue
            except OSError:
                continue
            try:
                process_request(path, dirs, logger)
            except Exception:
                logger.exception("unexpected error processing %s", path.name)
        if once:
            break
        time.sleep(poll_seconds)
    logger.info("cowork_read_watcher stopping")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer Cowork queue read requests from the file-drop bridge.")
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Process the current backlog once and exit (for testing).")
    parser.add_argument("--drop-root", type=Path, default=None, help="Override the cowork_read root (for testing).")
    args = parser.parse_args(argv)

    cfg = get_config()
    logger = configure_logger(cfg.logs_dir / "cowork_read_watch.log")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return run(args.poll_seconds, args.once, logger, drop_root_override=args.drop_root)


if __name__ == "__main__":
    raise SystemExit(main())
