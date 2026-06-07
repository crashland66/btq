from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import get_config
from queue_processor.processor_lock import lock_path_for, process_is_alive, read_lock
from queue_processor.processed_index import ProcessedIndexError, index_path_for, iter_records


DEFAULT_QUEUE_BACKLOG_THRESHOLD = 100
DEFAULT_FAILED_AGE_HOURS = 24
DEFAULT_STALE_CLAIMED_HOURS = 2
DEFAULT_METRICS_RETAIN_HOURS = 168
DEFAULT_ALERT_THROTTLE_HOURS = 24
ALERT_DIR_NAME = "alerts"
ALERT_HISTORY_NAME = "health_alerts.jsonl"
ALERT_LATEST_NAME = "latest_health_alert.json"
ALERT_STATE_NAME = "health_alert_state.json"
EXIT_OK = 0
EXIT_CRITICAL_THROTTLED = 1
EXIT_CRITICAL_ALERTED = 2


@dataclass(frozen=True)
class HealthResult:
    lines: list[str]
    critical: list[str]


@dataclass(frozen=True)
class AlertDecision:
    alerted: bool
    throttled: bool
    fingerprint: str | None
    alert_path: Path | None


def count_json_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.iterdir() if item.is_file() and item.suffix == ".json")


def count_stale_files(path: Path, older_than: timedelta, now: float | None = None) -> int:
    if not path.exists():
        return 0
    current = time.time() if now is None else now
    cutoff = current - older_than.total_seconds()
    return sum(1 for item in path.rglob("*") if item.is_file() and item.stat().st_mtime < cutoff)


