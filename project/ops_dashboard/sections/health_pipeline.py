from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from btq_vault.projector import _esc
from event_pipeline import couchdb_config
from ops_dashboard.common import render_table
from ops_dashboard.layout import html_page


WATCHER_LABELS = (
    "com.btq.couchdb-field-capture-watcher",
    "com.btq.couchdb-queue-watcher",
    "com.btq.field-capture-pipeline-watcher",
    "com.btq.queue-watch",
)

HEALTHY_REPLICATOR_STATES = {"running", "completed"}
KNOWN_REPLICATOR_STATES = HEALTHY_REPLICATOR_STATES | {"crashing", "failed", "initializing", "unknown"}
PIPELINE_TIMEOUT_SECONDS = 5


def _short_text(value: object, limit: int = 240) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _get_json(config: object, path: str) -> dict[str, object]:
    base_url = str(getattr(config, "base_url", "")).rstrip("/")
    headers = {"Accept": "application/json"}
    auth_header = getattr(config, "auth_header", None)
    if callable(auth_header):
        headers.update(auth_header())
    req = urllib_request.Request(f"{base_url}{path}", headers=headers, method="GET")
    with urllib_request.urlopen(req, timeout=PIPELINE_TIMEOUT_SECONDS) as resp:
        if not 200 <= int(getattr(resp, "status", 0)) < 300:
            raise RuntimeError(f"HTTP {getattr(resp, 'status', 'unknown')}")
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _get_session(config: object) -> dict[str, object]:
    return _get_json(config, "/_session")


def _get_scheduler_docs(config: object) -> dict[str, object]:
    return _get_json(config, "/_scheduler/docs")


def _launchctl_state(label: str) -> dict[str, object]:
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True,
            text=True,
            timeout=PIPELINE_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception:
        return {"label": label, "state": "unknown", "pid": None, "last_exit_code": None}
    if result.returncode != 0:
        return {"label": label, "state": "not loaded", "pid": None, "last_exit_code": None}

    state = "unknown"
    pid: int | None = None
    last_exit_code: int | None = None
    # `launchctl print` emits multiple "state = ..." lines: the top-level
    # daemon state (single-tab indent, e.g. "running") and inner sub-block
    # states (deeper indent, e.g. "active") under endpoints / sockets.
    # We want the daemon's outermost state; take the FIRST match only.
    state_seen = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state =") and not state_seen:
            state = stripped.split("=", 1)[1].strip() or "unknown"
            state_seen = True
        elif stripped.startswith("pid ="):
            try:
                pid = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pid = None
        elif stripped.startswith("last exit code ="):
            try:
                last_exit_code = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                last_exit_code = None
    return {"label": label, "state": state, "pid": pid, "last_exit_code": last_exit_code}


def _couchdb_reach(config: object) -> dict[str, object]:
    started = time.perf_counter()
    base_url = str(getattr(config, "base_url", couchdb_config.DEFAULT_COUCHDB_URL)).rstrip("/")
    try:
        _get_session(config)
    except urllib_error.HTTPError as exc:
        return {
            "reachable": False,
            "url": base_url,
            "round_trip_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "reachable": False,
            "url": base_url,
            "round_trip_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": _short_text(exc),
        }
    return {
        "reachable": True,
        "url": base_url,
        "round_trip_ms": round((time.perf_counter() - started) * 1000, 1),
        "error": "",
    }


def _replicator_row(doc: dict[str, object]) -> dict[str, object]:
    info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
    raw_state = str(doc.get("state") or "unknown")
    state = raw_state if raw_state in KNOWN_REPLICATOR_STATES else "unknown"
    last_error = doc.get("last_error") or info.get("last_error") or ""
    if isinstance(last_error, dict):
        last_error = last_error.get("reason") or last_error.get("message") or json.dumps(last_error, sort_keys=True)
    return {
        "doc_id": str(doc.get("doc_id") or doc.get("id") or doc.get("_id") or ""),
        "source": str(doc.get("source") or info.get("source") or ""),
        "target": str(doc.get("target") or info.get("target") or ""),
        "state": state,
        "error": _short_text(last_error),
        "error_count": int(doc.get("error_count") or info.get("error_count") or 0),
        "last_updated": str(doc.get("last_updated") or info.get("last_updated") or ""),
    }


def _replicator_rows(config: object) -> list[dict[str, object]]:
    payload = _get_scheduler_docs(config)
    docs = payload.get("docs") if isinstance(payload.get("docs"), list) else []
    return [_replicator_row(doc) for doc in docs if isinstance(doc, dict)]


