from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


DEFAULT_DATABASE = "btq_field_captures"
DEFAULT_DOC_ID = "btq_field_captures_vps_to_dell"
IGNORED_EXISTING_FIELDS = {"_rev", "_replication_state", "_replication_state_time", "_replication_id"}

REPLICATION_DATABASES: tuple[str, ...] = (
    "btq_field_captures",
    "btq_people",
    "btq_photo_vision",
    "btq_queue",
    "btq_sites",
    "btq_vault",
    "btq_voice_memos",
)


@dataclass(frozen=True)
class CouchDBPeer:
    label: str
    url: str
    user: str
    password: str


LEGACY_REPLICATION_SPECS: list[tuple[str, bool]] = [
    (database, True) for database in REPLICATION_DATABASES
]


class CouchDBReplicationError(Exception):
    pass


def basic_auth_header(username: str, password: str) -> str:
    credentials = f"{username}:{password}".encode("utf-8")
    return f"Basic {base64.b64encode(credentials).decode('ascii')}"


def request_headers(username: str, password: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if username or password:
        headers["Authorization"] = basic_auth_header(username, password)
    return headers


def request_json(
    method: str,
    url: str,
    *,
    username: str,
    password: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    headers = request_headers(username, password)
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, None
        raise
    except (error.URLError, OSError) as exc:
        raise CouchDBReplicationError(f"CouchDB replication request failed: {url}") from exc
    if not raw:
        return status, None
    try:
        payload_obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CouchDBReplicationError("CouchDB returned invalid JSON") from exc
    if not isinstance(payload_obj, dict):
        raise CouchDBReplicationError("CouchDB returned non-object JSON")
    return status, payload_obj


def database_url(base_url: str, database: str) -> str:
    return f"{base_url.rstrip('/')}/{parse.quote(database, safe='')}"


def replicator_doc_url(base_url: str, doc_id: str) -> str:
    return f"{base_url.rstrip('/')}/_replicator/{parse.quote(doc_id, safe='')}"


def build_replication_doc(
    source_url: str,
    source_user: str,
    source_password: str,
    target_url: str,
    target_user: str,
    target_password: str,
    database: str = DEFAULT_DATABASE,
    doc_id: str = DEFAULT_DOC_ID,
) -> dict[str, Any]:
    return {
        "_id": doc_id,
        "source": {
            "url": database_url(source_url, database),
            "headers": {"Authorization": basic_auth_header(source_user, source_password)},
        },
        "target": {
            "url": database_url(target_url, database),
            "headers": {"Authorization": basic_auth_header(target_user, target_password)},
        },
        "continuous": True,
        "create_target": False,
    }


def comparable_doc(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key not in IGNORED_EXISTING_FIELDS}


def setup_replication(
    source_url: str,
    source_user: str,
    source_password: str,
    target_url: str,
    target_user: str,
    target_password: str,
) -> str:
    desired = build_replication_doc(source_url, source_user, source_password, target_url, target_user, target_password)
    doc_url = replicator_doc_url(source_url, str(desired["_id"]))
    try:
        _status, existing = request_json("GET", doc_url, username=source_user, password=source_password)
    except error.HTTPError as exc:
        raise CouchDBReplicationError(f"CouchDB replication GET failed with HTTP {exc.code}") from exc
    if existing is not None and comparable_doc(existing) == desired:
        return "exists"

    payload = dict(desired)
    outcome = "created"
    if existing and existing.get("_rev"):
        payload["_rev"] = existing["_rev"]
        outcome = "updated"

    try:
        request_json("PUT", doc_url, username=source_user, password=source_password, payload=payload)
    except error.HTTPError as exc:
        raise CouchDBReplicationError(f"CouchDB replication PUT failed with HTTP {exc.code}") from exc
    return outcome


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise CouchDBReplicationError(f"{name} is required")
    return value


def setup_all_replications(
    pro_url: str,
    pro_user: str,
    pro_password: str,
    vps_url: str,
    vps_user: str,
    vps_password: str,
) -> dict[str, str]:
    """Set up bidirectional Pro/VPS replication for every BTQ database."""
    return setup_mesh_replications(
        [
            CouchDBPeer("pro", pro_url, pro_user, pro_password),
            CouchDBPeer("vps", vps_url, vps_user, vps_password),
        ]
    )


def setup_mesh_replications(peers: list[CouchDBPeer], databases: tuple[str, ...] = REPLICATION_DATABASES) -> dict[str, str]:
    """Set up continuous ordered-pair replication between every peer."""
    outcomes: dict[str, str] = {}
    for source in peers:
        for target in peers:
            if source.label == target.label:
                continue
            for database in databases:
                doc_id = f"{database}_{source.label}_to_{target.label}"
                outcomes[doc_id] = setup_replication(
                    source_url=source.url,
                    source_user=source.user,
                    source_password=source.password,
                    target_url=target.url,
                    target_user=target.user,
                    target_password=target.password,
                    database=database,
                    doc_id=doc_id,
                )
    return outcomes


def optional_peer(label: str, url_env: str, user_env: str, password_env: str) -> CouchDBPeer | None:
    values = [os.environ.get(url_env, ""), os.environ.get(user_env, ""), os.environ.get(password_env, "")]
    if not any(values):
        return None
    if not all(values):
        missing = [name for name, value in ((url_env, values[0]), (user_env, values[1]), (password_env, values[2])) if not value]
        raise CouchDBReplicationError(f"{', '.join(missing)} required when configuring {label} replication peer")
    return CouchDBPeer(label, values[0], values[1], values[2])


def main() -> int:
    try:
        pro_url = required_env("BTQ_PRO_COUCHDB_URL")
        pro_user = required_env("BTQ_PRO_COUCHDB_USER")
        pro_password = required_env("BTQ_PRO_COUCHDB_PASSWORD")
        vps_url = required_env("BTQ_VPS_COUCHDB_URL")
        vps_user = required_env("BTQ_VPS_COUCHDB_USER")
        vps_password = required_env("BTQ_VPS_COUCHDB_PASSWORD")
        peers = [
            CouchDBPeer("pro", pro_url, pro_user, pro_password),
            CouchDBPeer("vps", vps_url, vps_user, vps_password),
        ]
        dell = optional_peer("dell", "BTQ_DELL_COUCHDB_URL", "BTQ_DELL_COUCHDB_USER", "BTQ_DELL_COUCHDB_PASSWORD")
        if dell is not None:
            peers.append(dell)
        outcomes = setup_mesh_replications(peers)
    except CouchDBReplicationError as exc:
        print(f"error: {exc}")
        return 1
    for doc_id, outcome in outcomes.items():
        print(f"{doc_id}: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
