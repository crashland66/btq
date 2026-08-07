from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config


DOC_ID = "system_defaults"
DOC_TYPE = "system_defaults"
SCHEMA_VERSION = 1
DEFAULT_VOICE_NOTE_FORMULA = "Location. Condition. Action taken or needed."


class SystemDefaultsConflictError(Exception):
    def __init__(self, current_doc: dict[str, Any]) -> None:
        super().__init__("system_defaults was edited elsewhere")
        self.current_doc = current_doc


def default_skeleton() -> dict[str, Any]:
    return {
        "_id": DOC_ID,
        "type": DOC_TYPE,
        "default_vision_context": "",
        "default_capture_guidance": "",
        "default_display_categories": [],
        "default_voice_note_formula": DEFAULT_VOICE_NOTE_FORMULA,
        "default_capture_advice": [],
        "schema_version": SCHEMA_VERSION,
    }


def _config() -> couchdb_config.CouchDBConfig:
    if not os.environ.get("BTQ_COUCHDB_URL"):
        raise couchdb_config.CouchDBConfigError("BTQ_COUCHDB_URL must be set to edit system_defaults")
    return couchdb_config.from_env()


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
    config = _config()
    url = f"{config.base_url}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    headers.update(config.auth_header())
    body = None
    if payload is not None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=config.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, None
        raise
    if not raw:
        return status, None
    return status, json.loads(raw.decode("utf-8"))


def _doc_path() -> str:
    return f"{parse.quote(couchdb_config.vault_database(), safe='')}/{parse.quote(DOC_ID, safe='')}"


def load_system_defaults() -> dict[str, Any]:
    status, payload = request_json("GET", _doc_path())
    if status == 404 or payload is None:
        return default_skeleton()
    return payload


def save_system_defaults(doc: dict[str, Any], rev: str | None) -> dict[str, Any]:
    payload = dict(doc)
    payload["_id"] = DOC_ID
    payload["type"] = DOC_TYPE
    payload["schema_version"] = SCHEMA_VERSION
    if rev:
        payload["_rev"] = rev
    try:
        _status, response = request_json("PUT", _doc_path(), payload)
    except error.HTTPError as exc:
        if exc.code == 409:
            raise SystemDefaultsConflictError(load_system_defaults()) from exc
        raise
    saved = dict(payload)
    if response and response.get("rev"):
        saved["_rev"] = str(response["rev"])
    return saved
