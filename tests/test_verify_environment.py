import dataclasses
import json
import os
import time
from pathlib import Path

import pytest
import verify_environment
from config import PipelineConfig, load_config, repo_root
from verify_environment import check_queue_backlog


class StubConfig:
    def __init__(self, root: Path) -> None:
        self.local_runtime_dir = root / "runtime"
        self.queue_watch_log_path = root / "logs" / "queue_watch.log"
        self.queue_watch_launchd_stdout_log = root / "logs" / "queue_watch_stdout.log"
        self.queue_watch_launchd_stderr_log = root / "logs" / "queue_watch_stderr.log"


def touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_queue_backlog_reports_count_and_oldest_age(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    config = StubConfig(tmp_path)
    queue_dir = config.local_runtime_dir / "queue"
    touch(queue_dir / "job_new.json", now - 120)
    touch(queue_dir / "job_old.json", now - 3600)
    touch(config.queue_watch_log_path, now - 60)
    monkeypatch.setattr("verify_environment.get_config", lambda: config)

    result = check_queue_backlog(now=now)

    assert result.ok is True
    assert "backlog=2" in result.detail
    assert "oldest_age=1.0h" in result.detail
    assert "watcher_recent=yes" in result.detail


def test_queue_backlog_fails_when_jobs_exist_without_recent_activity(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    config = StubConfig(tmp_path)
    touch(config.local_runtime_dir / "queue" / "job_1.json", now - 3600)
    touch(config.queue_watch_log_path, now - 7200)
    monkeypatch.setattr("verify_environment.get_config", lambda: config)

    result = check_queue_backlog(now=now)

    assert result.ok is False
    assert "backlog=1" in result.detail
    assert "queued jobs exist but no recent watcher or processing activity detected" in result.detail


def test_queue_backlog_passes_when_recent_processing_exists(tmp_path: Path, monkeypatch) -> None:
    now = time.time()
    config = StubConfig(tmp_path)
    touch(config.local_runtime_dir / "queue" / "job_1.json", now - 3600)
    touch(config.local_runtime_dir / "processed" / "job_done.json", now - 30)
    monkeypatch.setattr("verify_environment.get_config", lambda: config)

    result = check_queue_backlog(now=now)

    assert result.ok is True
    assert "backlog=1" in result.detail
    assert "recent_processing=yes" in result.detail


def _build_pipeline_config(root: Path) -> PipelineConfig:
    kwargs: dict[str, object] = {}
    for field in dataclasses.fields(PipelineConfig):
        if field.type is Path or field.type in {"Path", "Path | None"}:
            kwargs[field.name] = root
        else:
            kwargs[field.name] = ""
    return PipelineConfig(**kwargs)


def test_run_checks_only_references_defined_config_attributes(tmp_path: Path, monkeypatch) -> None:
    """Guard against script-vs-config drift: every config.<attr> in run_checks
    must exist on PipelineConfig. Regression for the removed outbox_dir/working_dir."""
    config = _build_pipeline_config(tmp_path)
    monkeypatch.setattr("verify_environment.get_config", lambda: config)

    results = verify_environment.run_checks()

    assert results, "run_checks should return at least one result"
    assert all(isinstance(r, verify_environment.CheckResult) for r in results)


def _existing_config_path() -> Path:
    """Prefer the machine-specific config.json when present, else fall back to the
    committed synthetic example. Mirrors config.default_config_path() so these
    environment tests tolerate a fresh clone / CI where config.json is absent,
    without weakening what they check when config.json is present."""
    real = repo_root() / "config.json"
    if real.exists():
        return real
    example = repo_root() / "config.example.json"
    assert example.exists(), "neither config.json nor config.example.json present"
    return example


def test_load_config_rejects_unknown_key(tmp_path: Path) -> None:
    config_data = json.loads(_existing_config_path().read_text(encoding="utf-8"))
    config_data["not_a_real_key"] = "bogus"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown config key") as exc_info:
        load_config(config_path)

    assert "not_a_real_key" in str(exc_info.value)


def test_load_config_accepts_current_config_json() -> None:
    config = load_config(_existing_config_path())

    assert isinstance(config, PipelineConfig)


def test_config_json_has_no_outbox_or_working_dir() -> None:
    config_data = json.loads(_existing_config_path().read_text(encoding="utf-8"))

    assert "outbox_dir" not in config_data
    assert "working_dir" not in config_data
