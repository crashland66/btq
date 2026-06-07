from __future__ import annotations

import argparse
import difflib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_config
from io_atomic import atomic_write_text
from processing_core.time import utc_now
from queue_processor.idempotency import compute_job_id, has_job_been_applied, upsert_job_id_frontmatter
from queue_processor.main import canonical_person_path
from queue_processor.evidence import (
    CONFIDENCE_SEMANTICALLY_DRIFTED,
    CONFIDENCE_SEMANTICALLY_UNKNOWN,
    CONFIDENCE_STRUCTURALLY_SAFE,
    assess_drift,
    read_evidence,
)
from queue_processor.repair import load_json_file, scan_index, scan_manifests, scan_markers, scan_processed_files
from queue_processor.structured_log import write_event as write_structured_event


RISK_SAFE = "SAFE"
RISK_LOW_CONFIDENCE = "LOW_CONFIDENCE"
RISK_TARGET_DRIFTED = "TARGET_DRIFTED"
RISK_MARKER_CONFLICT = "MARKER_CONFLICT"
RISK_MISSING_CONTEXT = "MISSING_CONTEXT"
RISK_UNKNOWN_STATE = "UNKNOWN_STATE"
RISK_LOW_SEMANTIC_CONFIDENCE = "LOW_SEMANTIC_CONFIDENCE"
DANGEROUS_RISKS = {RISK_TARGET_DRIFTED, RISK_MARKER_CONFLICT, RISK_MISSING_CONTEXT, RISK_UNKNOWN_STATE, RISK_LOW_SEMANTIC_CONFIDENCE}
SUPPORTED_TEXT_JOBS = {
    "append_to_note",
    "personal_journal_entry",
    "flag_access_constraint",
    "trigger_recruiting",
    "close_recruiting",
    "remove_from_schedule",
    "flag_retention_risk",
}
CREATE_JOBS = {"add_person"}


@dataclass(frozen=True)
class ReplayPlanEntry:
    capture_id: str | None
    job_id: str
    original_queue_file: str
    target_path: str
    original_mutation_timestamp: str | None
    current_target_state_summary: str
    replay_risk_classification: str
    mutation_type: str
    replay_reason: str
    risk_notes: list[str] = field(default_factory=list)
    confidence_classification: str = CONFIDENCE_SEMANTICALLY_UNKNOWN


@dataclass(frozen=True)
class ReplayPreview:
    entry: ReplayPlanEntry
    before: str
    after: str
    diff: str
    risk_notes: list[str]
    markers_added: list[str]
    markers_already_present: list[str]
    conflicts_detected: list[str]
    ambiguous_conditions: list[str]


def replay_log_path(runtime_root: Path) -> Path:
    return runtime_root / "logs" / "replay_events.jsonl"


def log_replay_event(runtime_root: Path, event: str, **fields: Any) -> None:
    write_structured_event(replay_log_path(runtime_root), event, **fields)


def load_job(path: Path) -> dict[str, Any] | None:
    payload = load_json_file(path)
    if payload is None or not isinstance(payload.get("payload"), dict) or not isinstance(payload.get("job_type"), str):
        return None
    return payload


def candidate_paths(runtime_root: Path, failed_only: bool, from_queue: Path | None) -> list[Path]:
    if from_queue is not None:
        return [from_queue]
    roots = [runtime_root / "failed"] if failed_only else [runtime_root / "failed", runtime_root / "processed", runtime_root / "queue"]
    paths: list[Path] = []
    for root in roots:
        if root.exists():
            paths.extend(sorted(root.glob("*.json")))
    return paths


def capture_id_for_job(job: dict[str, Any], index_record: dict[str, Any] | None = None) -> str | None:
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    if isinstance(metadata.get("capture_id"), str):
        return metadata["capture_id"]
    if index_record is not None and isinstance(index_record.get("capture_id"), str):
        return index_record["capture_id"]
    return None


