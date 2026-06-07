"""Phase-2 gating tests for the config example-fallback.

The real ``config.json`` carries machine-specific paths and is gitignored, so a
fresh clone / CI environment has only the committed synthetic
``config.example.json``. ``config.default_config_path()`` must fall back to the
example when ``config.json`` is absent, while still preferring the real file when
it exists and always honoring the ``BT_PIPELINE_CONFIG_PATH`` override.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import config
from config import CONFIG_PATH_ENV, PipelineConfig, default_config_path, load_config, repo_root


def _real_example_content() -> str:
    return (repo_root() / "config.example.json").read_text(encoding="utf-8")


def test_falls_back_to_example_when_config_json_absent(tmp_path, monkeypatch) -> None:
    """Only config.example.json present -> default_config_path returns it and
    load_config parses it into a PipelineConfig."""
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    example = tmp_path / "config.example.json"
    example.write_text(_real_example_content(), encoding="utf-8")
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)

    assert default_config_path() == example

    parsed = load_config()
    assert isinstance(parsed, PipelineConfig)


def test_real_config_json_wins_over_example(tmp_path, monkeypatch) -> None:
    """Both files present -> the real config.json is preferred over the example."""
    monkeypatch.delenv(CONFIG_PATH_ENV, raising=False)
    example = tmp_path / "config.example.json"
    example.write_text(_real_example_content(), encoding="utf-8")
    real = tmp_path / "config.json"
    real.write_text(_real_example_content(), encoding="utf-8")
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)

    assert default_config_path() == real


def test_env_override_wins_over_both(tmp_path, monkeypatch) -> None:
    """BT_PIPELINE_CONFIG_PATH wins over both config.json and config.example.json."""
    example = tmp_path / "config.example.json"
    example.write_text(_real_example_content(), encoding="utf-8")
    real = tmp_path / "config.json"
    real.write_text(_real_example_content(), encoding="utf-8")
    override = tmp_path / "override" / "my-config.json"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(_real_example_content(), encoding="utf-8")
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)
    monkeypatch.setenv(CONFIG_PATH_ENV, str(override))

    assert default_config_path() == override
    assert isinstance(load_config(), PipelineConfig)
