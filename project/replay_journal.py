from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from action_validator import ValidationError
from queue_executor import QueueExecutorError, execute_jobs
from skill_to_queue import SkillQueueValidationError, map_actions_to_queue
from skills import SkillError, extract_structured_actions, get_skill, git_code_version, stable_yaml_dump


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReplayError(Exception):
    pass


def resolve_journal_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return REPO_ROOT / candidate


def load_journal(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReplayError(f"Invalid journal JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    if not isinstance(payload, dict):
        raise ReplayError("Journal must be a JSON object.")
    for field in ("skill_id", "version", "raw_output", "mapped_jobs"):
        if field not in payload:
            raise ReplayError(f"Journal is missing required field: {field}")
    return payload


def validate_skill_version(skill_id: str, version: str) -> None:
    try:
        skill = get_skill(skill_id)
        skill.prompt_path(version)
    except SkillError as exc:
        raise ReplayError(str(exc)) from exc


def remap_jobs(journal: dict[str, Any]) -> list:
    try:
        actions = extract_structured_actions(str(journal["raw_output"]))
        return map_actions_to_queue(actions, source=f"skill:{journal['skill_id']}:{journal['version']}")
    except (SkillError, SkillQueueValidationError, ValidationError) as exc:
        raise ReplayError(str(exc)) from exc


def diff_jobs(original: list, remapped: list) -> str:
    original_text = json.dumps(original, indent=2, sort_keys=True)
    remapped_text = json.dumps(remapped, indent=2, sort_keys=True)
    if original_text == remapped_text:
        return "No differences.\n"
    return "Original mapped_jobs:\n" + original_text + "\n\nReplayed mapped_jobs:\n" + remapped_text + "\n"


def replay_journal(
    journal_path: Path,
    execute: bool = False,
    mode: str = "dry-run",
    approve: bool = False,
    diff: bool = False,
    allowed_root: Path | None = None,
    log_path: Path | None = None,
    stdout: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    journal = load_journal(journal_path)
    skill_id = str(journal["skill_id"])
    version = str(journal["version"])
    validate_skill_version(skill_id, version)
    current_code_version = git_code_version()
    journal_code_version = journal.get("code_version")
    if journal_code_version and journal_code_version != current_code_version:
        print(f"Warning: code version changed since journal creation ({journal_code_version} -> {current_code_version}).", file=stdout)
    remapped = remap_jobs(journal)
    print("## Replay", file=stdout)
    print("", file=stdout)
    print(f"- skill: {skill_id}", file=stdout)
    print(f"- version: {version}", file=stdout)
    print(f"- jobs: {len(remapped)}", file=stdout)
    if diff:
        print("", file=stdout)
        print("## Diff", file=stdout)
        print("", file=stdout)
        stdout.write(diff_jobs(journal.get("mapped_jobs", []), remapped))
    if execute:
        print("", file=stdout)
        print("## Execution", file=stdout)
        print("", file=stdout)
        try:
            execute_jobs(remapped, mode=mode, approve=approve, allowed_root=allowed_root, log_path=log_path, stdout=stdout)
        except QueueExecutorError as exc:
            raise ReplayError(str(exc)) from exc
    else:
        print("", file=stdout)
        print("## Dry Run Jobs", file=stdout)
        print("", file=stdout)
        stdout.write(stable_yaml_dump(remapped) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a BTQ execution journal without calling a model.")
    parser.add_argument("journal_file")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--mode", choices=sorted({"dry-run", "approve-required", "auto-safe"}), default="dry-run")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--diff", action="store_true")
    return parser


def run(argv: list[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return replay_journal(
            resolve_journal_path(args.journal_file),
            execute=args.execute,
            mode=args.mode,
            approve=args.approve,
            diff=args.diff,
            stdout=stdout,
        )
    except ReplayError as exc:
        print(f"error: {exc}", file=stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=stderr)
        return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
