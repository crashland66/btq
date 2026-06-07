from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_KEYS = ("token", "password", "secret")


def redacted_payload(payload: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in payload.items():
        if any(term in key.lower() for term in SENSITIVE_KEYS):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


def append_audit(runtime_root: Path, event: dict[str, object]) -> None:
    log_dir = runtime_root.expanduser().resolve(strict=False) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = event.get("payload")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "route": str(event.get("route") or ""),
        "actor": str(event.get("actor") or "localhost"),
        "payload": redacted_payload(payload if isinstance(payload, dict) else {}),
        "result_summary": str(event.get("result_summary") or ""),
    }
    with (log_dir / "admin_audit.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
