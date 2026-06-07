from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProcessorLockError(RuntimeError):
    """Raised when the single queue processor lock cannot be acquired."""


@dataclass(frozen=True)
class LockInfo:
    pid: int
    hostname: str
    started_at: str
    command: str | None


def lock_path_for(runtime_root: Path) -> Path:
    return runtime_root / "temp" / "processor.lock"


def current_command() -> str | None:
    try:
        return " ".join(sys.argv)
    except Exception:
        return None


def process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    command = result.stdout.strip()
    return command or None


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return {
            "pid": -1,
            "hostname": "unknown",
            "started_at": "unknown",
            "command": None,
            "corrupt": True,
        }
    if not isinstance(payload, dict):
        return None
    return payload


def write_lock_fd(fd: int, info: LockInfo) -> None:
    payload = {
        "pid": info.pid,
        "hostname": info.hostname,
        "started_at": info.started_at,
        "command": info.command,
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    os.write(fd, encoded)
    os.fsync(fd)


class ProcessorLock:
    def __init__(self, runtime_root: Path) -> None:
        self.path = lock_path_for(runtime_root)
        self.info = LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat(),
            command=current_command(),
        )
        self._acquired = False

    def acquire(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        recovered = False
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError as exc:
                existing = read_lock(self.path)
                existing_pid = existing.get("pid") if isinstance(existing, dict) else None
                if isinstance(existing_pid, int) and process_is_alive(existing_pid):
                    command = process_command(existing_pid) or existing.get("command")
                    raise ProcessorLockError(
                        f"Queue processor already running: pid={existing_pid} hostname={existing.get('hostname')} command={command}"
                    ) from exc
                try:
                    self.path.unlink()
                    recovered = True
                except FileNotFoundError:
                    pass
                continue
            try:
                write_lock_fd(fd, self.info)
            finally:
                os.close(fd)
            self._acquired = True
            return "stale_recovered" if recovered else "acquired"

    def release(self) -> None:
        if not self._acquired:
            return
        existing = read_lock(self.path)
        if isinstance(existing, dict) and existing.get("pid") == self.info.pid and existing.get("hostname") == self.info.hostname:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._acquired = False

    def __enter__(self) -> "ProcessorLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.release()
