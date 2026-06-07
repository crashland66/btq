from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_writer import CouchDBCaptureWriterError, put_field_capture_document


DEFAULT_QUEUE_DIR = Path("/srv/btq/runtime/queue")
FAILED_PATH_LIMIT = 10
LOGGER = logging.getLogger(__name__)


class BackfillValidationError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def request_json(
    config: couchdb_config.CouchDBConfig,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{config.base_url}/{path.lstrip('/')}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return 404, None
        raise
    if not raw:
        return status, None
    payload_obj = json.loads(raw.decode("utf-8"))
    if not isinstance(payload_obj, dict):
        raise BackfillValidationError("CouchDB returned non-object JSON")
    return status, payload_obj


def get_existing_doc(
    config: couchdb_config.CouchDBConfig,
    database: str,
    doc_id: str,
) -> dict[str, Any] | None:
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(doc_id, safe="")
    _status, payload = request_json(config, "GET", f"{db}/{quoted_id}")
    return payload


def build_capture_document(queue_path: Path, queue_job: dict[str, Any]) -> dict[str, Any]:
    metadata = _object_field(queue_job, "metadata")
    payload = _object_field(queue_job, "payload")
    capture_id = _required_string(metadata, "metadata.capture_id")
    site_id = _required_string(metadata, "metadata.site_id")
    site = _required_string(payload, "payload.site")
    captured_at = _required_string(payload, "payload.captured_at")
    photos = _required_media_list(payload, "photos", require_media_type=False)
    audio = _optional_media_list(payload, "audio", require_media_type=True)

    doc: dict[str, Any] = {
        "_id": capture_id,
        "type": "field_capture",
        "capture_id": capture_id,
        "site": site,
        "site_id": site_id,
        "person_id": str(metadata.get("person_id") or ""),
        "person_name": str(metadata.get("person_name") or ""),
        "field_capture_token_id": str(metadata.get("field_capture_token_id") or ""),
        "field_capture_token_label": str(metadata.get("field_capture_token_label") or ""),
        "qc_category": str(payload.get("qc_category") or ""),
        "note": str(payload.get("note") or ""),
        "captured_at": captured_at,
        "exported_at": str(payload.get("exported_at") or ""),
        "photos": photos,
        "processing_state": "complete",
        "created_at": captured_at or utc_now_iso(),
        "backfill_source": "queue_file",
        "backfill_source_path": str(queue_path),
    }
    if audio:
        doc["audio"] = audio
    return doc


def run_backfill(
    *,
    queue_dir: Path,
    database: str,
    config: couchdb_config.CouchDBConfig | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    couch_config = config or couchdb_config.from_env()
    report: dict[str, Any] = {
        "created": 0,
        "skipped_already_exists": 0,
        "skipped_not_photo_capture": 0,
        "failed": 0,
        "failed_paths": [],
    }
    for queue_path in sorted(queue_dir.glob("*.json")):
        try:
            queue_job = _read_queue_file(queue_path)
            if queue_job.get("job_type") != "photo_capture":
                report["skipped_not_photo_capture"] += 1
                continue
            doc = build_capture_document(queue_path, queue_job)
            if get_existing_doc(couch_config, database, str(doc["_id"])) is not None:
                report["skipped_already_exists"] += 1
                continue
            if not dry_run:
                put_field_capture_document(couch_config, doc, database=database)
            report["created"] += 1
        except Exception as exc:  # noqa: BLE001 - per-file failures should not abort the backfill.
            _record_failure(report, queue_path, exc)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill legacy field-capture queue files into CouchDB.")
    parser.add_argument("--queue-dir", type=Path, default=DEFAULT_QUEUE_DIR, help="Directory containing legacy queue JSON files.")
    parser.add_argument(
        "--database",
        default=couchdb_config.field_captures_database(),
        help="CouchDB database for field-capture documents.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build and check documents without writing to CouchDB.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_backfill(queue_dir=args.queue_dir, database=args.database, dry_run=args.dry_run)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["failed"] == 0 else 1


def _read_queue_file(queue_path: Path) -> dict[str, Any]:
    with queue_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise BackfillValidationError("queue file root must be an object")
    return payload


def _object_field(payload: dict[str, Any], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise BackfillValidationError(f"{field} must be an object")
    return value


def _required_string(payload: dict[str, Any], path: str) -> str:
    key = path.rsplit(".", 1)[-1]
    value = str(payload.get(key) or "").strip()
    if not value:
        raise BackfillValidationError(f"{path} is required")
    return value


def _required_media_list(payload: dict[str, Any], field: str, *, require_media_type: bool) -> list[dict[str, Any]]:
    value = payload.get(field)
    if not isinstance(value, list) or not value:
        raise BackfillValidationError(f"payload.{field} must be a non-empty list")
    return [_validate_media_record(item, f"payload.{field}[{index}]", require_media_type=require_media_type) for index, item in enumerate(value)]


def _optional_media_list(payload: dict[str, Any], field: str, *, require_media_type: bool) -> list[dict[str, Any]]:
    value = payload.get(field)
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise BackfillValidationError(f"payload.{field} must be a list")
    return [_validate_media_record(item, f"payload.{field}[{index}]", require_media_type=require_media_type) for index, item in enumerate(value)]


def _validate_media_record(item: Any, path: str, *, require_media_type: bool) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise BackfillValidationError(f"{path} must be an object")
    required = ["filename", "mime_type", "stored_path", "upload_id"]
    if require_media_type:
        required.append("media_type")
    for key in required:
        if not str(item.get(key) or "").strip():
            raise BackfillValidationError(f"{path}.{key} is required")
    return dict(item)


def _record_failure(report: dict[str, Any], queue_path: Path, exc: Exception) -> None:
    report["failed"] += 1
    failed_paths = report["failed_paths"]
    if isinstance(failed_paths, list) and len(failed_paths) < FAILED_PATH_LIMIT:
        failed_paths.append(str(queue_path))
    LOGGER.error(json.dumps({"event": "legacy_capture_backfill_error", "path": str(queue_path), "error": str(exc)}, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
