from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

from instance_config import InstanceConfig, get_instance_config, load_instance_config


BASE_DIR_ENV = "BT_PIPELINE_BASE_DIR"
CONFIG_PATH_ENV = "BT_PIPELINE_CONFIG_PATH"
DEFAULT_CONFIG_NAME = "config.json"
EXAMPLE_CONFIG_NAME = "config.example.json"
ALLOWED_EXTRA_CONFIG_KEYS = frozenset({"instance"})


@dataclass(frozen=True)
class PipelineConfig:
    base_dir: Path
    project_dir: Path
    pipeline_dir: Path
    audio_inbox_dir: Path
    audio_archive_dir: Path
    local_root: Path
    transcription_output_dir: Path
    event_output_dir: Path
    queue_dir: Path
    local_runtime_dir: Path
    runtime_root: Path
    project_runtime_root: Path
    project_runtime_dry_root: Path
    vault_dir: Path | None
    personal_vault_dir: Path | None
    docs_dir: Path
    logs_dir: Path
    queue_processor_logs_dir: Path
    transcription_log_path: Path
    queue_watch_log_path: Path
    whisper_launchd_stdout_log: Path
    whisper_launchd_stderr_log: Path
    queue_watch_launchd_stdout_log: Path
    queue_watch_launchd_stderr_log: Path
    whisper_model: str
    ffmpeg_path_prefix: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    env_value = os.environ.get(CONFIG_PATH_ENV)
    if env_value:
        return Path(env_value).expanduser()
    # Real config.json carries machine-specific paths and is gitignored. When it's
    # absent (fresh clone / CI), fall back to the committed synthetic example so
    # get_config() works without per-machine setup.
    real = repo_root() / DEFAULT_CONFIG_NAME
    if real.exists():
        return real
    example = repo_root() / EXAMPLE_CONFIG_NAME
    if example.exists():
        return example
    return real


def _resolve_template(value: str, base_dir: Path) -> str:
    return value.replace("{base_dir}", str(base_dir))


def _path_from(raw: dict[str, object], key: str, base_dir: Path) -> Path:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config key '{key}' must be a non-empty string")
    return Path(_resolve_template(value, base_dir)).expanduser()


def _optional_path_from(raw: dict[str, object], key: str, base_dir: Path) -> Path | None:
    """Like ``_path_from`` but returns ``None`` when the key is absent.

    The Obsidian projection has been retired (CouchDB ``btq_vault`` is canonical),
    so ``vault_dir``/``personal_vault_dir`` are now optional. Config files that
    still carry these keys keep loading; ones that omit them also load. A present
    key must still be a non-empty string (catches typos / empty values).
    """
    if key not in raw:
        return None
    return _path_from(raw, key, base_dir)


def _string_from(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Config key '{key}' must be a non-empty string")
    return value


def _validate_known_keys(raw: dict[str, object]) -> None:
    known_keys = {field.name for field in fields(PipelineConfig)} | ALLOWED_EXTRA_CONFIG_KEYS
    unknown_keys = sorted(set(raw) - known_keys)
    if unknown_keys:
        joined_keys = ", ".join(unknown_keys)
        raise ValueError(f"unknown config key: {joined_keys}")


def load_config(config_path: Path | None = None) -> PipelineConfig:
    resolved_config_path = default_config_path() if config_path is None else config_path.expanduser()
    raw = json.loads(resolved_config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a JSON object: {resolved_config_path}")
    _validate_known_keys(raw)

    configured_base_dir = raw.get("base_dir")
    if not isinstance(configured_base_dir, str) or not configured_base_dir.strip():
        raise ValueError("Config key 'base_dir' must be a non-empty string")
    base_dir = Path(os.environ.get(BASE_DIR_ENV, configured_base_dir)).expanduser()

    return PipelineConfig(
        base_dir=base_dir,
        project_dir=_path_from(raw, "project_dir", base_dir),
        pipeline_dir=_path_from(raw, "pipeline_dir", base_dir),
        audio_inbox_dir=_path_from(raw, "audio_inbox_dir", base_dir),
        audio_archive_dir=_path_from(raw, "audio_archive_dir", base_dir),
        local_root=_path_from(raw, "local_root", base_dir),
        transcription_output_dir=_path_from(raw, "transcription_output_dir", base_dir),
        event_output_dir=_path_from(raw, "event_output_dir", base_dir),
        queue_dir=_path_from(raw, "queue_dir", base_dir),
        local_runtime_dir=_path_from(raw, "local_runtime_dir", base_dir),
        runtime_root=_path_from(raw, "runtime_root", base_dir),
        project_runtime_root=_path_from(raw, "project_runtime_root", base_dir),
        project_runtime_dry_root=_path_from(raw, "project_runtime_dry_root", base_dir),
        vault_dir=_optional_path_from(raw, "vault_dir", base_dir),
        personal_vault_dir=_optional_path_from(raw, "personal_vault_dir", base_dir),
        docs_dir=_path_from(raw, "docs_dir", base_dir),
        logs_dir=_path_from(raw, "logs_dir", base_dir),
        queue_processor_logs_dir=_path_from(raw, "queue_processor_logs_dir", base_dir),
        transcription_log_path=_path_from(raw, "transcription_log_path", base_dir),
        queue_watch_log_path=_path_from(raw, "queue_watch_log_path", base_dir),
        whisper_launchd_stdout_log=_path_from(raw, "whisper_launchd_stdout_log", base_dir),
        whisper_launchd_stderr_log=_path_from(raw, "whisper_launchd_stderr_log", base_dir),
        queue_watch_launchd_stdout_log=_path_from(raw, "queue_watch_launchd_stdout_log", base_dir),
        queue_watch_launchd_stderr_log=_path_from(raw, "queue_watch_launchd_stderr_log", base_dir),
        whisper_model=_string_from(raw, "whisper_model"),
        ffmpeg_path_prefix=_string_from(raw, "ffmpeg_path_prefix"),
    )


@lru_cache(maxsize=1)
def get_config() -> PipelineConfig:
    return load_config()


def require_directories(paths: dict[str, Path]) -> None:
    missing = [f"{label}: {path}" for label, path in paths.items() if not path.exists() or not path.is_dir()]
    if missing:
        joined = "\n".join(missing)
        raise FileNotFoundError(f"Required directories are missing:\n{joined}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read BT Pipeline configuration values.")
    parser.add_argument("command", choices=["get"])
    parser.add_argument("key")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = get_config()
    if not hasattr(config, args.key):
        raise SystemExit(f"Unknown config key: {args.key}")
    value = getattr(config, args.key)
    print(value)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
