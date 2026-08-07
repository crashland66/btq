from __future__ import annotations

import json
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config

REQUIRED_DATABASES = (
    couchdb_config.DEFAULT_FIELD_CAPTURES_DB,
    couchdb_config.DEFAULT_VOICE_MEMOS_DB,
    couchdb_config.DEFAULT_QUEUE_DB,
    couchdb_config.DEFAULT_VAULT_DB,
    couchdb_config.DEFAULT_PERSONAL_JOURNAL_DB,
)

PHOTO_VISION_MANGO_INDEXES = [
    {"index": {"fields": ["generated_at"]}, "name": "idx-generated", "type": "json"},
    {"index": {"fields": ["site_id", "generated_at"]}, "name": "idx-site-generated", "type": "json"},
    {"index": {"fields": ["quality.severity", "generated_at"]}, "name": "idx-quality-severity-generated", "type": "json"},
    {"index": {"fields": ["status", "generated_at"]}, "name": "idx-status-generated", "type": "json"},
    {"index": {"fields": ["capture_id"]}, "name": "idx-capture-id", "type": "json"},
    {"index": {"fields": ["qc_category", "generated_at"]}, "name": "idx-qc-category-generated", "type": "json"},
    {"index": {"fields": ["vision_category", "generated_at"]}, "name": "idx-vision-category-generated", "type": "json"},
    {"index": {"fields": ["category_agreement", "generated_at"]}, "name": "idx-category-agreement-generated", "type": "json"},
]

VAULT_MANGO_INDEXES = [
    {"index": {"fields": ["type", "status"]}, "name": "idx-type-status", "type": "json"},
]

FIELD_CAPTURE_MANGO_INDEXES = [
    {"index": {"fields": ["type", "status"]}, "name": "idx-type-status", "type": "json"},
]


class CouchDBSetupError(Exception):
    pass


def couchdb_url() -> str:
    return couchdb_config.base_url()


def auth_headers() -> dict[str, str]:
    return dict(couchdb_config.from_env().auth_header())


