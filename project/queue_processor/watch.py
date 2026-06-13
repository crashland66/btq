from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from config import get_config, require_directories
from queue_processor.main import DEFAULT_PROJECT_ROOT, ensure_local_runtime_root, process_all


DEFAULT_CONFIG = get_config()
DEFAULT_RUNTIME_ROOT = DEFAULT_CONFIG.runtime_root
DEFAULT_LOCAL_RUNTIME_DIR = DEFAULT_CONFIG.local_runtime_dir
DEFAULT_LOG_PATH = DEFAULT_CONFIG.queue_watch_log_path
DEFAULT_POLL_SECONDS = 5.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch the BTQ runtime queue and process jobs continuously.")
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Process one queue pass and exit.")
    return parser.parse_args(argv)


def configure_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("queue_watch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Rotating handler caps total log footprint at ~100 MB (5 backups x 20 MB).
    # The flat FileHandler was growing past 900 MB and breaking the ops
    # dashboard's /failed scan.
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def run_once(
    project_root: Path,
    runtime_root: Path,
    local_root: Path,
    logs_dir: Path,
    logger: logging.Logger,
) -> None:
    logger.info(
        "processing queue project_root=%s runtime_root=%s",
        project_root,
        runtime_root,
    )
    process_all(project_root=project_root, runtime_root=runtime_root, dry_run=False)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.project_root.expanduser()
    runtime_root = ensure_local_runtime_root(args.runtime_root.expanduser())
    log_path = args.log_path.expanduser()
    if log_path == DEFAULT_LOG_PATH.expanduser():
        log_path = runtime_root / "logs" / "queue_watch.log"
    logger = configure_logging(log_path)
    require_directories(
        {
            "project_root": project_root,
        }
    )

    if args.once:
        run_once(project_root, runtime_root, DEFAULT_CONFIG.local_root, DEFAULT_CONFIG.logs_dir, logger)
        return 0

    logger.info(
        "watching queue project_root=%s runtime_root=%s interval=%.1fs",
        project_root,
        runtime_root,
        args.poll_seconds,
    )
    while True:
        try:
            run_once(project_root, runtime_root, DEFAULT_CONFIG.local_root, DEFAULT_CONFIG.logs_dir, logger)
        except Exception:  # noqa: BLE001
            logger.exception("queue watch pass failed")
        time.sleep(args.poll_seconds)


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
