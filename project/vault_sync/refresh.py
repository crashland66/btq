from __future__ import annotations

import json
import logging
import os
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_writer import CouchDBCaptureWriterError, put_field_capture_document
from vault_sync.parsing import VaultParsingError, is_person_record, is_site_record, person_id_for, read_frontmatter, site_id_for


DEFAULT_VAULT_ROOT = Path(os.environ.get("BTQ_VAULT_ROOT", str(Path("~/vault").expanduser())))


@dataclass
class Counts:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "failed": self.failed,
        }


@dataclass
class VaultRecord:
    doc_id: str
    doc_type: str
    vault_type: str
    path: Path
    frontmatter: dict[str, Any]


@dataclass
class RefreshReport:
    sites: Counts = field(default_factory=Counts)
    people: Counts = field(default_factory=Counts)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"sites": self.sites.as_dict(), "people": self.people.as_dict(), "errors": self.errors}

    def failed_count(self) -> int:
        return self.sites.failed + self.people.failed


class RefreshError(Exception):
    pass


class CouchDBClient:
    def __init__(self, config: couchdb_config.CouchDBConfig) -> None:
        self.config = config

    def get_doc(self, database: str, doc_id: str) -> dict[str, Any] | None:
        url = f"{self.config.base_url}/{parse.quote(database, safe='')}/{parse.quote(doc_id, safe='')}"
        req = request.Request(url, headers={"Accept": "application/json", **self.config.auth_header()}, method="GET")
        try:
            with request.urlopen(req, timeout=self.config.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RefreshError(f"CouchDB GET failed with HTTP {exc.code} for {database}/{doc_id}") from exc
        except (error.URLError, OSError) as exc:
            raise RefreshError(f"CouchDB GET failed for {database}/{doc_id}") from exc
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise RefreshError(f"CouchDB GET returned non-object for {database}/{doc_id}")
        return parsed

    def put_doc(self, database: str, doc: dict[str, Any]) -> dict[str, Any]:
        try:
            return put_field_capture_document(self.config, doc, database=database)
        except CouchDBCaptureWriterError as exc:
            raise RefreshError(str(exc)) from exc

    def list_vault_docs(self, database: str) -> dict[str, dict[str, Any]]:
        url = f"{self.config.base_url}/{parse.quote(database, safe='')}/_all_docs?include_docs=true"
        req = request.Request(url, headers={"Accept": "application/json", **self.config.auth_header()}, method="GET")
        try:
            with request.urlopen(req, timeout=self.config.timeout) as response:
                raw = response.read()
        except (error.HTTPError, error.URLError, OSError) as exc:
            raise RefreshError(f"CouchDB _all_docs failed for {database}") from exc
        parsed = json.loads(raw.decode("utf-8"))
        docs: dict[str, dict[str, Any]] = {}
        for row in parsed.get("rows", []):
            doc = row.get("doc") if isinstance(row, dict) else None
            if isinstance(doc, dict) and doc.get("synced_from_vault") is True and not str(doc.get("_id", "")).startswith("_design/"):
                docs[str(doc["_id"])] = doc
        return docs


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(args: Any) -> int:
    configure_logging(getattr(args, "log_path", None))
    try:
        report = refresh(args)
    except RefreshError as exc:
        logging.error("%s", exc)
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    payload = report.as_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for label in ("sites", "people"):
            counts = payload[label]
            print(
                f"{label}: created={counts['created']} updated={counts['updated']} "
                f"skipped={counts['skipped']} deleted={counts['deleted']} failed={counts['failed']}"
            )
        for item in payload["errors"]:
            print(f"error: {item}", file=sys.stderr)
    return 1 if report.failed_count() else 0


def refresh(args: Any) -> RefreshReport:
    vault_root = resolve_vault_root(getattr(args, "vault_root", None))
    config = couchdb_config.from_env()
    client = getattr(args, "couchdb_client", None) or CouchDBClient(config)
    now = utc_now_iso()
    sites, people, report = load_vault_records(vault_root)
    if getattr(args, "people_only", False):
        sites = OrderedDict()
    if getattr(args, "sites_only", False):
        people = OrderedDict()
    if getattr(args, "sites_only", False) and getattr(args, "people_only", False):
        raise RefreshError("--sites-only and --people-only cannot be used together")
    if not getattr(args, "people_only", False):
        sync_collection(
            client,
            couchdb_config.sites_database(),
            sites,
            vault_root,
            now,
            report.sites,
            no_prune=getattr(args, "no_prune", False),
            dry_run=getattr(args, "dry_run", False),
        )
    if not getattr(args, "sites_only", False):
        sync_collection(
            client,
            couchdb_config.people_database(),
            people,
            vault_root,
            now,
            report.people,
            no_prune=getattr(args, "no_prune", False),
            dry_run=getattr(args, "dry_run", False),
        )
    return report


def resolve_vault_root(value: object) -> Path:
    root = Path(value).expanduser() if value else DEFAULT_VAULT_ROOT.expanduser()
    resolved = root.resolve(strict=False)
    if not resolved.exists():
        raise RefreshError(f"vault root does not exist: {resolved}")
    return resolved


def load_vault_records(vault_root: Path) -> tuple[OrderedDict[str, VaultRecord], OrderedDict[str, VaultRecord], RefreshReport]:
    sites: OrderedDict[str, VaultRecord] = OrderedDict()
    people: OrderedDict[str, VaultRecord] = OrderedDict()
    paths_by_id: dict[tuple[str, str], Path] = {}
    report = RefreshReport()
    for path in sorted(vault_root.rglob("*.md")):
        if any(part in {".obsidian", ".git"} for part in path.relative_to(vault_root).parts):
            continue
        fm = read_frontmatter(path)
        if fm is None:
            continue
        try:
            if fm.get("type") == "location":
                doc_id = site_id_for(fm)
                add_record(sites, paths_by_id, VaultRecord(doc_id, "site", "location", path, fm))
            elif fm.get("type") == "employee":
                doc_id = person_id_for(fm)
                add_record(people, paths_by_id, VaultRecord(doc_id, "person", "employee", path, fm))
        except VaultParsingError as exc:
            report.errors.append(f"{path}: {exc}")
            if fm.get("type") == "employee":
                report.people.failed += 1
            elif fm.get("type") == "location":
                report.sites.failed += 1
            logging.error("vault parse error %s: %s", path, exc)
    return sites, people, report


def add_record(target: OrderedDict[str, VaultRecord], paths_by_id: dict[tuple[str, str], Path], record: VaultRecord) -> None:
    key = (record.doc_type, record.doc_id)
    if key in paths_by_id:
        raise RefreshError(f"duplicate {record.doc_type} id {record.doc_id}: {paths_by_id[key]} and {record.path}")
    paths_by_id[key] = record.path
    target[record.doc_id] = record


def build_doc(record: VaultRecord, vault_root: Path, synced_at: str) -> dict[str, Any]:
    frontmatter = {key: value for key, value in record.frontmatter.items() if key != "type"}
    return {
        "_id": record.doc_id,
        "type": record.doc_type,
        "vault_type": record.vault_type,
        "vault_path": str(record.path.relative_to(vault_root)),
        "synced_at": synced_at,
        "synced_from_vault": True,
        **frontmatter,
    }


def sync_collection(
    client: Any,
    database: str,
    records: OrderedDict[str, VaultRecord],
    vault_root: Path,
    synced_at: str,
    counts: Counts,
    *,
    no_prune: bool,
    dry_run: bool,
) -> None:
    present_ids = set(records)
    for doc_id, record in records.items():
        doc = build_doc(record, vault_root, synced_at)
        try:
            existing = client.get_doc(database, doc_id)
            if existing is None:
                counts.created += 1
                if not dry_run:
                    client.put_doc(database, doc)
            else:
                if comparable(existing) == comparable(doc):
                    counts.skipped += 1
                else:
                    counts.updated += 1
                    if not dry_run:
                        update_doc = dict(doc)
                        update_doc["_rev"] = existing["_rev"]
                        put_with_conflict_retry(client, database, update_doc)
        except Exception as exc:
            counts.failed += 1
            logging.error("failed syncing %s/%s: %s", database, doc_id, exc)
    if no_prune:
        return
    for doc_id, existing in client.list_vault_docs(database).items():
        if doc_id in present_ids:
            continue
        counts.deleted += 1
        if not dry_run:
            tombstone = {"_id": doc_id, "_rev": existing["_rev"], "_deleted": True}
            try:
                put_with_conflict_retry(client, database, tombstone)
            except Exception as exc:
                counts.failed += 1
                counts.deleted -= 1
                logging.error("failed pruning %s/%s: %s", database, doc_id, exc)


def comparable(doc: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in doc.items() if key not in {"_id", "_rev", "synced_at"}}


def put_with_conflict_retry(client: Any, database: str, doc: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.put_doc(database, doc)
    except Exception as exc:
        if "409" not in str(exc):
            raise
        existing = client.get_doc(database, str(doc["_id"]))
        if existing is None:
            doc.pop("_rev", None)
        else:
            doc["_rev"] = existing["_rev"]
        return client.put_doc(database, doc)


def configure_logging(log_path: Path | str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path:
        path = Path(log_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
