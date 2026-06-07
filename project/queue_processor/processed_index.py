from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from queue_processor.idempotency import compute_job_id


HANDLER_VERSION = "queue_processor.phase1"


class ProcessedIndexError(RuntimeError):
    """Raised when the processed index is not parseable."""


@dataclass(frozen=True)
class ProcessedRecord:
    computed_job_id: str
    job_type: str
    target_path: str
    timestamp: str
    handler_version: str
    source_queue_file: str
    capture_id: str | None = None
    run_id: str | None = None


@dataclass
class ProcessedJobIdLookup:
    index_exists: bool
    indexed_job_ids: set[str]
    processed_archive_job_ids: set[str]

    def exists(self, job_id: str) -> tuple[bool, str]:
        if self.index_exists:
            if job_id in self.indexed_job_ids:
                return True, "index"
            return job_id in self.processed_archive_job_ids, "index+processed-scan"
        return job_id in self.processed_archive_job_ids, "processed-scan"

    def mark_indexed(self, job_id: str) -> None:
        self.index_exists = True
        self.indexed_job_ids.add(job_id)

    def mark_processed_archive(self, job_id: str) -> None:
        self.processed_archive_job_ids.add(job_id)


def index_path_for(runtime_root: Path) -> Path:
    return runtime_root / "processed_index.jsonl"


def append_record(index_path: Path, record: ProcessedRecord) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in record.__dict__.items() if value is not None}
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(index_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_records(index_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not index_path.exists():
        return records
    with index_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ProcessedIndexError(f"Corrupted processed index line {line_number}: {exc.msg}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("computed_job_id"), str):
                raise ProcessedIndexError(f"Corrupted processed index line {line_number}: missing computed_job_id")
            records.append(payload)
    return records


def job_id_exists_in_index(index_path: Path, job_id: str) -> bool:
    return any(record["computed_job_id"] == job_id for record in iter_records(index_path))


def job_id_exists_by_processed_scan(processed_dir: Path, job_id: str) -> bool:
    if not processed_dir.exists():
        return False
    for processed_path in sorted(path for path in processed_dir.iterdir() if path.is_file() and path.suffix == ".json"):
        try:
            payload = json.loads(processed_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and compute_job_id(payload) == job_id:
            return True
    return False


def job_ids_by_processed_scan(processed_dir: Path) -> set[str]:
    job_ids: set[str] = set()
    if not processed_dir.exists():
        return job_ids
    for processed_path in sorted(path for path in processed_dir.iterdir() if path.is_file() and path.suffix == ".json"):
        try:
            payload = json.loads(processed_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            job_ids.add(compute_job_id(payload))
    return job_ids


def load_processed_job_id_lookup(runtime_root: Path, processed_dir: Path) -> ProcessedJobIdLookup:
    index_path = index_path_for(runtime_root)
    indexed_job_ids: set[str] = set()
    index_exists = index_path.exists()
    if index_exists:
        indexed_job_ids = {str(record["computed_job_id"]) for record in iter_records(index_path)}
    return ProcessedJobIdLookup(
        index_exists=index_exists,
        indexed_job_ids=indexed_job_ids,
        processed_archive_job_ids=job_ids_by_processed_scan(processed_dir),
    )


def processed_job_id_exists(runtime_root: Path, processed_dir: Path, job_id: str) -> tuple[bool, str]:
    return load_processed_job_id_lookup(runtime_root, processed_dir).exists(job_id)


def build_record(
    computed_job_id: str,
    job_type: str,
    target_path: str,
    source_queue_file: Path,
    capture_id: str | None = None,
    run_id: str | None = None,
) -> ProcessedRecord:
    return ProcessedRecord(
        computed_job_id=computed_job_id,
        job_type=job_type,
        target_path=target_path,
        timestamp=datetime.now(timezone.utc).isoformat(),
        handler_version=HANDLER_VERSION,
        source_queue_file=str(source_queue_file),
        capture_id=capture_id,
        run_id=run_id,
    )
