from __future__ import annotations

import argparse
import json
from pathlib import Path

from event_to_queue.adapter import event_to_job
from io_atomic import atomic_write_text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert validated event JSON files into queue job JSON files.")
    parser.add_argument("--events-valid-dir", type=Path, required=True)
    parser.add_argument("--queue-jobs-dir", type=Path, required=True)
    return parser.parse_args(argv)


def job_path_for(queue_jobs_dir: Path, event_id: str) -> Path:
    return queue_jobs_dir / f"job_{event_id}.json"


def convert_event_paths(event_paths: list[Path], queue_jobs_dir: Path) -> list[Path]:
    queue_jobs_dir.mkdir(parents=True, exist_ok=True)
    written_paths: list[Path] = []

    for event_path in sorted(event_paths):
        event = json.loads(event_path.read_text(encoding="utf-8"))
        job = event_to_job(event)
        if job is None:
            continue
        capture_id = event.get("capture_id")
        if isinstance(capture_id, str) and capture_id.strip():
            job = {
                **job,
                "metadata": {
                    **(job.get("metadata") if isinstance(job.get("metadata"), dict) else {}),
                    "capture_id": capture_id,
                },
            }
        job = {
            **job,
            "intent": {
                "category": str(event.get("type", "operational_update")),
                "reason": str(event.get("details", ""))[:240],
                "source_context": str(event.get("source_excerpt", ""))[:500],
                "operator_relevance": str(event.get("site", "unknown")),
                "confidence": str(event.get("confidence", "unknown")),
                **({"epistemic_state": event["epistemic_state"]} if isinstance(event.get("epistemic_state"), dict) else {}),
            },
        }

        event_id = str(event["event_id"])
        job_path = job_path_for(queue_jobs_dir, event_id)
        if job_path.exists():
            continue

        atomic_write_text(job_path, json.dumps(job, indent=2, sort_keys=True) + "\n")
        written_paths.append(job_path)

    return written_paths


def convert_events(events_valid_dir: Path, queue_jobs_dir: Path) -> list[Path]:
    return convert_event_paths(sorted(events_valid_dir.glob("*.json")), queue_jobs_dir)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    convert_events(args.events_valid_dir.resolve(), args.queue_jobs_dir.resolve())
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