def request_json(
    method: str,
    path: str,
    *,
    base_url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(f"{base_url.rstrip('/')}/{path.lstrip('/')}", data=data, headers=request_headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError:
        raise
    except (error.URLError, OSError) as exc:
        raise CouchDBSetupError(f"CouchDB request failed: {base_url.rstrip('/')}/{path.lstrip('/')}") from exc
    if not raw:
        return status, None
    try:
        payload_obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBSetupError("CouchDB returned invalid JSON") from exc
    if not isinstance(payload_obj, dict):
        raise CouchDBSetupError("CouchDB returned non-object JSON")
    return status, payload_obj


def verify_connection(base_url: str, headers: dict[str, str]) -> str:
    try:
        status, payload = request_json("GET", "/", base_url=base_url, headers=headers)
    except error.HTTPError as exc:
        raise CouchDBSetupError(f"CouchDB connection failed with HTTP {exc.code}") from exc
    if status != 200 or not isinstance(payload, dict):
        raise CouchDBSetupError(f"CouchDB connection returned unexpected status {status}")
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise CouchDBSetupError("CouchDB root response did not include a version string")
    return version


def create_database(database: str, base_url: str, headers: dict[str, str]) -> str:
    quoted_database = parse.quote(database, safe="")
    try:
        status, _payload = request_json("PUT", quoted_database, base_url=base_url, headers=headers)
    except error.HTTPError as exc:
        if exc.code == 412:
            return "exists"
        raise CouchDBSetupError(f"CouchDB database creation failed for {database} with HTTP {exc.code}") from exc
    if status == 201:
        return "created"
    raise CouchDBSetupError(f"CouchDB database creation returned unexpected status {status} for {database}")


def create_required_databases(base_url: str, headers: dict[str, str]) -> dict[str, str]:
    return {database: create_database(database, base_url, headers) for database in REQUIRED_DATABASES}


def create_mango_index(database: str, index_def: dict[str, Any], base_url: str, headers: dict[str, str]) -> str:
    """Create a Mango index on a database. Returns 'created' or 'exists' (idempotent)."""
    quoted_database = parse.quote(database, safe="")
    path = f"{quoted_database}/_index"
    try:
        status, payload = request_json("POST", path, base_url=base_url, headers=headers, payload=index_def)
    except error.HTTPError as exc:
        raise CouchDBSetupError(f"Mango index creation failed on {database} with HTTP {exc.code}") from exc
    if payload and payload.get("result") in {"created", "exists"}:
        return str(payload["result"])
    if status in {200, 201}:
        return "created"
    raise CouchDBSetupError(f"Mango index creation returned unexpected status {status} on {database}")


def provision_photo_vision_database(base_url: str, headers: dict[str, str]) -> dict[str, object]:
    """Create btq_photo_vision and its Mango indexes. Idempotent."""
    db_name = couchdb_config.DEFAULT_PHOTO_VISION_DB
    db_outcome = create_database(db_name, base_url, headers)
    index_outcomes: dict[str, str] = {}
    for index_def in PHOTO_VISION_MANGO_INDEXES:
        name = str(index_def.get("name") or "")
        index_outcomes[name] = create_mango_index(db_name, index_def, base_url, headers)
    return {"database": db_outcome, "indexes": index_outcomes}


def provision_vault_indexes(base_url: str, headers: dict[str, str]) -> dict[str, str]:
    """Create btq_vault Mango indexes. Idempotent. DB itself is in REQUIRED_DATABASES."""
    db_name = couchdb_config.DEFAULT_VAULT_DB
    index_outcomes: dict[str, str] = {}
    for index_def in VAULT_MANGO_INDEXES:
        name = str(index_def.get("name") or "")
        index_outcomes[name] = create_mango_index(db_name, index_def, base_url, headers)
    return index_outcomes


def provision_field_capture_indexes(base_url: str, headers: dict[str, str]) -> dict[str, str]:
    """Create btq_field_captures Mango indexes. Idempotent. DB itself is in REQUIRED_DATABASES."""
    db_name = couchdb_config.DEFAULT_FIELD_CAPTURES_DB
    index_outcomes: dict[str, str] = {}
    for index_def in FIELD_CAPTURE_MANGO_INDEXES:
        name = str(index_def.get("name") or "")
        index_outcomes[name] = create_mango_index(db_name, index_def, base_url, headers)
    return index_outcomes


def main() -> int:
    config = couchdb_config.from_env()
    base_url = config.base_url
    headers = dict(config.auth_header())
    try:
        version = verify_connection(base_url, headers)
        outcomes = create_required_databases(base_url, headers)
        vault_index_outcomes = provision_vault_indexes(base_url, headers)
        field_capture_index_outcomes = provision_field_capture_indexes(base_url, headers)
        photo_vision_outcome = provision_photo_vision_database(base_url, headers)
    except CouchDBSetupError as exc:
        print(f"error: {exc}")
        return 1
    print(f"CouchDB {version}")
    for database, outcome in outcomes.items():
        print(f"{database}: {outcome}")
    print(f"{couchdb_config.DEFAULT_VAULT_DB} indexes:")
    for name, outcome in vault_index_outcomes.items():
        print(f"  index {name}: {outcome}")
    print(f"{couchdb_config.DEFAULT_FIELD_CAPTURES_DB} indexes:")
    for name, outcome in field_capture_index_outcomes.items():
        print(f"  index {name}: {outcome}")
    pv_db_outcome = photo_vision_outcome.get("database", "")
    print(f"{couchdb_config.DEFAULT_PHOTO_VISION_DB}: {pv_db_outcome}")
    for name, outcome in (photo_vision_outcome.get("indexes") or {}).items():
        print(f"  index {name}: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
