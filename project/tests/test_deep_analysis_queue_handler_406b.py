"""Verifier-authored tests for 406b: deep_analysis queue job type + handler.

Covers:
  * queue_spec validation table for JOB_DEEP_ANALYSIS
  * registry wiring (JOB_HANDLERS + ALLOWED_JOB_TYPES)
  * handler behaviour with run_deep_analysis monkeypatched to a spy:
      - field mapping (capture_id/photo_asset_id/preset_id/custom_prompt/actor)
      - upload_root == runtime_root/"uploads"; runtime_root passed
      - real run moves the job file to processed_dir
      - dry_run does NOT call the core and does NOT move the file
      - malformed payload raises QueueProcessorError (defensive)
      - a status:"failed" core result does NOT make the handler raise (fail-closed)
"""
from __future__ import annotations

from pathlib import Path

import pytest

import queue_processor.handlers.deep_analysis as handler_mod
import queue_spec
from queue_processor.handlers._shared import QueueJob, QueueProcessorError, RunContext


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

KNOWN_PRESET = queue_spec.DEEP_ANALYSIS_PRESET_IDS[0]  # "condition_detail"


def _base_payload(**overrides: object) -> dict:
    payload = {
        "capture_id": "cap-123",
        "photo_asset_id": "asset-456",
        "actor": "operator:greg",
    }
    payload.update(overrides)
    return payload


def _valid_job_dict(**payload_overrides: object) -> dict:
    return {
        "job_id": "job-1",
        "job_type": queue_spec.JOB_DEEP_ANALYSIS,
        "payload": _base_payload(**payload_overrides),
    }


def _context(tmp_path: Path, *, dry_run: bool = False) -> RunContext:
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return RunContext(
        project_root=tmp_path,
        runtime_root=runtime,
        log_path=runtime / "queue.log",
        dry_run=dry_run,
        valid_site_ids=set(),
        site_id_to_opportunities_dir={},
    )


def _queue_file(context: RunContext, name: str = "job-1.json") -> Path:
    queue_file = context.runtime_root / name
    queue_file.write_text("{}\n", encoding="utf-8")
    return queue_file


def _job(payload: dict, job_id: str = "job-1") -> QueueJob:
    return QueueJob(
        job_id=job_id,
        job_type=queue_spec.JOB_DEEP_ANALYSIS,
        payload=payload,
        metadata={},
        intent={},
    )


class _Spy:
    def __init__(self, result: object = None):
        self.calls: list[dict] = []
        self.result = result

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.result


# --------------------------------------------------------------------------- #
# 1. queue_spec validation table
# --------------------------------------------------------------------------- #

def test_validate_preset_only_accepted() -> None:
    assert queue_spec.validate_job(_valid_job_dict(preset_id=KNOWN_PRESET)) is True


def test_validate_custom_only_accepted() -> None:
    assert queue_spec.validate_job(
        _valid_job_dict(custom_prompt="Describe the floor wax build-up.")
    ) is True


def test_validate_all_known_presets_accepted() -> None:
    for preset_id in queue_spec.DEEP_ANALYSIS_PRESET_IDS:
        assert queue_spec.validate_job(_valid_job_dict(preset_id=preset_id)) is True, preset_id


@pytest.mark.parametrize("missing", ["capture_id", "photo_asset_id", "actor"])
def test_validate_missing_required_field_rejected(missing: str) -> None:
    payload = _base_payload(preset_id=KNOWN_PRESET)
    payload.pop(missing)
    job = {"job_id": "j", "job_type": queue_spec.JOB_DEEP_ANALYSIS, "payload": payload}
    assert queue_spec.validate_job(job) is False


@pytest.mark.parametrize("missing", ["capture_id", "photo_asset_id", "actor"])
def test_validate_empty_required_field_rejected(missing: str) -> None:
    job = _valid_job_dict(preset_id=KNOWN_PRESET, **{missing: "   "})
    assert queue_spec.validate_job(job) is False


def test_validate_neither_prompt_source_rejected() -> None:
    assert queue_spec.validate_job(_valid_job_dict()) is False


def test_validate_both_prompt_sources_rejected() -> None:
    assert queue_spec.validate_job(
        _valid_job_dict(preset_id=KNOWN_PRESET, custom_prompt="extra")
    ) is False


def test_validate_unknown_preset_rejected() -> None:
    assert queue_spec.validate_job(
        _valid_job_dict(preset_id="not_a_real_preset")
    ) is False


def test_validate_empty_custom_no_preset_rejected() -> None:
    assert queue_spec.validate_job(_valid_job_dict(custom_prompt="   ")) is False


def test_validate_non_string_preset_rejected() -> None:
    assert queue_spec.validate_job(_valid_job_dict(preset_id=123)) is False