def count_unknown_captures(vault_root: Path) -> int:
    journal_dir = vault_root / "Journal"
    if not journal_dir.exists():
        return 0
    count = 0
    for path in journal_dir.glob("*-unknown.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        count += text.count("type: unknown_capture")
    return count


def lock_status(runtime_root: Path) -> tuple[str, bool]:
    path = lock_path_for(runtime_root)
    payload = read_lock(path)
    if payload is None:
        return "absent", False
    pid = payload.get("pid")
    if isinstance(pid, int) and process_is_alive(pid):
        return f"held pid={pid} hostname={payload.get('hostname')} started_at={payload.get('started_at')}", False
    return f"stale pid={pid} hostname={payload.get('hostname')} started_at={payload.get('started_at')}", True


def runtime_usage_summary(runtime_root: Path) -> str:
    runtime_root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(runtime_root)
    total_mb = usage.total // (1024 * 1024)
    free_mb = usage.free // (1024 * 1024)
    used_mb = usage.used // (1024 * 1024)
    return f"used={used_mb}MB free={free_mb}MB total={total_mb}MB"


def alert_paths(runtime_root: Path) -> dict[str, Path]:
    alert_dir = runtime_root / ALERT_DIR_NAME
    return {
        "dir": alert_dir,
        "history": alert_dir / ALERT_HISTORY_NAME,
        "latest": alert_dir / ALERT_LATEST_NAME,
        "state": alert_dir / ALERT_STATE_NAME,
    }


def critical_fingerprint(critical: list[str]) -> str:
    normalized = json.dumps(sorted(critical), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_alert_state(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def maybe_emit_alert(
    result: HealthResult,
    runtime_root: Path,
    *,
    throttle_hours: float = DEFAULT_ALERT_THROTTLE_HOURS,
    now: datetime | None = None,
) -> AlertDecision:
    if not result.critical:
        return AlertDecision(alerted=False, throttled=False, fingerprint=None, alert_path=None)

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created_at = current.isoformat()
    paths = alert_paths(runtime_root)
    fingerprint = critical_fingerprint(result.critical)
    state = _read_alert_state(paths["state"])
    previous_fingerprint = state.get("fingerprint")
    previous_alerted_at = _parse_iso_datetime(state.get("last_alerted_at"))
    throttle_seconds = max(0.0, throttle_hours * 3600.0)
    same_condition = previous_fingerprint == fingerprint
    within_throttle = (
        same_condition
        and previous_alerted_at is not None
        and (current - previous_alerted_at).total_seconds() < throttle_seconds
    )
    if within_throttle:
        return AlertDecision(alerted=False, throttled=True, fingerprint=fingerprint, alert_path=paths["history"])

    alert_id = f"health-{current.strftime('%Y%m%dT%H%M%SZ')}-{fingerprint[:12]}"
    payload: dict[str, object] = {
        "alert_id": alert_id,
        "created_at": created_at,
        "status": "critical",
        "fingerprint": fingerprint,
        "critical": result.critical,
        "report": result.lines,
    }
    _append_jsonl(paths["history"], payload)
    _write_json_atomic(paths["latest"], payload)
    _write_json_atomic(
        paths["state"],
        {
            "fingerprint": fingerprint,
            "last_alert_id": alert_id,
            "last_alerted_at": created_at,
            "critical": result.critical,
        },
    )
    return AlertDecision(alerted=True, throttled=False, fingerprint=fingerprint, alert_path=paths["history"])


def processed_index_status(runtime_root: Path) -> tuple[str, bool]:
    path = index_path_for(runtime_root)
    if not path.exists():
        return "missing; dedupe will fallback to processed/ scan", False
    try:
        records = iter_records(path)
    except ProcessedIndexError as exc:
        return f"corrupted: {exc}", True
    return f"ok records={len(records)} path={path}", False


def build_health_report(
    runtime_root: Path,
    vault_root: Path,
    queue_backlog_threshold: int,
    failed_age_hours: int,
    stale_claimed_hours: int,
    now: float | None = None,
) -> HealthResult:
    critical: list[str] = []
    lines = ["BTQ runtime health", f"Checked at: {datetime.now(timezone.utc).isoformat()}"]

    status, stale_lock = lock_status(runtime_root)
    lines.append(f"Processor lock: {status}")
    if stale_lock:
        critical.append("stale processor lock")

    queue_depth = count_json_files(runtime_root / "queue")
    lines.append(f"Queue depth: {queue_depth}")
    if queue_depth > queue_backlog_threshold:
        critical.append(f"queue backlog above threshold ({queue_depth}>{queue_backlog_threshold})")

    failed_count = count_json_files(runtime_root / "failed")
    lines.append(f"Failed queue count: {failed_count}")

    stale_claimed = count_stale_files(runtime_root / "claimed" / "audio", timedelta(hours=stale_claimed_hours), now)
    lines.append(f"Stale claimed audio files: {stale_claimed}")

    unknown_count = count_unknown_captures(vault_root)
    lines.append(f"Unknown capture count: {unknown_count}")

    index_status, index_critical = processed_index_status(runtime_root)
    lines.append(f"Processed index: {index_status}")
    if index_critical:
        critical.append("corrupted processed index")

    old_failed = count_stale_files(runtime_root / "failed", timedelta(hours=failed_age_hours), now)
    lines.append(f"Failed jobs older than {failed_age_hours}h: {old_failed}")

    lines.append(f"Runtime disk usage: {runtime_usage_summary(runtime_root)}")
    if critical:
        lines.append("Critical: " + "; ".join(critical))
    else:
        lines.append("Critical: none")
    return HealthResult(lines=lines, critical=critical)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Report BTQ runtime health.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--queue-backlog-threshold", type=int, default=DEFAULT_QUEUE_BACKLOG_THRESHOLD)
    parser.add_argument("--failed-age-hours", type=int, default=DEFAULT_FAILED_AGE_HOURS)
    parser.add_argument("--stale-claimed-hours", type=int, default=DEFAULT_STALE_CLAIMED_HOURS)
    parser.add_argument("--emit-metrics", action="store_true", help="Append one pipeline metrics sidecar sample; call every 15 minutes from cron/launchd for trend data.")
    parser.add_argument("--metrics-retain-hours", type=int, default=DEFAULT_METRICS_RETAIN_HOURS)
    parser.add_argument("--monitor", action="store_true", help="Scheduled mode: emit a throttled durable alert when health is critical.")
    parser.add_argument("--alert-throttle-hours", type=float, default=DEFAULT_ALERT_THROTTLE_HOURS)
    parser.add_argument("--quiet", action="store_true", help="Suppress non-critical scheduled output.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_health_report(
        args.runtime_root.expanduser(),
        args.vault_root.expanduser(),
        args.queue_backlog_threshold,
        args.failed_age_hours,
        args.stale_claimed_hours,
    )
    if args.emit_metrics:
        from queue_processor import metrics

        metrics_path = metrics.metrics_path_for(args.runtime_root.expanduser())
        sample = metrics.sample_metrics(args.runtime_root.expanduser())
        metrics.append_sample(metrics_path, sample)
        metrics.prune(metrics_path, args.metrics_retain_hours)
    decision: AlertDecision | None = None
    if args.monitor:
        decision = maybe_emit_alert(result, args.runtime_root.expanduser(), throttle_hours=args.alert_throttle_hours)
    if not args.quiet:
        print("\n".join(result.lines))
        if decision is not None and decision.alerted:
            print(f"Health alert emitted: {decision.alert_path}")
        elif decision is not None and decision.throttled:
            print("Health alert throttled: identical critical condition is within throttle window.")
    if not result.critical:
        return EXIT_OK
    if args.monitor and decision is not None and decision.alerted:
        return EXIT_CRITICAL_ALERTED
    return EXIT_CRITICAL_THROTTLED


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
