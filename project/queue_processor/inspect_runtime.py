from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import get_config
from queue_processor.repair import DEFAULT_STALE_HOURS, manifest_break_findings, scan_manifests, stale_artifact_findings
from queue_processor.structured_log import write_event as write_structured_event


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Inspect stale and orphaned BTQ runtime artifacts.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--stale-hours", type=int, default=DEFAULT_STALE_HOURS)
    parser.add_argument("--emit-events", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_root = args.runtime_root.expanduser()
    findings = stale_artifact_findings(runtime_root, args.stale_hours)
    findings.extend(manifest_break_findings(runtime_root, scan_manifests(runtime_root)))
    if args.emit_events:
        for finding in findings:
            write_structured_event(
                runtime_root / "logs" / "queue_processor_events.jsonl",
                "stale_artifact_detected" if finding.kind in {"stale_claimed_audio", "abandoned_temp_file", "old_failed_job"} else "repair_ambiguity_detected",
                kind=finding.kind,
                severity=finding.severity,
                evidence=finding.evidence,
            )
    if args.json:
        print(json.dumps([finding.__dict__ for finding in findings], indent=2, sort_keys=True))
        return 0
    if not findings:
        print("No stale runtime artifacts found.")
        return 0
    print("BTQ runtime inspection")
    for finding in findings:
        print(f"- [{finding.severity}] {finding.kind}: {finding.message}")
        print(f"  evidence: {json.dumps(finding.evidence, sort_keys=True)}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
