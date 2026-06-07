from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


TRANSIENT_MOVE_ERRNOS = {errno.EDEADLK, errno.EAGAIN}


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    try:
        with temp_path.open("w", encoding=encoding) as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise


def safe_move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EDEADLK}:
            raise
        copy_then_replace(src, dst)


def copy_then_replace(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path_str = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    temp_path = Path(temp_path_str)
    try:
        try:
            with os.fdopen(fd, "wb") as temp_file, src.open("rb") as source_file:
                fd = -1
                shutil.copyfileobj(source_file, temp_file)
                temp_file.flush()
                os.fsync(temp_file.fileno())
        except OSError as exc:
            if exc.errno not in TRANSIENT_MOVE_ERRNOS or sys.platform != "darwin":
                raise
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            result = subprocess.run(
                ["/bin/dd", f"if={src}", f"of={temp_path}", "bs=1048576"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise exc
            temp_fd = os.open(temp_path, os.O_RDONLY)
            try:
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
        try:
            shutil.copystat(src, temp_path, follow_symlinks=True)
        except OSError as exc:
            if exc.errno not in TRANSIENT_MOVE_ERRNOS:
                raise
        os.replace(temp_path, dst)
        unlink_with_retry(src)
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


def unlink_with_retry(path: Path, attempts: int = 5, retry_base_seconds: float = 0.2) -> None:
    for attempt in range(1, attempts + 1):
        try:
            os.unlink(path)
            return
        except OSError as exc:
            if exc.errno not in TRANSIENT_MOVE_ERRNOS or attempt >= attempts:
                raise
            time.sleep(retry_base_seconds * attempt)