def resolve_target_path(vault_root: Path, personal_vault_root: Path, job: dict[str, Any], index_record: dict[str, Any] | None = None) -> str:
    if index_record is not None and isinstance(index_record.get("target_path"), str) and index_record["target_path"] != "unknown":
        return index_record["target_path"]
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = job.get("job_type")
    if job_type in {"append_to_note", "record_unknown_capture", "reclassify_unknown"} and isinstance(payload.get("path"), str):
        return str(vault_root / payload["path"])
    if job_type == "personal_journal_entry" and isinstance(payload.get("date"), str):
        return str(personal_vault_root / "Journal" / f"{payload['date']}.md")
    if job_type == "add_person" and isinstance(payload.get("name"), str):
        return str(canonical_person_path(vault_root, payload["name"]))
    return "unknown"


def mutation_text(job: dict[str, Any]) -> str | None:
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = job.get("job_type")
    if job_type == "append_to_note":
        content = payload.get("content")
        return content.strip() + "\n" if isinstance(content, str) else None
    if job_type == "personal_journal_entry":
        required = ("timestamp", "audio_file", "raw_transcript_path", "body")
        if not all(isinstance(payload.get(key), str) for key in required):
            return None
        return (
            f"### {payload['timestamp'].strip()}\n\n"
            f"source_audio: {payload['audio_file'].strip()}\n"
            f"raw_transcript: {payload['raw_transcript_path'].strip()}\n\n"
            f"{payload['body'].strip()}\n"
        )
    if job_type == "flag_access_constraint" and isinstance(payload.get("details"), str):
        return f"{str(payload.get('date', utc_now()[:10]))[:10]} - {payload['details'].strip()}\n"
    if job_type == "trigger_recruiting" and isinstance(payload.get("details"), str):
        priority = str(payload.get("priority", "normal"))
        return f"{str(payload.get('date', utc_now()[:10]))[:10]} - priority={priority} - {payload['details'].strip()}\n"
    if job_type == "close_recruiting":
        outcome = payload.get("outcome")
        if not isinstance(outcome, str):
            return None
        parts = [f"{str(payload.get('date', utc_now()[:10]))[:10]} - outcome={outcome.strip()}"]
        filled_by = payload.get("filled_by")
        trigger_id = payload.get("recruiting_trigger_id")
        notes = payload.get("notes")
        if isinstance(filled_by, str) and filled_by.strip():
            parts.append(f"filled_by={filled_by.strip()}")
        if isinstance(trigger_id, str) and trigger_id.strip():
            parts.append(f"trigger_id={trigger_id.strip()}")
        if isinstance(notes, str) and notes.strip():
            parts.append(notes.strip())
        return " - ".join(parts) + "\n"
    if job_type == "remove_from_schedule" and isinstance(payload.get("site"), str):
        return f"{str(payload.get('date', utc_now()[:10]))[:10]} - removed from schedule for {payload['site'].strip()}\n"
    if job_type == "flag_retention_risk" and isinstance(payload.get("details"), str):
        site = str(payload.get("site", "unknown")).strip()
        return f"{str(payload.get('date', utc_now()[:10]))[:10]} - {site} - {payload['details'].strip()}\n"
    return None


def is_create_job(job_type: str) -> bool:
    return job_type in CREATE_JOBS


