from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = {
    "read": {
        "script": REPO_ROOT / "scripts" / "install-cowork-read-launch-agent",
        "label": "com.btq.cowork-read-watch",
        "module": "queue_processor.cowork_read_watcher",
        "plist": "cowork-read-watch.plist",
    },
    "drop": {
        "script": REPO_ROOT / "scripts" / "install-cowork-drop-launch-agent",
        "label": "com.btq.cowork-drop-watch",
        "module": "queue_processor.cowork_drop_watcher",
        "plist": "cowork-drop-watch.plist",
    },
}

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("plutil") is None,
    reason="launchd plist linting requires macOS plutil",
)


def _write_config(tmp_path: Path) -> Path:
    base_dir = tmp_path / "btq_runtime"
    config = {
        "base_dir": str(base_dir),
        "project_dir": str(REPO_ROOT / "project"),
        "pipeline_dir": "{base_dir}/pipeline",
        "audio_inbox_dir": "{base_dir}/audio/inbox",
        "audio_archive_dir": "{base_dir}/audio/archive",
        "local_root": "{base_dir}/local",
        "transcription_output_dir": "{base_dir}/transcription",
        "event_output_dir": "{base_dir}/events",
        "queue_dir": "{base_dir}/queue",
        "local_runtime_dir": "{base_dir}/local_runtime",
        "runtime_root": "{base_dir}",
        "project_runtime_root": "{base_dir}/project_runtime",
        "project_runtime_dry_root": "{base_dir}/project_runtime_dry",
        "vault_dir": "{base_dir}/vault",
        "personal_vault_dir": "{base_dir}/personal_vault",
        "docs_dir": "{base_dir}/docs",
        "logs_dir": "{base_dir}/logs",
        "queue_processor_logs_dir": "{base_dir}/logs/queue_processor",
        "transcription_log_path": "{base_dir}/logs/transcription.log",
        "queue_watch_log_path": "{base_dir}/logs/queue_watch.log",
        "whisper_launchd_stdout_log": "{base_dir}/logs/whisper_stdout.log",
        "whisper_launchd_stderr_log": "{base_dir}/logs/whisper_stderr.log",
        "queue_watch_launchd_stdout_log": "{base_dir}/logs/queue_stdout.log",
        "queue_watch_launchd_stderr_log": "{base_dir}/logs/queue_stderr.log",
        "whisper_model": "base",
        "ffmpeg_path_prefix": "/usr/bin",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _write_stub_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_path = tmp_path / "launchctl.calls"
    sleep_calls_path = tmp_path / "sleep.calls"
    launchctl_path = bin_dir / "launchctl"
    launchctl_path.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$BTQ_LAUNCHCTL_CALLS"
case "${1:-}" in
  print)
    count="$(cat "$BTQ_PRINT_COUNT_FILE" 2>/dev/null || printf 0)"
    next=$((count + 1))
    printf '%s' "$next" > "$BTQ_PRINT_COUNT_FILE"
    if [ "$count" -lt "${BTQ_PRINT_PRESENT_COUNT:-0}" ]; then
      exit 0
    fi
    exit 1
    ;;
  bootstrap)
    count="$(cat "$BTQ_BOOTSTRAP_COUNT_FILE" 2>/dev/null || printf 0)"
    next=$((count + 1))
    printf '%s' "$next" > "$BTQ_BOOTSTRAP_COUNT_FILE"
    if [ "$count" -lt "${BTQ_BOOTSTRAP_FAILS:-0}" ]; then
      printf 'Bootstrap failed: 5: Input/output error\\n' >&2
      exit 5
    fi
    exit 0
    ;;
  bootout|kickstart)
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    launchctl_path.chmod(0o755)
    sleep_path = bin_dir / "sleep"
    sleep_path.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$BTQ_SLEEP_CALLS"
