from __future__ import annotations

from pathlib import Path

import pytest

from queue_executor import QueueExecutorError, execute_jobs


def test_safe_job_executes(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "queue_executor.log"

    results = execute_jobs(
        [
            {
                "job_type": "create_file",
                "target": "out/new.md",
                "payload": {"content": "Hello\n"},
                "source": "skill:test:v1",
            }
        ],
        mode="auto-safe",
        allowed_root=tmp_path,
        log_path=log_path,
    )

    assert (tmp_path / "out" / "new.md").read_text(encoding="utf-8") == "Hello\n"
    assert results[0].result == "executed"
    assert '"mode": "auto-safe"' in log_path.read_text(encoding="utf-8")


def test_restricted_job_blocked_without_approval(tmp_path: Path) -> None:
    target = tmp_path / "existing.md"
    target.write_text("old\n", encoding="utf-8")

    results = execute_jobs(
        [
            {
                "job_type": "update_existing_file",
                "target": "existing.md",
                "payload": {"content": "new\n"},
                "source": "skill:test:v1",
            }
        ],
        mode="approve-required",
        approve=False,
        allowed_root=tmp_path,
        log_path=tmp_path / "executor.log",
    )

    assert target.read_text(encoding="utf-8") == "old\n"
    assert results[0].result == "blocked: approval required"


def test_restricted_job_executes_with_approval(tmp_path: Path) -> None:
    target = tmp_path / "existing.md"
    target.write_text("old\n", encoding="utf-8")

    results = execute_jobs(
        [
            {
                "job_type": "update_existing_file",
                "target": "existing.md",
                "payload": {"content": "new\n"},
                "source": "skill:test:v1",
            }
        ],
        mode="approve-required",
        approve=True,
        allowed_root=tmp_path,
        log_path=tmp_path / "executor.log",
    )

    assert target.read_text(encoding="utf-8") == "new\n"
    assert results[0].result == "executed"


def test_unknown_job_rejected(tmp_path: Path) -> None:
    with pytest.raises(QueueExecutorError, match="Unknown job_type"):
        execute_jobs(
            [
                {
                    "job_type": "run_shell",
                    "target": "anything",
                    "payload": {},
                    "source": "skill:test:v1",
                }
            ],
            mode="auto-safe",
            allowed_root=tmp_path,
            log_path=tmp_path / "executor.log",
        )


def test_path_traversal_prevented(tmp_path: Path) -> None:
    with pytest.raises(QueueExecutorError, match="escapes allowed root"):
        execute_jobs(
            [
                {
                    "job_type": "create_file",
                    "target": "../escape.md",
                    "payload": {"content": "nope\n"},
                    "source": "skill:test:v1",
                }
            ],
            mode="auto-safe",
            allowed_root=tmp_path / "sandbox",
            log_path=tmp_path / "executor.log",
        )

    assert not (tmp_path / "escape.md").exists()