def summarize_target(runtime_root: Path, path: str, job_id: str, capture_id: str | None, text: str | None, job_type: str) -> tuple[str, list[str], str, str]:
    if path == "unknown":
        return "target unknown", ["target path could not be reconstructed"], RISK_MISSING_CONTEXT, CONFIDENCE_SEMANTICALLY_UNKNOWN
    target = Path(path)
    if not target.exists():
        if is_create_job(job_type):
            return "target missing; create replay allowed", [], RISK_SAFE, CONFIDENCE_STRUCTURALLY_SAFE
        return "target missing", ["target file missing"], RISK_UNKNOWN_STATE, CONFIDENCE_SEMANTICALLY_UNKNOWN
    current = target.read_text(encoding="utf-8")
    if is_create_job(job_type):
        return f"exists bytes={len(current.encode('utf-8'))}; create target already exists", ["create target already exists"], RISK_MARKER_CONFLICT, CONFIDENCE_SEMANTICALLY_UNKNOWN
    marker_present = has_job_been_applied(current, job_id)
    content_present = text is not None and text.strip() in current
    summary = f"exists bytes={len(current.encode('utf-8'))} marker_present={marker_present} content_present={content_present}"
    notes: list[str] = []
    risk = RISK_SAFE
    if marker_present and not content_present and text is not None:
        notes.append("marker is present but expected content is not visible")
        risk = RISK_MARKER_CONFLICT
    elif content_present and not marker_present:
        notes.append("content appears present but marker is absent")
        risk = RISK_TARGET_DRIFTED
    elif marker_present and content_present:
        notes.append("marker and content already present; replay would likely be a no-op")
        risk = RISK_LOW_CONFIDENCE
    elif text is None:
        notes.append("mutation preview is unsupported for this job type")
        risk = RISK_MISSING_CONTEXT
    drift = assess_drift(read_evidence(runtime_root, capture_id, job_id), current)
    actionable_drift = drift.confidence == CONFIDENCE_SEMANTICALLY_DRIFTED
    if actionable_drift:
        notes.extend(f"semantic drift indicator: {indicator}" for indicator in drift.indicators)
        if risk in {RISK_SAFE, RISK_LOW_CONFIDENCE}:
            risk = RISK_LOW_SEMANTIC_CONFIDENCE
    return summary, notes, risk, drift.confidence


def reason_for_candidate(path: Path, job_id: str, failed_only: bool, missing_index: bool, manifest_gap: bool, index: dict[str, dict[str, Any]], manifests: dict[str, dict[str, Any]]) -> str | None:
    if failed_only and "/failed/" in str(path):
        return "failed queue job selected for operator replay planning"
    if missing_index and job_id not in index:
        return "processed or queued job has no processed-index record"
    if manifest_gap:
        for manifest in manifests.values():
            records = manifest.get("processed_records")
            if isinstance(records, list) and not any(isinstance(record, dict) and record.get("computed_job_id") == job_id for record in records):
                return "manifest does not contain a processed record for this job"
    if not failed_only and not missing_index and not manifest_gap:
        return "explicit replay planning candidate"
    return None


def build_plan(
    runtime_root: Path,
    vault_root: Path,
    personal_vault_root: Path,
    *,
    capture_id: str | None = None,
    job_id: str | None = None,
    since: datetime | None = None,
    failed_only: bool = False,
    missing_index: bool = False,
    manifest_gap: bool = False,
    from_queue: Path | None = None,
) -> list[ReplayPlanEntry]:
    index, _index_findings = scan_index(runtime_root)
    manifests = scan_manifests(runtime_root)
    processed = scan_processed_files(runtime_root)
    paths = candidate_paths(runtime_root, failed_only, from_queue)
    entries: list[ReplayPlanEntry] = []
    for path in paths:
        job = load_job(path)
        if job is None:
            continue
        computed_job_id = compute_job_id(job)
        index_record = index.get(computed_job_id)
        selected_capture_id = capture_id_for_job(job, index_record)
        raw_job_id = job.get("job_id") if isinstance(job.get("job_id"), str) else None
        if job_id is not None and job_id not in {computed_job_id, raw_job_id}:
            continue
        if capture_id is not None and selected_capture_id != capture_id:
            continue
        if since is not None:
            timestamp = index_record.get("timestamp") if index_record else None
            if isinstance(timestamp, str):
                parsed = datetime.fromisoformat(timestamp)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed < since:
                    continue
        reason = reason_for_candidate(path, computed_job_id, failed_only, missing_index, manifest_gap, index, manifests)
        if reason is None:
            continue
        text = mutation_text(job)
        target_path = resolve_target_path(vault_root, personal_vault_root, job, index_record)
        job_type = str(job.get("job_type"))
        summary, notes, risk, confidence = summarize_target(runtime_root, target_path, computed_job_id, selected_capture_id, text, job_type)
        if computed_job_id in processed and "/failed/" in str(path):
            notes.append("same computed job also exists in processed history")
            risk = RISK_MARKER_CONFLICT
        entries.append(
            ReplayPlanEntry(
                capture_id=selected_capture_id,
                job_id=computed_job_id,
                original_queue_file=str(path),
                target_path=target_path,
                original_mutation_timestamp=index_record.get("timestamp") if index_record else None,
                current_target_state_summary=summary,
                replay_risk_classification=risk,
                mutation_type=job_type,
                replay_reason=reason,
                risk_notes=notes,
                confidence_classification=confidence,
            )
        )
        log_replay_event(
            runtime_root,
            "replay_planned",
            capture_id=selected_capture_id,
            job_id=computed_job_id,
            risk=risk,
            reason=reason,
        )
    return entries


