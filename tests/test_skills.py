from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import btq
import skills
from skills import SkillError, compose_prompt, discover_skills, get_skill, resolve_cli_path, show_skill, validate_skills


def write_skill(root: Path, skill_id: str = "example-skill", metadata: dict | None = None) -> Path:
    skill_root = root / skill_id
    skill_root.mkdir(parents=True)
    payload = {
        "id": skill_id,
        "name": "Example Skill",
        "description": "Example description.",
        "current_version": "v2",
        "versions": ["v1", "v2"],
        "tags": ["example"],
        "inputs": ["markdown"],
        "outputs": ["markdown-review"],
        "created_from": "test",
        "status": "active",
    }
    if metadata:
        payload.update(metadata)
    (skill_root / "skill.json").write_text(json.dumps(payload), encoding="utf-8")
    (skill_root / "v1.md").write_text(
        "---\n"
        f"id: {payload['id']}\n"
        "version: v1\n"
        "description: Example v1.\n"
        "breaking_change: false\n"
        "reason: Test version.\n"
        "structured_output: supported\n"
        "---\n\n"
        "# V1\n",
        encoding="utf-8",
    )
    (skill_root / "v2.md").write_text(
        "---\n"
        f"id: {payload['id']}\n"
        "version: v2\n"
        "description: Example v2.\n"
        "breaking_change: false\n"
        "reason: Test version.\n"
        "structured_output: supported\n"
        "---\n\n"
        "# V2\n",
        encoding="utf-8",
    )
    return skill_root


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_skill_discovery_finds_web_review() -> None:
    skills = discover_skills()

    assert any(skill.skill_id == "web-review" for skill in skills)


def test_metadata_parsing() -> None:
    skill = get_skill("web-review")

    assert skill.metadata["name"] == "Website Review"
    assert skill.metadata["current_version"] == "v2"
    assert "conversion" in skill.metadata["tags"]


def test_current_version_resolution() -> None:
    skill = get_skill("web-review")

    assert skill.prompt_path().name == "v2.md"


def test_explicit_version_resolution() -> None:
    skill = get_skill("web-review")

    assert skill.prompt_path("v1").name == "v1.md"


def test_missing_version_failure() -> None:
    skill = get_skill("web-review")

    with pytest.raises(SkillError, match="does not define version v9"):
        skill.prompt_path("v9")


