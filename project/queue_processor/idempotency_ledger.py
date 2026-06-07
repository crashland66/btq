from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from queue_processor.idempotency import compute_job_id


@dataclass(frozen=True)
class IdempotencyRecord:
    idempotency_key: str
    job_type: str
    payload_hash: str
    computed_job_id: str
    target_path: str
    timestamp: str
    person_id: Optional[str] = None
    source_queue_file: Optional[str] = None
    run_id: Optional[str] = None


def ledger_path_for(runtime_root: Path) -> Path:
    return runtime_root / "idempotency_keys.jsonl"


def payload_hash_for(job_type: str, payload: Dict[str, Any]) -> str:
    return compute_job_id({"job_type": job_type, "payload": payload})


def append_record(path: Path, record: IdempotencyRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in record.__dict__.items() if value is not None}
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_records(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Corrupted idempotency ledger line {line_number}: {exc.msg}") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("idempotency_key"), str):
                raise ValueError(f"Corrupted idempotency ledger line {line_number}: missing idempotency_key")
            records.append(payload)
    return records


def latest_record_for(path: Path, idempotency_key: str) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    for record in iter_records(path):
        if record.get("idempotency_key") == idempotency_key:
            latest = record
    return latest


def build_record(
    *,
    idempotency_key: str,
    job_type: str,
    payload: Dict[str, Any],
    computed_job_id: str,
    target_path: Path,
    person_id: Optional[str] = None,
    source_queue_file: Optional[Path] = None,
    run_id: Optional[str] = None,
) -> IdempotencyRecord:
    return IdempotencyRecord(
        idempotency_key=idempotency_key,
        job_type=job_type,
        payload_hash=payload_hash_for(job_type, payload),
        computed_job_id=computed_job_id,
        target_path=str(target_path),
        timestamp=datetime.now(timezone.utc).isoformat(),
        person_id=person_id,
        source_queue_file=None if source_queue_file is None else str(source_queue_file),
        run_id=run_id,
    )
