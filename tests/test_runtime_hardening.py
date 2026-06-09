import json
import os
import socket
import time
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from queue_processor.health import build_health_report
from queue_processor.processor_lock import ProcessorLock, ProcessorLockError, lock_path_for
from queue_processor.processed_index import index_path_for, iter_records
from queue_processor import main as queue_main
from queue_processor import inspect_runtime, repair, replay, reconciliation
from queue_processor import narrative
from queue_processor import governance
import epistemic
from queue_processor.evidence import CONFIDENCE_SEMANTICALLY_DRIFTED, assess_drift, read_evidence
from queue_processor.governance import (
    ACK_MANAGER_ACKNOWLEDGED_UNRESOLVED_AMBIGUITY,
    DISPUTED_BY_CLIENT,
    DISPUTED_BY_FIELD_REPORT,
    HIGH_OPERATIONAL_RISK,
    NEEDS_REVIEW,
    REQUIRES_CONFIRMATION,
    build_unresolved_report,
    iter_review_records,
    write_acknowledgment,
    write_dispute,
    write_review,
)
from epistemic import (
    ASSUMED,
    CONTRADICTED,
    INFERRED,
    OBSERVED,
    REPORTED_BY_HUMAN,
    STALE,
    UNCONFIRMED,
    classify_statement,
    iter_contradictions,
    transition_temporal_state,
)
from transcription_pipeline import main as pipeline

from test_queue_processor import make_roots, project_markdown_exports, run_jobs, write_job


def write_contradiction_record(
    runtime_root: Path,
    *,
    earlier_id: str,
    later_id: str,
    reason: str,
    capture_id: str | None = None,
    classification: str = CONTRADICTED,
) -> Path:
    contradiction_id = epistemic.slugify(f"{earlier_id}-{later_id}-{reason}")[:180]
    payload = {
        "contradiction_id": contradiction_id,
        "capture_id": capture_id,
        "earlier_id": earlier_id,
        "later_id": later_id,
        "reason": reason,
        "classification": classification,
        "created_at": "2026-05-31T00:00:00+00:00",
        "observational": True,
    }
    path = runtime_root / "contradictions" / f"{contradiction_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_second_processor_blocked(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    first = ProcessorLock(runtime_root)
    first.acquire()
    try:
        with pytest.raises(ProcessorLockError):
            ProcessorLock(runtime_root).acquire()
    finally:
        first.release()


