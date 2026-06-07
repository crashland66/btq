from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urljoin

import yaml

from action_validator import ValidationError
from queue_executor import QueueExecutorError, execute_jobs
from skill_to_queue import SkillQueueValidationError, map_actions_to_queue


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"
DEFAULT_JOURNAL_ROOT = REPO_ROOT / "runtime" / "journal"
REQUIRED_FIELDS = {
    "id",
    "name",
    "description",
    "current_version",
    "versions",
    "tags",
    "inputs",
    "outputs",
    "created_from",
    "status",
}
PROMPT_SEPARATOR = "\n\n---\n\n# Input\n\n"
STRUCTURED_OUTPUT_BLOCK = """\

## Output Mode: Structured

In addition to the normal Markdown output, produce a YAML block with this schema:

actions:
  - type: <action_type>
    target: <file|endpoint|resource>
    description: <short description>
    payload:
      <arbitrary structured fields>

Rules:
- Must be valid YAML
- Must be deterministic
- No commentary inside YAML
- No prose outside defined fields inside YAML block
"""
VERSION_REQUIRED_FIELDS = {
    "id",
    "version",
    "description",
    "breaking_change",
    "reason",
    "structured_output",
}
STRUCTURED_OUTPUT_VALUES = {"supported", "required", "none"}
urlopen = urllib.request.urlopen


class SkillError(Exception):
    pass


@dataclass(frozen=True)
class Skill:
    root: Path
    metadata_path: Path
    metadata: dict[str, Any]

    @property
    def skill_id(self) -> str:
        return str(self.metadata["id"])

    @property
    def current_version(self) -> str:
        return str(self.metadata["current_version"])

    def prompt_path(self, version: str | None = None) -> Path:
        selected = version or self.current_version
        versions = self.metadata.get("versions")
        if selected not in versions:
            raise SkillError(f"Skill {self.skill_id} does not define version {selected}.")
        path = self.root / f"{selected}.md"
        if not path.is_file():
            raise SkillError(f"Skill {self.skill_id} version {selected} is missing prompt file: {path}")
        return path


@dataclass(frozen=True)
class ParsedPrompt:
    metadata: dict[str, Any]
    body: str


@dataclass(frozen=True)
class ComposedSkillRun:
    skill_id: str
    version: str
    input_text: str
    agents_context: Any | None
    composed_prompt: str


def safe_relative(path: Path, root: Path = REPO_ROOT) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def resolve_cli_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return REPO_ROOT / candidate


