import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from transcription_pipeline import main as pipeline


def make_logger() -> logging.Logger:
    logger = logging.getLogger("transcription-pipeline-tests")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


class CollectHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def make_collecting_logger() -> tuple[logging.Logger, CollectHandler]:
    logger = logging.getLogger("transcription-pipeline-collect-tests")
    logger.handlers.clear()
    handler = CollectHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, handler


def read_single_process_log(local_root: Path, stem: str) -> dict[str, Any]:
    paths = sorted((local_root / "logs").glob(f"{stem}.*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_default_transcription_worker_count_constant_is_two() -> None:
    assert pipeline.DEFAULT_TRANSCRIPTION_WORKER_COUNT == 2


def test_augment_event_attaches_visited_by_when_sidecar_has_person_id() -> None:
    augmented = pipeline.augment_event_with_voice_memo_sidecar(
        {"event_id": "evt-visit-1", "type": "visit_create"},
        {"source": "voice_memo", "capture_id": "vm-test-123", "person_id": "per_test002"},
        transcript_text="walked the branch",
        transcript_path=Path("/tmp/memo.webm.whisper.txt"),
        audio_file_name="memo.webm",
    )

    assert augmented["visited_by"] == "per_test002"


def test_augment_event_omits_visited_by_when_sidecar_lacks_person_id() -> None:
    augmented = pipeline.augment_event_with_voice_memo_sidecar(
        {"event_id": "evt-visit-1", "type": "visit_create"},
        {"source": "voice_memo", "capture_id": "vm-test-123"},
        transcript_text="walked the branch",
        transcript_path=Path("/tmp/memo.webm.whisper.txt"),
        audio_file_name="memo.webm",
    )

    assert "visited_by" not in augmented


def test_parse_args_worker_count_defaults_and_override() -> None:
    assert pipeline.parse_args([]).worker_count == 2
    assert pipeline.parse_args(["--worker-count", "4"]).worker_count == 4


def test_parse_args_worker_count_below_one_degrades_to_one() -> None:
    assert pipeline.parse_args(["--worker-count", "0"]).worker_count == 1


def test_write_process_log_preserves_retry_history_for_same_audio_stem(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    audio_path = tmp_path / "note.m4a"
    transcript_path = tmp_path / "note.m4a.whisper.txt"

    first = pipeline.write_process_log(logs_dir, audio_path, transcript_path, 0, 0, "failed")
    second = pipeline.write_process_log(logs_dir, audio_path, transcript_path, 1, 1, "success")

    assert first != second
    paths = sorted(logs_dir.glob("note.*.json"))
    assert paths == sorted([first, second])
    statuses = {json.loads(path.read_text(encoding="utf-8"))["status"] for path in paths}
    assert statuses == {"failed", "success"}


def test_scan_once_processes_stable_file_end_to_end(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "note.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return (
            "Summit Wire team requires badge access through parking gate and elevator. "
            "Only one key exists. "
            "Second cleaner cannot access. "
            "Work cannot be performed without key holder.\n"
        )

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    assert not audio_path.exists()
    assert (archive_dir / "note.m4a").exists()
    transcript_path = local_root / "audio_processing" / "note.m4a.whisper.txt"
    assert transcript_path.exists()
    metadata_path = transcript_path.with_suffix(f"{transcript_path.suffix}.metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(metadata) == {"capture_id", "audio_file", "source_fingerprint", "transcript_file", "created_at"}
    assert metadata["audio_file"] == str(local_root / "audio_processing" / "note.m4a")
    assert metadata["transcript_file"] == str(transcript_path)
    assert metadata["source_fingerprint"]["path"].endswith("note.m4a")
    assert metadata_path.read_text(encoding="utf-8").endswith("\n")
    valid_events = sorted((local_root / "events_valid").glob("*.json"))
    queue_jobs = sorted((local_root / "queue_jobs").glob("*.json"))
    assert len(valid_events) >= 3
    assert len(queue_jobs) >= 1

    process_log = read_single_process_log(local_root, "note")
    assert process_log["status"] == "success"
    assert process_log["events_created"] >= 3
    assert process_log["jobs_created"] >= 1
    assert process_log["domain_corrections"] == []


def test_scan_once_single_worker_path_unchanged(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "note.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return (
            "Summit Wire team requires badge access through parking gate and elevator. "
            "Only one key exists. "
            "Second cleaner cannot access. "
            "Work cannot be performed without key holder.\n"
        )

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
        worker_count=1,
    )

    assert handled == 1
    assert not audio_path.exists()
    assert (archive_dir / "note.m4a").exists()
    assert (local_root / "audio_processing" / "note.m4a.whisper.txt").exists()


def test_scan_once_concurrent_processes_all_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    old_time = time.time() - 20
    names = ["first.m4a", "second.m4a", "third.m4a"]
    for name in names:
        audio_path = inbox_dir / name
        audio_path.write_bytes(b"fake audio bytes")
        os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return "personal journal: bought cleaning supplies.\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
        worker_count=2,
        transcribe_factory=lambda: fake_transcribe,
    )

    assert handled == 3
    for name in names:
        assert not (inbox_dir / name).exists()
        assert (archive_dir / name).exists()
        assert (local_root / "audio_processing" / f"{name}.whisper.txt").exists()


def test_scan_once_concurrent_isolates_per_file_failure(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    old_time = time.time() - 20
    names = ["good-one.m4a", "bad.m4a", "good-two.m4a"]
    for name in names:
        audio_path = inbox_dir / name
        audio_path.write_bytes(b"fake audio bytes")
        os.utime(audio_path, (old_time, old_time))

    logger, handler = make_collecting_logger()

    def transcribe(path: Path) -> str:
        if path.name == "bad.m4a":
            raise RuntimeError("boom")
        return "personal journal: bought cleaning supplies.\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=logger,
        transcribe=transcribe,
        now=time.time(),
        worker_count=2,
        transcribe_factory=lambda: transcribe,
    )

    assert handled == 3
    assert (archive_dir / "good-one.m4a").exists()
    assert (archive_dir / "good-two.m4a").exists()
    assert not (archive_dir / "bad.m4a").exists()
    assert (local_root / "audio_processing" / "good-one.m4a.whisper.txt").exists()
    assert (local_root / "audio_processing" / "good-two.m4a.whisper.txt").exists()
    assert any("audio remains eligible for retry" in message and "bad.m4a" in message for message in handler.messages)


def test_scan_once_concurrent_uses_distinct_transcriber_instances(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    old_time = time.time() - 20
    names = ["first.m4a", "second.m4a", "third.m4a"]
    for name in names:
        audio_path = inbox_dir / name
        audio_path.write_bytes(b"fake audio bytes")
        os.utime(audio_path, (old_time, old_time))

    instances: list[RecordingTranscriber] = []

    class RecordingTranscriber:
        def __init__(self) -> None:
            self.paths: list[Path] = []

        def __call__(self, path: Path) -> str:
            self.paths.append(path)
            return "personal journal: bought cleaning supplies.\n"

    def factory() -> RecordingTranscriber:
        instance = RecordingTranscriber()
        instances.append(instance)
        return instance

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=RecordingTranscriber(),
        now=time.time(),
        worker_count=2,
        transcribe_factory=factory,
    )

    assert handled == 3
    assert len(instances) >= 3
    assert all(len(instance.paths) <= 1 for instance in instances)
    assert sorted(path.name for instance in instances for path in instance.paths) == names


def test_scan_once_worker_count_above_one_requires_factory(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    def fake_transcribe(_path: Path) -> str:
        return "should not run\n"

    try:
        pipeline.scan_once(
            inbox_dir,
            archive_dir,
            local_root,
            stable_seconds=10.0,
            logger=make_logger(),
            transcribe=fake_transcribe,
            now=time.time(),
            worker_count=2,
            transcribe_factory=None,
        )
    except pipeline.TranscriptionPipelineError as exc:
        assert "transcribe_factory" in str(exc)
    else:
        raise AssertionError("expected TranscriptionPipelineError")


def test_claim_audio_file_concurrent_same_path_single_winner(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    local_runtime_dir = tmp_path / "runtime"
    inbox_dir.mkdir(parents=True)
    source_path = inbox_dir / "same.m4a"
    source_path.write_bytes(b"fake audio bytes")

    def claim() -> Path:
        return pipeline.claim_audio_file(source_path, local_runtime_dir, make_logger())

    results: list[Path] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    assert len(results) == 1
    assert len(errors) == 1
    assert results[0].exists()
    assert results[0].read_bytes() == b"fake audio bytes"
    assert not source_path.exists()
    assert sorted((local_runtime_dir / "claimed" / "audio").glob("same*.m4a")) == results


def test_scan_once_skips_unstable_file(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "fresh.wav"
    audio_path.write_bytes(b"fake audio bytes")

    called = False

    def fake_transcribe(_path: Path) -> str:
        nonlocal called
        called = True
        return "should not run\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 0
    assert called is False
    assert audio_path.exists()


def test_scan_once_skips_previously_processed_file_using_marker(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    local_root.mkdir()

    audio_path = inbox_dir / "note.mp3"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    pipeline.write_processed_marker(audio_path, datetime.now(timezone.utc))

    called = False

    def fake_transcribe(_path: Path) -> str:
        nonlocal called
        called = True
        return "should not run\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    assert called is False
    assert not audio_path.exists()
    assert not pipeline.processed_marker_path(audio_path).exists()
    assert (archive_dir / "note.mp3").exists()
    assert (archive_dir / "note.mp3.processed").exists()


def test_scan_once_does_not_suppress_later_same_filename_after_marker_archived(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    local_root.mkdir()

    audio_path = inbox_dir / "note.mp3"
    audio_path.write_bytes(b"already processed audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))
    pipeline.write_processed_marker(audio_path, datetime.now(timezone.utc))

    calls = {"count": 0}

    def fake_transcribe(_path: Path) -> str:
        calls["count"] += 1
        return "Summit Wire team requires badge access through parking gate and elevator.\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    assert calls["count"] == 0
    assert not audio_path.exists()
    assert not pipeline.processed_marker_path(audio_path).exists()
    assert (archive_dir / "note.mp3").exists()
    assert (archive_dir / "note.mp3.processed").exists()

    audio_path.write_bytes(b"new audio bytes")
    os.utime(audio_path, (old_time, old_time))

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    assert calls["count"] == 1
    assert not audio_path.exists()
    assert (archive_dir / "note-1.mp3").exists()
    assert not (archive_dir / "note-1.mp3.processed").exists()


def test_scan_once_logs_domain_corrections(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "domain.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return "Altuna community health has bct tile in rooms and vinyl plank in exam rooms.\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    process_log = read_single_process_log(local_root, "domain")
    assert {"from": "bct", "to": "VCT"} in process_log["domain_corrections"]
    assert {"from": "vinyl plank", "to": "LVP"} in process_log["domain_corrections"]

    corrections_path = local_root / "audio_processing" / "domain.m4a.whisper.corrections.json"
    assert corrections_path.exists()
    sidecar = json.loads(corrections_path.read_text(encoding="utf-8"))
    assert {"from": "bct", "to": "VCT"} in sidecar


def test_stage_queue_jobs_publishes_with_atomic_replace(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    source_dir = tmp_path / "queue_jobs"
    source_dir.mkdir()
    job_path = source_dir / "job_1.json"
    job_path.write_text('{"job_type":"append_to_note","payload":{}}\n', encoding="utf-8")

    original_replace = pipeline.os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def observe_replace(src: Path, dst: Path) -> None:
        src_path = Path(src)
        dst_path = Path(dst)
        replace_calls.append((src_path, dst_path))
        assert src_path.parent == runtime_root / "temp" / "queue-stage"
        assert dst_path.parent == runtime_root / "queue"
        assert not dst_path.exists()
        assert list((runtime_root / "queue").iterdir()) == []
        original_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", observe_replace)

    staged_paths = pipeline.stage_queue_jobs(runtime_root, [job_path])

    destination = runtime_root / "queue" / "job_1.json"
    assert staged_paths == [destination]
    assert len(replace_calls) == 1
    assert replace_calls[0][1] == destination
    assert destination.read_text(encoding="utf-8") == job_path.read_text(encoding="utf-8")
    assert list((runtime_root / "temp" / "queue-stage").iterdir()) == []


def test_stage_queue_jobs_skips_existing_destination_without_overwrite(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    source_dir = tmp_path / "queue_jobs"
    queue_dir = runtime_root / "queue"
    source_dir.mkdir()
    queue_dir.mkdir(parents=True)
    job_path = source_dir / "job_1.json"
    destination = queue_dir / "job_1.json"
    job_path.write_text('{"new": true}\n', encoding="utf-8")
    destination.write_text('{"existing": true}\n', encoding="utf-8")

    staged_paths = pipeline.stage_queue_jobs(runtime_root, [job_path])

    assert staged_paths == []
    assert destination.read_text(encoding="utf-8") == '{"existing": true}\n'


def test_personal_journal_trigger_at_start_stages_personal_job(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    runtime_root = tmp_path / "runtime"
    inbox_dir.mkdir(parents=True)
    audio_path = inbox_dir / "personal.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def stage_generated(_local_root: Path, job_paths: list[Path]) -> int:
        return len(pipeline.stage_queue_jobs(runtime_root, job_paths))

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=lambda _path: "Personal journal. Today I need to keep this separate.\n",
        process_generated_jobs=stage_generated,
        local_runtime_dir=runtime_root,
        now=time.time(),
    )

    assert handled == 1
    assert (archive_dir / "personal.m4a").exists()
    transcript_path = local_root / "audio_processing" / "personal.m4a.whisper.txt"
    assert transcript_path.read_text(encoding="utf-8") == "Personal journal. Today I need to keep this separate.\n"
    local_jobs = sorted((local_root / "queue_jobs").glob("*.json"))
    assert len(local_jobs) == 1
    assert local_jobs[0].name.startswith("job_personal_personal.m4a_")
    job = json.loads(local_jobs[0].read_text(encoding="utf-8"))
    assert job["job_id"].startswith("personal-")
    assert job["job_type"] == "personal_journal_entry"
    assert job["payload"]["body"] == "Today I need to keep this separate."
    assert job["payload"]["audio_file"] == "personal.m4a"
    assert job["payload"]["raw_transcript_path"] == str(transcript_path)
    assert (runtime_root / "queue" / local_jobs[0].name).exists()
    assert not (local_root / "events_valid").exists()
    assert not (local_root / "queue_jobs" / "job_missed_personal.m4a.json").exists()


def test_personal_journal_body_strips_supported_start_triggers() -> None:
    assert pipeline.personal_journal_body("this is a personal journal: Keep this separate.") == "Keep this separate."
    assert pipeline.personal_journal_body("PERSONAL NOTE - Keep this separate.") == "Keep this separate."
    assert pipeline.personal_journal_body("Operational note. personal journal later only.") is None


WESTERN_GAS_TRANSCRIPT = (
    "This is another voicenote for Western Gas Transmission from the BTQ Office Dashboard. "
    "And I have selected Western Gas Transmission from the site. And the voicenote is just "
    "the fact that Western Gas Transmission is no longer one of my accounts, therefore needs "
    "to be set to be inactive. So if there is a job that can generate an action that will set "
    "the Western Gas site to be inactive, that would be perfect. If not, we're going to have "
    "to create one to be able to turn accounts on and turn accounts off and turn employees on "
    "and turn employees off, because there are also several employees that are no longer with us "
    "as employees and need to be inactive. So they don't show up on my employee directory."
)


def western_gas_sidecar(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "voice_memo",
        "capture_id": "vm-western-gas",
        "routing_flag": "site_tagged",
        "site_id": "7030",
        "site_account": "Wgtco",
        "site_location": "Western Gas Transmission",
        "employee_slugs": [],
        "employee_names": [],
        "captured_at": "2026-05-31T14:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_voice_memo_semantics_stages_reviewable_site_inactive_action(tmp_path: Path, monkeypatch, couchdb_review, couchdb_job_drafts) -> None:
    root = tmp_path
    audio = root / "western-gas.webm"
    audio.write_bytes(b"audio")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTICS_ENABLED", "1")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "rule")

    def fake_process(_transcript_path: Path, _output_root: Path, capture_id: str | None = None):
        assert capture_id == "vm-western-gas"
        return [], [], []

    monkeypatch.setattr(pipeline, "process_transcript", fake_process)

    status, _transcript, events, jobs = pipeline.process_audio_file(
        pipeline.build_fingerprint(audio),
        root / "local",
        make_logger(),
        lambda _path: WESTERN_GAS_TRANSCRIPT,
        archive_dir=root / "archive",
        local_runtime_dir=root / "runtime",
        sidecar_metadata=western_gas_sidecar(),
    )

    assert status == "success"
    assert events == 1
    assert jobs == 1
    semantic_path = root / "runtime" / "voice_memo" / "semantics" / "vm-western-gas.json"
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    assert semantic["status"] == "complete"
    assert semantic["intent"] == "site_status_change"
    assert semantic["target_type"] == "site"
    assert semantic["target_id"] == "7030"
    assert semantic["action"] == "set_inactive"
    assert semantic["review_required"] is True
    assert semantic["action_count"] == 1
    assert semantic["extracted_actions"][0]["job_type"] == "set_entity_status"

    # 334b: the reviewable artifact emitted by run_semantic_pass is now a
    # pending_approval job_draft (not an action_candidate review doc). It carries
    # the same operational context the legacy voice_memo_operator_action
    # candidate did: a voice_memo-sourced set_entity_status proposing site 7030
    # inactive.
    drafts = list(couchdb_job_drafts.drafts.values())
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["type"] == "job_draft"
    assert draft["review_status"] == "pending_approval"
    assert draft["source_kind"] == "voice_memo"
    assert draft["site_id"] == "7030"
    assert draft["job_type"] == "set_entity_status"
    assert draft["payload"]["entity_type"] == "site"
    assert draft["payload"]["entity_id"] == "7030"
    assert draft["payload"]["status"] == "inactive"
    assert "inactive" in draft["message"].lower()

    [job_path] = sorted((root / "local" / "queue_jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "voice_memo_note"
    assert job["metadata"]["semantic_artifact_path"] == str(semantic_path)
    # The voice_memo_note job still references the reviewable item by its CouchDB
    # pseudo-path (one entry, the single reviewable action), built deterministically
    # from the semantic artifact -- unchanged by 334b. Assert that single,
    # self-consistent pseudo-path is present and well-formed rather than reading a
    # (no-longer-projected) filesystem candidate file.
    candidate_paths = job["metadata"]["action_candidate_paths"]
    assert len(candidate_paths) == 1
    pseudo_path = candidate_paths[0]
    assert pseudo_path.startswith("couchdb/btq_field_captures/action_candidate_")
    assert job["payload"]["semantic_intent"] == "site_status_change"
    assert job["payload"]["semantic_primary_intent"] == "site_status_change"
    assert job["payload"]["semantic_action_count"] == 1
    assert job["payload"]["semantic_review_required"] is True
    assert job["payload"]["action_candidate_path"] == pseudo_path
    assert job["payload"]["action_candidate_paths"] == [pseudo_path]


def test_voice_memo_semantic_failure_falls_back_to_voice_memo_note(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    audio = root / "failing-semantics.webm"
    audio.write_bytes(b"audio")

    class FailingEngine:
        engine_name = "test-failing-engine"
        prompt_version = "test"

        def __call__(self, _context):
            raise ValueError("semantic failure")

    monkeypatch.setattr(pipeline, "process_transcript", lambda *_args, **_kwargs: ([], [], []))

    status, _transcript, events, jobs = pipeline.process_audio_file(
        pipeline.build_fingerprint(audio),
        root / "local",
        make_logger(),
        lambda _path: "A plain note that should survive semantic failure.",
        archive_dir=root / "archive",
        local_runtime_dir=root / "runtime",
        sidecar_metadata=western_gas_sidecar(capture_id="vm-semantic-failure"),
        voice_memo_semantic_engine=FailingEngine(),
    )

    assert status == "success"
    assert events == 1
    assert jobs == 1
    semantic_path = root / "runtime" / "voice_memo" / "semantics" / "vm-semantic-failure.json"
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    assert semantic["status"] == "failed"
    assert semantic["error"]["message"] == "semantic failure"
    assert not (root / "runtime" / "reviews" / "action_candidates" / "field_capture").exists()

    [job_path] = sorted((root / "local" / "queue_jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "voice_memo_note"
    assert job["payload"]["semantic_status"] == "failed"
    assert job["payload"]["semantic_error"] == "semantic failure"
    assert job["payload"]["transcript_text"] == "A plain note that should survive semantic failure."


def test_voice_memo_semantics_disabled_does_not_attach_missing_artifact_metadata(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    audio = root / "semantics-disabled.webm"
    audio.write_bytes(b"audio")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTICS_ENABLED", "0")
    monkeypatch.setattr(pipeline, "process_transcript", lambda *_args, **_kwargs: ([], [], []))

    status, _transcript, events, jobs = pipeline.process_audio_file(
        pipeline.build_fingerprint(audio),
        root / "local",
        make_logger(),
        lambda _path: "A site-tagged note with semantics disabled.",
        archive_dir=root / "archive",
        local_runtime_dir=root / "runtime",
        sidecar_metadata=western_gas_sidecar(capture_id="vm-disabled"),
    )

    assert status == "success"
    assert events == 1
    assert jobs == 1
    assert not (root / "runtime" / "voice_memo" / "semantics" / "vm-disabled.json").exists()
    [job_path] = sorted((root / "local" / "queue_jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "voice_memo_note"
    assert "semantic_artifact_path" not in job["payload"]
    assert "semantic_artifact_path" not in job["metadata"]


def test_general_voice_memo_without_extracted_events_still_routes_to_voice_memo_note(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    audio = root / "general.webm"
    audio.write_bytes(b"audio")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTICS_ENABLED", "1")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "rule")
    monkeypatch.setattr(pipeline, "process_transcript", lambda *_args, **_kwargs: ([], [], []))

    status, _transcript, events, jobs = pipeline.process_audio_file(
        pipeline.build_fingerprint(audio),
        root / "local",
        make_logger(),
        lambda _path: "General operator note from the dashboard.",
        archive_dir=root / "archive",
        local_runtime_dir=root / "runtime",
        sidecar_metadata=western_gas_sidecar(
            capture_id="vm-general",
            routing_flag="general",
            site_id="",
            site_account="",
            site_location="",
        ),
    )

    assert status == "success"
    assert events == 1
    assert jobs == 1
    assert not (root / "local" / "queue_jobs" / "job_missed_general.webm.json").exists()
    [job_path] = sorted((root / "local" / "queue_jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "voice_memo_note"
    assert job["payload"]["routing_flag"] == "general"
    assert job["payload"]["semantic_intent"] == "unknown"
    assert job["payload"]["semantic_action_count"] == 0


def test_personal_journal_sidecar_bypasses_voice_memo_semantics(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    audio = root / "personal-sidecar.webm"
    audio.write_bytes(b"audio")

    class ExplodingEngine:
        engine_name = "should-not-run"
        prompt_version = "test"

        def __call__(self, _context):
            raise AssertionError("semantic engine should be bypassed")

    fake_process = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("process_transcript should be bypassed"))
    monkeypatch.setattr(pipeline, "process_transcript", fake_process)

    status, _transcript, events, jobs = pipeline.process_audio_file(
        pipeline.build_fingerprint(audio),
        root / "local",
        make_logger(),
        lambda _path: "Keep this as my private journal entry.",
        archive_dir=root / "archive",
        local_runtime_dir=root / "runtime",
        sidecar_metadata=western_gas_sidecar(capture_id="vm-personal", routing_flag="personal_journal"),
        voice_memo_semantic_engine=ExplodingEngine(),
    )

    assert status == "success"
    assert events == 0
    assert jobs == 1
    assert not (root / "runtime" / "voice_memo" / "semantics").exists()
    [job_path] = sorted((root / "local" / "queue_jobs").glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "personal_journal_entry"
    assert job["payload"]["body"] == "Keep this as my private journal entry."


def test_personal_journal_jobs_do_not_collide_for_reused_audio_filename(tmp_path: Path) -> None:
    queue_jobs_dir = tmp_path / "queue_jobs"
    transcript_path = tmp_path / "personal.m4a.whisper.txt"
    first = pipeline.emit_personal_journal_job(
        queue_jobs_dir,
        "personal.m4a",
        "First personal entry.",
        transcript_path,
        datetime(2026, 4, 26, 14, 0, tzinfo=timezone.utc),
    )
    second = pipeline.emit_personal_journal_job(
        queue_jobs_dir,
        "personal.m4a",
        "Second personal entry.",
        transcript_path,
        datetime(2026, 4, 26, 15, 0, tzinfo=timezone.utc),
    )

    assert first is not None
    assert second is not None
    assert first != second
    assert len(sorted(queue_jobs_dir.glob("job_personal_personal.m4a_*.json"))) == 2


def test_personal_journal_trigger_phrase_later_does_not_trigger_personal_mode(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)
    audio_path = inbox_dir / "ops.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=lambda _path: "Summit Wire team requires badge access through parking gate and elevator. I said personal journal later as words only.\n",
        now=time.time(),
    )

    assert handled == 1
    queue_jobs = sorted((local_root / "queue_jobs").glob("*.json"))
    assert queue_jobs
    assert not any(path.name.startswith("job_personal_") for path in queue_jobs)
    assert sorted((local_root / "events_valid").glob("*.json"))


def test_process_btq_queue_jobs_stages_only_and_does_not_drain_queue(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    job_dir = local_root / "queue_jobs"
    job_dir.mkdir(parents=True)
    job_path = job_dir / "job_1.json"
    job_path.write_text('{"job_type":"append_to_note","payload":{}}\n', encoding="utf-8")

    staged_count = pipeline.process_btq_queue_jobs(local_root, [job_path])

    assert staged_count == 1
    assert (local_root / "runtime" / "queue" / "job_1.json").exists()
    assert not (local_root / "runtime" / "processed" / "job_1.json").exists()
    assert not (local_root / "runtime" / "failed" / "job_1.json").exists()


def test_scan_once_logs_discovery_and_transcription_progress(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "progress.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    logger, handler = make_collecting_logger()

    class FakeTranscriber:
        def __init__(self) -> None:
            self.last_run: Any = None

        def __call__(self, path: Path) -> str:
            logger.info("starting transcription audio=%s enhanced_model=large-v3 compare_mode=False", path)
            text = "Summit Wire team requires badge access through parking gate and elevator.\n"
            self.last_run = pipeline.TranscriptionRun(
                baseline_profile=None,
                enhanced_profile=pipeline.TranscriptionProfile(
                    label="enhanced",
                    model_name="large-v3",
                    options={},
                ),
                baseline_metrics=None,
                enhanced_metrics=pipeline.transcript_metrics(text),
                evaluation=None,
                compare_dir=None,
            )
            logger.info(
                "finished transcription audio=%s chars=%s words=%s compare_dir=%s",
                path,
                self.last_run.enhanced_metrics.character_count,
                self.last_run.enhanced_metrics.word_count,
                None,
            )
            return text

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=logger,
        transcribe=FakeTranscriber(),
        now=time.time(),
    )

    assert handled == 1
    assert any("found stable audio files count=1" in message for message in handler.messages)
    assert any("claiming stable audio path=" in message for message in handler.messages)
    assert any("starting transcription audio=" in message for message in handler.messages)
    assert any("finished transcription audio=" in message for message in handler.messages)


def test_scan_once_emits_missed_capture_job_when_no_events_created(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "missed.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return "This is a walkthrough note with no extracted operational events.\n"

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    missed_job_path = local_root / "queue_jobs" / "job_missed_missed.m4a.json"
    assert missed_job_path.exists()
    missed_job = json.loads(missed_job_path.read_text(encoding="utf-8"))
    assert missed_job["job_type"] == "record_unknown_capture"
    expected_unknown_path = f"Journal/{datetime.now(timezone.utc).date().isoformat()}-unknown.md"
    assert missed_job["payload"]["path"] == expected_unknown_path
    assert missed_job["payload"]["timestamp"]
    assert missed_job["payload"]["audio_file"] == "missed.m4a"
    assert missed_job["payload"]["content"].startswith("---\ntype: unknown_capture\n")
    assert "status: unresolved" in missed_job["payload"]["content"]
    assert "## Original Transcript" in missed_job["payload"]["content"]
    assert "## Normalized Transcript" in missed_job["payload"]["content"]
    assert "## Notes\n#unknown #needs-review" in missed_job["payload"]["content"]


def test_scan_once_emits_site_note_for_site_resolved_memo_without_events(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "Continental 5:7.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return (
            "This is a report for my visit to Continental Metalworks today. "
            "Cody watched the safety briefing and got set up in eHub. "
            "The executive offices were reviewed before service.\n"
        )

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    missed_job_path = local_root / "queue_jobs" / "job_missed_Continental-5-7.m4a.json"
    assert missed_job_path.exists()
    missed_job = json.loads(missed_job_path.read_text(encoding="utf-8"))
    assert missed_job["job_type"] == "append_to_note"
    assert missed_job["payload"]["path"] == "Accounts/Contworks/Locations/7060 - Continental Metalworks/about.md"
    assert missed_job["payload"]["destination"] == "site_note"
    assert missed_job["payload"]["content"].startswith("---\ntype: site_audio_memo\n")
    assert "Site detected: Continental Metalworks" in missed_job["payload"]["content"]
    assert "Cody watched the safety briefing" in missed_job["payload"]["content"]


def test_scan_once_emits_missed_capture_job_for_partial_extraction(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "partial.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    def fake_transcribe(_path: Path) -> str:
        return (
            "Western gas transmission stalls are hard to clean. "
            "There is also a long walkthrough note that does not map cleanly to an event yet.\n"
        )

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    missed_job_path = local_root / "queue_jobs" / "job_missed_partial.m4a.json"
    assert missed_job_path.exists()
    missed_job = json.loads(missed_job_path.read_text(encoding="utf-8"))
    assert missed_job["payload"]["path"] == "Accounts/Wgtco/Locations/7030 - Western Gas Transmission/about.md"
    assert missed_job["payload"]["destination"] == "site_note"
    assert missed_job["payload"]["content"].startswith("---\ntype: site_audio_memo\n")
    assert "status: needs_review" in missed_job["payload"]["content"]
    assert "## Notes\n#partial #needs-review" in missed_job["payload"]["content"]


def test_transcription_enhanced_runs(tmp_path: Path) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio")
    calls: list[tuple[str, dict]] = []

    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        def transcribe(self, _path: str, **kwargs: object) -> dict[str, str]:
            calls.append((self.name, dict(kwargs)))
            return {"text": "Enhanced transcript"}

    class FakeWhisper:
        @staticmethod
        def load_model(name: str) -> FakeModel:
            return FakeModel(name)

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeTorch:
        cuda = FakeCuda()

    original_whisper = sys.modules.get("whisper")
    original_torch = sys.modules.get("torch")
    sys.modules["whisper"] = FakeWhisper
    sys.modules["torch"] = FakeTorch
    try:
        transcriber = pipeline.build_transcriber("base", tmp_path, compare_mode=False)
        transcript = transcriber(audio_path)
    finally:
        if original_whisper is None:
            sys.modules.pop("whisper", None)
        else:
            sys.modules["whisper"] = original_whisper
        if original_torch is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = original_torch

    assert transcript == "Enhanced transcript\n"
    assert calls == [
        (
            "large-v3",
            {
                "fp16": False,
                "beam_size": 5,
                "best_of": 5,
                "temperature": 0.0,
                "language": "en",
                "condition_on_previous_text": True,
                "patience": 2.0,
                "word_timestamps": True,
                "initial_prompt": pipeline.DEFAULT_INITIAL_PROMPT,
            },
        )
    ]


def test_subprocess_transcriber_invokes_worker_and_reads_output(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        output_path = Path(command[command.index("--output-path") + 1])
        payload = {
            "text": "Worker transcript\n",
            "run": {
                "baseline_profile": None,
                "enhanced_profile": {
                    "label": "enhanced",
                    "model_name": "large-v3",
                    "options": {"language": "en"},
                },
                "baseline_metrics": None,
                "enhanced_metrics": {
                    "character_count": 17,
                    "word_count": 2,
                },
                "evaluation": None,
                "compare_dir": None,
            },
        }
        output_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="worker ok\n", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    transcriber = pipeline.SubprocessWhisperTranscriber(
        "base",
        tmp_path,
        compare_mode=False,
        initial_prompt=None,
        timeout_seconds=12,
        logger=make_logger(),
    )
    transcript = transcriber(audio_path)

    assert transcript == "Worker transcript\n"
    assert transcriber.last_run is not None
    assert transcriber.last_run.enhanced_profile.model_name == "large-v3"
    assert commands
    assert commands[0][:3] == [sys.executable, "-m", "transcription_pipeline.worker"]
    assert "--no-initial-prompt" in commands[0]
    assert "--audio-path" in commands[0]
    assert not list((tmp_path / "worker_outputs").glob("*.worker.json"))


def test_subprocess_transcriber_raises_on_worker_failure(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="boom")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    transcriber = pipeline.SubprocessWhisperTranscriber("base", tmp_path, logger=make_logger())

    try:
        transcriber(audio_path)
    except pipeline.WorkerTranscriptionError as exc:
        assert "exit code 2" in str(exc)
    else:
        raise AssertionError("expected WorkerTranscriptionError")


def test_remote_whisper_transcriber_posts_audio_and_returns_text(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    requests: list[Any] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self) -> bytes:
            return b'{"text": " Remote transcript  "}'

    def fake_urlopen(req, timeout):
        requests.append((req, timeout))
        return FakeResponse()

    monkeypatch.setattr(pipeline.request, "urlopen", fake_urlopen)

    transcriber = pipeline.RemoteWhisperTranscriber(
        "http://10.0.0.10:11434",
        "large-v3",
        timeout_seconds=12,
        logger=make_logger(),
    )
    transcript = transcriber(audio_path)

    assert transcript == "Remote transcript\n"
    assert transcriber.last_run is not None
    assert transcriber.last_run.enhanced_profile.label == "remote"
    assert transcriber.last_run.enhanced_profile.model_name == "large-v3"
    assert transcriber.last_run.enhanced_metrics is not None
    assert requests
    req, timeout = requests[0]
    assert req.full_url == "http://10.0.0.10:11434/v1/audio/transcriptions"
    assert timeout == 12
    assert b'name="model"\r\n\r\nlarge-v3' in req.data
    assert b'name="file"; filename="note.m4a"' in req.data
    assert b"fake audio bytes" in req.data


def test_remote_whisper_transcriber_raises_worker_error_on_http_failure(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio")

    def fake_urlopen(req, timeout):
        raise pipeline.HTTPError(req.full_url, 500, "server error", hdrs=None, fp=None)

    monkeypatch.setattr(pipeline.request, "urlopen", fake_urlopen)
    transcriber = pipeline.RemoteWhisperTranscriber("http://10.0.0.10:11434", "large-v3", timeout_seconds=12)

    try:
        transcriber(audio_path)
    except pipeline.WorkerTranscriptionError as exc:
        assert "note.m4a" in str(exc)
        assert "HTTP 500" in str(exc)
    else:
        raise AssertionError("expected WorkerTranscriptionError")


def test_remote_whisper_transcriber_raises_worker_error_on_timeout(tmp_path: Path, monkeypatch) -> None:
    audio_path = tmp_path / "note.m4a"
    audio_path.write_bytes(b"fake audio")

    def fake_urlopen(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(pipeline.request, "urlopen", fake_urlopen)
    transcriber = pipeline.RemoteWhisperTranscriber("http://10.0.0.10:11434", "large-v3", timeout_seconds=12)

    try:
        transcriber(audio_path)
    except pipeline.WorkerTranscriptionError as exc:
        assert "note.m4a" in str(exc)
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected WorkerTranscriptionError")


def test_validate_local_whisper_url_accepts_lan_and_rejects_public() -> None:
    pipeline.validate_local_whisper_url("http://10.0.0.10:11434")
    pipeline.validate_local_whisper_url("http://127.0.0.1:11434")

    for url in ["http://8.8.8.8:11434", "http://whisper.example.test:11434"]:
        try:
            pipeline.validate_local_whisper_url(url)
        except pipeline.TranscriptionPipelineError:
            pass
        else:
            raise AssertionError(f"expected TranscriptionPipelineError for {url}")


def test_transcriber_factory_returns_remote_when_whisper_url_set(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(pipeline, "configure_logging", lambda _path: make_logger())
    monkeypatch.setattr(pipeline, "require_directories", lambda _directories: None)

    def fake_scan_once(*args, **kwargs):
        captured["transcribe"] = kwargs.get("transcribe", args[5])
        captured["factory_transcribe"] = kwargs["transcribe_factory"]()
        return 0

    monkeypatch.setattr(pipeline, "scan_once", fake_scan_once)

    result = pipeline.run(
        [
            "--once",
            "--no-voice-memo-intake",
            "--inbox-dir",
            str(tmp_path / "inbox"),
            "--archive-dir",
            str(tmp_path / "archive"),
            "--local-root",
            str(tmp_path / "local"),
            "--local-runtime-dir",
            str(tmp_path / "runtime"),
            "--log-path",
            str(tmp_path / "logs" / "transcription.log"),
            "--whisper-url",
            "http://10.0.0.10:11434",
        ]
    )

    assert result == 0
    assert isinstance(captured["transcribe"], pipeline.RemoteWhisperTranscriber)
    assert isinstance(captured["factory_transcribe"], pipeline.RemoteWhisperTranscriber)


def test_transcriber_factory_returns_subprocess_when_whisper_url_unset(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.delenv("BTQ_WHISPER_URL", raising=False)
    monkeypatch.setattr(pipeline, "configure_logging", lambda _path: make_logger())
    monkeypatch.setattr(pipeline, "require_directories", lambda _directories: None)

    def fake_scan_once(*args, **kwargs):
        captured["transcribe"] = kwargs.get("transcribe", args[5])
        captured["factory_transcribe"] = kwargs["transcribe_factory"]()
        return 0

    monkeypatch.setattr(pipeline, "scan_once", fake_scan_once)

    result = pipeline.run(
        [
            "--once",
            "--no-voice-memo-intake",
            "--inbox-dir",
            str(tmp_path / "inbox"),
            "--archive-dir",
            str(tmp_path / "archive"),
            "--local-root",
            str(tmp_path / "local"),
            "--local-runtime-dir",
            str(tmp_path / "runtime"),
            "--log-path",
            str(tmp_path / "logs" / "transcription.log"),
        ]
    )

    assert result == 0
    assert isinstance(captured["transcribe"], pipeline.SubprocessWhisperTranscriber)
    assert isinstance(captured["factory_transcribe"], pipeline.SubprocessWhisperTranscriber)


def test_compare_mode_outputs_files(tmp_path: Path) -> None:
    audio_path = tmp_path / "compare.m4a"
    audio_path.write_bytes(b"fake audio")

    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        def transcribe(self, _path: str, **kwargs: object) -> dict[str, str]:
            if self.name == "base":
                return {"text": "Baseline transcript"}
            return {"text": "Enhanced transcript with better wording"}

    class FakeWhisper:
        @staticmethod
        def load_model(name: str) -> FakeModel:
            return FakeModel(name)

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeTorch:
        cuda = FakeCuda()

    original_whisper = sys.modules.get("whisper")
    original_torch = sys.modules.get("torch")
    sys.modules["whisper"] = FakeWhisper
    sys.modules["torch"] = FakeTorch
    try:
        transcriber = pipeline.build_transcriber("base", tmp_path, compare_mode=True)
        transcript = transcriber(audio_path)
    finally:
        if original_whisper is None:
            sys.modules.pop("whisper", None)
        else:
            sys.modules["whisper"] = original_whisper
        if original_torch is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = original_torch

    assert transcript == "Enhanced transcript with better wording\n"
    compare_dir = tmp_path / "transcripts" / datetime.now(timezone.utc).date().isoformat() / "compare"
    assert (compare_dir / "original.txt").read_text(encoding="utf-8") == "Baseline transcript\n"
    assert (compare_dir / "enhanced.txt").read_text(encoding="utf-8") == "Enhanced transcript with better wording\n"
    assert (compare_dir / "diff.txt").exists()


def test_pipeline_output_unchanged_interface(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "interface.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    class FakeModel:
        def __init__(self, name: str) -> None:
            self.name = name

        def transcribe(self, _path: str, **kwargs: object) -> dict[str, str]:
            if self.name == "large-v3":
                return {
                    "text": (
                        "Summit Wire team requires badge access through parking gate and elevator. "
                        "Only one key exists."
                    )
                }
            return {"text": "older transcript"}

    class FakeWhisper:
        @staticmethod
        def load_model(name: str) -> FakeModel:
            return FakeModel(name)

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return False

    class FakeTorch:
        cuda = FakeCuda()

    original_whisper = sys.modules.get("whisper")
    original_torch = sys.modules.get("torch")
    sys.modules["whisper"] = FakeWhisper
    sys.modules["torch"] = FakeTorch
    try:
        transcriber = pipeline.build_transcriber("base", local_root, compare_mode=False)
        handled = pipeline.scan_once(
            inbox_dir,
            archive_dir,
            local_root,
            stable_seconds=10.0,
            logger=make_logger(),
            transcribe=transcriber,
            now=time.time(),
        )
    finally:
        if original_whisper is None:
            sys.modules.pop("whisper", None)
        else:
            sys.modules["whisper"] = original_whisper
        if original_torch is None:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = original_torch

    assert handled == 1
    transcript_path = local_root / "audio_processing" / "interface.m4a.whisper.txt"
    assert transcript_path.read_text(encoding="utf-8").endswith("\n")
    assert not audio_path.exists()
    assert (archive_dir / "interface.m4a").exists()
    process_log = read_single_process_log(local_root, "interface")
    assert process_log["status"] == "success"
    assert process_log["transcription"]["enhanced_profile"]["model_name"] == "large-v3"


def test_scan_once_moves_failed_local_audio_out_of_icloud(tmp_path: Path, monkeypatch) -> None:
    inbox_dir = tmp_path / "BTpipeline" / "inbox" / "voice"
    archive_dir = tmp_path / "BTpipeline" / "archive" / "voice"
    local_root = tmp_path / "local"
    inbox_dir.mkdir(parents=True)

    audio_path = inbox_dir / "retry.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    old_time = time.time() - 20
    os.utime(audio_path, (old_time, old_time))

    calls = {"count": 0}

    def fake_transcribe(_path: Path) -> str:
        calls["count"] += 1
        return "Summit Wire team requires badge access through parking gate and elevator.\n"

    original_process_transcript = pipeline.process_transcript

    def crash_once(*args, **kwargs):
        monkeypatch.setattr(pipeline, "process_transcript", original_process_transcript)
        raise RuntimeError("event processing failed")

    monkeypatch.setattr(pipeline, "process_transcript", crash_once)

    handled = pipeline.scan_once(
        inbox_dir,
        archive_dir,
        local_root,
        stable_seconds=10.0,
        logger=make_logger(),
        transcribe=fake_transcribe,
        now=time.time(),
    )

    assert handled == 1
    assert calls["count"] == 1
    assert not audio_path.exists()
    assert not pipeline.processed_marker_path(audio_path).exists()
    assert (local_root / "runtime" / "failed" / "audio" / "retry.m4a").exists()


def test_initial_prompt_has_no_real_names_and_respects_env(monkeypatch) -> None:
    # Stage F: real customer/employee names must not live in the code default;
    # operators supply hints via BTQ_WHISPER_INITIAL_PROMPT.
    monkeypatch.delenv("BTQ_WHISPER_INITIAL_PROMPT", raising=False)
    default_prompt = pipeline._resolve_initial_prompt()
    for leaked in ("Michele Deter", "Damon", "Maria Hutton", "Summit Wire", "Contworks", "Glenco"):
        assert leaked not in default_prompt

    monkeypatch.setenv("BTQ_WHISPER_INITIAL_PROMPT", "Acme Foods, Jane Doe")
    assert pipeline._resolve_initial_prompt() == "Acme Foods, Jane Doe"