exit 0
""",
        encoding="utf-8",
    )
    sleep_path.chmod(0o755)
    return bin_dir, calls_path, sleep_calls_path


def _base_env(
    tmp_path: Path,
    *,
    bin_dir: Path | None = None,
    calls_path: Path | None = None,
    sleep_calls_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "BTQ_COUCHDB_QUEUE_DB",
        "BTQ_QUEUE_DATABASE",
        "BTQ_INSTALL_DRY_RUN",
        "BTQ_INSTALL_DRY_RUN_PATH",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "BT_PIPELINE_CONFIG_PATH": str(_write_config(tmp_path)),
            "BTQ_COUCHDB_URL": "http://127.0.0.1:5984",
            "BTQ_COUCHDB_USER": "reader",
            "BTQ_COUCHDB_PASSWORD": "reader-password",
            "BTQ_PRINT_COUNT_FILE": str(tmp_path / "print.count"),
            "BTQ_BOOTSTRAP_COUNT_FILE": str(tmp_path / "bootstrap.count"),
            "BTQ_PRINT_PRESENT_COUNT": "0",
            "BTQ_BOOTSTRAP_FAILS": "0",
        }
    )
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    if calls_path is not None:
        env["BTQ_LAUNCHCTL_CALLS"] = str(calls_path)
    if sleep_calls_path is not None:
        env["BTQ_SLEEP_CALLS"] = str(sleep_calls_path)
    if extra_env:
        env.update(extra_env)
    return env


def _run_installer(
    kind: str,
    tmp_path: Path,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    bootstrap_fails: int = 0,
    print_present_count: int = 0,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bin_dir, calls_path, sleep_calls_path = _write_stub_tools(tmp_path)
    env = _base_env(
        tmp_path,
        bin_dir=bin_dir,
        calls_path=calls_path,
        sleep_calls_path=sleep_calls_path,
        extra_env={
            "BTQ_BOOTSTRAP_FAILS": str(bootstrap_fails),
            "BTQ_PRINT_PRESENT_COUNT": str(print_present_count),
            **(extra_env or {}),
        },
    )
    proc = subprocess.run(
        [str(INSTALLERS[kind]["script"]), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    return proc, calls_path, sleep_calls_path


def _load_plist(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert isinstance(payload, dict)
    return payload


def _calls(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_dry_run_output_renders_valid_plist_without_launchctl(tmp_path: Path, kind: str) -> None:
    output_path = tmp_path / str(INSTALLERS[kind]["plist"])
    proc, calls_path, _ = _run_installer(kind, tmp_path, ["--dry-run-output", str(output_path)])

    assert proc.returncode == 0, proc.stderr
    assert output_path.exists(), "installer dry-run did not render a plist"
    assert _calls(calls_path) == []
    assert "plutil -lint: OK" in proc.stdout
    lint = subprocess.run(["plutil", "-lint", str(output_path)], text=True, capture_output=True)
    assert lint.returncode == 0, "rendered plist failed plutil lint"

    plist = _load_plist(output_path)
    assert plist["Label"] == INSTALLERS[kind]["label"]
    assert plist["ProgramArguments"][2] == INSTALLERS[kind]["module"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_dry_run_env_var_renders_without_launchctl(tmp_path: Path, kind: str) -> None:
    output_path = tmp_path / f"{kind}-env-dry-run.plist"
    proc, calls_path, _ = _run_installer(
        kind,
        tmp_path,
        [],
        extra_env={
            "BTQ_INSTALL_DRY_RUN": "1",
            "BTQ_INSTALL_DRY_RUN_PATH": str(output_path),
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert output_path.exists()
    assert _calls(calls_path) == []
    assert f"Dry-run rendered {output_path}" in proc.stdout


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_plist_renders_valid_with_special_char_password(tmp_path: Path, kind: str) -> None:
    password = 'p&ss<w"o>rd'
    url = "http://127.0.0.1:5984?role=read&scope=queue"
    output_path = tmp_path / str(INSTALLERS[kind]["plist"])
    extra_env = {
        "BTQ_COUCHDB_URL": url,
        "BTQ_COUCHDB_PASSWORD": password,
        "BTQ_QUEUE_DATABASE": "btq_queue_read",
    }
    proc, _, _ = _run_installer(kind, tmp_path, ["--dry-run-output", str(output_path)], extra_env=extra_env)
    assert proc.returncode == 0, proc.stderr

    env = _load_plist(output_path)["EnvironmentVariables"]
    assert isinstance(env, dict)
    if env.get("BTQ_COUCHDB_PASSWORD") != password:
        pytest.fail("password did not round-trip through the plist")
    if env.get("BTQ_COUCHDB_URL") != url:
        pytest.fail("URL did not round-trip through the plist")
    if kind == "read":
        assert env["BTQ_COUCHDB_QUEUE_DB"] == "btq_queue_read"
    else:
        assert "BTQ_COUCHDB_QUEUE_DB" not in env
        assert "BTQ_QUEUE_DATABASE" not in env


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_plist_omits_unset_queue_database(tmp_path: Path, kind: str) -> None:
    output_path = tmp_path / str(INSTALLERS[kind]["plist"])
    proc, _, _ = _run_installer(kind, tmp_path, ["--dry-run-output", str(output_path)])
    assert proc.returncode == 0, proc.stderr

    env = _load_plist(output_path)["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["BTQ_COUCHDB_URL"] == "http://127.0.0.1:5984"
    assert env["BTQ_COUCHDB_USER"] == "reader"
    assert env["BTQ_COUCHDB_PASSWORD"] == "reader-password"
    assert "BTQ_COUCHDB_QUEUE_DB" not in env
    assert "BTQ_QUEUE_DATABASE" not in env


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_install_does_not_print_secret_values(tmp_path: Path, kind: str) -> None:
    password = 'p&ss<w"o>rd'
    output_path = tmp_path / str(INSTALLERS[kind]["plist"])
    proc, _, _ = _run_installer(
        kind,
        tmp_path,
        ["--dry-run-output", str(output_path)],
        extra_env={"BTQ_COUCHDB_PASSWORD": password},
    )
    assert proc.returncode == 0, proc.stderr
    output = proc.stdout + proc.stderr

    assert "PASSWORD=SET" in output
    if password in output:
        pytest.fail("dry-run output leaked the password")


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_unknown_arg_errors_without_launchctl(tmp_path: Path, kind: str) -> None:
    proc, calls_path, _ = _run_installer(kind, tmp_path, ["--bogus"])

    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert _calls(calls_path) == []


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_bootstrap_retries_io_error_then_succeeds(tmp_path: Path, kind: str) -> None:
    proc, calls_path, sleep_calls_path = _run_installer(
        kind,
        tmp_path,
        [],
        bootstrap_fails=2,
        print_present_count=2,
    )

    calls = _calls(calls_path)
    assert proc.returncode == 0, proc.stderr
    assert sum(1 for call in calls if call.startswith("print ")) == 3
    assert sum(1 for call in calls if call.startswith("bootstrap ")) == 3
    assert calls[-1].startswith("kickstart -k ")
    assert "retrying (1/3)" in proc.stderr
    assert "retrying (2/3)" in proc.stderr
    assert _calls(sleep_calls_path) == ["1", "1", "2", "2"]


@pytest.mark.parametrize("kind", ["read", "drop"])
def test_bootstrap_retry_failure_exits_loudly_without_kickstart(tmp_path: Path, kind: str) -> None:
    proc, calls_path, _ = _run_installer(kind, tmp_path, [], bootstrap_fails=99)

    calls = _calls(calls_path)
    assert proc.returncode != 0
    assert sum(1 for call in calls if call.startswith("bootstrap ")) == 3
    assert not any(call.startswith("kickstart ") for call in calls)
    assert "launchctl bootstrap failed after retries" in proc.stderr
    assert "watcher may be down" in proc.stderr
    assert "Bootstrap failed: 5: Input/output error" in proc.stderr
