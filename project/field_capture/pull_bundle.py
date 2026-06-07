from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from config import get_config
from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object


CAPTURE_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class PullBundleError(Exception):
    pass


def default_runtime_root() -> Path:
    return get_config().runtime_root


def default_intake_dir(runtime_root: Path) -> Path:
    return runtime_root / "field_capture" / "intake"


def capture_date_from_id(capture_id: str) -> str:
    match = CAPTURE_DATE_RE.search(capture_id)
    if not match:
        raise PullBundleError(f"cannot determine capture date from capture_id: {capture_id}")
    return match.group(1)


def default_bundle_upload_dir(bundle_path: Path, capture_id: str) -> Path:
    date = capture_date_from_id(capture_id)
    expected = bundle_path / "uploads" / date / capture_id
    if expected.is_dir():
        return expected
    matches = sorted((bundle_path / "uploads").glob(f"*/{capture_id}"))
    if len(matches) == 1 and matches[0].is_dir():
        return matches[0]
    if not matches:
        raise PullBundleError(f"missing upload directory for capture_id: {capture_id}")
    raise PullBundleError(f"multiple upload directories found for capture_id: {capture_id}")


def matching_queue_jobs(bundle_path: Path, capture_id: str) -> list[tuple[Path, dict[str, object]]]:
    jobs: list[tuple[Path, dict[str, object]]] = []
    queue_dir = bundle_path / "queue"
    for path in sorted(queue_dir.glob("*.json")):
        payload = read_json_object(path)
        if not payload or payload.get("job_type") != "photo_capture":
            continue
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("capture_id") or "") == capture_id:
            jobs.append((path, payload))
    return jobs


def one_matching_queue_job(bundle_path: Path, capture_id: str) -> tuple[Path, dict[str, object]]:
    jobs = matching_queue_jobs(bundle_path, capture_id)
    if not jobs:
        raise PullBundleError(f"missing matching photo_capture queue JSON for capture_id: {capture_id}")
    if len(jobs) > 1:
        raise PullBundleError(f"multiple matching photo_capture queue JSON files found for capture_id: {capture_id}")
    return jobs[0]


def media_records(job: dict[str, object]) -> list[dict[str, object]]:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise PullBundleError("queue JSON payload must be an object")
    records: list[dict[str, object]] = []
    for field in ("photos", "audio"):
        raw_records = payload.get(field, [])
        if not isinstance(raw_records, list):
            raise PullBundleError(f"queue JSON payload.{field} must be a list")
        for record in raw_records:
            if not isinstance(record, dict):
                raise PullBundleError(f"queue JSON payload.{field} contains a non-object media record")
            records.append(record)
    return records


def rewritten_queue_job(
    job: dict[str, object],
    *,
    upload_source_dir: Path,
    upload_dest_dir: Path,
    upload_root: Path,
) -> tuple[dict[str, object], list[tuple[Path, Path]]]:
    rewritten = json.loads(json.dumps(job))
    payload = rewritten.get("payload")
    if not isinstance(payload, dict):
        raise PullBundleError("queue JSON payload must be an object")
    copies: list[tuple[Path, Path]] = []
    for field in ("photos", "audio"):
        records = payload.get(field, [])
        if not isinstance(records, list):
            raise PullBundleError(f"queue JSON payload.{field} must be a list")
        for record in records:
            if not isinstance(record, dict):
                raise PullBundleError(f"queue JSON payload.{field} contains a non-object media record")
            filename = str(record.get("filename") or "").strip()
            upload_id = str(record.get("upload_id") or "").strip()
            if not filename and upload_id:
                filename = Path(upload_id).name
            if not filename:
                raise PullBundleError("media record is missing filename/upload_id")
            source_path = resolve_within_root(upload_source_dir / filename, upload_source_dir)
            if not source_path.is_file():
                raise PullBundleError(f"referenced media file is missing from bundle: {source_path}")
            dest_path = resolve_within_root(upload_dest_dir / filename, upload_root)
            record["stored_path"] = str(dest_path)
            if not upload_id:
                record["upload_id"] = f"{upload_dest_dir.parent.name}/{upload_dest_dir.name}/{filename}"
            copies.append((source_path, dest_path))
    return rewritten, copies


def copy_file_idempotently(source: Path, dest: Path, *, dry_run: bool) -> str:
    if dest.exists():
        if dest.read_bytes() == source.read_bytes():
            return "skipped"
        raise PullBundleError(f"local file exists with different content: {dest}")
    if dry_run:
        return "would_copy"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest.with_name(f".{dest.name}.tmp")
    shutil.copy2(source, temp_path)
    temp_path.replace(dest)
    return "copied"


