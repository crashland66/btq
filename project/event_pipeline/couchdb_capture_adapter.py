from __future__ import annotations

import subprocess
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture import pull_bundle
from processing_core.artifacts import read_json_object, write_json_object
from queue_spec import JOB_PHOTO_CAPTURE, validate_job


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
DEFAULT_REMOTE_COMMAND_TIMEOUT_SECONDS = 30.0


class CaptureAdapterError(Exception):
    pass


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    raw_timeout = os.environ.get("BTQ_FIELD_CAPTURE_REMOTE_COMMAND_TIMEOUT_SECONDS", "")
    timeout_seconds = DEFAULT_REMOTE_COMMAND_TIMEOUT_SECONDS
    if raw_timeout:
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError:
            timeout_seconds = DEFAULT_REMOTE_COMMAND_TIMEOUT_SECONDS
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        stderr = f"remote command timed out after {timeout_seconds:g} seconds"
        return subprocess.CompletedProcess(args, 124, stdout=exc.stdout or "", stderr=stderr)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def date_from_capture(captured_at: str, fallback_now: datetime | None = None) -> str:
    if len(captured_at) >= 10:
        candidate = captured_at[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            pass
    now = fallback_now if fallback_now is not None else datetime.now(timezone.utc)
    return now.date().isoformat()


def completed_process_output(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    return stderr or stdout


def remote_file_size(remote_host: str, remote_path: str, runner: CommandRunner) -> int:
    result = runner(["ssh", remote_host, "stat", "-c", "%s", remote_path])
    if result.returncode != 0:
        message = completed_process_output(result) or f"remote stat failed: {remote_path}"
        raise CaptureAdapterError(message)
    try:
        return int((result.stdout or "").strip())
    except ValueError as exc:
        raise CaptureAdapterError(f"remote stat returned non-integer size for {remote_path}: {result.stdout!r}") from exc


def copy_remote_file_idempotently(
    *,
    remote_host: str,
    remote_path: str,
    local_path: Path,
    runner: CommandRunner,
    dry_run: bool,
) -> str:
    if local_path.exists():
        remote_size = remote_file_size(remote_host, remote_path, runner)
        if local_path.stat().st_size == remote_size:
            return "skipped"
        raise CaptureAdapterError(f"local file exists with different size: {local_path}")
    if dry_run:
        return "would_copy"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    result = runner(["scp", f"{remote_host}:{remote_path}", str(local_path)])
    if result.returncode != 0:
        message = completed_process_output(result) or f"scp failed: {remote_path}"
        raise CaptureAdapterError(message)
    if not local_path.exists():
        raise CaptureAdapterError(f"scp did not create expected local file: {local_path}")
    return "copied"


def build_intake_job(
    *,
    doc: dict[str, Any],
    site: str,
    captured_at: str,
    exported_at: str,
    local_photos: list[dict[str, Any]],
    local_audio: list[dict[str, Any]],
) -> dict[str, object]:
    capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
    metadata: dict[str, object] = {
        "capture_id": capture_id,
        "source": "couchdb",
    }
    for field in (
        "site_id",
        "person_id",
        "person_name",
        "field_capture_token_id",
        "field_capture_token_label",
    ):
        value = str(doc.get(field) or "").strip()
        if value:
            metadata[field] = value
    job = {
        "job_id": capture_id,
        "job_type": JOB_PHOTO_CAPTURE,
        "payload": {
            "site": site,
            "qc_category": str(doc.get("qc_category") or "").strip(),
            "note": str(doc.get("note") or ""),
            "captured_at": captured_at,
            "exported_at": exported_at,
            "photos": local_photos,
        },
        "metadata": metadata,
    }
    if local_audio:
        job["payload"]["audio"] = local_audio
    if not validate_job(job):
        raise CaptureAdapterError("generated photo_capture intake JSON failed queue validation")
    return job


def media_copy_plan(
    *,
    doc: dict[str, Any],
    field: str,
    upload_dest_dir: Path,
    date: str,
    capture_id: str,
) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    raw_records = doc.get(field, [])
    if raw_records is None:
        raw_records = []
    if not isinstance(raw_records, list):
        raise CaptureAdapterError(f"field_capture document {field} must be a list")
    local_records: list[dict[str, Any]] = []
    copy_plan: list[tuple[str, Path]] = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise CaptureAdapterError(f"field_capture document {field} must contain objects")
        filename = str(record.get("filename") or "").strip()
        mime_type = str(record.get("mime_type") or "").strip()
        remote_path = str(record.get("stored_path") or "").strip()
        if not filename or not mime_type or not remote_path:
            raise CaptureAdapterError(f"{field} record is missing filename, mime_type, or stored_path")
        local_path = upload_dest_dir / filename
        local_record = {str(key): value for key, value in record.items() if isinstance(key, str)}
        local_record["filename"] = filename
        local_record["mime_type"] = mime_type
        local_record["stored_path"] = str(local_path)
        local_record.setdefault("upload_id", f"{date}/{capture_id}/{filename}")
        if field == "audio":
            local_record.setdefault("media_type", "audio")
        local_records.append(local_record)
        copy_plan.append((remote_path, local_path))
    return local_records, copy_plan


def _media_record_key(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    upload_id = str(record.get("upload_id") or "").strip()
    if upload_id:
        return upload_id
    filename = str(record.get("filename") or "").strip()
    mime_type = str(record.get("mime_type") or "").strip()
    return f"{filename}\0{mime_type}" if filename else ""


def is_safe_intake_media_expansion(
    existing: dict[str, object] | None,
    incoming: dict[str, object],
) -> bool:
    """Return True only when incoming strictly adds media to the same intake job."""
    if not isinstance(existing, dict):
        return False
    existing_payload = existing.get("payload")
    incoming_payload = incoming.get("payload")
    if not isinstance(existing_payload, dict) or not isinstance(incoming_payload, dict):
        return False

    existing_without_media = dict(existing)
    incoming_without_media = dict(incoming)
    existing_payload_without_media = dict(existing_payload)
    incoming_payload_without_media = dict(incoming_payload)
    for field in ("photos", "audio"):
        existing_payload_without_media.pop(field, None)
        incoming_payload_without_media.pop(field, None)
    existing_without_media["payload"] = existing_payload_without_media
    incoming_without_media["payload"] = incoming_payload_without_media
    if existing_without_media != incoming_without_media:
        return False

    existing_count = 0
    incoming_count = 0
    for field in ("photos", "audio"):
        existing_records = existing_payload.get(field, [])
        incoming_records = incoming_payload.get(field, [])
        if not isinstance(existing_records, list) or not isinstance(incoming_records, list):
            return False
        existing_by_key = {_media_record_key(record): record for record in existing_records}
        incoming_by_key = {_media_record_key(record): record for record in incoming_records}
        if (
            "" in existing_by_key
            or "" in incoming_by_key
            or len(existing_by_key) != len(existing_records)
            or len(incoming_by_key) != len(incoming_records)
        ):
            return False
        if any(incoming_by_key.get(key) != record for key, record in existing_by_key.items()):
            return False
        existing_count += len(existing_records)
        incoming_count += len(incoming_records)
    return incoming_count > existing_count


def import_couchdb_capture(
    *,
    doc: dict[str, Any],
    runtime_root: Path,
    remote_host: str,
    registry: CouchDBSiteRegistry | None = None,
    runner: CommandRunner = run_command,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Materializes a claimed CouchDB field_capture document into the local
    file-based intake format.

    Returns a result dict with keys:
      capture_id, ok, counts (copied, skipped, failed, would_copy), results
    This matches the shape returned by pull_bundle.import_field_capture_bundle()
    so the caller can treat both paths uniformly.
    """
    runtime_resolved = runtime_root.expanduser().resolve(strict=False)
    upload_root = runtime_resolved / "uploads"
    intake_root = pull_bundle.default_intake_dir(runtime_resolved)
    capture_id = str(doc.get("capture_id") or doc.get("_id") or "").strip()
    if not capture_id:
        raise CaptureAdapterError("field_capture document is missing capture_id")
    site_id = str(doc.get("site_id") or "").strip()
    captured_at = str(doc.get("captured_at") or "").strip() or utc_now_iso()
    date = date_from_capture(captured_at)
    upload_dest_dir = upload_root / date / capture_id
    intake_dest_path = intake_root / f"{capture_id}.json"
    existing_intake_exists = intake_dest_path.exists()
    existing_intake = read_json_object(intake_dest_path) if existing_intake_exists else None
    existing_payload = existing_intake.get("payload") if isinstance(existing_intake, dict) else None
    existing_exported_at = existing_payload.get("exported_at") if isinstance(existing_payload, dict) else None
    exported_at = str(existing_exported_at) if isinstance(existing_exported_at, str) and existing_exported_at else utc_now_iso()
    active_registry = registry if registry is not None else CouchDBSiteRegistry()
    site = active_registry.resolve_canonical(site_id) if site_id else None
    canonical_site = site or site_id

    local_photos, photo_copy_plan = media_copy_plan(
        doc=doc,
        field="photos",
        upload_dest_dir=upload_dest_dir,
        date=date,
        capture_id=capture_id,
    )
    local_audio, audio_copy_plan = media_copy_plan(
        doc=doc,
        field="audio",
        upload_dest_dir=upload_dest_dir,
        date=date,
        capture_id=capture_id,
    )
    note = str(doc.get("note") or "").strip()
    if not local_photos and not local_audio and not note:
        raise CaptureAdapterError("field_capture document must include at least one photo, audio, or note")
    copy_plan = [*photo_copy_plan, *audio_copy_plan]

    intake_job = build_intake_job(
        doc=doc,
        site=canonical_site,
        captured_at=captured_at,
        exported_at=exported_at,
        local_photos=local_photos,
        local_audio=local_audio,
    )
    existing_intake_matches = existing_intake_exists and existing_intake == intake_job

    results: list[dict[str, Any]] = []
    counts = {"copied": 0, "skipped": 0, "failed": 0, "would_copy": 0}
    try:
        for remote_path, local_path in copy_plan:
            if existing_intake_exists and local_path.exists():
                action = "skipped"
            else:
                action = copy_remote_file_idempotently(
                    remote_host=remote_host,
                    remote_path=remote_path,
                    local_path=local_path,
                    runner=runner,
                    dry_run=dry_run,
                )
            counts[action] += 1
            results.append({"type": "media", "action": action, "source": f"{remote_host}:{remote_path}", "destination": str(local_path)})
        intake_was_expanded = is_safe_intake_media_expansion(existing_intake, intake_job)
        if existing_intake_exists and intake_was_expanded:
            if dry_run:
                action = "would_copy"
            else:
                write_json_object(intake_dest_path, intake_job)
                action = "copied"
        elif existing_intake_exists:
            action = "skipped"
        else:
            try:
                action = pull_bundle.write_intake_json_idempotently(intake_dest_path, intake_job, dry_run=dry_run)
            except pull_bundle.PullBundleError as exc:
                raise CaptureAdapterError(str(exc)) from exc
        counts[action] += 1
        intake_result = {
            "type": "intake_json",
            "action": action,
            "source": f"couchdb:{capture_id}",
            "destination": str(intake_dest_path),
        }
        if intake_was_expanded:
            intake_result["note"] = "existing intake refreshed with additional media"
        elif existing_intake_exists and not existing_intake_matches:
            intake_result["note"] = "existing intake retained; regenerated content differed"
        results.append(intake_result)
    except CaptureAdapterError as exc:
        counts["failed"] += 1
        results.append({"type": "error", "action": "failed", "error": str(exc)})
        raise

    return {
        "capture_id": capture_id,
        "runtime_root": str(runtime_resolved),
        "upload_destination_dir": str(upload_dest_dir),
        "intake_destination_path": str(intake_dest_path),
        "dry_run": dry_run,
        "counts": counts,
        "results": results,
        "ok": counts["failed"] == 0,
    }
