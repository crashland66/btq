from __future__ import annotations

import plistlib
from pathlib import Path


PLIST_PATH = Path("project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist")
FORBIDDEN_COMMANDS = (
    "queue_processor",
    "stage-approved-drafts",
    "generate-approved-drafts",
    "review-candidate",
    "queue-processor",
    "queue_processor.main",
)


def load_plist() -> dict[str, object]:
    with PLIST_PATH.open("rb") as handle:
        payload = plistlib.load(handle)
    assert isinstance(payload, dict)
    return payload


def test_field_capture_pipeline_watcher_launchagent_exists_and_runs_safe_command() -> None:
    payload = load_plist()

    assert payload["Label"] == "com.btq.field-capture-pipeline-watcher"
    assert payload["WorkingDirectory"] == "@@BTQ_PROJECT_ROOT@@"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == "@@BTQ_RUNTIME@@/logs/field-capture-pipeline-watcher.out.log"
    assert payload["StandardErrorPath"] == "@@BTQ_RUNTIME@@/logs/field-capture-pipeline-watcher.err.log"

    args = payload["ProgramArguments"]
    assert args == [
        "@@BTQ_PROJECT_ROOT@@/scripts/btq",
        "watch-field-capture-pipeline",
        "--poll-seconds",
        "60",
        "--vision-limit",
        "3",
        "--vision-auto-retry-per-cycle",
        "1",
        "--json",
    ]
    joined_args = " ".join(args)
    for forbidden in FORBIDDEN_COMMANDS:
        assert forbidden not in joined_args


def test_field_capture_pipeline_watcher_launchagent_sets_local_vision_environment() -> None:
    payload = load_plist()
    env = payload["EnvironmentVariables"]

    assert env["BTQ_FIELD_CAPTURE_VISION_MODEL"] == "gemma4:26b"
    assert env["BTQ_OLLAMA_URL"] == "http://127.0.0.1:11434"
    assert env["BTQ_FIELD_CAPTURE_VISION_TIMEOUT_SECONDS"] == "180"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert "api.openai.com" not in " ".join(str(value) for value in env.values())
    assert "vision.googleapis.com" not in " ".join(str(value) for value in env.values())


def test_field_capture_launchagent_docs_include_pipeline_watcher_commands() -> None:
    docs = "\n".join(
        [
            Path("project/field_capture/README.md").read_text(encoding="utf-8"),
            Path("project/docs/runbook.md").read_text(encoding="utf-8"),
        ]
    )

    assert "cp @@BTQ_PROJECT_ROOT@@/project/field_capture/launchagents/com.btq.field-capture-pipeline-watcher.plist ~/Library/LaunchAgents/" in docs
    assert "launchctl load ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist" in docs
    assert "launchctl unload ~/Library/LaunchAgents/com.btq.field-capture-pipeline-watcher.plist" in docs
    assert "launchctl list | grep com.btq.field-capture-pipeline-watcher" in docs
    assert "field-capture-pipeline-watcher.out.log" in docs
    assert "field-capture-pipeline-watcher.err.log" in docs
    assert "Ollama should" in docs


def test_field_capture_pipeline_watcher_launchagent_no_committed_credentials() -> None:
    payload = load_plist()
    env = payload["EnvironmentVariables"]
    assert env["BTQ_COUCHDB_USER"] == "REPLACE_ME"
    assert env["BTQ_COUCHDB_PASSWORD"] == "REPLACE_ME"
