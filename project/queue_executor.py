from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_PATH = REPO_ROOT / "runtime" / "logs" / "queue_executor.log"
MODES = {"dry-run", "approve-required", "auto-safe"}
SAFE_JOB_TYPES = {"create_file", "update_file", "generate_agents_txt"}
RESTRICTED_JOB_TYPES = {"update_existing_file", "delete_file", "external_call"}


class QueueExecutorError(Exception):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    job_type: str
    target: str
    classification: str
    result: str

    def to_dict(self) -> dict[str, str]:
        return {
            "job_type": self.job_type,
            "target": self.target,
            "classification": self.classification,
            "result": self.result,
        }


def classify_job(job: dict[str, Any]) -> str:
    job_type = job.get("job_type")
    if job_type in SAFE_JOB_TYPES:
        return "safe"
    if job_type in RESTRICTED_JOB_TYPES:
        return "restricted"
    raise QueueExecutorError(f"Unknown job_type: {job_type}")


def content_from_payload(payload: dict[str, Any]) -> str:
    for key in ("content", "text", "body"):
        if key in payload:
            value = payload[key]
            return value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True) + "\n"
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def validate_target_path(target: str, allowed_root: Path) -> Path:
    if not isinstance(target, str) or not target.strip():
        raise QueueExecutorError("Job target must be a non-empty string.")
    root = allowed_root.resolve()
    candidate = Path(target)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise QueueExecutorError(f"Job target escapes allowed root: {target}") from exc
    return resolved


def write_log(job: dict[str, Any], result: ExecutionResult, mode: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "job": job,
        "result": result.to_dict(),
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def execute_one(job: dict[str, Any], mode: str, approve: bool, allowed_root: Path) -> ExecutionResult:
    if not isinstance(job, dict):
        raise QueueExecutorError("Each job must be a mapping.")
    job_type = job.get("job_type")
    target = job.get("target")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise QueueExecutorError(f"Job {job_type} payload must be a mapping.")
    classification = classify_job(job)
    if mode == "dry-run":
        return ExecutionResult(str(job_type), str(target), classification, "dry-run")
    if classification == "restricted":
        if job_type == "external_call":
            return ExecutionResult(str(job_type), str(target), classification, "blocked: external_call is never executed")
        if mode != "approve-required" or not approve:
            return ExecutionResult(str(job_type), str(target), classification, "blocked: approval required")
    if mode == "approve-required" and not approve:
        return ExecutionResult(str(job_type), str(target), classification, "blocked: approval required")
    path = validate_target_path(str(target), allowed_root)
    if job_type == "create_file":
        if path.exists():
            raise QueueExecutorError(f"create_file refuses to overwrite existing file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content_from_payload(payload), encoding="utf-8")
        return ExecutionResult(str(job_type), str(target), classification, "executed")
    if job_type == "update_file":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content_from_payload(payload), encoding="utf-8")
        return ExecutionResult(str(job_type), str(target), classification, "executed")
    if job_type == "generate_agents_txt":
        target_path = path if path.name == "agents.txt" else path / "agents.txt"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content_from_payload(payload), encoding="utf-8")
        return ExecutionResult(str(job_type), str(target), classification, "executed")
    if job_type == "update_existing_file":
        if not path.is_file():
            raise QueueExecutorError(f"update_existing_file requires an existing file: {path}")
        path.write_text(content_from_payload(payload), encoding="utf-8")
        return ExecutionResult(str(job_type), str(target), classification, "executed")
    if job_type == "delete_file":
        if not path.is_file():
            raise QueueExecutorError(f"delete_file requires an existing file: {path}")
        path.unlink()
        return ExecutionResult(str(job_type), str(target), classification, "executed")
    raise QueueExecutorError(f"Unknown job_type: {job_type}")


def execute_jobs(
    jobs: list,
    mode: str = "dry-run",
    approve: bool = False,
    allowed_root: Path | None = None,
    log_path: Path | None = None,
    stdout: TextIO | None = None,
) -> list[ExecutionResult]:
    if mode not in MODES:
        raise QueueExecutorError(f"Invalid executor mode: {mode}")
    if not isinstance(jobs, list):
        raise QueueExecutorError("jobs must be a list.")
    stdout = stdout or sys.stdout
    allowed_root = allowed_root or REPO_ROOT
    log_path = log_path or DEFAULT_LOG_PATH
    results: list[ExecutionResult] = []
    for index, job in enumerate(jobs, start=1):
        result = execute_one(job, mode, approve, allowed_root)
        results.append(result)
        print(f"- job {index}: {result.job_type} {result.target} -> {result.result}", file=stdout)
        write_log(job, result, mode, log_path)
    return results