def test_stale_lock_recovery_works(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    lock_path = lock_path_for(runtime_root)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "hostname": socket.gethostname(),
                "started_at": "2026-04-20T00:00:00+00:00",
                "command": "dead command",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    lock = ProcessorLock(runtime_root)
    try:
        assert lock.acquire() == "stale_recovered"
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    finally:
        lock.release()


def test_processed_index_updates_and_dedupe_prefers_index(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    target_path = vault_root / "Journal" / "2026-04-27.md"
    payload = {
        "job_id": "job-index",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-test"},
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Indexed queue note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-index.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)

    records = iter_records(index_path_for(runtime_root))
    assert records[-1]["computed_job_id"]
    assert records[-1]["job_type"] == "append_to_note"
    assert records[-1]["target_path"] == str(target_path)
    assert records[-1]["capture_id"] == "cap-test"

    (runtime_root / "processed" / "corrupt.json").write_text("{not-json", encoding="utf-8")
    write_job(runtime_root / "queue", "job-index-rerun.json", payload)
    stdout, log_text = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id already processed" in stdout
    assert "reason=job-id-already-processed" in log_text
    assert target_path.read_text(encoding="utf-8").count("Indexed queue note.") == 1


def test_replay_behavior_preserved_when_index_missing(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    target_path = vault_root / "Journal" / "2026-04-27.md"
    payload = {
        "job_id": "job-index-missing",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Fallback scan note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "first.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    index_path_for(runtime_root).unlink()

    write_job(runtime_root / "queue", "second.json", payload)
    stdout, _log = run_jobs(project_root, vault_root, runtime_root, log_path)

    assert "job_id already processed" in stdout
    assert target_path.read_text(encoding="utf-8").count("Fallback scan note.") == 1


def test_lineage_propagates_from_audio_to_events_jobs_and_logs(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    audio_path = inbox_dir / "lineage.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=pipeline.configure_logging(tmp_path / "transcription.log"),
        transcribe=lambda _path: "Summit Wire team requires badge access through parking gate and elevator.\n",
        now=time.time(),
    )

    assert handled == 1
    process_log_paths = sorted((local_root / "logs").glob("lineage.*.json"))
    assert len(process_log_paths) == 1
    process_log = json.loads(process_log_paths[0].read_text(encoding="utf-8"))
    capture_id = process_log["capture_id"]
    assert capture_id.startswith("cap-")
    transcript_path = local_root / "audio_processing" / "lineage.m4a.whisper.txt"
    metadata = json.loads(transcript_path.with_suffix(".txt.metadata.json").read_text(encoding="utf-8"))
    assert metadata["capture_id"] == capture_id
    manifest_path = runtime_root = local_root / "runtime"
    assert (manifest_path / "manifests" / f"{capture_id}.json").exists()
    manifest = json.loads((runtime_root / "manifests" / f"{capture_id}.json").read_text(encoding="utf-8"))
    assert manifest["observational"] is True
    assert manifest["artifacts"]["transcript"] == str(transcript_path)
    valid_event = json.loads(sorted((local_root / "events_valid").glob("*.json"))[0].read_text(encoding="utf-8"))
    assert valid_event["capture_id"] == capture_id
    job = json.loads(sorted((local_root / "queue_jobs").glob("*.json"))[0].read_text(encoding="utf-8"))
    assert job["metadata"]["capture_id"] == capture_id


def test_health_reports_critical_conditions(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    (runtime_root / "queue").mkdir(parents=True)
    (vault_root / "Journal").mkdir(parents=True)
    for index in range(2):
        (runtime_root / "queue" / f"job-{index}.json").write_text("{}\n", encoding="utf-8")
    lock_path = lock_path_for(runtime_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": 99999998,
                "hostname": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": "dead command",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    index_path_for(runtime_root).write_text("{bad-json\n", encoding="utf-8")

    result = build_health_report(
        runtime_root,
        vault_root,
        queue_backlog_threshold=1,
        failed_age_hours=24,
        stale_claimed_hours=2,
    )

    assert "stale processor lock" in result.critical
    assert any(item.startswith("queue backlog above threshold") for item in result.critical)
    assert "corrupted processed index" in result.critical


def test_repair_detects_crash_after_write_before_index(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-crash-window",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Write happened before index append.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-crash-window.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    index_path_for(runtime_root).unlink()

    findings, _evidence = repair.analyze(runtime_root, vault_root, mode="full")

    assert any(finding.kind == "missing_index_entry" for finding in findings)
    assert any(finding.kind == "missing_index_entry" and not finding.ambiguous for finding in findings)


def test_repair_detects_orphaned_marker_and_reports_ambiguity(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    orphan_id = "abc123-orphaned-marker"
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text(f"---\nbtq_job_ids:\n  - {orphan_id}\n---\nHuman text remains.\n", encoding="utf-8")

    findings, _evidence = repair.analyze(runtime_root, vault_root, mode="markers-only")

    orphan = [finding for finding in findings if finding.kind == "orphaned_marker"]
    assert orphan
    assert orphan[0].ambiguous is True
    assert "no processed or index evidence" in orphan[0].message


def test_repair_detects_corrupted_index_lines(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    runtime_root.mkdir(parents=True, exist_ok=True)
    index_path_for(runtime_root).write_text("{broken-json\n", encoding="utf-8")

    findings, _evidence = repair.analyze(runtime_root, vault_root, mode="full")

    assert any(finding.kind == "corrupted_index" and finding.severity == "critical" for finding in findings)


def test_repair_dry_run_does_not_rebuild_index(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-dry-run-repair",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Dry run should not rebuild.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-dry-run-repair.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    index_path_for(runtime_root).unlink()

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        repair.run(["--runtime-root", str(runtime_root), "--vault-root", str(vault_root), "--dry-run"])

    assert "missing_index_entry" in stdout.getvalue()
    assert not index_path_for(runtime_root).exists()


def test_repair_force_rebuilds_missing_index_from_processed_file(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-force-repair",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Force rebuild should append index.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-force-repair.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    index_path_for(runtime_root).unlink()

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        repair.run(["--runtime-root", str(runtime_root), "--vault-root", str(vault_root), "--force"])

    assert "Repair actions applied" in stdout.getvalue()
    assert iter_records(index_path_for(runtime_root))


def test_inspect_runtime_reports_stale_artifact(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    stale = runtime_root / "claimed" / "audio" / "old.m4a"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    old_time = time.time() - 48 * 60 * 60
    os.utime(stale, (old_time, old_time))

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        inspect_runtime.run(["--runtime-root", str(runtime_root), "--stale-hours", "1"])

    assert "stale_claimed_audio" in stdout.getvalue()


def test_inspect_runtime_default_does_not_write_events_log(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    stale = runtime_root / "claimed" / "audio" / "old.m4a"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    old_time = time.time() - 48 * 60 * 60
    os.utime(stale, (old_time, old_time))

    result = inspect_runtime.run(["--runtime-root", str(runtime_root), "--stale-hours", "1"])

    assert result == 0
    assert not (runtime_root / "logs" / "queue_processor_events.jsonl").exists()


def test_inspect_runtime_with_emit_events_writes_events_log(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    stale = runtime_root / "claimed" / "audio" / "old.m4a"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    old_time = time.time() - 48 * 60 * 60
    os.utime(stale, (old_time, old_time))

    result = inspect_runtime.run(["--runtime-root", str(runtime_root), "--stale-hours", "1", "--emit-events"])

    events_path = runtime_root / "logs" / "queue_processor_events.jsonl"
    assert result == 0
    assert events_path.exists()
    assert len(events_path.read_text(encoding="utf-8").splitlines()) >= 1


def test_replay_plan_for_failed_job(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    _ = project_root
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text("Existing note.\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-plan",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-replay"},
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Replay candidate note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "failed", "job-replay-plan.json", payload)

    entries = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)

    assert len(entries) == 1
    assert entries[0].capture_id == "cap-replay"
    assert entries[0].mutation_type == "append_to_note"
    assert entries[0].replay_risk_classification == replay.RISK_SAFE
    assert entries[0].target_path == str(target)


def test_replay_plan_allows_create_when_target_missing(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-replay-create-person",
        "job_type": "add_person",
        "idempotency_key": "person-avery-create",
        "payload": {
            "name": "Avery Replay",
            "role": "Cleaner",
            "job": "7060",
            "additional_jobs": ["7071"],
        },
    }
    write_job(runtime_root / "failed", "job-replay-create-person.json", payload)

    entries = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)
    preview = replay.preview_entry(entries[0])

    assert entries[0].mutation_type == "add_person"
    assert entries[0].current_target_state_summary == "target missing; create replay allowed"
    assert entries[0].replay_risk_classification == replay.RISK_SAFE
    assert preview.conflicts_detected == []


def test_replay_create_refuses_existing_target(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "People" / "Replay, Avery.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nname: Avery Replay\n---\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-create-existing-person",
        "job_type": "add_person",
        "payload": {
            "name": "Avery Replay",
            "role": "Cleaner",
        },
    }
    write_job(runtime_root / "failed", "job-replay-create-existing-person.json", payload)

    entry = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0]
    preview = replay.preview_entry(entry)

    assert entry.replay_risk_classification == replay.RISK_MARKER_CONFLICT
    assert "create target already exists" in preview.conflicts_detected


def test_replay_update_missing_target_still_fails(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-replay-update-missing",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/missing.md",
            "content": "Cannot replay onto a missing update target.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "failed", "job-replay-update-missing.json", payload)

    entry = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0]
    preview = replay.preview_entry(entry)

    assert entry.replay_risk_classification == replay.RISK_UNKNOWN_STATE
    assert "target file missing or unknown" in preview.conflicts_detected


def test_replay_execute_create_with_approval_uses_queue_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recording_vault_store: None) -> None:
    monkeypatch.setenv("BTQ_VAULT_MARKDOWN_WRITE", "1")
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "People" / "Replay, Avery.md"
    payload = {
        "job_id": "job-replay-create-execute",
        "job_type": "add_person",
        "idempotency_key": "person-avery-create",
        "payload": {
            "name": "Avery Replay",
            "role": "Cleaner",
            "employment_type": "part_time",
            "status": "active",
            "job": "7060",
            "additional_jobs": ["7071"],
        },
    }
    failed_job = write_job(runtime_root / "failed", "job-replay-create-execute.json", payload)
    preview = replay.preview_entry(replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0])

    result = replay.apply_previews(
        runtime_root,
        [preview],
        approve=True,
        force_dangerous=False,
        project_root=project_root,
        vault_root=vault_root,
        personal_vault_root=vault_root,
    )

    assert result == 0
    project_markdown_exports(vault_root)
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    # person_id is now the readable lastname_firstname form (42e475e), not the opaque per_ id.
    assert "person_id: replay_avery" in text
    assert "first: Avery" in text
    assert "last: Replay" in text
    assert "additional_jobs:" in text
    assert not failed_job.exists()
    assert (runtime_root / "processed" / failed_job.name).exists()


def test_process_all_logs_under_supplied_runtime_root(tmp_path: Path, monkeypatch) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    global_logs_root = tmp_path / "global-logs" / "queue_processor"
    monkeypatch.setattr(queue_main, "DEFAULT_LOGS_ROOT", global_logs_root)
    monkeypatch.setattr(queue_main, "DEFAULT_PERSONAL_VAULT_ROOT", vault_root)

    queue_main.process_all(project_root, vault_root, runtime_root, dry_run=False)

    assert (runtime_root / "logs" / "queue_processor").exists()
    assert not global_logs_root.exists()


def test_run_context_rejects_runtime_log_path_outside_runtime_root(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)

    with pytest.raises(queue_main.QueueProcessorError, match="queue processor log path is outside allowed root"):
        queue_main.RunContext(
            project_root=project_root,
            vault_root=vault_root,
            personal_vault_root=vault_root,
            runtime_root=runtime_root,
            log_path=tmp_path / "outside-logs" / "run.log",
            dry_run=False,
            valid_site_ids=set(),
            site_id_to_opportunities_dir={},
        )


def test_replay_dry_run_generates_diff_without_mutation(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text("Existing note.\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-diff",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Replay diff note.",
            "destination": "journal",
        },
    }
    job_path = write_job(runtime_root / "failed", "job-replay-diff.json", payload)
    entries = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)

    preview = replay.preview_entry(entries[0])

    assert "Replay diff note." in preview.after
    assert "+Replay diff note." in preview.diff
    assert target.read_text(encoding="utf-8") == "Existing note.\n"
    assert entries[0].original_queue_file == str(job_path)


def test_replay_execution_requires_approval(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text("Existing note.\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-approval",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Approval gated note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "failed", "job-replay-approval.json", payload)
    previews = [replay.preview_entry(replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0])]

    result = replay.apply_previews(runtime_root, previews, approve=False, force_dangerous=False)

    assert result == 1
    assert "Approval gated note." not in target.read_text(encoding="utf-8")
    replay_log = (runtime_root / "logs" / "replay_events.jsonl").read_text(encoding="utf-8")
    assert "replay_rejected" in replay_log


def test_replay_dangerous_refusal_for_marker_conflict(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    _ = project_root
    payload = {
        "job_id": "job-replay-conflict",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Missing content with marker.",
            "destination": "journal",
        },
    }
    computed = replay.compute_job_id(payload)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text(f"---\nbtq_job_ids:\n  - {computed}\n---\nHuman removed the content.\n", encoding="utf-8")
    write_job(runtime_root / "failed", "job-replay-conflict.json", payload)
    entry = replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0]

    assert entry.replay_risk_classification == replay.RISK_MARKER_CONFLICT
    preview = replay.preview_entry(entry)
    result = replay.apply_previews(runtime_root, [preview], approve=True, force_dangerous=False)

    assert result == 1
    assert "Missing content with marker." not in target.read_text(encoding="utf-8")
    assert "replay_aborted" in (runtime_root / "logs" / "replay_events.jsonl").read_text(encoding="utf-8")


def test_replay_manifest_gap_candidate(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text("Existing note.\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-manifest",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-gap"},
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Manifest gap note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "failed", "job-replay-manifest.json", payload)
    manifest_dir = runtime_root / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "cap-gap.json").write_text(
        json.dumps({"capture_id": "cap-gap", "observational": True, "queue_jobs": [], "processed_records": []}) + "\n",
        encoding="utf-8",
    )

    entries = replay.build_plan(runtime_root, vault_root, vault_root, capture_id="cap-gap", manifest_gap=True)

    assert len(entries) == 1
    assert entries[0].replay_reason == "manifest does not contain a processed record for this job"


def test_replay_execute_safe_candidate_with_approval(tmp_path: Path) -> None:
    _project_root, vault_root, runtime_root, _log_path = make_roots(tmp_path)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text("Existing note.\n", encoding="utf-8")
    payload = {
        "job_id": "job-replay-execute",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Approved replay note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "failed", "job-replay-execute.json", payload)
    preview = replay.preview_entry(replay.build_plan(runtime_root, vault_root, vault_root, failed_only=True)[0])

    result = replay.apply_previews(runtime_root, [preview], approve=True, force_dangerous=False)

    text = target.read_text(encoding="utf-8")
    assert result == 0
    assert "Approved replay note." in text
    assert "btq_job_ids:" in text
    assert "replay_executed" in (runtime_root / "logs" / "replay_events.jsonl").read_text(encoding="utf-8")


def test_evidence_snapshot_generation_and_intent_preservation(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-evidence",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-evidence"},
        "intent": {
            "category": "safety issue",
            "reason": "floor hazard reported",
            "source_context": "voice memo excerpt",
            "operator_relevance": "site manager review",
            "confidence": "medium",
        },
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Evidence snapshot note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-evidence.json", payload)

    run_jobs(project_root, vault_root, runtime_root, log_path)

    evidence = read_evidence(runtime_root, "cap-evidence", replay.compute_job_id(payload))
    assert evidence is not None
    assert evidence["intent"]["category"] == "safety issue"
    assert evidence["mutation_intent_summary"].startswith("safety issue")
    assert evidence["pre_mutation_fingerprint"]["document_hash"]
    assert evidence["post_mutation_fingerprint"]["document_hash"]
    assert "Evidence snapshot note." in evidence["nearby_content_excerpt"]


def test_fingerprint_drift_detection_and_replay_semantic_classification(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-drift",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-drift"},
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Original drift-sensitive note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-drift.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    job_id = replay.compute_job_id(payload)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text(target.read_text(encoding="utf-8").replace("Original drift-sensitive note.", "Human rewrote this section."), encoding="utf-8")
    write_job(runtime_root / "failed", "job-drift-rerun.json", payload)

    evidence = read_evidence(runtime_root, "cap-drift", job_id)
    drift = assess_drift(evidence, target.read_text(encoding="utf-8"))
    entries = replay.build_plan(runtime_root, vault_root, vault_root, capture_id="cap-drift", failed_only=True)

    assert drift.confidence == CONFIDENCE_SEMANTICALLY_DRIFTED
    assert any("mutation region removed" in indicator for indicator in drift.indicators)
    assert entries[0].replay_risk_classification == replay.RISK_MARKER_CONFLICT
    assert entries[0].confidence_classification == CONFIDENCE_SEMANTICALLY_DRIFTED


def test_reconciliation_report_generation(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-report",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-report"},
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Report evidence note.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-report.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    target = vault_root / "Journal" / "2026-04-27.md"
    target.write_text(target.read_text(encoding="utf-8").replace("Report evidence note.", "Report note was edited by a human."), encoding="utf-8")

    report = reconciliation.generate_report(runtime_root, vault_root, vault_root, capture_id="cap-report")

    assert report.mutation_confidence_summary[CONFIDENCE_SEMANTICALLY_DRIFTED] == 1
    assert report.semantic_drift_findings
    assert report.semantic_drift_findings[0]["capture_id"] == "cap-report"


def test_epistemic_observation_inference_separation() -> None:
    assert classify_statement("Damon did not respond to three calls.").classification == OBSERVED
    assert classify_statement("Damon is presumed resigned because he did not respond.").classification == INFERRED
    assert classify_statement("Client reported water in the office.").classification == REPORTED_BY_HUMAN
    assert classify_statement("Team assumed storm caused the damage.").classification == ASSUMED


def test_contradiction_linkage_and_temporal_transition(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    path = write_contradiction_record(
        runtime_root,
        earlier_id="storm-cause",
        later_id="sink-hose-cause",
        reason="later inspection found hose leak",
        capture_id="cap-contradiction",
    )

    records = iter_contradictions(runtime_root, "cap-contradiction")
    state = transition_temporal_state(
        {
            "classification": ASSUMED,
            "temporal_state": "CURRENTLY_VALID",
            "confidence": "low",
        },
        CONTRADICTED,
        "later inspection found hose leak",
    )

    assert path.exists()
    assert records[0]["earlier_id"] == "storm-cause"
    assert state["classification"] == CONTRADICTED
    assert state["temporal_transitions"][0]["to"] == CONTRADICTED


def test_epistemic_classification_persists_to_evidence_and_narrative(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-epistemic",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-epistemic"},
        "intent": {
            "category": "staffing update",
            "reason": "Damon is presumed resigned because he did not respond.",
            "source_context": "Damon did not respond for 72 hours.",
            "operator_relevance": "schedule review",
            "confidence": "low",
            "epistemic_state": {
                "classification": INFERRED,
                "source_type": "transcript",
                "confidence": "low",
                "derived_from": "Damon did not respond for 72 hours.",
                "timestamp_context": "2026-04-27T00:00:00+00:00",
                "confidence_basis": ["no response for 72 hours"],
                "temporal_state": "CURRENTLY_VALID",
            },
        },
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Damon presumed resigned after no response.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-epistemic.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    job_id = replay.compute_job_id(payload)
    write_contradiction_record(
        runtime_root,
        earlier_id=job_id,
        later_id="job-epistemic-correction",
        reason="later contact contradicted resignation assumption",
        capture_id="cap-epistemic",
    )

    evidence = read_evidence(runtime_root, "cap-epistemic", job_id)
    entries = narrative.build_narrative(runtime_root, capture_id="cap-epistemic", include_contradictions=True)
    rendered = narrative.format_narrative(entries, json_output=False)

    assert evidence is not None
    assert evidence["epistemic_state"]["classification"] == INFERRED
    assert evidence["epistemic_state"]["confidence_basis"] == ["no response for 72 hours"]
    assert entries[0].classification == INFERRED
    assert entries[0].contradictions
    assert "Inferences" in rendered


def test_stale_truth_classification_transition() -> None:
    state = {
        "classification": "CURRENTLY_VALID",
        "temporal_state": "CURRENTLY_VALID",
        "confidence": "medium",
    }
    updated = transition_temporal_state(state, STALE, "staffing shortage resolved")

    assert updated["temporal_state"] == STALE
    assert updated["classification"] == "HISTORICALLY_TRUE"


def test_write_contradiction_dead_surface_is_retired() -> None:
    assert not hasattr(epistemic, "write_contradiction")
    assert callable(classify_statement)
    assert callable(iter_contradictions)
    assert callable(transition_temporal_state)


def test_review_provenance_persistence(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    path = write_review(
        runtime_root,
        review_id="review-damon-presumed-resigned",
        reviewer="manager",
        timestamp="2026-04-27T12:00:00+00:00",
        target_artifact="job-damon-presumed-resigned",
        epistemic_classification_reviewed=INFERRED,
        prior_state="INFERRED",
        proposed_updated_state=REQUIRES_CONFIRMATION,
        rationale="No-response pattern is operationally important but not HR confirmation.",
        supporting_evidence_refs=["runtime/evidence/cap/job.json"],
        contradictory_evidence_refs=["People/Carver, Damon.md"],
        confidence_rationale="Low confidence because evidence is behavioral, not direct confirmation.",
        escalation_state=REQUIRES_CONFIRMATION,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    records = iter_review_records(runtime_root)

    assert payload["reviewer"] == "manager"
    assert payload["prior_state"] == "INFERRED"
    assert payload["proposed_updated_state"] == REQUIRES_CONFIRMATION
    assert payload["contradictory_evidence_refs"] == ["People/Carver, Damon.md"]
    assert records[0]["escalation_state"] == REQUIRES_CONFIRMATION


def test_dispute_visibility_and_conflicting_reviewer_states(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_dispute(
        runtime_root,
        dispute_id="client-disputes-cleaning-complete",
        reviewer="client",
        timestamp="2026-04-27T13:00:00+00:00",
        target_artifact="Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md",
        disputed_state="CURRENTLY_VALID",
        dispute_state=DISPUTED_BY_CLIENT,
        rationale="Client reported supply area was still not addressed.",
        supporting_evidence_refs=["client-email-20260427"],
    )
    write_dispute(
        runtime_root,
        dispute_id="field-disputes-client-cleaning-claim",
        reviewer="field",
        timestamp="2026-04-27T14:00:00+00:00",
        target_artifact="Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md",
        disputed_state=DISPUTED_BY_CLIENT,
        dispute_state=DISPUTED_BY_FIELD_REPORT,
        rationale="Field report says area was cleaned after the client email.",
        supporting_evidence_refs=["field-note-20260427"],
        contradictory_evidence_refs=["client-email-20260427"],
    )

    report = build_unresolved_report(runtime_root, tmp_path / "vault", tmp_path / "personal")
    disputes = report.disputed_operational_states

    assert len(disputes) == 2
    assert {item.summary for item in disputes} == {
        "Client reported supply area was still not addressed.",
        "Field report says area was cleaned after the client email.",
    }


def test_unresolved_report_surfaces_unreviewed_inference_and_acknowledgment_suppresses_it(tmp_path: Path) -> None:
    project_root, vault_root, runtime_root, log_path = make_roots(tmp_path)
    payload = {
        "job_id": "job-unreviewed-inference",
        "job_type": "append_to_note",
        "metadata": {"capture_id": "cap-governance"},
        "intent": {
            "reason": "Damon is presumed resigned because he did not respond.",
            "source_context": "Damon did not respond for 72 hours.",
            "epistemic_state": {
                "classification": INFERRED,
                "source_type": "transcript",
                "confidence": "low",
                "derived_from": "Damon did not respond for 72 hours.",
                "timestamp_context": "2026-04-27T00:00:00+00:00",
                "confidence_basis": ["no response for 72 hours"],
                "temporal_state": "CURRENTLY_VALID",
            },
        },
        "payload": {
            "path": "Journal/2026-04-27.md",
            "content": "Damon presumed resigned after no response.",
            "destination": "journal",
        },
    }
    write_job(runtime_root / "queue", "job-unreviewed-inference.json", payload)
    run_jobs(project_root, vault_root, runtime_root, log_path)
    computed = replay.compute_job_id(payload)

    report = build_unresolved_report(runtime_root, vault_root, vault_root)

    assert any(item.target_artifact == computed for item in report.unreviewed_inferences)

    write_acknowledgment(
        runtime_root,
        acknowledgment_id="ack-unreviewed-inference",
        reviewer="manager",
        target_artifact=computed,
        acknowledgment_type=ACK_MANAGER_ACKNOWLEDGED_UNRESOLVED_AMBIGUITY,
        rationale="Manager saw that this remains an inference, not HR confirmation.",
    )

    acknowledged_report = build_unresolved_report(runtime_root, vault_root, vault_root)

    assert not any(item.target_artifact == computed for item in acknowledged_report.unreviewed_inferences)


def test_escalation_workflow_keeps_claim_unconfirmed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_review(
        runtime_root,
        review_id="review-high-risk-unconfirmed",
        reviewer="manager",
        target_artifact="job-high-risk",
        epistemic_classification_reviewed=UNCONFIRMED,
        prior_state="UNCONFIRMED",
        proposed_updated_state=HIGH_OPERATIONAL_RISK,
        rationale="Operationally important staffing claim lacks direct confirmation.",
        supporting_evidence_refs=["voice-memo"],
        contradictory_evidence_refs=[],
        confidence_rationale="Escalation requests attention; it is not confirmation.",
        escalation_state=HIGH_OPERATIONAL_RISK,
    )
    write_review(
        runtime_root,
        review_id="review-needs-review",
        reviewer="supervisor",
        target_artifact="job-high-risk",
        epistemic_classification_reviewed=UNCONFIRMED,
        prior_state=HIGH_OPERATIONAL_RISK,
        proposed_updated_state=NEEDS_REVIEW,
        rationale="Supervisor wants HR confirmation before using this operationally.",
        supporting_evidence_refs=["voice-memo"],
        contradictory_evidence_refs=[],
        confidence_rationale="Conflicting reviewer state is preserved.",
        escalation_state=NEEDS_REVIEW,
    )

    records = iter_review_records(runtime_root)

    assert [record["proposed_updated_state"] for record in records] == [HIGH_OPERATIONAL_RISK, NEEDS_REVIEW]
    assert all(record["epistemic_classification_reviewed"] == UNCONFIRMED for record in records)


def test_unresolved_report_command_outputs_disputes_json(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    personal_root = tmp_path / "personal"
    write_dispute(
        runtime_root,
        dispute_id="manager-disputes-narrative",
        reviewer="manager",
        target_artifact="Journal/2026-04-27.md",
        disputed_state="INFERRED",
        dispute_state=DISPUTED_BY_CLIENT,
        rationale="Client disputes inferred narrative.",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        governance.run(
            [
                "--runtime-root",
                str(runtime_root),
                "--vault-root",
                str(vault_root),
                "--personal-vault-root",
                str(personal_root),
                "--json",
            ]
        )

    payload = json.loads(stdout.getvalue())
    assert payload["disputed_operational_states"][0]["summary"] == "Client disputes inferred narrative."
