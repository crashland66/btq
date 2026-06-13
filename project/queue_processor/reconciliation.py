from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_config
from queue_processor.evidence import assess_drift, canonical_evidence_text
from queue_processor.handlers import _shared
from queue_processor.repair import analyze, parse_date, scan_manifests
from queue_processor.replay import build_plan


@dataclass(frozen=True)
class ReconciliationReport:
    generated_at: str
    semantic_drift_findings: list[dict[str, Any]]
    unresolved_ambiguities: list[dict[str, Any]]
    orphaned_evidence: list[dict[str, Any]]
    replay_risks: list[dict[str, Any]]
    mutation_confidence_summary: dict[str, int]
    stale_operational_references: list[dict[str, Any]]
    unresolved_lineage_gaps: list[dict[str, Any]]


def iter_evidence(runtime_root: Path, capture_id: str | None = None) -> list[dict[str, Any]]:
    root = runtime_root / "evidence"
    if not root.exists():
        return []
    pattern = f"{capture_id}/*.json" if capture_id else "*/*.json"
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payload["_evidence_path"] = str(path)
            records.append(payload)
    return records


def generate_report(
    runtime_root: Path,
    vault_root: Path,
    personal_vault_root: Path,
    *,
    capture_id: str | None = None,
    since: datetime | None = None,
    high_risk_only: bool = False,
) -> ReconciliationReport:
    repair_findings, _evidence = analyze(runtime_root, vault_root, mode="full", since=since, capture_id=capture_id)
    semantic_drift: list[dict[str, Any]] = []
    orphaned_evidence: list[dict[str, Any]] = []
    confidence_summary: dict[str, int] = {}
    store = _shared._vault_store()
    for record in iter_evidence(runtime_root, capture_id):
        target = record.get("target_doc_id") or record.get("target_path")
        current_text = None
        doc = store.get_optional(str(target)) if isinstance(target, str) and target else None
        if doc is not None:
            current_text = canonical_evidence_text(doc)
        else:
            orphaned_evidence.append({"reason": "evidence target missing", "evidence": record.get("_evidence_path"), "target_path": target, "target_doc_id": target})
        drift = assess_drift(record, current_text)
        confidence_summary[drift.confidence] = confidence_summary.get(drift.confidence, 0) + 1
        if drift.indicators:
            semantic_drift.append(
                {
                    "confidence": drift.confidence,
                    "indicators": drift.indicators,
                    "job_id": record.get("job_id"),
                    "capture_id": record.get("capture_id"),
                    "target_path": target,
                    "target_doc_id": target,
                    "evidence": record.get("_evidence_path"),
                }
            )
    replay_entries = build_plan(
        runtime_root,
        vault_root,
        personal_vault_root,
        capture_id=capture_id,
        failed_only=True,
        missing_index=True,
        manifest_gap=True,
    )
    replay_risks = [asdict(entry) for entry in replay_entries]
    if high_risk_only:
        semantic_drift = [item for item in semantic_drift if item.get("confidence") in {"SEMANTICALLY_DRIFTED", "REPLAY_HIGH_RISK"}]
        replay_risks = [item for item in replay_risks if item.get("replay_risk_classification") not in {"SAFE", "LOW_CONFIDENCE"}]
    manifests = scan_manifests(runtime_root)
    lineage_gaps: list[dict[str, Any]] = []
    for manifest_capture_id, manifest in manifests.items():
        if capture_id and manifest_capture_id != capture_id:
            continue
        if not manifest.get("processed_records") and manifest.get("queue_jobs"):
            lineage_gaps.append({"capture_id": manifest_capture_id, "reason": "manifest has queue jobs but no processed records"})
    return ReconciliationReport(
        generated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        semantic_drift_findings=semantic_drift,
        unresolved_ambiguities=[asdict(finding) for finding in repair_findings if finding.ambiguous],
        orphaned_evidence=orphaned_evidence,
        replay_risks=replay_risks,
        mutation_confidence_summary=confidence_summary,
        stale_operational_references=[],
        unresolved_lineage_gaps=lineage_gaps,
    )


def format_report(report: ReconciliationReport, json_output: bool) -> str:
    if json_output:
        return json.dumps(asdict(report), indent=2, sort_keys=True)
    lines = ["BTQ reconciliation report", f"Generated: {report.generated_at}"]
    lines.append("Mutation confidence summary:")
    if report.mutation_confidence_summary:
        for key, value in sorted(report.mutation_confidence_summary.items()):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- no evidence snapshots")
    lines.append("Semantic drift findings:")
    for item in report.semantic_drift_findings:
        lines.append(f"- {item.get('job_id')} {item.get('confidence')}: {', '.join(item.get('indicators', []))}")
    lines.append("Unresolved ambiguities:")
    for item in report.unresolved_ambiguities:
        lines.append(f"- {item.get('kind')}: {item.get('message')}")
    lines.append("Replay risks:")
    for item in report.replay_risks:
        lines.append(f"- {item.get('job_id')} {item.get('replay_risk_classification')}: {item.get('replay_reason')}")
    lines.append("Lineage gaps:")
    for item in report.unresolved_lineage_gaps:
        lines.append(f"- {item.get('capture_id')}: {item.get('reason')}")
    lines.append("Orphaned evidence:")
    for item in report.orphaned_evidence:
        lines.append(f"- {item.get('evidence')}: {item.get('reason')}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Generate a BTQ operational reconciliation report.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--personal-vault-root", type=Path, default=config.personal_vault_dir)
    parser.add_argument("--capture-id")
    parser.add_argument("--since")
    parser.add_argument("--high-risk-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = generate_report(
        args.runtime_root.expanduser(),
        args.vault_root.expanduser(),
        args.personal_vault_root.expanduser(),
        capture_id=args.capture_id,
        since=parse_date(args.since),
        high_risk_only=args.high_risk_only,
    )
    print(format_report(report, args.json))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
