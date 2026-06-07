from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from config import get_config
from field_capture.approved_job_drafts import default_draft_dir
from processing_core.draft_staging import stage_approved_drafts, stage_approved_drafts_report


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_status_dir(runtime_root: Path | None = None) -> Path:
    root = default_runtime_root() if runtime_root is None else runtime_root
    return root / "reviews" / "staging" / "field_capture"


def stage_field_capture_drafts(
    *,
    runtime_root: Path,
    draft_dir: Path | None = None,
    queue_dir: Path | None = None,
    status_dir: Path | None = None,
    draft_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    return stage_approved_drafts(
        draft_dir or default_draft_dir(runtime_root),
        runtime_root=runtime_root,
        queue_dir=queue_dir or runtime_root / "queue",
        status_dir=status_dir or default_status_dir(runtime_root),
        draft_ids=draft_ids,
    )


def stage_field_capture_drafts_report(
    *,
    runtime_root: Path,
    draft_dir: Path | None = None,
    queue_dir: Path | None = None,
    status_dir: Path | None = None,
    dry_run: bool = False,
    draft_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    return stage_approved_drafts_report(
        draft_dir or default_draft_dir(runtime_root),
        runtime_root=runtime_root,
        queue_dir=queue_dir or runtime_root / "queue",
        status_dir=status_dir or default_status_dir(runtime_root),
        dry_run=dry_run,
        draft_ids=draft_ids,
    )


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Stage approved field-capture job drafts into the runtime queue.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--draft-dir", type=Path)
    parser.add_argument("--queue-dir", type=Path)
    parser.add_argument("--status-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    if args.dry_run:
        report = stage_field_capture_drafts_report(
            runtime_root=runtime_root,
            draft_dir=args.draft_dir.expanduser() if args.draft_dir is not None else None,
            queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
            status_dir=args.status_dir.expanduser() if args.status_dir is not None else None,
            dry_run=True,
        )
        counts = report["counts"]
    else:
        report = None
        counts = stage_field_capture_drafts(
            runtime_root=runtime_root,
            draft_dir=args.draft_dir.expanduser() if args.draft_dir is not None else None,
            queue_dir=args.queue_dir.expanduser() if args.queue_dir is not None else None,
            status_dir=args.status_dir.expanduser() if args.status_dir is not None else None,
        )
    if args.json:
        print(json.dumps(report if args.dry_run else counts, indent=2, sort_keys=True))
    else:
        prefix = "field approved draft staging dry-run" if args.dry_run else "field approved draft staging"
        print(
            f"{prefix}: "
            f"discovered={counts['discovered']} skipped={counts['skipped']} "
            f"completed={counts['completed']} failed={counts['failed']}"
        )
        if args.dry_run and report is not None:
            for result in report["results"]:
                print(f"- {result['draft_id']} {result['status']}: {result['reason']}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
