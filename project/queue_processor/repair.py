from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import get_config
from queue_processor.handlers import _shared
from queue_processor.idempotency import compute_job_id
from queue_processor.manifest import manifest_path_for
from queue_processor.processed_index import HANDLER_VERSION, ProcessedIndexError, append_record, index_path_for, iter_records
from queue_processor.processed_index import build_record as build_processed_record
from queue_processor.structured_log import write_event as write_structured_event


MODES = {"full", "markers-only", "processed-only", "stale-artifacts"}
DEFAULT_STALE_HOURS = 24


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    message: str
    evidence: dict[str, Any]
    ambiguous: bool = True


def parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def should_include_timestamp(timestamp: str | None, since: datetime | None) -> bool:
    if since is None or timestamp is None:
        return True
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= since


def load_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def scan_processed_files(runtime_root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    processed_dir = runtime_root / "processed"
    if not processed_dir.exists():
        return records
    for path in sorted(processed_dir.glob("*.json")):
        payload = load_json_file(path)
        if payload is None:
            continue
        computed_job_id = compute_job_id(payload)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        records[computed_job_id] = {
            "computed_job_id": computed_job_id,
            "job_type": payload.get("job_type"),
            "payload": payload.get("payload"),
            "metadata": metadata,
            "path": str(path),
        }
    return records


def scan_index(runtime_root: Path) -> tuple[dict[str, dict[str, Any]], list[Finding]]:
    try:
        records = iter_records(index_path_for(runtime_root))
    except ProcessedIndexError as exc:
        return {}, [
            Finding(
                kind="corrupted_index",
                severity="critical",
                message=str(exc),
                evidence={"path": str(index_path_for(runtime_root))},
            )
        ]
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        by_id[str(record["computed_job_id"])] = record
    return by_id, []


def scan_canonical_job_ids(store: Any) -> dict[str, list[dict[str, Any]]]:
    markers: dict[str, list[dict[str, Any]]] = {}
    for doc in store.scan_job_id_docs():
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            continue
        job_ids = doc.get("btq_job_ids")
        if not isinstance(job_ids, list):
            continue
        record = {
            "doc_id": doc_id,
            "type": doc.get("type"),
            "content": doc.get("content") if isinstance(doc.get("content"), str) else "",
        }
        for job_id in job_ids:
            if not isinstance(job_id, str) or not job_id.strip():
                continue
            markers.setdefault(job_id, []).append(dict(record))
    return markers


def scan_logs(runtime_root: Path) -> dict[str, list[dict[str, Any]]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    log_path = runtime_root / "logs" / "queue_processor_events.jsonl"
    if not log_path.exists():
        return by_id
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        job_id = payload.get("computed_job_id")
        if isinstance(job_id, str):
            by_id.setdefault(job_id, []).append(payload)
    return by_id


def scan_manifests(runtime_root: Path) -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    manifests_dir = runtime_root / "manifests"
    if not manifests_dir.exists():
        return manifests
    for path in sorted(manifests_dir.glob("*.json")):
        payload = load_json_file(path)
        if payload is None:
            continue
        capture_id = payload.get("capture_id")
        if isinstance(capture_id, str):
            manifests[capture_id] = payload
    return manifests


def job_matches_capture(job_id: str, capture_id: str | None, processed: dict[str, Any], index: dict[str, Any], manifests: dict[str, dict[str, Any]]) -> bool:
    if capture_id is None:
        return True
    processed_record = processed.get(job_id)
    if isinstance(processed_record, dict):
        metadata = processed_record.get("metadata") if isinstance(processed_record.get("metadata"), dict) else {}
        if metadata.get("capture_id") == capture_id:
            return True
    index_record = index.get(job_id)
    if isinstance(index_record, dict) and index_record.get("capture_id") == capture_id:
        return True
    manifest = manifests.get(capture_id)
    if isinstance(manifest, dict):
        for key in ("processed_records", "vault_mutations"):
            records = manifest.get(key)
            if isinstance(records, list) and any(isinstance(record, dict) and record.get("computed_job_id") == job_id for record in records):
                return True
    return False


def target_hint_from_job(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return "unknown"
    job_type = record.get("job_type")
    if job_type in {"append_to_note", "record_unknown_capture", "reclassify_unknown"} and isinstance(payload.get("path"), str):
        return payload["path"]
    if job_type == "personal_journal_entry" and isinstance(payload.get("date"), str):
        return f"Journal/{payload['date']}.md"
    if job_type in {"flag_access_constraint", "trigger_recruiting", "close_recruiting", "visit_create"} and isinstance(payload.get("site"), str):
        return f"site:{payload['site']}"
    if job_type in {"remove_from_schedule", "flag_retention_risk"} and isinstance(payload.get("employee"), str):
        return f"employee:{payload['employee']}"
    if job_type == "add_person" and isinstance(payload.get("name"), str):
        return f"People/{payload['name'].strip()}.md"
    if job_type == "parse_supply_email" and isinstance(payload.get("html_path"), str):
        return payload["html_path"]
    return "unknown"


def expected_content_visible(processed: dict[str, Any], marker: dict[str, Any]) -> bool | None:
    payload = processed.get("payload")
    if not isinstance(payload, dict):
        return None
    job_type = processed.get("job_type")
    text = marker.get("content")
    if text is None:
        text = marker.get("body", "")
    body = str(text)
    if job_type == "append_to_note":
        content = payload.get("content")
        return isinstance(content, str) and content.strip() in body
    if job_type == "personal_journal_entry":
        content = payload.get("body")
        return isinstance(content, str) and content.strip() in body
    if job_type in {"flag_access_constraint", "trigger_recruiting", "flag_retention_risk"}:
        details = payload.get("details")
        return isinstance(details, str) and details.strip() in body
    if job_type == "add_person":
        name = payload.get("name")
        return isinstance(name, str) and f"# {name.strip()}" in body
    return None


def stale_artifact_findings(runtime_root: Path, stale_hours: int) -> list[Finding]:
    findings: list[Finding] = []
    cutoff = time.time() - timedelta(hours=stale_hours).total_seconds()
    targets = [
        ("stale_claimed_audio", runtime_root / "claimed" / "audio"),
        ("abandoned_temp_file", runtime_root / "temp"),
        ("old_failed_job", runtime_root / "failed"),
    ]
    for kind, root in targets:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                findings.append(
                    Finding(
                        kind=kind,
                        severity="warning",
                        message=f"stale artifact detected: {path}",
                        evidence={"path": str(path), "mtime": datetime.fromtimestamp(mtime, timezone.utc).isoformat()},
                    )
                )
    return findings


def manifest_break_findings(runtime_root: Path, manifests: dict[str, dict[str, Any]]) -> list[Finding]:
    findings: list[Finding] = []
    for capture_id, manifest in manifests.items():
        artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
        for key in ("audio", "transcript", "normalized_transcript", "process_log"):
            value = artifacts.get(key)
            if isinstance(value, str) and value and not Path(value).exists():
                findings.append(
                    Finding(
                        kind="manifest_missing_artifact",
                        severity="warning",
                        message=f"manifest references missing {key}",
                        evidence={"capture_id": capture_id, "field": key, "path": value},
                    )
                )
        if not manifest.get("queue_jobs") and artifacts.get("transcript"):
            findings.append(
                Finding(
                    kind="lineage_break",
                    severity="warning",
                    message="manifest has transcript but no downstream queue jobs",
                    evidence={"capture_id": capture_id, "manifest": str(manifest_path_for(runtime_root, capture_id))},
                )
            )
    return findings


def analyze(
    runtime_root: Path,
    vault_root: Path,
    *,
    mode: str,
    since: datetime | None = None,
    capture_id: str | None = None,
    stale_hours: int = DEFAULT_STALE_HOURS,
    store: Any | None = None,
) -> tuple[list[Finding], dict[str, Any]]:
    store = _shared._vault_store() if store is None else store
    processed = scan_processed_files(runtime_root)
    index, index_findings = scan_index(runtime_root)
    markers = scan_canonical_job_ids(store)
    logs = scan_logs(runtime_root)
    manifests = scan_manifests(runtime_root)
    findings = list(index_findings)

    if mode in {"full", "processed-only"}:
        for job_id, record in processed.items():
            if not job_matches_capture(job_id, capture_id, processed, index, manifests):
                continue
            if job_id not in index:
                findings.append(
                    Finding(
                        kind="missing_index_entry",
                        severity="warning",
                        message="processed queue file has no processed-index record",
                        evidence={"computed_job_id": job_id, "processed_file": record["path"]},
                        ambiguous=False,
                    )
                )
            if job_id not in markers:
                findings.append(
                    Finding(
                        kind="processed_without_canonical_record",
                        severity="warning",
                        message="processed job has no canonical CouchDB document carrying its job_id; success cannot be proven from canonical state",
                        evidence={"computed_job_id": job_id, "processed_file": record["path"]},
                    )
                )

        for job_id, record in index.items():
            if not job_matches_capture(job_id, capture_id, processed, index, manifests):
                continue
            if not should_include_timestamp(record.get("timestamp"), since):
                continue
            if job_id not in processed:
                findings.append(
                    Finding(
                        kind="index_without_processed_file",
                        severity="warning",
                        message="processed-index row has no matching processed queue file",
                        evidence={"computed_job_id": job_id, "index_record": record},
                    )
                )
            if job_id in processed and processed[job_id].get("job_type") != record.get("job_type"):
                findings.append(
                    Finding(
                        kind="index_drift",
                        severity="warning",
                        message="processed-index job_type differs from processed queue file",
                        evidence={"computed_job_id": job_id, "processed": processed[job_id], "index_record": record},
                    )
                )

    if mode in {"full", "markers-only"}:
        for job_id, marker_records in markers.items():
            if not job_matches_capture(job_id, capture_id, processed, index, manifests):
                continue
            if job_id not in processed and job_id not in index:
                findings.append(
                    Finding(
                        kind="orphaned_canonical_job_id",
                        severity="warning",
                        message="job_id present in a canonical document but no processed/index evidence",
                        evidence={"computed_job_id": job_id, "markers": [{"doc_id": marker["doc_id"], "type": marker.get("type")} for marker in marker_records]},
                    )
                )
            if job_id in processed:
                for marker in marker_records:
                    visible = expected_content_visible(processed[job_id], marker)
                    if visible is False:
                        findings.append(
                            Finding(
                                kind="canonical_content_missing",
                                severity="warning",
                                message="job_id present in canonical document but expected payload content is not in the document content",
                                evidence={"computed_job_id": job_id, "doc_id": marker["doc_id"], "type": marker.get("type"), "processed_file": processed[job_id]["path"]},
                            )
                        )

    if mode in {"full", "stale-artifacts"}:
        findings.extend(stale_artifact_findings(runtime_root, stale_hours))
        findings.extend(manifest_break_findings(runtime_root, manifests))

    for finding in findings:
        if finding.ambiguous:
            write_structured_event(
                runtime_root / "logs" / "queue_processor_events.jsonl",
                "repair_ambiguity_detected",
                kind=finding.kind,
                severity=finding.severity,
                evidence=finding.evidence,
            )
        if finding.kind in {"canonical_content_missing", "orphaned_canonical_job_id"}:
            write_structured_event(
                runtime_root / "logs" / "queue_processor_events.jsonl",
                "marker_divergence_detected",
                kind=finding.kind,
                severity=finding.severity,
                evidence=finding.evidence,
            )
        if finding.kind in {"stale_claimed_audio", "abandoned_temp_file", "old_failed_job", "manifest_missing_artifact"}:
            write_structured_event(
                runtime_root / "logs" / "queue_processor_events.jsonl",
                "stale_artifact_detected",
                kind=finding.kind,
                severity=finding.severity,
                evidence=finding.evidence,
            )

    evidence = {"processed": processed, "index": index, "markers": markers, "logs": logs, "manifests": manifests}
    return findings, evidence


def apply_repairs(runtime_root: Path, findings: list[Finding], evidence: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    processed = evidence.get("processed", {})
    index = evidence.get("index", {})
    for finding in findings:
        if finding.kind != "missing_index_entry":
            continue
        job_id = finding.evidence["computed_job_id"]
        if job_id in index or job_id not in processed:
            continue
        record = processed[job_id]
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        processed_record = build_processed_record(
            computed_job_id=job_id,
            job_type=str(record.get("job_type")),
            target_path=target_hint_from_job(record),
            source_queue_file=Path(str(record["path"])),
            capture_id=metadata.get("capture_id") if isinstance(metadata.get("capture_id"), str) else None,
            run_id="repair-index",
        )
        append_record(index_path_for(runtime_root), processed_record)
        action = f"appended processed-index record for {job_id}"
        actions.append(action)
        write_structured_event(
            runtime_root / "logs" / "queue_processor_events.jsonl",
            "repair_action_applied",
            action=action,
            computed_job_id=job_id,
        )
    return actions


def _display_finding(finding: Finding) -> dict[str, Any]:
    payload = asdict(finding)
    return payload


def format_findings(findings: list[Finding], json_output: bool) -> str:
    if json_output:
        return json.dumps([_display_finding(finding) for finding in findings], indent=2, sort_keys=True)
    if not findings:
        return "No repair findings."
    lines = ["BTQ repair findings"]
    for finding in findings:
        marker = "AMBIGUOUS" if finding.ambiguous else "OBSERVED"
        lines.append(f"- [{finding.severity}] {finding.kind} ({marker}): {finding.message}")
        lines.append(f"  evidence: {json.dumps(_display_finding(finding)['evidence'], sort_keys=True)}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Inspect and optionally repair derived BTQ runtime indexes.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--since")
    parser.add_argument("--capture-id")
    parser.add_argument("--mode", choices=sorted(MODES), default="full")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    write_structured_event(
        args.runtime_root / "logs" / "queue_processor_events.jsonl",
        "repair_scan_started",
        mode=args.mode,
        dry_run=not args.force,
        since=args.since,
        capture_id=args.capture_id,
    )
    findings, evidence = analyze(
        args.runtime_root.expanduser(),
        args.vault_root.expanduser(),
        mode=args.mode,
        since=parse_date(args.since),
        capture_id=args.capture_id,
    )
    actions: list[str] = []
    if args.force:
        actions = apply_repairs(args.runtime_root.expanduser(), findings, evidence)
    print(format_findings(findings, args.json))
    if actions and not args.json:
        print("Repair actions applied:")
        for action in actions:
            print(f"- {action}")
    return 1 if any(finding.severity == "critical" for finding in findings) else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
