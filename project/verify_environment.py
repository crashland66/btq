from __future__ import annotations

import importlib
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from config import get_config


MIN_PYTHON = (3, 9)
RECENT_ACTIVITY_SECONDS = 15 * 60
PROJECT_DIR = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_DIR.parent


@dataclass(frozen=True)
class CheckResult:
    label: str
    ok: bool
    detail: str


def format_path(path: Path) -> str:
    return str(path.expanduser())


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


def check_python_version() -> CheckResult:
    current = sys.version_info[:3]
    required = ".".join(str(part) for part in MIN_PYTHON)
    actual = ".".join(str(part) for part in current)
    ok = current >= MIN_PYTHON
    return CheckResult("python", ok, f"found {actual}; requires >= {required}")


def check_import(module_name: str) -> CheckResult:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(module_name, False, f"import failed: {exc}")

    version = getattr(module, "__version__", None)
    detail = f"import ok ({version})" if version else "import ok"
    return CheckResult(module_name, True, detail)


def check_ffmpeg() -> CheckResult:
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        return CheckResult("ffmpeg", False, "not found on PATH")
    return CheckResult("ffmpeg", True, ffmpeg_path)


def check_directory(label: str, path: Path) -> CheckResult:
    expanded = path.expanduser()
    if not expanded.exists():
        return CheckResult(label, False, f"missing: {format_path(expanded)}")
    if not expanded.is_dir():
        return CheckResult(label, False, f"not a directory: {format_path(expanded)}")
    return CheckResult(label, True, format_path(expanded))


def newest_mtime(paths: list[Path]) -> float | None:
    mtimes: list[float] = []
    for path in paths:
        try:
            if path.exists():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return max(mtimes)


def newest_file_mtime(directory: Path) -> float | None:
    if not directory.exists() or not directory.is_dir():
        return None
    mtimes: list[float] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return max(mtimes)


def check_queue_backlog(now: float | None = None) -> CheckResult:
    config = get_config()
    current_time = time.time() if now is None else now
    runtime_root = config.local_runtime_dir.expanduser()
    queue_dir = runtime_root / "queue"
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"

    queue_files = sorted(path for path in queue_dir.glob("*.json") if path.is_file()) if queue_dir.exists() else []
    oldest_mtimes: list[float] = []
    for path in queue_files:
        try:
            oldest_mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    oldest_mtime = min(oldest_mtimes) if oldest_mtimes else None
    oldest_age = None if oldest_mtime is None else current_time - oldest_mtime

    recent_candidates = [
        config.queue_watch_log_path.expanduser(),
        config.queue_watch_launchd_stdout_log.expanduser(),
        config.queue_watch_launchd_stderr_log.expanduser(),
    ]
    watcher_mtime = newest_mtime(recent_candidates)
    processed_mtime = newest_file_mtime(processed_dir)
    failed_mtime = newest_file_mtime(failed_dir)
    processing_mtimes = [mtime for mtime in (processed_mtime, failed_mtime) if mtime is not None]
    processing_mtime = max(processing_mtimes) if processing_mtimes else None

    watcher_age = None if watcher_mtime is None else current_time - watcher_mtime
    processing_age = None if processing_mtime is None else current_time - processing_mtime
    watcher_recent = watcher_age is not None and watcher_age <= RECENT_ACTIVITY_SECONDS
    processing_recent = processing_age is not None and processing_age <= RECENT_ACTIVITY_SECONDS

    detail = (
        f"backlog={len(queue_files)} oldest_age={format_age(oldest_age)} "
        f"watcher_recent={'yes' if watcher_recent else 'no'} watcher_age={format_age(watcher_age)} "
        f"recent_processing={'yes' if processing_recent else 'no'} processing_age={format_age(processing_age)} "
        f"queue_dir={format_path(queue_dir)}"
    )
    if queue_files and not watcher_recent and not processing_recent:
        return CheckResult("queue_backlog", False, f"{detail}; queued jobs exist but no recent watcher or processing activity detected")
    return CheckResult("queue_backlog", True, detail)


def run_checks() -> list[CheckResult]:
    config = get_config()
    return [
        check_python_version(),
        check_import("torch"),
        check_import("whisper"),
        check_import("markdown"),
        check_ffmpeg(),
        check_directory("base_dir", config.base_dir),
        check_directory("project_dir", config.project_dir),
        check_directory("pipeline_dir", config.pipeline_dir),
        check_directory("audio_inbox_dir", config.audio_inbox_dir),
        check_directory("local_runtime_dir", config.local_runtime_dir),
        check_directory("audio_archive_dir", config.audio_archive_dir),
        check_directory("vault_dir", config.vault_dir),
        check_directory("personal_vault_dir", config.personal_vault_dir),
        check_queue_backlog(),
    ]


def run(argv: list[str] | None = None) -> int:
    cwd = Path.cwd().resolve()
    if cwd != PROJECT_DIR:
        print(
            "verify_environment must be run from the project directory. "
            f"Run: cd {REPO_ROOT} && ./scripts/btq-verify-environment",
            file=sys.stderr,
        )
        return 2

    results = run_checks()
    has_failure = False
    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"[{status}] {result.label}: {result.detail}")
        if not result.ok:
            has_failure = True

    if has_failure:
        return 1
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
