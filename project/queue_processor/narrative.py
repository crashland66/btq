from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config import get_config
from epistemic import ASSUMED, INFERRED, OBSERVED, REPORTED_BY_HUMAN, UNCONFIRMED, iter_contradictions
from queue_processor.reconciliation import iter_evidence
from queue_processor.repair import parse_date, scan_manifests


@dataclass(frozen=True)
class NarrativeEntry:
    timestamp: str
    capture_id: str | None
    target_path: str
    statement: str
    classification: str
    confidence: str
    confidence_basis: list[str]
    source: str
    temporal_state: str
    contradictions: list[dict[str, Any]]


def entry_from_evidence(record: dict[str, Any], contradictions: list[dict[str, Any]]) -> NarrativeEntry:
    epistemic = record.get("epistemic_state") if isinstance(record.get("epistemic_state"), dict) else {}
    intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
    statement = str(intent.get("reason") or record.get("mutation_intent_summary") or record.get("nearby_content_excerpt") or "")
    job_id = str(record.get("job_id", ""))
    target = record.get("target_doc_id") or record.get("target_path") or ""
    related = [item for item in contradictions if item.get("earlier_id") == job_id or item.get("later_id") == job_id]
    return NarrativeEntry(
        timestamp=str(record.get("mutation_timestamp", "")),
        capture_id=record.get("capture_id") if isinstance(record.get("capture_id"), str) else None,
        target_path=str(target),
        statement=statement,
        classification=str(epistemic.get("classification", intent.get("classification", UNCONFIRMED))),
        confidence=str(epistemic.get("confidence", intent.get("confidence", "unknown"))),
        confidence_basis=list(epistemic.get("confidence_basis") if isinstance(epistemic.get("confidence_basis"), list) else []),
        source=str(record.get("_evidence_path", "")),
        temporal_state=str(epistemic.get("temporal_state", "CURRENTLY_VALID")),
        contradictions=related,
    )


def build_narrative(
    runtime_root: Path,
    *,
    capture_id: str | None = None,
    account: str | None = None,
    since: datetime | None = None,
    include_contradictions: bool = False,
) -> list[NarrativeEntry]:
    contradictions = iter_contradictions(runtime_root, capture_id) if include_contradictions else []
    entries: list[NarrativeEntry] = []
    for record in iter_evidence(runtime_root, capture_id):
        if account and account.lower() not in json.dumps(record).lower():
            continue
        entry = entry_from_evidence(record, contradictions)
        if since and entry.timestamp:
            try:
                parsed = datetime.fromisoformat(entry.timestamp)
            except ValueError:
                parsed = None
            if parsed is not None and parsed < since.replace(tzinfo=parsed.tzinfo):
                continue
        entries.append(entry)
    entries.sort(key=lambda item: item.timestamp)
    return entries


def format_narrative(entries: list[NarrativeEntry], json_output: bool) -> str:
    if json_output:
        return json.dumps([asdict(entry) for entry in entries], indent=2, sort_keys=True)
    if not entries:
        return "No narrative entries."
    grouped = {
        OBSERVED: "Observations",
        REPORTED_BY_HUMAN: "Human Reports",
        INFERRED: "Inferences",
        ASSUMED: "Assumptions",
        UNCONFIRMED: "Unresolved Ambiguity",
    }
    lines = ["BTQ operational narrative"]
    for classification, heading in grouped.items():
        selected = [entry for entry in entries if entry.classification == classification]
        if not selected:
            continue
        lines.append(heading)
        for entry in selected:
            lines.append(f"- {entry.timestamp} [{entry.confidence}] {entry.statement}")
            if entry.confidence_basis:
                lines.append(f"  basis: {', '.join(entry.confidence_basis)}")
            if entry.contradictions:
                lines.append(f"  contradictions: {len(entry.contradictions)} linked")
    remaining = [entry for entry in entries if entry.classification not in grouped]
    if remaining:
        lines.append("Other Epistemic States")
        for entry in remaining:
            lines.append(f"- {entry.timestamp} [{entry.classification}/{entry.confidence}] {entry.statement}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Generate epistemic operational narrative timelines.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--capture-id")
    parser.add_argument("--account")
    parser.add_argument("--since")
    parser.add_argument("--include-contradictions", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entries = build_narrative(
        args.runtime_root.expanduser(),
        capture_id=args.capture_id,
        account=args.account,
        since=parse_date(args.since),
        include_contradictions=args.include_contradictions,
    )
    print(format_narrative(entries, args.json))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