def test_invalid_skill_json_failure(tmp_path: Path) -> None:
    skill_root = tmp_path / "broken"
    skill_root.mkdir()
    (skill_root / "skill.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SkillError, match="Invalid JSON"):
        discover_skills(tmp_path)


def test_cli_list_behavior(capsys) -> None:
    assert btq.run(["skill", "list"]) == 0

    out = capsys.readouterr().out
    assert "id\tname\tcurrent_version\ttags\tstatus" in out
    assert "web-review\tWebsite Review\tv2" in out


def test_cli_show_behavior(capsys) -> None:
    assert btq.run(["skill", "show", "web-review", "--version", "v2"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "web-review"
    assert payload["selected_version"] == "v2"
    assert payload["prompt_path"] == "project/skills/web-review/v2.md"


def test_show_skill_prints_metadata_and_prompt_path() -> None:
    stdout = io.StringIO()

    show_skill("web-review", version="v1", stdout=stdout)

    payload = json.loads(stdout.getvalue())
    assert payload["selected_version"] == "v1"
    assert payload["prompt_path"] == "project/skills/web-review/v1.md"


def test_cli_run_prompt_composition(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Input Site\nHero says Learn More.\n", encoding="utf-8")

    assert btq.run(["skill", "run", "web-review", "--input", str(input_path)]) == 0

    out = capsys.readouterr().out
    assert out.startswith("## Agents Context (if available)\nNone\n\n")
    assert "# Website Review Skill v2" in out
    assert "---\n\n# Input\n\n# Input Site" in out
    assert "Hero says Learn More." in out


def test_compose_prompt_uses_explicit_version(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("Page notes.\n", encoding="utf-8")

    composed = compose_prompt("web-review", input_path, version="v1")

    assert composed.startswith("## Agents Context (if available)\nNone\n\n# Website Review Skill v1")
    assert "# Input\n\nPage notes." in composed


def test_cli_run_with_out(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    out_path = tmp_path / "composed" / "prompt.md"
    input_path.write_text("Site notes.\n", encoding="utf-8")

    assert btq.run(["skill", "run", "web-review", "--version", "v2", "--input", str(input_path), "--out", str(out_path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "# Website Review Skill v2" in out_path.read_text(encoding="utf-8")
    assert "Site notes." in out_path.read_text(encoding="utf-8")


def test_resolve_cli_path_uses_repo_root_for_relative_paths() -> None:
    assert resolve_cli_path("project/skills/web-review/v2.md").is_file()


def test_validation_success_detects_fixtures() -> None:
    messages = validate_skills(skill_id="web-review")

    assert messages == ["ok web-review: v2, 2 version(s), 2 fixture file(s)"]


def test_valid_agents_txt_ingestion(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("https://example.com\n", encoding="utf-8")

    def fake_urlopen(url: str, timeout: int):
        assert url == "https://example.com/agents.txt"
        assert timeout == 2
        return FakeResponse("site:\n  audience: local service businesses\npriority: high\n")

    monkeypatch.setattr(skills, "urlopen", fake_urlopen)

    composed = compose_prompt("web-review", input_path, version="v2")

    assert composed.startswith(
        "## Agents Context (if available)\npriority: high\nsite:\n  audience: local service businesses\n\n# Website Review Skill v2"
    )


def test_missing_agents_txt_graceful_fallback(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("https://example.com\n", encoding="utf-8")

    def fake_urlopen(_url: str, timeout: int):
        raise OSError("not found")

    monkeypatch.setattr(skills, "urlopen", fake_urlopen)

    assert compose_prompt("web-review", input_path, version="v2").startswith("## Agents Context (if available)\nNone\n\n")


def test_malformed_agents_txt_graceful_fallback(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("https://example.com\n", encoding="utf-8")
    monkeypatch.setattr(skills, "urlopen", lambda _url, timeout: FakeResponse("name: [broken"))

    assert compose_prompt("web-review", input_path, version="v2").startswith("## Agents Context (if available)\nNone\n\n")


def test_agents_context_serialization_is_deterministic() -> None:
    assert skills.agents_context_section({"z": 1, "a": {"b": 2}}) == "## Agents Context (if available)\na:\n  b: 2\nz: 1\n\n"


def test_structured_block_is_appended_when_requested(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("Site notes.\n", encoding="utf-8")

    composed = compose_prompt("web-review", input_path, version="v2", structured=True)

    assert composed.endswith(skills.STRUCTURED_OUTPUT_BLOCK)
    assert "## Output Mode: Structured" in composed


def test_structured_block_is_not_appended_by_default(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("Site notes.\n", encoding="utf-8")

    composed = compose_prompt("web-review", input_path, version="v2")

    assert "## Output Mode: Structured" not in composed


def test_cli_structured_flag_appends_block(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("Site notes.\n", encoding="utf-8")

    assert btq.run(["skill", "run", "web-review", "--version", "v2", "--input", str(input_path), "--structured"]) == 0

    out = capsys.readouterr().out
    assert skills.STRUCTURED_OUTPUT_BLOCK in out
    assert "## Execution Journal" in out


def test_prompt_frontmatter_missing_fields_fail_validation(tmp_path: Path) -> None:
    skill_root = write_skill(tmp_path)
    (skill_root / "v2.md").write_text(
        "---\n"
        "id: example-skill\n"
        "version: v2\n"
        "description: Missing fields.\n"
        "---\n\n"
        "# V2\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillError, match="missing required fields"):
        validate_skills(tmp_path)


def test_prompt_frontmatter_invalid_structured_output_fails_validation(tmp_path: Path) -> None:
    skill_root = write_skill(tmp_path)
    (skill_root / "v2.md").write_text(
        "---\n"
        "id: example-skill\n"
        "version: v2\n"
        "description: Bad enum.\n"
        "breaking_change: false\n"
        "reason: Test bad enum.\n"
        "structured_output: maybe\n"
        "---\n\n"
        "# V2\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillError, match="structured_output must be one of"):
        validate_skills(tmp_path)


def test_prompt_frontmatter_valid_metadata_passes_validation(tmp_path: Path) -> None:
    write_skill(tmp_path)

    assert validate_skills(tmp_path) == ["ok example-skill: v2, 2 version(s), no fixture files"]


def test_composition_is_byte_identical_for_same_inputs(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("https://example.com\n", encoding="utf-8")
    monkeypatch.setattr(skills, "urlopen", lambda _url, timeout: FakeResponse("b: 2\na: 1\n"))

    first = compose_prompt("web-review", input_path, version="v2", structured=True)
    second = compose_prompt("web-review", input_path, version="v2", structured=True)

    assert first.encode("utf-8") == second.encode("utf-8")


def test_validation_failure_for_missing_current_version(tmp_path: Path) -> None:
    write_skill(tmp_path, metadata={"current_version": "v3", "versions": ["v1", "v2", "v3"]})

    with pytest.raises(SkillError, match="version v3 is missing prompt file"):
        validate_skills(tmp_path)


def test_validation_failure_for_duplicate_ids(tmp_path: Path) -> None:
    write_skill(tmp_path, skill_id="first", metadata={"id": "same"})
    write_skill(tmp_path, skill_id="second", metadata={"id": "same"})

    with pytest.raises(SkillError, match="Duplicate skill id same"):
        validate_skills(tmp_path)


def test_validation_failure_for_missing_required_field(tmp_path: Path) -> None:
    write_skill(tmp_path, metadata={"status": ""})

    with pytest.raises(SkillError, match="field status must be a non-empty string"):
        validate_skills(tmp_path)
