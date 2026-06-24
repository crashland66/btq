from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from io_atomic import atomic_write_text


def read_json_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_object(path: Path, payload: dict[str, object]) -> None:
    write_json_value(path, payload)


def write_json_value(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_json_line(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def resolve_within_root(path: Path, root: Path) -> Path:
    resolved_root = root.expanduser().resolve(strict=False)
    resolved_path = path.expanduser().resolve(strict=False)
    resolved_path.relative_to(resolved_root)
    return resolved_path