def test_validate_non_string_custom_rejected() -> None:
    assert queue_spec.validate_job(_valid_job_dict(custom_prompt=["a", "b"])) is False


# --------------------------------------------------------------------------- #
# 2. registry wiring
# --------------------------------------------------------------------------- #

def test_job_type_is_allowed() -> None:
    assert queue_spec.JOB_DEEP_ANALYSIS in queue_spec.ALLOWED_JOB_TYPES


def test_job_schema_required_fields() -> None:
    assert queue_spec.JOB_SCHEMAS[queue_spec.JOB_DEEP_ANALYSIS] == [
        "capture_id",
        "photo_asset_id",
        "actor",
    ]


def test_registry_maps_type_to_handler() -> None:
    from queue_processor.registry import JOB_HANDLERS

    assert (
        JOB_HANDLERS[queue_spec.JOB_DEEP_ANALYSIS]
        is handler_mod.process_deep_analysis_job
    )


# --------------------------------------------------------------------------- #
# 3. handler behaviour
# --------------------------------------------------------------------------- #

def test_handler_maps_fields_and_upload_root_preset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(result={"status": "complete"})
    monkeypatch.setattr(handler_mod, "run_deep_analysis", spy)

    context = _context(tmp_path)
    queue_file = _queue_file(context)
    processed_dir = context.runtime_root / "processed"
    payload = _base_payload(preset_id=KNOWN_PRESET)

    handler_mod.process_deep_analysis_job(queue_file, _job(payload), context, processed_dir)

    assert len(spy.calls) == 1
    kw = spy.calls[0]
    assert kw["capture_id"] == "cap-123"
    assert kw["photo_asset_id"] == "asset-456"
    assert kw["actor"] == "operator:greg"
    assert kw["preset_id"] == KNOWN_PRESET
    assert kw["custom_prompt"] is None
    assert kw["runtime_root"] == context.runtime_root
    assert kw["upload_root"] == context.runtime_root / "uploads"

    # real run moves the job file to processed_dir
    assert (processed_dir / queue_file.name).exists()
    assert not queue_file.exists()


def test_handler_maps_custom_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy()
    monkeypatch.setattr(handler_mod, "run_deep_analysis", spy)

    context = _context(tmp_path)
    queue_file = _queue_file(context)
    processed_dir = context.runtime_root / "processed"
    payload = _base_payload(custom_prompt="Count the floor buffers.")

    handler_mod.process_deep_analysis_job(queue_file, _job(payload), context, processed_dir)

    kw = spy.calls[0]
    assert kw["custom_prompt"] == "Count the floor buffers."
    assert kw["preset_id"] is None


def test_handler_dry_run_does_not_call_core_or_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy()
    monkeypatch.setattr(handler_mod, "run_deep_analysis", spy)

    context = _context(tmp_path, dry_run=True)
    queue_file = _queue_file(context)
    processed_dir = context.runtime_root / "processed"
    payload = _base_payload(preset_id=KNOWN_PRESET)

    handler_mod.process_deep_analysis_job(queue_file, _job(payload), context, processed_dir)

    assert spy.calls == []
    assert queue_file.exists()
    assert not (processed_dir / queue_file.name).exists()


def test_handler_failed_core_result_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(result={"status": "failed", "error": "vision unavailable"})
    monkeypatch.setattr(handler_mod, "run_deep_analysis", spy)

    context = _context(tmp_path)
    queue_file = _queue_file(context)
    processed_dir = context.runtime_root / "processed"
    payload = _base_payload(preset_id=KNOWN_PRESET)

    # must NOT raise — an operator analysis that fails is a visible result, not a queue error
    handler_mod.process_deep_analysis_job(queue_file, _job(payload), context, processed_dir)

    assert len(spy.calls) == 1
    # job still considered done: moved to processed (not retried in a loop)
    assert (processed_dir / queue_file.name).exists()


@pytest.mark.parametrize(
    "payload",
    [
        _base_payload(),  # neither prompt source
        _base_payload(preset_id=KNOWN_PRESET, custom_prompt="both"),  # both sources
        _base_payload(preset_id="unknown_preset"),  # unknown preset
        {"photo_asset_id": "a", "actor": "x", "preset_id": KNOWN_PRESET},  # missing capture_id
    ],
)
def test_handler_malformed_payload_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    spy = _Spy()
    monkeypatch.setattr(handler_mod, "run_deep_analysis", spy)

    context = _context(tmp_path)
    queue_file = _queue_file(context)
    processed_dir = context.runtime_root / "processed"

    with pytest.raises(QueueProcessorError):
        handler_mod.process_deep_analysis_job(queue_file, _job(payload), context, processed_dir)

    # defensive raise happens before any core call and before moving the file
    assert spy.calls == []
    assert queue_file.exists()
