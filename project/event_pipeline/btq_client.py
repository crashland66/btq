from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from event_pipeline import couchdb_config
from event_pipeline.couchdb_queue_reader import (
    QueueReaderConfig,
    load_reader_config,
)
from queue_spec import ALLOWED_JOB_TYPES, ALLOWED_TOP_LEVEL_FIELDS, JOB_SCHEMAS, validate_job


DATABASE_ALIASES = {
    "vault": couchdb_config.DEFAULT_VAULT_DB,
    "queue": couchdb_config.DEFAULT_QUEUE_DB,
    "captures": couchdb_config.DEFAULT_FIELD_CAPTURES_DB,
    "photo_vision": couchdb_config.DEFAULT_PHOTO_VISION_DB,
    "journal": couchdb_config.DEFAULT_PERSONAL_JOURNAL_DB,
    "voice_memos": couchdb_config.DEFAULT_VOICE_MEMOS_DB,
}
ALLOWED_QUEUE_EXTRAS = ALLOWED_TOP_LEVEL_FIELDS - {"job_id", "job_type", "payload"}


class BTQClientError(Exception):
    """Public btq_client error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_database(database: str) -> str:
    return DATABASE_ALIASES.get(database, database)


def get_doc(database: str, doc_id: str) -> dict[str, Any] | None:
    cfg = load_reader_config()
    url = _doc_url(cfg, resolve_database(database), doc_id)
    try:
        return _request_json(cfg, url, method="GET")
    except BTQClientError as exc:
        if exc.code == "not_found":
            return None
        raise


def all_docs(database: str, limit: int | None = None, include_docs: bool = True) -> list[dict[str, Any]]:
    cfg = load_reader_config()
    params: dict[str, str] = {"include_docs": "true" if include_docs else "false"}
    if limit is not None:
        params["limit"] = str(limit)
    url = f"{_db_url(cfg, resolve_database(database))}/_all_docs?{urllib.parse.urlencode(params)}"
    data = _request_json(cfg, url, method="GET")
    docs: list[dict[str, Any]] = []
    for row in data.get("rows", []):
        if isinstance(row, dict) and str(row.get("id", "")).startswith("_design/"):
            continue
        doc = row.get("doc") if include_docs else row
        if isinstance(doc, dict) and not str(doc.get("_id", "")).startswith("_design/"):
            docs.append(doc)
    return docs


def find(
    database: str,
    selector: dict[str, Any],
    fields: list[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    cfg = load_reader_config()
    body: dict[str, Any] = {"selector": selector, "limit": limit}
    if fields is not None:
        body["fields"] = fields
    data = _request_json(cfg, f"{_db_url(cfg, resolve_database(database))}/_find", method="POST", body=body)
    docs = data.get("docs", [])
    return docs if isinstance(docs, list) else []


def job_types() -> dict[str, list[str]]:
    return {job_type: list(JOB_SCHEMAS[job_type]) for job_type in sorted(ALLOWED_JOB_TYPES)}


def job_schema(job_type: str) -> list[str]:
    if job_type not in ALLOWED_JOB_TYPES:
        raise BTQClientError("unknown_job_type", f"unknown job_type: {job_type}")
    return list(JOB_SCHEMAS[job_type])


def diagnose_job(job: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not isinstance(job, dict):
        return ["job must be a JSON object"]

    stray_keys = sorted(set(job) - ALLOWED_TOP_LEVEL_FIELDS)
    for key in stray_keys:
        reasons.append(f"top-level field is not allowed by queue_spec: {key}")

    job_type = job.get("job_type")
    if not isinstance(job_type, str) or not job_type:
        reasons.append("job_type must be a non-empty string")
    elif job_type not in ALLOWED_JOB_TYPES:
        reasons.append(f"unknown job_type: {job_type}")

    payload = job.get("payload")
    if not isinstance(payload, dict):
        reasons.append("payload must be an object")
    elif isinstance(job_type, str) and job_type in ALLOWED_JOB_TYPES:
        for field in JOB_SCHEMAS[job_type]:
            if field not in payload:
                reasons.append(f"payload missing required field for {job_type}: {field}")

    if not validate_job(job) and not reasons:
        reasons.append("job failed queue_spec.validate_job deep validation")
    return reasons


def slugify(text: str, *, max_len: int = 40) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len] or "job"


def derive_job_id(job: dict[str, Any]) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    job_type = job.get("job_type", "unknown")
    payload = job.get("payload") or {}
    payload_for_hash = payload if isinstance(payload, dict) else {}
    h = hashlib.sha256(json.dumps(payload_for_hash, sort_keys=True, default=str).encode()).hexdigest()[:8]
    candidates = [
        payload_for_hash.get("slug"),
        payload_for_hash.get("title"),
        payload_for_hash.get("path"),
        payload_for_hash.get("site_id"),
        payload_for_hash.get("person_id"),
        job_type,
    ]
    slug_source = next((c for c in candidates if isinstance(c, str) and c), job_type)
    return f"{now}__{slugify(slug_source)}__{h}"


def build_queue_doc(job: dict[str, Any], created_by: str = "greg") -> dict[str, Any]:
    if "job_type" not in job:
        raise BTQClientError("invalid_job", "input JSON must have 'job_type'")
    if "payload" not in job:
        raise BTQClientError("invalid_job", "input JSON must have 'payload'")

    job_id = job.get("job_id") or derive_job_id(job)
    created_at = job.get("created_at") or dt.datetime.now(dt.timezone.utc).isoformat()
    doc = {
        "_id": job_id,
        "btq_state": "pending",
        "created_at": created_at,
        "created_by": created_by,
        "job_id": job_id,
        "job_type": job["job_type"],
        "payload": job["payload"],
    }
    for key in ALLOWED_QUEUE_EXTRAS:
        if key in job and key not in doc:
            doc[key] = job[key]
    return doc


def enqueue(
    job: dict[str, Any],
    created_by: str = "greg",
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    reasons = diagnose_job(job)
    if reasons and not force:
        raise BTQClientError("invalid_job", "job failed validation: " + "; ".join(reasons))

    doc = build_queue_doc(job, created_by=created_by)
    if dry_run:
        return doc
    return _put_queue_doc(load_reader_config(), doc)


def _request_json(
    cfg: QueueReaderConfig,
    url: str,
    *,
    method: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {**cfg.couchdb.auth_header(), "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg.couchdb.timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise BTQClientError("not_found", f"CouchDB document not found: {url}") from exc
        raise BTQClientError("couchdb_error", f"HTTP {exc.code} from CouchDB: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BTQClientError("couchdb_unavailable", f"cannot reach CouchDB at {cfg.couchdb.base_url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise BTQClientError("couchdb_error", f"CouchDB returned invalid JSON from {url}: {exc}") from exc


def _put_queue_doc(cfg: QueueReaderConfig, doc: dict[str, Any]) -> dict[str, Any]:
    url = _doc_url(cfg, cfg.queue_database, str(doc["_id"]))
    data = json.dumps(doc).encode("utf-8")
    headers = {**cfg.couchdb.auth_header(), "Accept": "application/json", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=cfg.couchdb.timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 409:
            return {"ok": True, "duplicate": True, "id": doc["_id"], "status": 409}
        raise BTQClientError("couchdb_error", f"PUT {url} -> HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BTQClientError("couchdb_unavailable", f"cannot reach CouchDB at {url}: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise BTQClientError("couchdb_error", f"CouchDB returned invalid JSON from {url}: {exc}") from exc


def _db_url(cfg: QueueReaderConfig, database: str) -> str:
    return f"{cfg.couchdb.base_url}/{urllib.parse.quote(database, safe='')}"


def _doc_url(cfg: QueueReaderConfig, database: str, doc_id: str) -> str:
    return f"{_db_url(cfg, database)}/{urllib.parse.quote(doc_id, safe='')}"