def pipeline_status(runtime_root: Path) -> dict[str, object]:
    _ = runtime_root
    try:
        config = couchdb_config.from_env()
    except Exception as exc:
        config = couchdb_config.CouchDBConfig(
            base_url=couchdb_config.DEFAULT_COUCHDB_URL,
            username="",
            password="",
            timeout=PIPELINE_TIMEOUT_SECONDS,
            heartbeat_ms=couchdb_config.DEFAULT_HEARTBEAT_MS,
        )
        couchdb = {"reachable": False, "url": config.base_url, "round_trip_ms": None, "error": _short_text(exc)}
        replicators: list[dict[str, object]] = []
    else:
        couchdb = _couchdb_reach(config)
        try:
            replicators = _replicator_rows(config)
        except Exception:
            replicators = []

    watchers = [_launchctl_state(label) for label in WATCHER_LABELS]

    failing: list[str] = []
    if not couchdb["reachable"]:
        failing.append(f"couchdb: {couchdb['error'] or 'unreachable'}")
    for replicator in replicators:
        state = str(replicator.get("state") or "unknown")
        if state not in HEALTHY_REPLICATOR_STATES:
            failing.append(f"replicator {replicator.get('doc_id') or '(unknown)'}: {state}")
    for watcher in watchers:
        state = str(watcher.get("state") or "unknown")
        if state != "running":
            failing.append(f"watcher {watcher.get('label')}: {state}")

    summary = {
        "ok": bool(couchdb["reachable"]) and not failing,
        "failing": failing,
        "replicator_count": len(replicators),
        "running_count": sum(1 for item in replicators if item.get("state") == "running"),
        "watcher_count": len(watchers),
        "watcher_running_count": sum(1 for item in watchers if item.get("state") == "running"),
    }
    return {"couchdb": couchdb, "replicators": replicators, "watchers": watchers, "summary": summary}


def _state_class(state: object) -> str:
    state_text = str(state or "")
    return "" if state_text in HEALTHY_REPLICATOR_STATES or state_text == "running" else "color:#b42318;font-weight:600"


def _render_html(status: dict[str, object]) -> str:
    summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
    failing = summary.get("failing") if isinstance(summary.get("failing"), list) else []
    if summary.get("ok"):
        banner = '<section><h1>Pipeline OK</h1><p class="zero-state">All monitored pipeline components are healthy.</p></section>'
    else:
        items = "".join(f"<li>{_esc(item)}</li>" for item in failing)
        banner = f'<section><h1>Pipeline Health</h1><h2 style="color:#b42318">Pipeline failures</h2><ul>{items}</ul></section>'

    couchdb = status.get("couchdb") if isinstance(status.get("couchdb"), dict) else {}
    couchdb_table = render_table(
        [couchdb],
        [
            {"key": "url", "label": "URL"},
            {"key": "reachable", "label": "Reachable"},
            {"key": "round_trip_ms", "label": "Round trip ms"},
            {"key": "error", "label": "Error"},
        ],
    )

    def source_formatter(value: object, _item: dict[str, object]) -> str:
        return _esc(_short_text(value, 80))

    def state_formatter(value: object, _item: dict[str, object]) -> str:
        return f'<span style="{_state_class(value)}">{_esc(value)}</span>'

    def error_formatter(value: object, _item: dict[str, object]) -> str:
        error = str(value or "")
        if not error:
            return ""
        return f"<details><summary>{_esc(_short_text(error, 80))}</summary><p>{_esc(error)}</p></details>"

    replicators = status.get("replicators") if isinstance(status.get("replicators"), list) else []
    replicator_table = render_table(
        replicators,
        [
            {"key": "doc_id", "label": "Doc id"},
            {"key": "source", "label": "Source", "format": source_formatter, "priority": 2},
            {"key": "target", "label": "Target", "format": source_formatter, "priority": 2},
            {"key": "state", "label": "State", "format": state_formatter},
            {"key": "error_count", "label": "Error count"},
            {"key": "error", "label": "Last error", "format": error_formatter},
            {"key": "last_updated", "label": "Last updated", "priority": 2},
        ],
        empty_text="No replicator scheduler docs found.",
    )

    watchers = status.get("watchers") if isinstance(status.get("watchers"), list) else []
    watcher_table = render_table(
        watchers,
        [
            {"key": "label", "label": "Label"},
            {"key": "state", "label": "State", "format": state_formatter},
            {"key": "pid", "label": "PID"},
            {"key": "last_exit_code", "label": "Last exit code"},
        ],
        empty_text="No watchers configured.",
    )

    return (
        banner
        + f"<section><h2>CouchDB</h2>{couchdb_table}</section>"
        + f"<section><h2>Replicators</h2>{replicator_table}</section>"
        + f"<section><h2>Watchers</h2>{watcher_table}</section>"
    )


def render(request_ctx: object) -> str:
    runtime_root = getattr(request_ctx, "runtime_root", Path("."))
    status = pipeline_status(runtime_root)
    body = _render_html(status)
    return html_page("Pipeline Health — BTQ Ops", body, active_section="health")
