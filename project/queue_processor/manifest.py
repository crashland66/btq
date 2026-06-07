from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from io_atomic import atomic_write_text
from processing_core.time import utc_now


def manifest_path_for(runtime_root: Path, capture_id: str) -> Path:
    return runtime_root / "manifests" / f"{capture_id}.json"


def read_manifest(runtime_root: Path, capture_id: str) -> dict[str, Any]:
    path = manifest_path_for(runtime_root, capture_id)
    if not path.exists():
        return {
            "capture_id": capture_id,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "observational": True,
            "artifacts": {},
            "events": [],
            "queue_jobs": [],
            "vault_mutations": [],
            "processed_records": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest must be a JSON object: {path}")
    return payload


def write_manifest(runtime_root: Path, capture_id: str, manifest: dict[str, Any]) -> Path:
    path = manifest_path_for(runtime_root, capture_id)
    manifest["capture_id"] = capture_id
    manifest["observational"] = True
    manifest["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def upsert_unique(items: list[dict[str, Any]], item: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for index, existing in enumerate(items):
        if all(existing.get(key) == item.get(key) for key in keys):
            merged = dict(existing)
            merged.update({key: value for key, value in item.items() if value is not None})
            items[index] = merged
            return items
    items.append(item)
    return items


def create_capture_manifest(
    runtime_root: Path,
    capture_id: str,
    *,
    audio_file: Path,
    source_fingerprint: dict[str, Any],
    transcript_file: Path | None,
    normalized_transcript_file: Path | None,
    event_paths: list[Path],
    queue_job_paths: list[Path],
    process_log: Path | None,
) -> Path:
    manifest = read_manifest(runtime_root, capture_id)
    artifacts = dict(manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {})
    artifacts.update(
        {
            "audio": str(audio_file),
            "source_fingerprint": source_fingerprint,
            "transcript": str(transcript_file) if transcript_file is not None else None,
            "normalized_transcript": str(normalized_transcript_file) if normalized_transcript_file is not None else None,
            "process_log": str(process_log) if process_log is not None else None,
        }
    )
    manifest["artifacts"] = artifacts
    events = list(manifest.get("events") if isinstance(manifest.get("events"), list) else [])
    for path in event_paths:
        events = upsert_unique(events, {"path": str(path), "timestamp": utc_now()}, ("path",))
    manifest["events"] = events
    queue_jobs = list(manifest.get("queue_jobs") if isinstance(manifest.get("queue_jobs"), list) else [])
    for path in queue_job_paths:
        queue_jobs = upsert_unique(queue_jobs, {"path": str(path), "timestamp": utc_now()}, ("path",))
    manifest["queue_jobs"] = queue_jobs
    return write_manifest(runtime_root, capture_id, manifest)


def record_processed_mutation(
    runtime_root: Path,
    capture_id: str,
    *,
    computed_job_id: str,
    job_type: str,
    target_path: str,
    source_queue_file: str,
    processed_record: dict[str, Any],
) -> Path:
    manifest = read_manifest(runtime_root, capture_id)
    vault_mutations = list(manifest.get("vault_mutations") if isinstance(manifest.get("vault_mutations"), list) else [])
    vault_mutations = upsert_unique(
        vault_mutations,
        {
            "computed_job_id": computed_job_id,
            "job_type": job_type,
            "target_path": target_path,
            "source_queue_file": source_queue_file,
            "timestamp": utc_now(),
        },
        ("computed_job_id", "target_path"),
    )
    manifest["vault_mutations"] = vault_mutations
    processed_records = list(manifest.get("processed_records") if isinstance(manifest.get("processed_records"), list) else [])
    processed_records = upsert_unique(
        processed_records,
        dict(processed_record),
        ("computed_job_id", "source_queue_file"),
    )
    manifest["processed_records"] = processed_records
    return write_manifest(runtime_root, capture_id, manifest)