def plan_to_json(entries: list[ReplayPlanEntry]) -> str:
    return json.dumps([asdict(entry) for entry in entries], indent=2, sort_keys=True)


def format_plan(entries: list[ReplayPlanEntry]) -> str:
    if not entries:
        return "No replay candidates."
    lines = ["BTQ replay plan"]
    for entry in entries:
        lines.append(f"- job_id={entry.job_id} risk={entry.replay_risk_classification} confidence={entry.confidence_classification} type={entry.mutation_type}")
        lines.append(f"  capture_id={entry.capture_id or 'unknown'}")
        lines.append(f"  queue_file={entry.original_queue_file}")
        lines.append(f"  target={entry.target_path}")
        lines.append(f"  reason={entry.replay_reason}")
        lines.append(f"  current={entry.current_target_state_summary}")
        for note in entry.risk_notes:
            lines.append(f"  risk_note={note}")
    return "\n".join(lines)


def load_plan_file(path: Path) -> list[ReplayPlanEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Replay plan file must contain a JSON list")
    return [ReplayPlanEntry(**item) for item in payload if isinstance(item, dict)]


def preview_entry(entry: ReplayPlanEntry) -> ReplayPreview:
    job = load_job(Path(entry.original_queue_file))
    before = "" if entry.target_path == "unknown" or not Path(entry.target_path).exists() else Path(entry.target_path).read_text(encoding="utf-8")
    risk_notes = list(entry.risk_notes)
    markers_added: list[str] = []
    markers_already_present: list[str] = []
    conflicts: list[str] = []
    ambiguous: list[str] = []
    if job is None:
        conflicts.append("original queue file is missing or invalid")
        after = before
    elif is_create_job(entry.mutation_type):
        if entry.target_path == "unknown":
            conflicts.append("target path missing or unknown")
        elif Path(entry.target_path).exists():
            conflicts.append("create target already exists")
        after = before
    elif entry.target_path == "unknown" or not Path(entry.target_path).exists():
        conflicts.append("target file missing or unknown")
        after = before
    else:
        text = mutation_text(job)
        if text is None:
            ambiguous.append("mutation preview unsupported for this job type")
            after = before
        elif has_job_been_applied(before, entry.job_id):
            markers_already_present.append(entry.job_id)
            after = before
        else:
            body = before
            if text.strip() not in before:
                body = f"{before}\n{text}" if before and not before.endswith("\n") else f"{before}{text}"
            else:
                ambiguous.append("content already present without marker")
            after = upsert_job_id_frontmatter(body, entry.job_id)
            markers_added.append(entry.job_id)
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"before:{entry.target_path}",
            tofile=f"after:{entry.target_path}",
        )
    )
    if entry.replay_risk_classification in DANGEROUS_RISKS:
        conflicts.append(f"dangerous replay risk: {entry.replay_risk_classification}")
    return ReplayPreview(
        entry=entry,
        before=before,
        after=after,
        diff=diff,
        risk_notes=risk_notes,
        markers_added=markers_added,
        markers_already_present=markers_already_present,
        conflicts_detected=conflicts,
        ambiguous_conditions=ambiguous,
    )


def format_previews(previews: list[ReplayPreview], json_output: bool) -> str:
    if json_output:
        return json.dumps([asdict(preview) for preview in previews], indent=2, sort_keys=True)
    if not previews:
        return "No replay previews."
    lines: list[str] = []
    for preview in previews:
        lines.append(f"REPLAY PREVIEW job_id={preview.entry.job_id} risk={preview.entry.replay_risk_classification} confidence={preview.entry.confidence_classification}")
        lines.append("BEFORE")
        lines.append(preview.before)
        lines.append("AFTER")
        lines.append(preview.after)
        lines.append("DIFF")
        lines.append(preview.diff or "(no textual diff)")
        lines.append("RISK NOTES")
        for item in preview.risk_notes + preview.conflicts_detected + preview.ambiguous_conditions:
            lines.append(f"- {item}")
    return "\n".join(lines)


