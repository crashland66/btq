from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class VoiceMemoCouchDBError(Exception):
    pass


@dataclass(frozen=True)
class VoiceMemoCouchDBConfig:
    base_url: str
    username: str
    password: str
    timeout: float
    database: str

    def auth_header(self) -> dict[str, str]:
        if not self.username and not self.password:
            return {}
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}


def voice_memo_config_from_env() -> VoiceMemoCouchDBConfig:
    raw_timeout = os.environ.get("VOICE_COUCHDB_TIMEOUT") or "10.0"
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise VoiceMemoCouchDBError("Invalid VOICE_COUCHDB_TIMEOUT.") from exc
    if timeout <= 0:
        raise VoiceMemoCouchDBError("Invalid VOICE_COUCHDB_TIMEOUT.")
    return VoiceMemoCouchDBConfig(
        base_url=(os.environ.get("VOICE_COUCHDB_URL") or "http://127.0.0.1:5984").rstrip("/"),
        username=os.environ.get("VOICE_COUCHDB_USER") or "",
        password=os.environ.get("VOICE_COUCHDB_PASSWORD") or "",
        timeout=timeout,
        database=os.environ.get("VOICE_COUCHDB_DB") or "btq_voice_memos",
    )


def _get_current_rev(config: VoiceMemoCouchDBConfig, doc_id: str) -> str | None:
    """Return the current _rev of a document, or None if it does not exist."""
    url = f"{config.base_url}/{quote(config.database, safe='')}/{quote(doc_id, safe='')}"
    headers = {"Accept": "application/json", **config.auth_header()}
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
        parsed = json.loads(raw.decode("utf-8"))
        return str(parsed.get("_rev") or "") or None
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {doc_id}: HTTP {exc.code}.") from exc
    except (URLError, OSError) as exc:
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {doc_id}: {exc.__class__.__name__}.") from exc


def get_voice_memo_document(config: VoiceMemoCouchDBConfig, doc_id: str) -> dict | None:
    """Fetch a voice-memo document by _id, returning None for HTTP 404."""
    normalized_doc_id = str(doc_id or "").strip()
    if not normalized_doc_id:
        raise VoiceMemoCouchDBError("CouchDB document _id is required.")
    url = f"{config.base_url}/{quote(config.database, safe='')}/{quote(normalized_doc_id, safe='')}"
    headers = {"Accept": "application/json", **config.auth_header()}
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=config.timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {normalized_doc_id}: HTTP {exc.code}.") from exc
    except (URLError, OSError) as exc:
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {normalized_doc_id}: {exc.__class__.__name__}.") from exc
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {normalized_doc_id}: invalid JSON response.") from exc
    if not isinstance(parsed, dict):
        raise VoiceMemoCouchDBError(f"CouchDB GET failed for {normalized_doc_id}: invalid JSON response.")
    return parsed


def put_capture_document(config: VoiceMemoCouchDBConfig, doc: dict) -> dict:
    """PUT a voice-memo document. Fetches current _rev; retries once on 409 conflict."""
    doc_id = str(doc.get("_id") or "").strip()
    if not doc_id:
        raise VoiceMemoCouchDBError("CouchDB document _id is required.")
    url = f"{config.base_url}/{quote(config.database, safe='')}/{quote(doc_id, safe='')}"
    headers = {"Content-Type": "application/json", **config.auth_header()}

    for attempt in range(1, 3):
        rev = _get_current_rev(config, doc_id)
        doc_to_put = dict(doc)
        if rev:
            doc_to_put["_rev"] = rev

        body = json.dumps(doc_to_put, sort_keys=True).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="PUT")
        try:
            with urlopen(req, timeout=config.timeout) as response:
                response_body = response.read()
                if response.status < 200 or response.status >= 300:
                    raise VoiceMemoCouchDBError(f"CouchDB write failed for {doc_id}: HTTP {response.status}.")
        except HTTPError as exc:
            if exc.code == 409 and attempt < 2:
                continue
            raise VoiceMemoCouchDBError(f"CouchDB write failed for {doc_id}: HTTP {exc.code}.") from exc
        except (URLError, OSError) as exc:
            raise VoiceMemoCouchDBError(f"CouchDB write failed for {doc_id}: {exc.__class__.__name__}.") from exc

        try:
            parsed = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceMemoCouchDBError(f"CouchDB write failed for {doc_id}: invalid JSON response.") from exc
        if not isinstance(parsed, dict):
            raise VoiceMemoCouchDBError(f"CouchDB write failed for {doc_id}: invalid JSON response.")
        return parsed

    raise VoiceMemoCouchDBError(f"CouchDB write exhausted retries for {doc_id}.")


def query_couchdb_find(config: VoiceMemoCouchDBConfig, database: str, selector: dict) -> dict:
    url = f"{config.base_url}/{quote(database, safe='')}/_find"
    body = json.dumps(selector, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json", **config.auth_header()}
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=config.timeout) as response:
            response_body = response.read()
            if response.status < 200 or response.status >= 300:
                raise VoiceMemoCouchDBError(f"CouchDB _find failed for {database}: HTTP {response.status}.")
    except HTTPError as exc:
        raise VoiceMemoCouchDBError(f"CouchDB _find failed for {database}: HTTP {exc.code}.") from exc
    except (URLError, OSError) as exc:
        raise VoiceMemoCouchDBError(f"CouchDB _find failed for {database}: {exc.__class__.__name__}.") from exc
    try:
        parsed = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoiceMemoCouchDBError(f"CouchDB _find failed for {database}: invalid JSON response.") from exc
    if not isinstance(parsed, dict):
        raise VoiceMemoCouchDBError(f"CouchDB _find failed for {database}: invalid JSON response.")
    return parsed