def write_intake_json_idempotently(path: Path, payload: dict[str, object], *, dry_run: bool) -> str:
    if path.exists():
        existing = read_json_object(path)
        if existing == payload:
            return "skipped"
        raise PullBundleError(f"local field-capture intake JSON exists with different content: {path}")
    if dry_run:
        return "would_copy"
    write_json_object(path, payload)
    return "copied"


def import_field_capture_bundle(
    *,
    capture_id: str,
    bundle_path: Path,
    runtime_root: Path,
    dry_run: bool = False,
) -> dict[str, object]:
    bundle_root = bundle_path.expanduser().resolve(strict=False)
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    upload_root = runtime_resolved / "uploads"
    intake_root = default_intake_dir(runtime_resolved)
    upload_source_dir = default_bundle_upload_dir(bundle_root, capture_id).resolve(strict=False)
    date = upload_source_dir.parent.name
    upload_dest_dir = upload_root / date / capture_id
    queue_source_path, job = one_matching_queue_job(bundle_root, capture_id)
    rewritten_job, copies = rewritten_queue_job(
        job,
        upload_source_dir=upload_source_dir,
        upload_dest_dir=upload_dest_dir,
        upload_root=upload_root,
    )
    intake_dest_path = intake_root / queue_source_path.name
    results: list[dict[str, Any]] = []
    counts = {"copied": 0, "skipped": 0, "failed": 0, "would_copy": 0}
    try:
        for source, dest in copies:
            if dest.exists() and dest.read_bytes() != source.read_bytes():
                raise PullBundleError(f"local file exists with different content: {dest}")
        if intake_dest_path.exists() and read_json_object(intake_dest_path) != rewritten_job:
            raise PullBundleError(f"local field-capture intake JSON exists with different content: {intake_dest_path}")
        for source, dest in copies:
            action = copy_file_idempotently(source, dest, dry_run=dry_run)
            counts[action] += 1
            results.append({"type": "media", "action": action, "source": str(source), "destination": str(dest)})
        action = write_intake_json_idempotently(intake_dest_path, rewritten_job, dry_run=dry_run)
        counts[action] += 1
        results.append({"type": "intake_json", "action": action, "source": str(queue_source_path), "destination": str(intake_dest_path)})
    except PullBundleError as exc:
        counts["failed"] += 1
        results.append({"type": "error", "action": "failed", "error": str(exc)})
    return {
        "capture_id": capture_id,
        "bundle_path": str(bundle_root),
        "runtime_root": str(runtime_resolved),
        "upload_source_dir": str(upload_source_dir),
        "upload_destination_dir": str(upload_dest_dir),
        "queue_source_path": str(queue_source_path),
        "intake_destination_path": str(intake_dest_path),
        "dry_run": dry_run,
        "counts": counts,
        "results": results,
        "ok": counts["failed"] == 0,
    }


def format_report(report: dict[str, object]) -> str:
    counts = report["counts"]
    lines = [
        f"field-capture bundle import capture_id={report['capture_id']}",
        f"bundle={report['bundle_path']}",
        f"runtime={report['runtime_root']}",
        f"dry_run={report['dry_run']}",
        f"copied={counts['copied']} skipped={counts['skipped']} would_copy={counts['would_copy']} failed={counts['failed']}",
    ]
    for item in report["results"]:
        if item["type"] == "error":
            lines.append(f"- failed: {item['error']}")
        else:
            lines.append(f"- {item['type']} {item['action']}: {item['source']} -> {item['destination']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(description="Import one exported field-capture upload bundle into the local runtime.")
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--bundle-path", type=Path, required=True, help="Local exported bundle root containing uploads/ and queue/.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = import_field_capture_bundle(
            capture_id=args.capture_id,
            bundle_path=args.bundle_path,
            runtime_root=args.runtime_root,
            dry_run=args.dry_run,
        )
    except PullBundleError as exc:
        report = {
            "capture_id": args.capture_id,
            "bundle_path": str(args.bundle_path.expanduser().resolve(strict=False)),
            "runtime_root": str(args.runtime_root.expanduser().resolve(strict=False)),
            "dry_run": args.dry_run,
            "counts": {"copied": 0, "skipped": 0, "failed": 1, "would_copy": 0},
            "results": [{"type": "error", "action": "failed", "error": str(exc)}],
            "ok": False,
        }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
