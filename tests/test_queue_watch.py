import errno
import logging
import json
import sys
from errno import EDEADLK, EXDEV
from pathlib import Path
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1] / "project"
sys.path.insert(0, str(PROJECT_ROOT))

import io_atomic  # noqa: E402
import queue_processor.main as queue_main  # noqa: E402
from queue_processor import watch  # noqa: E402

pytestmark = pytest.mark.usefixtures("recording_vault_store")


def test_queue_watcher_writes_personal_journal_entry(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    vault_root = tmp_path / "vault"
    personal_vault_root = tmp_path / "personal-vault"
    runtime_root = tmp_path / "runtime"
    local_root = tmp_path / "local"
    logs_dir = tmp_path / "logs"
    for directory in (
        project_root,
        vault_root / "Accounts",
        vault_root / "People",
        vault_root / "Journal",
        personal_vault_root,
        runtime_root / "queue",
        local_root,
        logs_dir / "queue_processor",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(queue_main, "DEFAULT_LOGS_ROOT", logs_dir / "queue_processor")
    monkeypatch.setattr(queue_main, "DEFAULT_PERSONAL_VAULT_ROOT", personal_vault_root)
    job_payload = {
        "job_type": "personal_journal_entry",
        "payload": {
            "date": "2026-04-26",
            "timestamp": "2026-04-26T14:00:00+00:00",
            "audio_file": "personal.m4a",
            "body": "This belongs in personal memory only.",
            "raw_transcript_path": "/tmp/personal.m4a.whisper.txt",
        },
    }
    (runtime_root / "queue" / "job_personal.json").write_text(json.dumps(job_payload) + "\n", encoding="utf-8")

    watch.run_once(
        project_root,
        vault_root,
        runtime_root,
        local_root,
        logs_dir,
        logging.getLogger("test_queue_watch_personal"),
    )

    personal_path = personal_vault_root / "Journal" / "2026-04-26.md"
    text = personal_path.read_text(encoding="utf-8")
    assert "This belongs in personal memory only." in text
    assert "source_audio: personal.m4a" in text
    assert not (vault_root / "Journal" / "2026-04-26.md").exists()
    assert not (runtime_root / "queue" / "job_personal.json").exists()
    assert (runtime_root / "processed" / "job_personal.json").exists()


def test_watch_default_log_path_follows_runtime_root(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    vault_root = tmp_path / "vault"
    runtime_root = tmp_path / "runtime"
    for directory in (project_root, vault_root):
        directory.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Path] = {}

    def fake_configure_logging(log_path: Path) -> logging.Logger:
        captured["log_path"] = log_path
        return logging.getLogger("test_queue_watch_default_log")

    def fake_run_once(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(watch, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(watch, "run_once", fake_run_once)

    result = watch.run(
        [
            "--project-root",
            str(project_root),
            "--vault-root",
            str(vault_root),
            "--runtime-root",
            str(runtime_root),
            "--once",
        ]
    )

    assert result == 0
    assert captured["log_path"] == runtime_root / "logs" / "queue_watch.log"


def test_safe_move_falls_back_on_exdev(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "dst" / "moved.txt"
    source.write_text("hello\n", encoding="utf-8")

    original_replace = io_atomic.os.replace

    def fake_replace(src: Path, dst: Path) -> None:
        if Path(src) == source and Path(dst) == destination:
            raise OSError(EXDEV, "cross-device link")
        return original_replace(src, dst)

    monkeypatch.setattr(io_atomic.os, "replace", fake_replace)

    io_atomic.safe_move(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "hello\n"


def test_safe_move_falls_back_on_icloud_deadlock(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "dst" / "moved.txt"
    source.write_text("hello\n", encoding="utf-8")

    original_replace = io_atomic.os.replace

    def fake_replace(src: Path, dst: Path) -> None:
        if Path(src) == source and Path(dst) == destination:
            raise OSError(EDEADLK, "Resource deadlock avoided")
        return original_replace(src, dst)

    monkeypatch.setattr(io_atomic.os, "replace", fake_replace)

    io_atomic.safe_move(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "hello\n"


def test_safe_move_tolerates_icloud_copystat_deadlock(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "dst" / "moved.txt"
    source.write_text("hello\n", encoding="utf-8")

    original_replace = io_atomic.os.replace

    def fake_replace(src: Path, dst: Path) -> None:
        if Path(src) == source and Path(dst) == destination:
            raise OSError(EXDEV, "cross-device link")
        return original_replace(src, dst)

    def fake_copystat(src: Path, dst: Path, follow_symlinks: bool = True) -> None:
        raise OSError(EDEADLK, "Resource deadlock avoided")

    monkeypatch.setattr(io_atomic.os, "replace", fake_replace)
    monkeypatch.setattr(io_atomic.shutil, "copystat", fake_copystat)

    io_atomic.safe_move(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "hello\n"


def test_copy_then_replace_uses_dd_fallback_on_edeadlk(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "dst" / "moved.txt"
    source.write_text("hello\n", encoding="utf-8")
    original_open = io_atomic.Path.open
    dd_calls: list[list[str]] = []

    def fake_open(self: Path, *args, **kwargs):
        if self == source and args and args[0] == "rb":
            raise OSError(errno.EDEADLK, "deadlock")
        return original_open(self, *args, **kwargs)

    def fake_run(args: list[str], capture_output: bool, text: bool):
        dd_calls.append(args)
        output = Path(args[2].removeprefix("of="))
        output.write_text("hello\n", encoding="utf-8")
        return io_atomic.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(io_atomic.sys, "platform", "darwin")
    monkeypatch.setattr(io_atomic.Path, "open", fake_open)
    monkeypatch.setattr(io_atomic.subprocess, "run", fake_run)

    io_atomic.copy_then_replace(source, destination)

    assert dd_calls
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "hello\n"


def test_copy_then_replace_raises_when_dd_fallback_fails(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "dst" / "moved.txt"
    source.write_text("hello\n", encoding="utf-8")
    original_open = io_atomic.Path.open

    def fake_open(self: Path, *args, **kwargs):
        if self == source and args and args[0] == "rb":
            raise OSError(errno.EDEADLK, "deadlock")
        return original_open(self, *args, **kwargs)

    def fake_run(args: list[str], capture_output: bool, text: bool):
        return io_atomic.subprocess.CompletedProcess(args, 1, "", "dd failed")

    monkeypatch.setattr(io_atomic.sys, "platform", "darwin")
    monkeypatch.setattr(io_atomic.Path, "open", fake_open)
    monkeypatch.setattr(io_atomic.subprocess, "run", fake_run)

    try:
        io_atomic.copy_then_replace(source, destination)
    except OSError as exc:
        assert exc.errno == errno.EDEADLK
    else:
        raise AssertionError("expected EDEADLK")
