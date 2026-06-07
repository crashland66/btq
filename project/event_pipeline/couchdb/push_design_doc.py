from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from event_pipeline.couchdb import system_defaults


DEFAULT_COUCHDB_URL = "http://127.0.0.1:5984"
DEFAULT_SITES_DB = "btq_sites"
DEFAULT_FIELD_CAPTURES_DB = "btq_field_captures"
DEFAULT_VAULT_DB = "btq_vault"
DESIGN_DOCS = {
    "sites": {
        "database_env": "BTQ_COUCHDB_SITES_DB",
        "default_database": DEFAULT_SITES_DB,
        "path": "design_btq_sites.json",
        "doc_id": "_design/btq_sites",
    },
    "field_captures": {
        "database_env": "BTQ_COUCHDB_FIELD_CAPTURES_DB",
        "default_database": DEFAULT_FIELD_CAPTURES_DB,
        "path": "design_btq_field_captures.json",
        "doc_id": "_design/btq_field_captures",
    },
    "vault": {
        "database_env": "BTQ_COUCHDB_VAULT_DB",
        "default_database": DEFAULT_VAULT_DB,
        "path": "design_btq_vault.json",
        "doc_id": "_design/btq_vault",
    },
}


def couchdb_url() -> str:
    return os.environ.get("BTQ_COUCHDB_URL", DEFAULT_COUCHDB_URL).rstrip("/")


def database_name(design_name: str) -> str:
    design = DESIGN_DOCS[design_name]
    return os.environ.get(str(design["database_env"]), str(design["default_database"]))


def auth_headers() -> dict[str, str]:
    username = os.environ.get("BTQ_COUCHDB_USER", "")
    password = os.environ.get("BTQ_COUCHDB_PASSWORD", "")
    if not username and not password:
        return {}
    credentials = f"{username}:{password}".encode("utf-8")
    return {"Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}"}


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json"}
    headers.update(auth_headers())
    data = None
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{couchdb_url()}/{path.lstrip('/')}", data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, None
        raise
    if not raw:
        return status, None
    return status, json.loads(raw.decode("utf-8"))


def without_rev(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key != "_rev"}


def ensure_database_exists(db_name: str) -> str:
    """Create *db_name* in CouchDB if it does not already exist.

    Returns ``'created'`` when the database was just created or
    ``'exists'`` when it was already present.  Raises on unexpected errors.
    """
    encoded = parse.quote(db_name, safe="")
    status, _ = request_json("GET", encoded)
    if status != 404:
        return "exists"
    try:
        request_json("PUT", encoded)
    except error.HTTPError as exc:
        if exc.code == 412:  # Precondition Failed — created by a concurrent caller
            return "exists"
        raise
    return "created"


def push_design_doc(design_name: str) -> str:
    design = DESIGN_DOCS[design_name]
    design_path = Path(__file__).with_name(str(design["path"]))
    desired = json.loads(design_path.read_text(encoding="utf-8"))
    db_raw = database_name(design_name)
    ensure_database_exists(db_raw)
    database = parse.quote(db_raw, safe="")
    doc_id = parse.quote(str(design["doc_id"]), safe="")

    _status, existing = request_json("GET", f"{database}/{doc_id}")
    if existing is not None and without_rev(existing) == desired:
        return "already up to date"

    merged = dict(desired)
    if existing and existing.get("_rev"):
        merged["_rev"] = existing["_rev"]
        outcome = "updated"
    else:
        outcome = "created"

    request_json("PUT", f"{database}/{doc_id}", merged)
    return outcome


def seed_system_defaults_if_absent() -> str:
    doc = system_defaults.load_system_defaults()
    if doc.get("_rev"):
        return "already exists"
    saved = system_defaults.save_system_defaults(doc, None)
    return "created" if saved.get("_rev") else "created"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Push BTQ CouchDB design documents.")
    parser.add_argument(
        "--design",
        choices=sorted(DESIGN_DOCS),
        default=None,
        help="Push one design document. Defaults to all supported design documents.",
    )
    args = parser.parse_args(argv)
    names = [args.design] if args.design else sorted(DESIGN_DOCS)
    for name in names:
        outcome = push_design_doc(name)
        print(f"{name}: design document {outcome}.")
    outcome = seed_system_defaults_if_absent()
    print(f"system_defaults: document {outcome}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