def execute_create_replay(
    runtime_root: Path,
    preview: ReplayPreview,
    project_root: Path | None = None,
    vault_root: Path | None = None,
    personal_vault_root: Path | None = None,
) -> None:
    from queue_processor.main import (  # noqa: PLC0415
        DEFAULT_PERSONAL_VAULT_ROOT,
        DEFAULT_PROJECT_ROOT,
        DEFAULT_VAULT_ROOT,
        RunContext,
        ensure_local_runtime_root,
        process_job,
    )

    resolved_runtime_root = ensure_local_runtime_root(runtime_root)
    resolved_project_root = project_root or DEFAULT_PROJECT_ROOT
    resolved_vault_root = vault_root or DEFAULT_VAULT_ROOT
    resolved_personal_vault_root = personal_vault_root or DEFAULT_PERSONAL_VAULT_ROOT
    processed_dir = resolved_runtime_root / "processed"
    failed_dir = resolved_runtime_root / "failed"
    logs_root = resolved_runtime_root / "logs" / "queue_processor"
    processed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    logs_root.mkdir(parents=True, exist_ok=True)
    run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    context = RunContext(
        project_root=resolved_project_root,
        vault_root=resolved_vault_root,
        personal_vault_root=resolved_personal_vault_root,
        runtime_root=resolved_runtime_root,
        log_path=logs_root / f"replay-{run_timestamp}.log",
        dry_run=False,
        run_id=f"replay-{run_timestamp}",
        structured_log_path=resolved_runtime_root / "logs" / "queue_processor_events.jsonl",
    )
    process_job(Path(preview.entry.original_queue_file), context, processed_dir, failed_dir)
    processed_path = processed_dir / Path(preview.entry.original_queue_file).name
    if not processed_path.exists() and not Path(preview.entry.target_path).exists():
        raise RuntimeError(f"create replay did not create target: {preview.entry.target_path}")


def apply_previews(
    runtime_root: Path,
    previews: list[ReplayPreview],
    approve: bool,
    force_dangerous: bool,
    project_root: Path | None = None,
    vault_root: Path | None = None,
    personal_vault_root: Path | None = None,
) -> int:
    if not approve:
        for preview in previews:
            log_replay_event(runtime_root, "replay_rejected", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="missing-approve")
        print("Replay execution refused: --approve is required.")
        return 1
    exit_code = 0
    for preview in previews:
        dangerous = preview.entry.replay_risk_classification in DANGEROUS_RISKS or preview.conflicts_detected or preview.ambiguous_conditions
        if dangerous and not force_dangerous:
            log_replay_event(runtime_root, "replay_aborted", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="dangerous-refused", conflicts=preview.conflicts_detected, ambiguous=preview.ambiguous_conditions)
            print(f"Replay refused for {preview.entry.job_id}: dangerous conditions require --force-dangerous-replay")
            exit_code = 1
            continue
        if dangerous and force_dangerous:
            log_replay_event(runtime_root, "replay_approved", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="force-dangerous-replay")
        else:
            log_replay_event(runtime_root, "replay_approved", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="approve")
        if is_create_job(preview.entry.mutation_type):
            if preview.entry.target_path == "unknown":
                log_replay_event(runtime_root, "replay_aborted", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="target-unknown")
                print(f"Replay aborted for {preview.entry.job_id}: target unknown")
                exit_code = 1
                continue
            if Path(preview.entry.target_path).exists():
                log_replay_event(runtime_root, "replay_aborted", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="create-target-exists")
                print(f"Replay aborted for {preview.entry.job_id}: create target already exists")
                exit_code = 1
                continue
            try:
                execute_create_replay(runtime_root, preview, project_root, vault_root, personal_vault_root)
            except Exception as exc:
                log_replay_event(runtime_root, "replay_aborted", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="create-execution-failed", error=str(exc))
                print(f"Replay aborted for {preview.entry.job_id}: {exc}")
                exit_code = 1
                continue
            log_replay_event(runtime_root, "replay_executed", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, target_path=preview.entry.target_path)
            print(f"Replay executed for {preview.entry.job_id}: {preview.entry.target_path}")
            continue
        if preview.entry.target_path == "unknown" or not Path(preview.entry.target_path).exists():
            log_replay_event(runtime_root, "replay_aborted", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, operator_action="target-missing")
            print(f"Replay aborted for {preview.entry.job_id}: target missing")
            exit_code = 1
            continue
        atomic_write_text(Path(preview.entry.target_path), preview.after)
        log_replay_event(runtime_root, "replay_executed", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, target_path=preview.entry.target_path)
        print(f"Replay executed for {preview.entry.job_id}: {preview.entry.target_path}")
    return exit_code


def parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def add_plan_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-root", type=Path, default=get_config().project_runtime_root)
    parser.add_argument("--vault-root", type=Path, default=get_config().vault_dir)
    parser.add_argument("--personal-vault-root", type=Path, default=get_config().personal_vault_dir)
    parser.add_argument("--capture-id")
    parser.add_argument("--job-id")
    parser.add_argument("--since")
    parser.add_argument("--failed-only", action="store_true")
    parser.add_argument("--missing-index", action="store_true")
    parser.add_argument("--manifest-gap", action="store_true")
    parser.add_argument("--from-queue", type=Path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan, preview, or execute explicit BTQ replay.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    add_plan_filters(plan_parser)
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.add_argument("--output", type=Path)
    dry_parser = subparsers.add_parser("dry-run")
    add_plan_filters(dry_parser)
    dry_parser.add_argument("--plan-file", type=Path)
    dry_parser.add_argument("--json", action="store_true")
    execute_parser = subparsers.add_parser("execute")
    add_plan_filters(execute_parser)
    execute_parser.add_argument("--plan-file", type=Path)
    execute_parser.add_argument("--approve", action="store_true")
    execute_parser.add_argument("--force-dangerous-replay", action="store_true")
    return parser.parse_args(argv)


def entries_from_args(args: argparse.Namespace) -> list[ReplayPlanEntry]:
    if getattr(args, "plan_file", None) is not None:
        return load_plan_file(args.plan_file)
    return build_plan(
        args.runtime_root.expanduser(),
        args.vault_root.expanduser(),
        args.personal_vault_root.expanduser(),
        capture_id=args.capture_id,
        job_id=args.job_id,
        since=parse_date(args.since),
        failed_only=args.failed_only,
        missing_index=args.missing_index,
        manifest_gap=args.manifest_gap,
        from_queue=args.from_queue.expanduser() if args.from_queue is not None else None,
    )


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        entries = entries_from_args(args)
        output = plan_to_json(entries) if args.json or args.output is not None else format_plan(entries)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(plan_to_json(entries) + "\n", encoding="utf-8")
        print(output)
        return 0
    if args.command == "dry-run":
        entries = entries_from_args(args)
        previews = [preview_entry(entry) for entry in entries]
        for preview in previews:
            if preview.entry.replay_risk_classification == RISK_TARGET_DRIFTED:
                log_replay_event(args.runtime_root.expanduser(), "replay_drift_detected", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification)
            if preview.conflicts_detected:
                log_replay_event(args.runtime_root.expanduser(), "replay_conflict", capture_id=preview.entry.capture_id, job_id=preview.entry.job_id, risk=preview.entry.replay_risk_classification, conflicts=preview.conflicts_detected)
        print(format_previews(previews, args.json))
        return 0
    if args.command == "execute":
        entries = entries_from_args(args)
        previews = [preview_entry(entry) for entry in entries]
        return apply_previews(
            args.runtime_root.expanduser(),
            previews,
            args.approve,
            args.force_dangerous_replay,
            vault_root=args.vault_root.expanduser(),
            personal_vault_root=args.personal_vault_root.expanduser(),
        )
    raise SystemExit(f"Unknown replay command: {args.command}")


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
