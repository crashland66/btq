from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from event_pipeline import couchdb_config


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "btq" / "config.json"


class CouchDBQueueReaderError(Exception):
    """Public queue-reader error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class QueueReaderConfig:
    couchdb: couchdb_config.CouchDBConfig
    queue_database: str = couchdb_config.DEFAULT_QUEUE_DB


def load_reader_config(path: Path | None = DEFAULT_CONFIG_PATH) -> QueueReaderConfig:
    cfg: dict[str, Any] = {}
    path_exists = path is not None and path.exists()
    if path_exists:
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CouchDBQueueReaderError("config_error", f"cannot read queue reader config at {path}: {exc}") from exc

    env_map = {
        "couchdb_url": "BTQ_COUCHDB_URL",
        "couchdb_user": "BTQ_COUCHDB_USER",
        "couchdb_password": "BTQ_COUCHDB_PASSWORD",
        "queue_database": "BTQ_COUCHDB_QUEUE_DB",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    if os.environ.get("BTQ_QUEUE_DATABASE"):
        cfg["queue_database"] = os.environ["BTQ_QUEUE_DATABASE"]

    missing = [key for key in ("couchdb_url", "couchdb_user", "couchdb_password") if not cfg.get(key)]
    if missing:
        source = str(path) if path is not None else "environment"
        raise CouchDBQueueReaderError(
            "config_error",
            f"missing {missing}. Set them in {source} or via BTQ_COUCHDB_URL, BTQ_COUCHDB_USER, and BTQ_COUCHDB_PASSWORD.",
        )

    try:
        couchdb = couchdb_config.from_env(
            base_url_override=cfg.get("couchdb_url"),
            username_override=cfg.get("couchdb_user"),
            password_override=cfg.get("couchdb_password"),
        )
    except couchdb_config.CouchDBConfigError as exc:
        raise CouchDBQueueReaderError("config_error", str(exc)) from exc

    queue_database = str(cfg.get("queue_database") or couchdb_config.DEFAULT_QUEUE_DB)
    return QueueReaderConfig(couchdb=couchdb, queue_database=queue_database)


def list_queue_jobs(
    date: str | None = None,
    since: str | None = None,
    state: str | None = None,
    all_dates: bool = False,
    limit: int | None = None,
    *,
    config: QueueReaderConfig | None = None,
) -> list[dict[str, Any]]:
    docs = _fetch_all_docs(config or load_reader_config())
    filtered = filter_docs(docs, date=None if all_dates else date, since=None if all_dates else since, state=state)
    ordered = sort_docs(filtered)
    if limit is not None:
        return ordered[:limit]
    return ordered


def queue_state(
    states: tuple[str, ...] = ("failed", "pending", "processing", "claimed"),
    recent_limit: int = 10,
    *,
    config: QueueReaderConfig | None = None,
) -> dict[str, Any]:
    docs = sort_docs(_fetch_all_docs(config or load_reader_config()))
    now = dt.datetime.now(dt.timezone.utc)
    result: dict[str, Any] = {"counts": {}, "recent": {}}
    for state in states:
        matches = [doc for doc in docs if doc.get("btq_state") == state]
        result["counts"][state] = len(matches)
        result["recent"][state] = [_summary(doc, now) for doc in matches[-recent_limit:]]
    return result


def get_job(job_id: str, *, config: QueueReaderConfig | None = None) -> dict[str, Any] | None:
    cfg = config or load_reader_config()
    url = _doc_url(cfg, job_id)
    try:
        return _read_json(url, cfg)
    except CouchDBQueueReaderError as exc:
        if exc.code == "not_found":
            return None
        raise


def doc_date(doc: dict[str, Any]) -> str:
    created = str(doc.get("created_at") or "")
    if created[:10].count("-") == 2:
        return created[:10]
    doc_id = str(doc.get("_id") or "")
    if len(doc_id) >= 10 and doc_id[4] == "-" and doc_id[7] == "-":
        return doc_id[:10]
    return ""


def filter_docs(
    docs: list[dict[str, Any]],
    *,
    date: str | None,
    since: str | None,
    state: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in docs:
        d_date = doc_date(doc)
        if date and d_date != date:
            continue
        if since and d_date and d_date < since:
            continue
        if state and doc.get("btq_state") != state:
            continue
        out.append(doc)
    return out


def sort_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(docs, key=lambda doc: (str(doc.get("created_at") or ""), str(doc.get("_id") or "")))


def _fetch_all_docs(cfg: QueueReaderConfig) -> list[dict[str, Any]]:
    url = f"{_db_url(cfg)}/_all_docs?include_docs=true"
    data = _read_json(url, cfg)
    docs: list[dict[str, Any]] = []
    for row in data.get("rows", []):
        doc = row.get("doc")
        if isinstance(doc, dict) and not str(doc.get("_id", "")).startswith("_design/"):
            docs.append(doc)
    return docs


def _summary(doc: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    created_at = doc.get("created_at")
    summary = {
        "id": doc.get("_id"),
        "job_id": doc.get("job_id") or doc.get("_id"),
        "slug": _slug(doc),
        "job_type": doc.get("job_type"),
        "btq_state": doc.get("btq_state"),
        "created_at": created_at,
    }
    age_seconds = _age_seconds(created_at, now)
    if age_seconds is not None:
        summary["age_seconds"] = age_seconds
    return summary


def _slug(doc: dict[str, Any]) -> str | None:
    doc_id = str(doc.get("_id") or "")
    if "__" in doc_id:
        return doc_id.split("__", 1)[1]
    return doc_id or None


def _age_seconds(created_at: object, now: dt.datetime) -> int | None:
    if not isinstance(created_at, str) or not created_at:
        return None
    raw = created_at.replace("Z", "+00:00")
    try:
        created = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return max(0, int((now - created.astimezone(dt.timezone.utc)).total_seconds()))


def _read_json(url: str, cfg: QueueReaderConfig) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={**cfg.couchdb.auth_header(), "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg.couchdb.timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise CouchDBQueueReaderError("not_found", f"CouchDB document not found: {url}") from exc
        raise CouchDBQueueReaderError("couchdb_unavailable", f"HTTP {exc.code} from CouchDB: {body}") from exc
    except urllib.error.URLError as exc:
        raise CouchDBQueueReaderError(
            "couchdb_unavailable",
            f"cannot reach CouchDB at {cfg.couchdb.base_url}: {exc.reason}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise CouchDBQueueReaderError("couchdb_unavailable", f"CouchDB returned invalid JSON from {url}: {exc}") from exc


def _db_url(cfg: QueueReaderConfig) -> str:
    return f"{cfg.couchdb.base_url}/{urllib.parse.quote(cfg.queue_database, safe='')}"


def _doc_url(cfg: QueueReaderConfig, doc_id: str) -> str:
    return f"{_db_url(cfg)}/{urllib.parse.quote(doc_id, safe='')}"
