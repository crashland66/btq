from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import get_config


@dataclass(frozen=True)
class QueueStatus:
    local_intake_count: int
    durable_queue_count: int
    processing_count: int
    failed_count: int
    quarantine_count: int
    last_successful_process_timestamp: str | None


def count_json_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.iterdir() if item.is_file() and item.suffix == ".json")


def count_files_recursive(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def latest_file_timestamp(path: Path) -> str | None:
    if not path.exists():
        return None
    latest_mtime: float | None = None
    for item in path.iterdir():
        if not item.is_file():
            continue
        if latest_mtime is None or item.stat().st_mtime > latest_mtime:
            latest_mtime = item.stat().st_mtime
    if latest_mtime is None:
        return None
    return datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat()


def build_status(runtime_root: Path) -> QueueStatus:
    return QueueStatus(
        local_intake_count=count_json_files(runtime_root / "intake" / "outbox"),
        durable_queue_count=count_json_files(runtime_root / "queue"),
        processing_count=count_files_recursive(runtime_root / "processing"),
        failed_count=count_files_recursive(runtime_root / "failed"),
        quarantine_count=count_files_recursive(runtime_root / "quarantine"),
        last_successful_process_timestamp=latest_file_timestamp(runtime_root / "processed"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Show BTQ queue transport and runtime status.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    status = build_status(args.runtime_root.expanduser())
    if args.json:
        print(json.dumps(asdict(status), indent=2, sort_keys=True))
        return 0

    print("BTQ queue status")
    print(f"Local intake: {status.local_intake_count}")
    print(f"Durable queue: {status.durable_queue_count}")
    print(f"Processing: {status.processing_count}")
    print(f"Failed: {status.failed_count}")
    print(f"Quarantine: {status.quarantine_count}")
    print(f"Last successful process: {status.last_successful_process_timestamp or 'none'}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
