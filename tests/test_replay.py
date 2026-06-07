from __future__ import annotations

import io
import json
from pathlib import Path

from replay_journal import diff_jobs, remap_jobs, replay_journal, resolve_journal_path


RAW_OUTPUT = (
    "# Review\n\n"
    "actions:\n"
    "  - type: add_file\n"
    "    target: generated/replay.md\n"
    "    description: Create replay artifact.\n"
    "    payload:\n"
    "      content: Replay content\n"
)


def write_journal(path: Path, mapped_jobs: list | None = None) -> Path:
    payload = {
        "timestamp": "2026-05-03T00:00:00Z",
        "skill_id": "web-review",
        "version": "v2",
        "input": "input",
        "agents_context": None,
        "composed_prompt": RAW_OUTPUT,
        "raw_output": RAW_OUTPUT,
        "parsed_actions": [
            {
                "type": "add_file",
                "target": "generated/replay.md",
                "description": "Create replay artifact.",
                "payload": {"content": "Replay content"},
            }
        ],
        "mapped_jobs": mapped_jobs
        if mapped_jobs is not None
        else [
            {
                "job_type": "create_file",
                "target": "generated/replay.md",
                "payload": {"content": "Replay content"},
                "source": "skill:web-review:v2",
            }
        ],
        "execution_mode": "dry-run",
        "execution_results": [],
        "code_version": "test-version",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_replay_produces_identical_jobs(tmp_path: Path) -> None:
    journal = json.loads(write_journal(tmp_path / "journal.json").read_text(encoding="utf-8"))

    assert remap_jobs(journal) == journal["mapped_jobs"]


def test_replay_without_execute_does_nothing(tmp_path: Path) -> None:
    journal_path = write_journal(tmp_path / "journal.json")
    stdout = io.StringIO()

    assert replay_journal(journal_path, stdout=stdout, allowed_root=tmp_path, log_path=tmp_path / "executor.log") == 0

    assert not (tmp_path / "generated" / "replay.md").exists()
    assert "## Dry Run Jobs" in stdout.getvalue()


def test_replay_with_execute_respects_safety_modes(tmp_path: Path) -> None:
    journal_path = write_journal(tmp_path / "journal.json")
    stdout = io.StringIO()

    assert (
        replay_journal(
            journal_path,
            execute=True,
            mode="auto-safe",
            allowed_root=tmp_path,
            log_path=tmp_path / "executor.log",
            stdout=stdout,
        )
        == 0
    )

    assert (tmp_path / "generated" / "replay.md").read_text(encoding="utf-8") == "Replay content"
    assert "-> executed" in stdout.getvalue()


def test_diff_detects_changes(tmp_path: Path) -> None:
    journal = json.loads(
        write_journal(
            tmp_path / "journal.json",
            mapped_jobs=[
                {
                    "job_type": "update_file",
                    "target": "different.md",
                    "payload": {},
                    "source": "skill:web-review:v2",
                }
            ],
        ).read_text(encoding="utf-8")
    )

    diff = diff_jobs(journal["mapped_jobs"], remap_jobs(journal))

    assert "Original mapped_jobs" in diff
    assert "Replayed mapped_jobs" in diff
    assert "different.md" in diff


def test_replay_path_resolution_uses_repo_root() -> None:
    assert str(resolve_journal_path("runtime/journal/example.json")).endswith("/btq/runtime/journal/example.json")
