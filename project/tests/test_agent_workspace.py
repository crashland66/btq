from __future__ import annotations

import datetime as dt
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

from event_pipeline import agent_workspace as aw


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_SCRIPT = REPO_ROOT / "scripts" / "btq-agent-workspace"


def _load_cli():
    loader = SourceFileLoader("btq_agent_workspace_cli", str(CLI_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load_cli()


# ---------------------------------------------------------------------------
# workspace_path / init_workspace
# ---------------------------------------------------------------------------


def test_workspace_path_layout_and_slugify(tmp_path: Path) -> None:
    path = aw.workspace_path("Darren Operator", "JWF Stockholm!", base=tmp_path)
    expected = tmp_path.resolve() / "agent-workspaces" / "darren-operator" / "jwf-stockholm"
    assert path == expected
    # confirms confinement: tmp_path is an ancestor
    assert tmp_path.resolve() in path.parents


def test_init_creates_all_subdirs_and_files(tmp_path: Path) -> None:
    path = aw.init_workspace("Op A", "Proj B", base=tmp_path)
    for sub in ("context", "drafts", "evidence", "handoffs", "runs"):
        assert (path / sub).is_dir(), sub
    assert (path / "README.md").is_file()
    manifest_path = path / "manifest.json"
    assert manifest_path.is_file()
    # No files written outside the temp base.
    assert tmp_path.resolve() in path.parents


def test_manifest_and_readme_assert_non_canonical(tmp_path: Path) -> None:
    path = aw.init_workspace("Op", "Proj", base=tmp_path)
    manifest = json.loads((path / "manifest.json").read_text())
    rule = manifest["canonicality_rule"].lower()
    assert "couchdb is canonical" in rule
    assert "non-canonical" in rule
    assert manifest["operator_slug"] == "op"
    assert manifest["project_slug"] == "proj"
    readme = (path / "README.md").read_text().lower()
    assert "couchdb is canonical" in readme
    assert "non-canonical" in readme


def test_init_idempotent_does_not_clobber(tmp_path: Path) -> None:
    path = aw.init_workspace("Op", "Proj", base=tmp_path)
    sentinel = path / "drafts" / "keep.txt"
    sentinel.write_text("precious", encoding="utf-8")
    # Mutate the seeded README to prove re-init does not overwrite existing files.
    readme = path / "README.md"
    readme.write_text("CUSTOM", encoding="utf-8")

    again = aw.init_workspace("Op", "Proj", base=tmp_path)
    assert again == path
    assert sentinel.read_text() == "precious"
    assert readme.read_text() == "CUSTOM"
    # dirs and manifest still present
    for sub in ("context", "drafts", "evidence", "handoffs", "runs"):
        assert (path / sub).is_dir()
    assert (path / "manifest.json").is_file()


# ---------------------------------------------------------------------------
# new_run
# ---------------------------------------------------------------------------


def test_new_run_deterministic_dated_bundle(tmp_path: Path) -> None:
    aw.init_workspace("Op", "Proj", base=tmp_path)
    fixed = dt.date(2026, 6, 23)
    run_path = aw.new_run("Op", "Proj", "Contract Audit!", base=tmp_path, now=fixed)
    assert run_path.name == "2026-06-23-contract-audit"
    assert run_path.is_dir()
    assert run_path.parent.name == "runs"
    for sub in ("evidence", "drafts"):
        assert (run_path / sub).is_dir()
    run_manifest = json.loads((run_path / "run.json").read_text())
    assert run_manifest["date"] == "2026-06-23"
    assert run_manifest["run_slug"] == "contract-audit"
    assert tmp_path.resolve() in run_path.parents


def test_new_run_accepts_datetime_now(tmp_path: Path) -> None:
    aw.init_workspace("Op", "Proj", base=tmp_path)
    run_path = aw.new_run(
        "Op", "Proj", "x", base=tmp_path, now=dt.datetime(2025, 1, 2, 13, 30)
    )
    assert run_path.name == "2025-01-02-x"


def test_new_run_requires_initialized_workspace(tmp_path: Path) -> None:
    # workspace not yet initialized -> error (executor chose require, not auto-init)
    with pytest.raises(aw.WorkspaceNotInitializedError):
        aw.new_run("Op", "Proj", "slug", base=tmp_path, now=dt.date(2026, 1, 1))
    # and nothing was created under base
    assert not (tmp_path / "agent-workspaces").exists()


def test_new_run_duplicate_rejected(tmp_path: Path) -> None:
    aw.init_workspace("Op", "Proj", base=tmp_path)
    fixed = dt.date(2026, 6, 23)
    aw.new_run("Op", "Proj", "dup", base=tmp_path, now=fixed)
    with pytest.raises(aw.RunAlreadyExistsError):
        aw.new_run("Op", "Proj", "dup", base=tmp_path, now=fixed)


# ---------------------------------------------------------------------------
# Traversal / safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../evil", "..", "/etc/passwd", "a/b", "a\\b", "", "   ", "...", "\x00x"],
)
def test_slugify_rejects_unsafe_tokens(bad: str) -> None:
    with pytest.raises(aw.AgentWorkspaceError):
        aw.slugify(bad)


def test_workspace_path_rejects_traversal_operator(tmp_path: Path) -> None:
    with pytest.raises(aw.AgentWorkspaceError):
        aw.workspace_path("../evil", "proj", base=tmp_path)


def test_workspace_path_rejects_absolute_project(tmp_path: Path) -> None:
    with pytest.raises(aw.AgentWorkspaceError):
        aw.workspace_path("op", "/etc/passwd", base=tmp_path)


def test_new_run_rejects_traversal_slug(tmp_path: Path) -> None:
    aw.init_workspace("Op", "Proj", base=tmp_path)
    with pytest.raises(aw.AgentWorkspaceError):
        aw.new_run("Op", "Proj", "../escape", base=tmp_path, now=dt.date(2026, 1, 1))


def test_any_token_containing_dotdot_rejected(tmp_path: Path) -> None:
    # Any token containing '..' is rejected outright (stricter than slug-and-confine).
    with pytest.raises(aw.AgentWorkspaceError):
        aw.workspace_path("..-op", "proj", base=tmp_path)
    # A safe token that merely slugifies stays confined under base.
    path = aw.workspace_path("Safe Op", "proj", base=tmp_path)
    assert tmp_path.resolve() in path.parents


# ---------------------------------------------------------------------------
# CLI via main([...]) with --base
# ---------------------------------------------------------------------------


def test_cli_init_creates_under_base(tmp_path: Path, capsys) -> None:
    rc = CLI.main(["init", "--operator", "Op", "--project", "Proj", "--base", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    printed = Path(out)
    assert printed.is_dir()
    assert tmp_path.resolve() in printed.parents
    assert (printed / "manifest.json").is_file()


def test_cli_path_text_and_json(tmp_path: Path, capsys) -> None:
    rc = CLI.main(["path", "--operator", "Op", "--project", "Proj", "--base", str(tmp_path), "--format", "text"])
    assert rc == 0
    text_out = capsys.readouterr().out.strip()
    expected = tmp_path.resolve() / "agent-workspaces" / "op" / "proj"
    assert Path(text_out) == expected
    # path subcommand does NOT create anything
    assert not expected.exists()

    rc = CLI.main(["path", "--operator", "Op", "--project", "Proj", "--base", str(tmp_path), "--format", "json"])
    assert rc == 0
    json_out = json.loads(capsys.readouterr().out)
    assert Path(json_out["workspace"]) == expected


def test_cli_new_run(tmp_path: Path, capsys) -> None:
    rc = CLI.main(["init", "--operator", "Op", "--project", "Proj", "--base", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()
    rc = CLI.main(
        ["new-run", "--operator", "Op", "--project", "Proj", "--slug", "audit", "--base", str(tmp_path)]
    )
    assert rc == 0
    run_out = Path(capsys.readouterr().out.strip())
    assert run_out.is_dir()
    assert run_out.parent.name == "runs"
    assert tmp_path.resolve() in run_out.parents


def test_cli_traversal_operator_exits_nonzero(tmp_path: Path, capsys) -> None:
    rc = CLI.main(["path", "--operator", "../evil", "--project", "proj", "--base", str(tmp_path)])
    assert rc != 0
    # nothing written outside base
    assert list(tmp_path.iterdir()) == []


def test_cli_missing_required_arg_nonzero(tmp_path: Path) -> None:
    rc = CLI.main(["path", "--project", "proj", "--base", str(tmp_path)])
    assert rc != 0
