from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
from typing import Any, Callable, Iterable
from urllib import error, request

from btq_vault.couch_store import CouchDBEntityStore
from event_pipeline import couchdb_config


VAULT_PATH_FIELD = "vault_path"


class StripVaultPathError(Exception):
    pass


@dataclass
class StripVaultPathDatabaseResult:
    database: str
    backup_database: str = ""
    matched: int = 0
    changed: int = 0
    skipped_missing: int = 0
    conflicts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "backup_database": self.backup_database,
            "matched": self.matched,
            "changed": self.changed,
            "skipped_missing": self.skipped_missing,
            "conflicts": self.conflicts,
        }


@dataclass
class StripVaultPathResult:
    dry_run: bool
    applied: bool
    databases: list[StripVaultPathDatabaseResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return sum(item.matched for item in self.databases)

    @property
    def changed(self) -> int:
        return sum(item.changed for item in self.databases)

    @property
    def conflicts(self) -> int:
        return sum(item.conflicts for item in self.databases)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "applied": self.applied,
            "matched": self.matched,
            "changed": self.changed,
            "conflicts": self.conflicts,
            "databases": [item.as_dict() for item in self.databases],
            "errors": list(self.errors),
        }


StoreFactory = Callable[[str], Any]
BackupFn = Callable[[str, str], None]


def default_databases() -> tuple[str, ...]:
    return (
        couchdb_config.vault_database(),
        couchdb_config.sites_database(),
        couchdb_config.people_database(),
    )


def backup_database_name(database: str, backup_day: date | None = None) -> str:
    day = backup_day or date.today()
    return f"{database}_bak_{day:%Y%m%d}"


def replicate_database(source_database: str, target_database: str) -> None:
    cfg = couchdb_config.from_env()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(cfg.auth_header())
    payload = {
        "source": source_database,
        "target": target_database,
        "create_target": True,
    }
    req = request.Request(
        f"{cfg.base_url.rstrip('/')}/_replicate",
        data=json.dumps(payload, sort_keys=True).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=cfg.timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise StripVaultPathError(f"backup replicate failed for {source_database}: HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise StripVaultPathError(f"backup replicate failed for {source_database}: {exc}") from exc
    if not 200 <= status < 300:
        raise StripVaultPathError(f"backup replicate failed for {source_database}: HTTP {status}")
    if raw:
        try:
            payload_obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StripVaultPathError(f"backup replicate returned invalid JSON for {source_database}") from exc
        if isinstance(payload_obj, dict) and payload_obj.get("ok") is False:
            raise StripVaultPathError(f"backup replicate returned ok=false for {source_database}")


def strip_vault_path(
    *,
    apply: bool = False,
    databases: Iterable[str] | None = None,
    store_factory: StoreFactory | None = None,
    backup_fn: BackupFn | None = None,
    backup_day: date | None = None,
) -> StripVaultPathResult:
    database_names = tuple(databases or default_databases())
    factory = store_factory or CouchDBEntityStore.for_database_from_env
    result = StripVaultPathResult(dry_run=not apply, applied=apply)

    for database in database_names:
        store = factory(database)
        docs = _docs_with_vault_path(store)
        result.databases.append(
            StripVaultPathDatabaseResult(
                database=database,
                backup_database=backup_database_name(database, backup_day),
                matched=len(docs),
            )
        )

    if not apply:
        return result

    backup = backup_fn or replicate_database
    for item in result.databases:
        backup(item.database, item.backup_database)

    for item in result.databases:
        store = factory(item.database)
        docs = _docs_with_vault_path(store)
        item.matched = len(docs)
        for doc in docs:
            doc_id = str(doc.get("_id") or "").strip()
            if not doc_id:
                item.skipped_missing += 1
                continue
            changed = False

            def pop_vault_path(current: dict[str, Any] | None) -> dict[str, Any] | None:
                nonlocal changed
                if not isinstance(current, dict) or VAULT_PATH_FIELD not in current:
                    return None
                outgoing = dict(current)
                outgoing.pop(VAULT_PATH_FIELD, None)
                changed = True
                return outgoing

            try:
                store.update_doc(
                    doc_id,
                    pop_vault_path,
                    require_existing=False,
                    max_conflict_retries=1,
                )
            except Exception as exc:
                if _is_conflict(exc):
                    item.conflicts += 1
                    continue
                raise
            if changed:
                item.changed += 1
            else:
                item.skipped_missing += 1
    return result


def _docs_with_vault_path(store: Any) -> list[dict[str, Any]]:
    finder = getattr(store, "find_docs_with_field", None)
    if not callable(finder):
        raise StripVaultPathError("store does not support find_docs_with_field")
    return [dict(doc) for doc in finder(VAULT_PATH_FIELD)]


def _is_conflict(exc: Exception) -> bool:
    if isinstance(exc, error.HTTPError) and exc.code == 409:
        return True
    message = str(exc).lower()
    return "conflict" in message or "409" in message
