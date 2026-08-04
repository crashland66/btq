"""Tests for the semantic model A/B harness plumbing.

The harness's value is honest failure accounting: a model error must be
recorded (not hidden by LocalModelCaptureEngine's silent rule fallback), and
the compare view must key on action-type agreement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import semantic_model_ab as ab
from processing_core.capture_semantics import LocalModelCaptureEngine


class _FailingClient:
    provider = "mlx"
    model = "test-model"

    def generate_json(self, prompt: str) -> dict:
        raise RuntimeError("model exploded")


class _HappyClient:
    provider = "mlx"
    model = "test-model"

    def generate_json(self, prompt: str) -> dict:
        return {"extracted_actions": []}


def test_recording_client_records_and_reraises() -> None:
    recorder = ab.RecordingClient(_FailingClient())
    with pytest.raises(RuntimeError):
        recorder.generate_json("prompt")
    assert recorder.last_error == "RuntimeError: model exploded"


def test_recording_client_clears_error_on_success() -> None:
    recorder = ab.RecordingClient(_FailingClient())
    with pytest.raises(RuntimeError):
        recorder.generate_json("prompt")
    recorder._client = _HappyClient()
    assert recorder.generate_json("prompt") == {"extracted_actions": []}
    assert recorder.last_error is None


def test_engine_fallback_is_visible_through_recorder() -> None:
    # MUTATION GUARD: the engine swallows the exception and returns the rule
    # result; the recorder is the only witness. Without it a broken model
    # scores as "working" in the A/B.
    recorder = ab.RecordingClient(_FailingClient())
    engine = LocalModelCaptureEngine(recorder)
    from processing_core.capture_semantics import CaptureSemanticInput

    result = engine(
        CaptureSemanticInput(
            capture_id="cap-1",
            source_kind="field_capture_audio",
            source_text="the sink is leaking in the break room",
            site_id="7050",
        )
    )
    assert result is not None  # rule fallback produced a result
    assert recorder.last_error is not None  # ...and the harness knows why


def _row(asset: str, model: str, types: list[str], fallback: bool = False) -> dict:
    return {
        "model": model,
        "audio_asset_id": asset,
        "raw_text": "text",
        "latency_seconds": 1.0,
        "fell_back_to_rules": fallback,
        "model_error": "boom" if fallback else None,
        "issue_type": "other",
        "urgency": "normal",
        "visit_proposed": False,
        "actions": [{"candidate_type": t, "job_type": t, "summary": "", "action_key": "", "proposed_queue_job_error": ""} for t in types],
    }


def test_compare_reports_agreement_and_disagreement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text(
        json.dumps(_row("asset1", "model-a", ["log_site_issue"])) + "\n"
        + json.dumps(_row("asset2", "model-a", ["log_supply_need"])) + "\n"
    )
    b.write_text(
        json.dumps(_row("asset1", "model-b", ["log_site_issue"])) + "\n"
        + json.dumps(_row("asset2", "model-b", [], fallback=True)) + "\n"
    )

    assert ab.compare(a, b) == 0
    out = capsys.readouterr().out
    assert "identical action-type sets on 1/2" in out
    assert "asset2" in out
    assert "fallback=True" in out
