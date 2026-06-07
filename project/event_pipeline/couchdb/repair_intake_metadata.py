from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error, parse, request

from config import get_config
from event_pipeline import couchdb_config
from processing_core.artifacts import read_json_object, write_json_object


IDENTITY_FIELDS = (
    "site_id",
    "person_id",
    "person_name",
    "field_capture_token_id",
    "field_capture_token_label",
)
LOGGER = logging.getLogger(__name__)
DocFetcher = Callable[[str], Optional[dict[str, Any]]]


class RepairValidationError(Exception):
    pass


def request_json(
    config: couchdb_config.CouchDBConfig,
    method: str,
    path: str,
) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    req = request.Request(f"{config.base_url}/{path.lstrip('/')}", headers=headers, method=method)
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
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RepairValidationError("CouchDB returned non-object JSON")
    return status, payload


def fetch_couchdb_doc(
    config: couchdb_config.CouchDBConfig,
    database: str,
    capture_id: str,
) -> dict[str, Any] | None:
    db = parse.quote(database, safe="")
    quoted_id = parse.quote(capture_id, safe="")
    _status, payload = request_json(config, "GET", f"{db}/{quoted_id}")
    return payload


def run_repair(
    *,
    runtime_root: Path,
    database: str,
    apply: bool = False,
    limit: int = 0,
    config: couchdb_config.CouchDBConfig | None = None,
    fetch_doc: DocFetcher | None = None,
) -> dict[str, int]:
    couch_config = config or couchdb_config.from_env()
    doc_fetcher = fetch_doc or (lambda capture_id: fetch_couchdb_doc(couch_config, database, capture_id))
    intake_dir = runtime_root.expanduser().resolve(strict=False) / "field_capture" / "intake"
    report = {
        "patched": 0,
        "already_complete": 0,
        "no_couchdb_doc": 0,
        "skipped_not_couchdb": 0,
        "errors": 0,
    }
    processed = 0

    for intake_path in sorted(intake_dir.glob("*.json")):
        if limit > 0 and processed >= limit:
            break
        processed += 1
        try:
            intake = read_json_object(intake_path)
            if intake is None:
                raise RepairValidationError("intake JSON is missing, malformed, or not an object")
            metadata = _object_field(intake, "metadata")
            if str(metadata.get("source") or "").strip() != "couchdb":
                report["skipped_not_couchdb"] += 1
                continue
            if _metadata_complete(metadata):
                report["already_complete"] += 1
                continue
            capture_id = str(metadata.get("capture_id") or "").strip()
            if not capture_id:
                raise RepairValidationError("metadata.capture_id is required")
            doc = doc_fetcher(capture_id)
            if doc is None:
                print(f"no CouchDB doc for {capture_id}; skipped")
                report["no_couchdb_doc"] += 1
                continue
            patched_metadata = _patched_metadata(metadata, doc)
            if patched_metadata == metadata:
                report["already_complete"] += 1
                continue
            patched_intake = dict(intake)
            patched_intake["metadata"] = patched_metadata
            if apply:
                write_json_object(intake_path, patched_intake)
                print(f"patched {intake_path}")
            else:
                added = sorted(key for key in IDENTITY_FIELDS if key in patched_metadata and key not in metadata)
                print(f"would patch {intake_path}: add {', '.join(added)}")
            report["patched"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad intake should not abort the repair pass.
            report["errors"] += 1
            LOGGER.error(json.dumps({"event": "intake_metadata_repair_error", "path": str(intake_path), "error": str(exc)}, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    default_config = get_config()
    parser = argparse.ArgumentParser(description="Repair CouchDB-source field-capture intake metadata from CouchDB docs.")
    parser.add_argument("--runtime-root", type=Path, default=default_config.runtime_root)
    parser.add_argument("--couchdb-database", default=couchdb_config.DEFAULT_FIELD_CAPTURES_DB)
    parser.add_argument("--apply", action="store_true", help="Write patched metadata back to intake JSON files.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of intake JSON files to inspect; 0 processes all.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = run_repair(
        runtime_root=args.runtime_root,
        database=args.couchdb_database,
        apply=args.apply,
        limit=args.limit,
    )
    action = "patched" if args.apply else "would patch"
    print(f"{action} {report['patched']} intake JSONs")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["errors"] == 0 else 1


def _object_field(payload: dict[str, object], field: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise RepairValidationError(f"{field} must be an object")
    return value


def _metadata_complete(metadata: dict[str, Any]) -> bool:
    return all(str(metadata.get(field) or "").strip() for field in IDENTITY_FIELDS)


def _patched_metadata(metadata: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    patched = dict(metadata)
    for field in IDENTITY_FIELDS:
        if str(patched.get(field) or "").strip():
            continue
        value = str(doc.get(field) or "").strip()
        if value:
            patched[field] = value
    return patched


if __name__ == "__main__":
    raise SystemExit(main())