def is_url(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("http://") or stripped.startswith("https://")


def stable_yaml_dump(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=True, allow_unicode=False, default_flow_style=False).rstrip()


def parse_yaml_document(text: str) -> Any:
    return yaml.safe_load(text)


def parse_prompt_file(path: Path) -> ParsedPrompt:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SkillError(f"Skill prompt {path} is missing YAML frontmatter.")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SkillError(f"Skill prompt {path} has unterminated YAML frontmatter.")
    frontmatter_text = text[4:end]
    try:
        metadata = parse_yaml_document(frontmatter_text)
    except yaml.YAMLError as exc:
        raise SkillError(f"Invalid YAML frontmatter in {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillError(f"Skill prompt {path} frontmatter must be a YAML mapping.")
    missing = sorted(VERSION_REQUIRED_FIELDS - set(metadata))
    if missing:
        raise SkillError(f"Skill prompt {path} frontmatter is missing required fields: {', '.join(missing)}.")
    for field in ("id", "version", "description", "reason", "structured_output"):
        if not isinstance(metadata[field], str) or not metadata[field].strip():
            raise SkillError(f"Skill prompt {path} frontmatter field {field} must be a non-empty string.")
    if not isinstance(metadata["breaking_change"], bool):
        raise SkillError(f"Skill prompt {path} frontmatter field breaking_change must be true or false.")
    if metadata["structured_output"] not in STRUCTURED_OUTPUT_VALUES:
        raise SkillError(
            f"Skill prompt {path} frontmatter field structured_output must be one of: {', '.join(sorted(STRUCTURED_OUTPUT_VALUES))}."
        )
    body = text[end + len("\n---\n") :]
    if body.startswith("\n"):
        body = body[1:]
    return ParsedPrompt(metadata=metadata, body=body)


def agents_txt_url(url: str) -> str:
    return urljoin(url.rstrip("/") + "/", "agents.txt")


def load_agents_context(input_text: str) -> Any | None:
    if not is_url(input_text):
        return None
    try:
        with urlopen(agents_txt_url(input_text.strip()), timeout=2) as response:
            raw = response.read()
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    try:
        parsed = parse_yaml_document(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    return parsed if parsed is not None else None


def agents_context_section(agents_context: Any | None) -> str:
    serialized = "None" if agents_context is None else stable_yaml_dump(agents_context)
    return f"## Agents Context (if available)\n{serialized}\n\n"


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_filename_timestamp(timestamp: str) -> str:
    return timestamp.replace(":", "-")


def git_code_version(repo_root: Path = REPO_ROOT) -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}-dirty" if status else commit


def journal_path_for(timestamp: str, skill_id: str, version: str, journal_root: Path = DEFAULT_JOURNAL_ROOT) -> Path:
    return journal_root / f"{safe_filename_timestamp(timestamp)}__{skill_id}__{version}.json"


def strip_structured_instruction_template(text: str) -> str:
    return text.replace(STRUCTURED_OUTPUT_BLOCK, "")


def extract_yaml_block_starting_at_actions(text: str) -> str | None:
    searchable = strip_structured_instruction_template(text)
    input_marker = "\n# Input\n\n"
    if input_marker in searchable:
        searchable = searchable.rsplit(input_marker, 1)[1]
    lines = searchable.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != "actions:":
            continue
        block = [lines[index]]
        for line in lines[index + 1 :]:
            if line.startswith("#") or line.startswith("## "):
                break
            if line and not line.startswith((" ", "-")):
                break
            block.append(line)
        yaml_block = "\n".join(block).rstrip() + "\n"
        try:
            parsed = parse_yaml_document(yaml_block)
        except yaml.YAMLError:
            return yaml_block
        if isinstance(parsed, dict) and parsed.get("actions") is None:
            continue
        return yaml_block
    return None


def extract_structured_actions(text: str) -> list:
    yaml_block = extract_yaml_block_starting_at_actions(text)
    if yaml_block is None:
        return []
    try:
        parsed = parse_yaml_document(yaml_block)
    except yaml.YAMLError as exc:
        raise SkillError(f"Invalid structured YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillError("Structured YAML must be a mapping with an actions key.")
    actions = parsed.get("actions")
    if actions is None:
        raise SkillError("Structured YAML is missing actions.")
    if not isinstance(actions, list):
        raise SkillError("Structured YAML actions must be a list.")
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise SkillError(f"Structured action {index} must be a mapping.")
        missing = [field for field in ("type", "target", "description", "payload") if field not in action]
        if missing:
            raise SkillError(f"Structured action {index} is missing required fields: {', '.join(missing)}.")
        if not isinstance(action["payload"], dict):
            raise SkillError(f"Structured action {index} payload must be a mapping.")
    return actions


def source_for_skill(skill_id: str, version: str | None, skills_root: Path = DEFAULT_SKILLS_ROOT) -> str:
    skill = get_skill(skill_id, skills_root)
    selected_version = version or skill.current_version
    return f"skill:{skill.skill_id}:{selected_version}"


def queue_preview_markdown(jobs: list[dict[str, Any]], warning: str | None = None) -> str:
    lines = ["", "## Queue Preview", ""]
    if warning:
        lines.append(f"Warning: {warning}")
        lines.append("")
    if not jobs:
        lines.append("- no queue jobs mapped")
        return "\n".join(lines) + "\n"
    for index, job in enumerate(jobs, start=1):
        lines.append(f"- job {index}: `{job['job_type']}` -> `{job['target']}`")
    return "\n".join(lines) + "\n"


def journal_payload(
    timestamp: str,
    run: ComposedSkillRun,
    raw_output: str,
    parsed_actions: list,
    mapped_jobs: list,
    execution_mode: str,
    execution_results: list,
    code_version: str | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "skill_id": run.skill_id,
        "version": run.version,
        "input": run.input_text,
        "agents_context": run.agents_context,
        "composed_prompt": run.composed_prompt,
        "raw_output": raw_output,
        "parsed_actions": parsed_actions,
        "mapped_jobs": mapped_jobs,
        "execution_mode": execution_mode,
        "execution_results": execution_results,
        "code_version": code_version or git_code_version(),
        "raw_output_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
    }


def write_journal(payload: dict[str, Any], journal_root: Path = DEFAULT_JOURNAL_ROOT) -> Path:
    path = journal_path_for(str(payload["timestamp"]), str(payload["skill_id"]), str(payload["version"]), journal_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_metadata(metadata_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON in {metadata_path}: {exc.msg} at line {exc.lineno}, column {exc.colno}.") from exc
    if not isinstance(data, dict):
        raise SkillError(f"Skill metadata must be a JSON object: {metadata_path}")
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        raise SkillError(f"Skill metadata {metadata_path} is missing required fields: {', '.join(missing)}.")
    if not isinstance(data["versions"], list) or not all(isinstance(item, str) for item in data["versions"]):
        raise SkillError(f"Skill metadata {metadata_path} field versions must be a list of strings.")
    if not data["versions"]:
        raise SkillError(f"Skill metadata {metadata_path} field versions must not be empty.")
    if not isinstance(data["current_version"], str):
        raise SkillError(f"Skill metadata {metadata_path} field current_version must be a string.")
    for field in ("tags", "inputs", "outputs"):
        if not isinstance(data[field], list) or not all(isinstance(item, str) for item in data[field]):
            raise SkillError(f"Skill metadata {metadata_path} field {field} must be a list of strings.")
    for field in ("id", "name", "description", "created_from", "status"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise SkillError(f"Skill metadata {metadata_path} field {field} must be a non-empty string.")
    return data


def discover_skills(skills_root: Path = DEFAULT_SKILLS_ROOT) -> list[Skill]:
    if not skills_root.is_dir():
        raise SkillError(f"Skills directory does not exist: {skills_root}")
    skills: list[Skill] = []
    seen_ids: dict[str, Path] = {}
    for child in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        metadata_path = child / "skill.json"
        if not metadata_path.is_file():
            raise SkillError(f"Skill directory is missing skill.json: {child}")
        metadata = load_metadata(metadata_path)
        skill_id = str(metadata["id"])
        if skill_id in seen_ids:
            raise SkillError(f"Duplicate skill id {skill_id}: {seen_ids[skill_id]} and {metadata_path}")
        seen_ids[skill_id] = metadata_path
        skills.append(Skill(root=child, metadata_path=metadata_path, metadata=metadata))
    return skills


def get_skill(skill_id: str, skills_root: Path = DEFAULT_SKILLS_ROOT) -> Skill:
    for skill in discover_skills(skills_root):
        if skill.skill_id == skill_id:
            return skill
    raise SkillError(f"Unknown skill: {skill_id}")


def validate_skills(skills_root: Path = DEFAULT_SKILLS_ROOT, skill_id: str | None = None) -> list[str]:
    skills = discover_skills(skills_root)
    if skill_id is not None:
        skills = [skill for skill in skills if skill.skill_id == skill_id]
        if not skills:
            raise SkillError(f"Unknown skill: {skill_id}")
    messages: list[str] = []
    for skill in skills:
        current_prompt = skill.prompt_path(skill.current_version)
        current_metadata = parse_prompt_file(current_prompt).metadata
        if current_metadata["id"] != skill.skill_id:
            raise SkillError(f"Skill prompt {current_prompt} frontmatter id does not match skill id {skill.skill_id}.")
        if current_metadata["version"] != skill.current_version:
            raise SkillError(f"Skill prompt {current_prompt} frontmatter version does not match current_version {skill.current_version}.")
        for version in skill.metadata["versions"]:
            prompt_path = skill.prompt_path(version)
            version_metadata = parse_prompt_file(prompt_path).metadata
            if version_metadata["id"] != skill.skill_id:
                raise SkillError(f"Skill prompt {prompt_path} frontmatter id does not match skill id {skill.skill_id}.")
            if version_metadata["version"] != version:
                raise SkillError(f"Skill prompt {prompt_path} frontmatter version does not match filename version {version}.")
        fixtures_dir = skill.root / "fixtures"
        fixture_count = len([path for path in fixtures_dir.iterdir() if path.is_file()]) if fixtures_dir.is_dir() else 0
        fixture_note = f"{fixture_count} fixture file(s)" if fixture_count else "no fixture files"
        messages.append(f"ok {skill.skill_id}: {skill.current_version}, {len(skill.metadata['versions'])} version(s), {fixture_note}")
    return messages


def compose_prompt(
    skill_id: str,
    input_path: Path,
    version: str | None = None,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    structured: bool = False,
) -> str:
    return compose_skill_run(skill_id, input_path, version=version, skills_root=skills_root, structured=structured).composed_prompt


def compose_skill_run(
    skill_id: str,
    input_path: Path,
    version: str | None = None,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
    structured: bool = False,
) -> ComposedSkillRun:
    skill = get_skill(skill_id, skills_root)
    selected_version = version or skill.current_version
    prompt = parse_prompt_file(skill.prompt_path(version)).body
    user_input = input_path.read_text(encoding="utf-8")
    agents_context = load_agents_context(user_input)
    composed = agents_context_section(agents_context) + prompt.rstrip() + PROMPT_SEPARATOR + user_input.rstrip() + "\n"
    if structured:
        composed = composed.rstrip() + STRUCTURED_OUTPUT_BLOCK
    return ComposedSkillRun(
        skill_id=skill.skill_id,
        version=selected_version,
        input_text=user_input,
        agents_context=agents_context,
        composed_prompt=composed,
    )


def list_skills(stdout: TextIO | None = None, skills_root: Path = DEFAULT_SKILLS_ROOT) -> None:
    stdout = stdout or sys.stdout
    print("id\tname\tcurrent_version\ttags\tstatus", file=stdout)
    for skill in discover_skills(skills_root):
        metadata = skill.metadata
        print(
            f"{metadata['id']}\t{metadata['name']}\t{metadata['current_version']}\t{', '.join(metadata['tags'])}\t{metadata['status']}",
            file=stdout,
        )


def show_skill(skill_id: str, version: str | None = None, stdout: TextIO | None = None, skills_root: Path = DEFAULT_SKILLS_ROOT) -> None:
    stdout = stdout or sys.stdout
    skill = get_skill(skill_id, skills_root)
    selected_version = version or skill.current_version
    prompt_path = skill.prompt_path(selected_version)
    payload = dict(skill.metadata)
    payload["selected_version"] = selected_version
    payload["prompt_path"] = safe_relative(prompt_path)
    print(json.dumps(payload, indent=2, sort_keys=True), file=stdout)


def run_skill(
    skill_id: str,
    input_path: Path,
    version: str | None = None,
    out_path: Path | None = None,
    structured: bool = False,
    to_queue_dry_run: bool = False,
    out_queue_path: Path | None = None,
    execute: bool = False,
    mode: str = "dry-run",
    approve: bool = False,
    journal_root: Path = DEFAULT_JOURNAL_ROOT,
    stdout: TextIO | None = None,
    skills_root: Path = DEFAULT_SKILLS_ROOT,
) -> None:
    stdout = stdout or sys.stdout
    if not input_path.is_file():
        raise SkillError(f"Input file does not exist: {input_path}")
    skill_run = compose_skill_run(skill_id, input_path, version=version, skills_root=skills_root, structured=structured)
    composed = skill_run.composed_prompt
    preview = ""
    actions: list = []
    jobs: list = []
    execution_results: list[dict[str, Any]] = []
    if to_queue_dry_run:
        if not structured:
            raise SkillError("--to-queue-dry-run requires --structured.")
        actions = extract_structured_actions(composed)
        warning = "structured output did not include actions" if not actions else None
        try:
            jobs = map_actions_to_queue(actions, source_for_skill(skill_id, version, skills_root))
        except SkillQueueValidationError as exc:
            raise SkillError(str(exc)) from exc
        preview = queue_preview_markdown(jobs, warning=warning)
        if out_queue_path is not None:
            out_queue_path.parent.mkdir(parents=True, exist_ok=True)
            out_queue_path.write_text(json.dumps(jobs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if execute:
            preview += "\n## Queue Execution\n\n"
            try:
                from io import StringIO

                execution_stdout = StringIO()
                results = execute_jobs(jobs, mode=mode, approve=approve, stdout=execution_stdout)
                execution_results = [result.to_dict() for result in results]
                preview += execution_stdout.getvalue()
            except QueueExecutorError as exc:
                raise SkillError(str(exc)) from exc
    elif execute:
        raise SkillError("--execute requires --to-queue-dry-run.")
    journal_notice = ""
    if structured:
        timestamp = current_timestamp()
        journal = journal_payload(
            timestamp=timestamp,
            run=skill_run,
            raw_output=composed,
            parsed_actions=actions,
            mapped_jobs=jobs,
            execution_mode=mode if execute else "dry-run",
            execution_results=execution_results,
        )
        journal_path = write_journal(journal, journal_root=journal_root)
        journal_notice = f"\n## Execution Journal\n\n- {safe_relative(journal_path)}\n"
    output = composed + preview
    if journal_notice:
        output += journal_notice
    if out_path is None:
        stdout.write(output)
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List, inspect, validate, and compose versioned BTQ skills.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List available skills.")
    show_parser = subparsers.add_parser("show", help="Show skill metadata and prompt path.")
    show_parser.add_argument("skill_id")
    show_parser.add_argument("--version")
    run_parser = subparsers.add_parser("run", help="Compose a skill prompt with an input file.")
    run_parser.add_argument("skill_id")
    run_parser.add_argument("--version")
    run_parser.add_argument("--input", required=True)
    run_parser.add_argument("--out")
    run_parser.add_argument("--structured", action="store_true")
    run_parser.add_argument("--to-queue-dry-run", action="store_true")
    run_parser.add_argument("--out-queue")
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--mode", choices=sorted({"dry-run", "approve-required", "auto-safe"}), default="dry-run")
    run_parser.add_argument("--approve", action="store_true")
    validate_parser = subparsers.add_parser("validate", help="Validate all skills or one skill.")
    validate_parser.add_argument("skill_id", nargs="?")
    return parser


def run(argv: list[str] | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            list_skills(stdout=stdout)
        elif args.command == "show":
            show_skill(args.skill_id, version=args.version, stdout=stdout)
        elif args.command == "run":
            run_skill(
                args.skill_id,
                resolve_cli_path(args.input),
                version=args.version,
                out_path=resolve_cli_path(args.out) if args.out else None,
                structured=args.structured,
                to_queue_dry_run=args.to_queue_dry_run,
                out_queue_path=resolve_cli_path(args.out_queue) if args.out_queue else None,
                execute=args.execute,
                mode=args.mode,
                approve=args.approve,
                stdout=stdout,
            )
        elif args.command == "validate":
            for message in validate_skills(skill_id=args.skill_id):
                print(message, file=stdout)
        else:
            raise SkillError(f"Unknown skill command: {args.command}")
    except SkillError as exc:
        print(f"error: {exc}", file=stderr)
        return 2
    except ValidationError as exc:
        print("## Action Validation Failed", file=stderr)
        print("", file=stderr)
        for error in exc.errors:
            print(f"- {error}", file=stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=stderr)
        return 2
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
