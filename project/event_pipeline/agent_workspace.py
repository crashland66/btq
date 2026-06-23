from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from config import get_config


WORKSPACE_DIRS = ("context", "drafts", "evidence", "handoffs", "runs")
RUN_DIRS = ("evidence", "drafts")
CANONICALITY_RULE = (
    "CouchDB is canonical; this workspace is non-canonical local scratch - never the source of truth; "
    "it may reference CouchDB doc ids / queue job ids."
)


class AgentWorkspaceError(ValueError):
    """Raised when an agent workspace path or operation is invalid."""


class WorkspaceNotInitializedError(AgentWorkspaceError):
    """Raised when a run is requested before init_workspace has prepared the workspace."""


class RunAlreadyExistsError(AgentWorkspaceError):
    """Raised when a requested run bundle already exists."""


def default_base() -> Path:
    return get_config().project_runtime_root


def slugify(value: str, *, label: str = "value") -> str:
    raw = _validate_path_token(value, label=label)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not slug:
        raise AgentWorkspaceError(f"{label} must contain at least one letter or number")
    return slug


def workspace_path(operator: str, project: str, *, base: str | Path | None = None) -> Path:
    """Return the local non-canonical workspace path without creating it."""

    root = _base_path(base)
    operator_slug = slugify(operator, label="operator")
    project_slug = slugify(project, label="project")
    return _confined(root / "agent-workspaces" / operator_slug / project_slug, root)


def init_workspace(operator: str, project: str, *, base: str | Path | None = None) -> Path:
    """Create the workspace directory tree and seed README.md / manifest.json if missing."""

    path = workspace_path(operator, project, base=base)
    path.mkdir(parents=True, exist_ok=True)
    for dirname in WORKSPACE_DIRS:
        (path / dirname).mkdir(exist_ok=True)

    readme_path = path / "README.md"
    if not readme_path.exists():
        readme_path.write_text(_readme_text(operator, project), encoding="utf-8")

    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(
            json.dumps(_manifest(operator, project), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return path


def new_run(
    operator: str,
    project: str,
    slug: str,
    *,
    base: str | Path | None = None,
    now: dt.date | dt.datetime | None = None,
) -> Path:
    """Create a dated run bundle.

    The workspace must already be initialized with init_workspace; this function
    does not auto-init so callers do not accidentally create operator-owned
    scratch roots from a typo.
    """

    root = _base_path(base)
    workspace = workspace_path(operator, project, base=root)
    _require_initialized(workspace)

    run_slug = slugify(slug, label="slug")
    date_text = _date_text(now)
    run_id = f"{date_text}-{run_slug}"
    run_path = _confined(workspace / "runs" / run_id, root)
    if run_path.exists():
        raise RunAlreadyExistsError(f"run already exists: {run_path}")

    run_path.mkdir(parents=True)
    for dirname in RUN_DIRS:
        (run_path / dirname).mkdir()
    (run_path / "README.md").write_text(_run_readme_text(run_id), encoding="utf-8")
    (run_path / "run.json").write_text(
        json.dumps(_run_manifest(operator, project, run_slug, run_id, date_text), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return run_path


def _base_path(base: str | Path | None) -> Path:
    raw = default_base() if base is None else Path(base)
    return raw.expanduser().resolve(strict=False)


def _validate_path_token(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise AgentWorkspaceError(f"{label} must be a string")
    raw = value.strip()
    if not raw:
        raise AgentWorkspaceError(f"{label} must be non-empty")
    if "\x00" in raw:
        raise AgentWorkspaceError(f"{label} must not contain NUL bytes")
    if Path(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
        raise AgentWorkspaceError(f"{label} must not be an absolute path")
    if "/" in raw or "\\" in raw:
        raise AgentWorkspaceError(f"{label} must not contain path separators")
    if ".." in raw:
        raise AgentWorkspaceError(f"{label} must not contain '..'")
    return raw


def _confined(path: Path, base: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise AgentWorkspaceError(f"path escapes base: {resolved}") from exc
    return resolved


def _require_initialized(path: Path) -> None:
    if not path.is_dir():
        raise WorkspaceNotInitializedError(f"workspace is not initialized: {path}")
    missing = [name for name in WORKSPACE_DIRS if not (path / name).is_dir()]
    for filename in ("README.md", "manifest.json"):
        if not (path / filename).is_file():
            missing.append(filename)
    if missing:
        joined = ", ".join(missing)
        raise WorkspaceNotInitializedError(f"workspace is not initialized: {path} (missing {joined})")


def _date_text(now: dt.date | dt.datetime | None) -> str:
    value = dt.date.today() if now is None else now
    if isinstance(value, dt.datetime):
        value = value.date()
    if not isinstance(value, dt.date):
        raise AgentWorkspaceError("now must be a date or datetime")
    return value.isoformat()


def _manifest(operator: str, project: str) -> dict[str, Any]:
    return {
        "canonicality_rule": CANONICALITY_RULE,
        "directories": {
            "context": "Resolved-context snapshots and local analysis notes.",
            "drafts": "Draft queue JSON and other proposed mutations awaiting deterministic handling.",
            "evidence": "Evidence extracts, local source snippets, and supporting artifacts.",
            "handoffs": "Handoff notes for humans or later agent runs.",
            "runs": "Dated run bundles for scoped agent work.",
        },
        "operator": operator,
        "operator_slug": slugify(operator, label="operator"),
        "project": project,
        "project_slug": slugify(project, label="project"),
        "schema_version": 1,
        "workspace_type": "agent-workspace",
    }


def _run_manifest(operator: str, project: str, run_slug: str, run_id: str, date_text: str) -> dict[str, Any]:
    return {
        "canonicality_rule": CANONICALITY_RULE,
        "date": date_text,
        "directories": {
            "drafts": "Draft files scoped to this run.",
            "evidence": "Evidence files scoped to this run.",
        },
        "operator": operator,
        "operator_slug": slugify(operator, label="operator"),
        "project": project,
        "project_slug": slugify(project, label="project"),
        "run_id": run_id,
        "run_slug": run_slug,
        "schema_version": 1,
    }


def _readme_text(operator: str, project: str) -> str:
    operator_slug = slugify(operator, label="operator")
    project_slug = slugify(project, label="project")
    return f"""# Agent Workspace: {operator_slug}/{project_slug}

{CANONICALITY_RULE}

## Directories

- `context/`: resolved-context snapshots and local analysis notes.
- `drafts/`: draft queue JSON and other proposed mutations awaiting deterministic handling.
- `evidence/`: evidence extracts, local source snippets, and supporting artifacts.
- `handoffs/`: handoff notes for humans or later agent runs.
- `runs/`: dated run bundles for scoped agent work.
"""


def _run_readme_text(run_id: str) -> str:
    return f"""# Agent Workspace Run: {run_id}

{CANONICALITY_RULE}

- `evidence/`: evidence files scoped to this run.
- `drafts/`: draft files scoped to this run.
"""
