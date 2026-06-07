from __future__ import annotations

import json
from pathlib import Path

import pytest

import btq
from skill_to_queue import SkillQueueValidationError, map_actions_to_queue
from skills import SkillError, extract_structured_actions, queue_preview_markdown, stable_yaml_dump


def test_valid_mapping() -> None:
    actions = [
        {
            "type": "update_file",
            "target": "People/Ada.md",
            "description": "Update Ada trust proof section.",
            "payload": {"change": "Add proof"},
        },
        {
            "type": "add_file",
            "target": "Journal/new.md",
            "description": "Add review note file.",
            "payload": {"body": "Note"},
        },
        {
            "type": "http_call",
            "target": "https://example.invalid/hook",
            "description": "Preview external call.",
            "payload": {"method": "POST"},
        },
    ]

    assert map_actions_to_queue(actions, source="skill:web-review:v2") == [
        {
            "job_type": "update_file",
            "target": "People/Ada.md",
            "payload": {"change": "Add proof"},
            "source": "skill:web-review:v2",
        },
        {
            "job_type": "create_file",
            "target": "Journal/new.md",
            "payload": {"body": "Note"},
            "source": "skill:web-review:v2",
        },
        {
            "job_type": "external_call",
            "target": "https://example.invalid/hook",
            "payload": {"method": "POST"},
            "source": "skill:web-review:v2",
        },
    ]


def test_unknown_action_type() -> None:
    with pytest.raises(SkillQueueValidationError, match="unknown action type"):
        map_actions_to_queue(
            [
                {
                    "type": "rename_file",
                    "target": "People/Ada.md",
                    "description": "Rename Ada note file.",
                    "payload": {},
                }
            ]
        )


def test_malformed_yaml() -> None:
    with pytest.raises(SkillError, match="Invalid structured YAML"):
        extract_structured_actions("actions:\n  - type: [broken\n")


def test_schema_only_actions_heading_is_ignored() -> None:
    text = (
        "## Output Mode: Structured\n\n"
        "### YAML Actions\n\n"
        "actions:\n\n"
        "* type: <create_file|update_file|generate_agents_txt>\n"
        "  target: <path>\n"
    )

    assert extract_structured_actions(text) == []


def test_empty_actions() -> None:
    actions = extract_structured_actions("actions: []\n")

    assert actions == []
    assert map_actions_to_queue(actions, source="skill:web-review:v2") == []
    assert queue_preview_markdown([], warning="structured output did not include actions") == (
        "\n## Queue Preview\n\n"
        "Warning: structured output did not include actions\n\n"
        "- no queue jobs mapped\n"
    )


def test_deterministic_mapping() -> None:
    actions = [
        {
            "type": "update_file",
            "target": "Accounts/example.md",
            "description": "Update example account note.",
            "payload": {"change": "Set fields", "b": 2, "a": 1},
        }
    ]

    first = map_actions_to_queue(actions, source="skill:web-review:v2")
    second = map_actions_to_queue(actions, source="skill:web-review:v2")

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert stable_yaml_dump(first) == stable_yaml_dump(second)


def test_cli_queue_dry_run_preview_with_empty_actions(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("Site notes.\n", encoding="utf-8")

    assert btq.run(["skill", "run", "web-review", "--version", "v2", "--input", str(input_path), "--structured", "--to-queue-dry-run"]) == 0

    out = capsys.readouterr().out
    assert "## Output Mode: Structured" in out
    assert "## Queue Preview" in out
    assert "Warning: structured output did not include actions" in out
    assert "- no queue jobs mapped" in out


def test_cli_out_queue_writes_preview_json(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    queue_path = tmp_path / "queue.json"
    input_path.write_text(
        "actions:\n"
        "  - type: update_file\n"
        "    target: People/Ada.md\n"
        "    description: Update Ada note.\n"
        "    payload:\n"
        "      change: Add proof\n",
        encoding="utf-8",
    )

    assert (
        btq.run(
            [
                "skill",
                "run",
                "web-review",
                "--version",
                "v2",
                "--input",
                str(input_path),
                "--structured",
                "--to-queue-dry-run",
                "--out-queue",
                str(queue_path),
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    assert "- job 1: `update_file` -> `People/Ada.md`" in out
    assert payload == [
        {
            "job_type": "update_file",
            "payload": {"change": "Add proof"},
            "source": "skill:web-review:v2",
            "target": "People/Ada.md",
        }
    ]


def test_cli_action_validation_failure_prints_header(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text(
        "actions:\n"
        "  - type: update_file\n"
        "    target: /\n"
        "    description: improve\n"
        "    payload: {}\n",
        encoding="utf-8",
    )

    assert (
        btq.run(
            [
                "skill",
                "run",
                "web-review",
                "--version",
                "v2",
                "--input",
                str(input_path),
                "--structured",
                "--to-queue-dry-run",
            ]
        )
        == 2
    )

    err = capsys.readouterr().err
    assert "## Action Validation Failed" in err
    assert "description must be at least 10 characters" in err
    assert "target is too generic" in err
