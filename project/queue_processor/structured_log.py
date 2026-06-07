from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from processing_core.time import utc_now


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        encoded = line.encode("utf-8")
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_event(path: Path, event: str, **fields: Any) -> None:
    payload = {
        "timestamp": utc_now(),
        "event": event,
        **fields,
    }
    append_jsonl(path, payload)
