from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_capture import action_candidates as field_action_candidates
from field_capture import approved_job_drafts as field_approved_job_drafts
from field_capture import audio_semantics
from field_capture import draft_staging as field_draft_staging
from field_capture import review_status as field_review_status
from field_capture.site_viewer import audio_asset_id, build_site_payload, render_site_page
from queue_processor import main as queue_processor_main
from queue_spec import validate_job
from processing_core.approved_job_drafts import write_approved_job_draft
from processing_core.action_candidates import (
    STATUS_APPROVED as CANDIDATE_STATUS_APPROVED,
    STATUS_FAILED as CANDIDATE_STATUS_FAILED,
    STATUS_PENDING_REVIEW,
    STATUS_REJECTED,
    action_candidate_payload,
    write_action_candidate_review,
)
from processing_core.draft_staging import queue_job_from_draft
import btq


def capture_docs_from_queue(queue_dir: Path) -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    for path in sorted(queue_dir.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        docs.append(
            {
                "_id": str(metadata.get("capture_id") or path.stem),
                "type": "field_capture",
                "capture_id": str(metadata.get("capture_id") or ""),
                "site_id": str(metadata.get("site_id") or ""),
                "site": str(payload.get("site") or ""),
                "qc_category": str(payload.get("qc_category") or ""),
                "phase": str(payload.get("phase") or ""),
                "note": str(payload.get("note") or ""),
                "captured_at": str(payload.get("captured_at") or ""),
                "exported_at": str(payload.get("exported_at") or ""),
                "photos": payload.get("photos") if isinstance(payload.get("photos"), list) else [],
                "audio": payload.get("audio") if isinstance(payload.get("audio"), list) else [],
            }
        )
    return docs


def write_transcript(
    transcript_dir: Path,
    *,
    asset_id: str = "fca_test",
    raw_text: str = "this shit is fucked again, bathroom two has water under the sink and there ain't no towels",
    status: str = "complete",
) -> Path:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"{asset_id}.json"
    path.write_text(
        json.dumps(
            {
                "type": "field_audio_transcript",
                "site_id": "7050",
                "upload_id": "cap-audio",
                "area": "Restrooms",
                "phase": "issue",
                "audio_asset_id": asset_id,
                "audio_filename": "voice.webm",
                "audio_media_url": "/media/2026-05-02/cap-audio/voice.webm",
                "created_at": "2026-05-02T23:31:00+00:00",
                "transcription_engine": "stub",
                "status": status,
                "raw_text": raw_text,
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeSemanticEngine:
    engine_name = "stub-semantic"

    def __init__(self) -> None:
        self.calls: list[audio_semantics.FieldAudioTranscript] = []

    def __call__(self, transcript: audio_semantics.FieldAudioTranscript) -> audio_semantics.SemanticResult:
        self.calls.append(transcript)
        return audio_semantics.SemanticResult(
            cleaned_internal_note="Bathroom 2 has water under the sink and paper towels are out.",
            client_safe_note="Bathroom 2 requires follow-up for sink-area moisture and paper towel restocking.",
            operational_summary="Supply issue and possible sink leak in Bathroom 2.",
            issue_detected=True,
            issue_type="water",
            urgency="normal",
            suggested_tags=["field-audio/water"],
            action_candidates=[
                "Restock Bathroom 2 paper towels.",
                "Inspect sink area for leak or moisture source.",
            ],
        )


class FakeMlxTextClient:
    provider = "mlx"
    model = "test-model"

    def __init__(self, response):
        if isinstance(response, str):
            self.model = response
            self._response = {}
        else:
            self._response = response

    def generate_json(self, prompt):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class FakeOllamaTextClient:
    provider = "ollama"

    created: dict[str, object] = {}

    def __init__(
        self,
        model: str,
        base_url: str,
        timeout_seconds: float,
        keep_alive: str,
        disable_thinking: bool,
    ) -> None:
        self.model = model
        self._response = valid_model_semantic_dict()
        FakeOllamaTextClient.created = {
            "model": model,
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
            "keep_alive": keep_alive,
            "disable_thinking": disable_thinking,
        }

    def generate_json(self, prompt):
        return self._response


def semantic_transcript(*, person_id: str = "") -> audio_semantics.FieldAudioTranscript:
    return audio_semantics.FieldAudioTranscript(
        site_id="7050",
        upload_id="cap-audio",
        area="Restrooms",
        phase="issue",
        audio_asset_id="fca_model",
        source_transcript_path=Path("/runtime/transcript.json"),
        raw_text="Bathroom two has water under the sink and no paper towels.",
        person_id=person_id,
    )


def valid_model_semantic_dict() -> dict[str, object]:
    return {
        "cleaned_internal_note": "Bathroom two has water under the sink and no paper towels.",
        "client_safe_note": "Bathroom two requires follow-up for moisture and paper towel restocking.",
        "operational_summary": "Water and supply issue identified in Bathroom two.",
        "issue_detected": True,
        "issue_type": "water",
        "urgency": "normal",
        "suggested_tags": ["field-audio/water"],
        "action_candidates": ["Inspect the sink area for moisture source."],
        "visit_proposed": False,
        "visit_type": "",
        "submitter_person_id": "model_should_not_win",
    }


def test_generic_semantic_transform_is_only_writer() -> None:
    assert hasattr(audio_semantics, "semantic_transform_spec")
    assert not hasattr(audio_semantics, "base_semantic_payload")
    assert not hasattr(audio_semantics, "write_success_semantic")


def test_build_semantic_engine_default_returns_rule_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", raising=False)

    engine = audio_semantics.build_semantic_engine()

    assert isinstance(engine, audio_semantics.LocalRuleSemanticEngine)


def test_build_semantic_engine_local_rule_explicit_returns_rule_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")

    engine = audio_semantics.build_semantic_engine()

    assert isinstance(engine, audio_semantics.LocalRuleSemanticEngine)


def test_build_semantic_engine_cloud_raises_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "cloud")

    with pytest.raises(ValueError, match="not implemented"):
        audio_semantics.build_semantic_engine()


def test_build_semantic_engine_unknown_raises_with_supported_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "foobar")

    with pytest.raises(ValueError, match="not a recognized value"):
        audio_semantics.build_semantic_engine()


def test_build_semantic_engine_local_model_defaults_to_vision_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_backends

    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.delenv("BTQ_SEMANTIC_MODEL", raising=False)
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_MODEL", "vision-model")
    monkeypatch.setattr(vision_backends, "MlxTextClient", FakeMlxTextClient)

    engine = audio_semantics.build_semantic_engine()

    assert isinstance(engine, audio_semantics.LocalModelSemanticEngine)
    assert engine.engine_name == "mlx:vision-model:capture-semantics"


def test_build_semantic_engine_local_model_unsupported_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "bogus")
    monkeypatch.setenv("BTQ_SEMANTIC_MODEL", "test-model")

    with pytest.raises(ValueError, match="supported: mlx, ollama"):
        audio_semantics.build_semantic_engine()


def test_build_semantic_engine_local_model_uses_mlx_text_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_backends

    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    monkeypatch.setenv("BTQ_SEMANTIC_MODEL", "test-model")
    monkeypatch.setattr(vision_backends, "MlxTextClient", FakeMlxTextClient)

    engine = audio_semantics.build_semantic_engine()

    assert isinstance(engine, audio_semantics.LocalModelSemanticEngine)
    assert engine.engine_name == "mlx:test-model:capture-semantics"


def test_build_semantic_engine_local_model_uses_ollama_text_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_backends

    FakeOllamaTextClient.created = {}
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "ollama")
    monkeypatch.setenv("BTQ_SEMANTIC_MODEL", "qwen3:4b")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_URL", "http://10.0.0.10:11434")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_KEEP_ALIVE", "10m")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_DISABLE_THINKING", "1")
    monkeypatch.setattr(vision_backends, "OllamaTextClient", FakeOllamaTextClient)

    engine = audio_semantics.build_semantic_engine()

    assert isinstance(engine, audio_semantics.LocalModelSemanticEngine)
    assert engine.engine_name == "ollama:qwen3:4b:capture-semantics"
    assert FakeOllamaTextClient.created == {
        "model": "qwen3:4b",
        "base_url": "http://10.0.0.10:11434",
        "timeout_seconds": 180.0,
        "keep_alive": "10m",
        "disable_thinking": True,
    }


def test_build_semantic_engine_local_model_ollama_can_enable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_backends

    FakeOllamaTextClient.created = {}
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "ollama")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_DISABLE_THINKING", "0")
    monkeypatch.setattr(vision_backends, "OllamaTextClient", FakeOllamaTextClient)

    audio_semantics.build_semantic_engine()

    assert FakeOllamaTextClient.created["disable_thinking"] is False


def test_local_model_engine_returns_valid_semantic_result_on_good_json() -> None:
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(valid_model_semantic_dict()))

    result = engine(semantic_transcript())

    assert result.issue_type == "water"
    assert result.urgency == "normal"
    assert result.action_candidates == ["Inspect the sink area for moisture source."]
    assert engine.engine_name == "mlx:test-model:capture-semantics"


def test_local_model_engine_strips_non_field_operation_actions() -> None:
    raw = valid_model_semantic_dict()
    raw["action_candidates"] = [
        "Add journal entry",
        "Review and test the new transcription process",
        "Inspect the sink area for moisture source.",
    ]
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(raw))

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7080",
            upload_id="cap-test",
            area="Other",
            phase="",
            audio_asset_id="fca_test",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text=(
                "This is a test of the new transcription process and I would like a "
                "journal entry added."
            ),
        )
    )

    assert result.action_candidates == ["Inspect the sink area for moisture source."]


def test_local_model_engine_fails_soft_on_json_decode_error() -> None:
    error = json.JSONDecodeError("bad", "not json", 0)
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(error))

    result = engine(semantic_transcript())

    assert result.issue_type == "supplies"
    assert result.action_candidates == ["Review supply/order follow-up.", "Inspect the affected sink or floor area for moisture source."]


def test_local_model_engine_fails_soft_on_invalid_enum() -> None:
    raw = valid_model_semantic_dict()
    raw["issue_type"] = "not_a_valid_type"
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(raw))

    result = engine(semantic_transcript())

    assert result.issue_type == "supplies"


def test_local_model_engine_fails_soft_on_missing_required_field() -> None:
    raw = valid_model_semantic_dict()
    del raw["issue_type"]
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(raw))

    result = engine(semantic_transcript())

    assert result.issue_type == "supplies"


def test_local_model_engine_fails_soft_on_wrong_type() -> None:
    raw = valid_model_semantic_dict()
    raw["issue_detected"] = "yes"
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(raw))

    result = engine(semantic_transcript())

    assert result.issue_type == "supplies"


def test_local_model_engine_overrides_submitter_person_id_from_transcript() -> None:
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(valid_model_semantic_dict()))

    result = engine(semantic_transcript(person_id="per_correct"))

    assert result.submitter_person_id == "per_correct"


def test_local_model_engine_normalizes_common_model_aliases() -> None:
    raw = valid_model_semantic_dict()
    raw["issue_type"] = "moisture"
    raw["urgency"] = "medium"
    raw["suggested_tags"] = None
    raw["action_candidates"] = None
    raw["visit_type"] = None
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(raw))

    result = engine(semantic_transcript())

    assert result.issue_type == "water"
    assert result.urgency == "normal"
    assert result.suggested_tags == []
    assert result.action_candidates == []
    assert result.visit_type == ""


def test_completed_transcript_discovery(tmp_path: Path) -> None:
    transcript_dir = tmp_path / "runtime" / "field_capture" / "audio_transcripts"
    write_transcript(transcript_dir)
    write_transcript(transcript_dir, asset_id="fca_failed", status="failed")

    transcripts = audio_semantics.discover_completed_transcripts(transcript_dir)

    assert len(transcripts) == 1
    assert transcripts[0].audio_asset_id == "fca_test"
    assert transcripts[0].site_id == "7050"
    assert "bathroom two" in transcripts[0].raw_text


def test_field_audio_transcript_carries_person_id_from_payload(tmp_path: Path) -> None:
    payload = {
        "type": "field_audio_transcript",
        "status": "complete",
        "site_id": "7050",
        "upload_id": "cap-audio",
        "area": "Restrooms",
        "phase": "issue",
        "person_id": "per_test002",
        "audio_asset_id": "fca_test",
        "raw_text": "All done looks good.",
    }

    transcript = audio_semantics.transcript_from_payload(tmp_path / "transcript.json", payload)

    assert transcript is not None
    assert transcript.person_id == "per_test002"


def test_semantic_json_artifact_is_written(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    transcript_path = write_transcript(transcript_dir)
    engine = FakeSemanticEngine()

    counts = audio_semantics.process_semantics(transcript_dir, semantic_dir, engine, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert len(engine.calls) == 1
    assert transcript_path.read_text(encoding="utf-8")
    payload = json.loads((semantic_dir / "fca_test.json").read_text(encoding="utf-8"))
    assert payload["type"] == "field_audio_semantic_summary"
    assert payload["status"] == "complete"
    assert payload["semantic_engine"] == "stub-semantic"
    assert payload["source_transcript_path"] == str(transcript_path)
    assert payload["raw_text_hash"] == audio_semantics.raw_text_hash(engine.calls[0].raw_text)
    assert payload["cleaned_internal_note"] == "Bathroom 2 has water under the sink and paper towels are out."
    assert payload["client_safe_note"].startswith("Bathroom 2 requires follow-up")
    assert payload["issue_detected"] is True
    assert payload["issue_type"] == "water"
    assert payload["action_candidates"] == [
        "Restock Bathroom 2 paper towels.",
        "Inspect sink area for leak or moisture source.",
    ]


def test_semantic_artifact_provenance_carries_prompt_version_for_model_engine(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    write_transcript(transcript_dir)
    engine = audio_semantics.LocalModelSemanticEngine(FakeMlxTextClient(valid_model_semantic_dict()))

    counts = audio_semantics.process_semantics(transcript_dir, semantic_dir, engine, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    payload = json.loads((semantic_dir / "fca_test.json").read_text(encoding="utf-8"))
    assert payload["prompt_version"] == "capture-actions-v1"
    assert payload["source_kind"] == "field_capture_audio"
    assert payload["capture_id"] == "cap-audio"
    assert "extracted_actions" in payload


def test_semantic_artifact_provenance_prompt_version_empty_for_rule_engine(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    write_transcript(transcript_dir)

    counts = audio_semantics.process_semantics(
        transcript_dir,
        semantic_dir,
        audio_semantics.LocalRuleSemanticEngine(),
        runtime_root=runtime_root,
    )

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    payload = json.loads((semantic_dir / "fca_test.json").read_text(encoding="utf-8"))
    assert payload["prompt_version"] == ""


def test_validate_semantic_result_rejects_non_bool_issue_detected() -> None:
    result = audio_semantics.SemanticResult(
        cleaned_internal_note="Note.",
        client_safe_note="Note.",
        operational_summary="Note.",
        issue_detected="yes",
        issue_type="other",
        urgency="low",
        suggested_tags=[],
        action_candidates=[],
    )

    with pytest.raises(TypeError, match="issue_detected must be bool"):
        audio_semantics.validate_semantic_result(result)


def test_validate_semantic_result_rejects_non_list_action_candidates() -> None:
    result = audio_semantics.SemanticResult(
        cleaned_internal_note="Note.",
        client_safe_note="Note.",
        operational_summary="Note.",
        issue_detected=False,
        issue_type="other",
        urgency="low",
        suggested_tags=[],
        action_candidates="some string",
    )

    with pytest.raises(TypeError, match="action_candidates must be a list of str"):
        audio_semantics.validate_semantic_result(result)


def test_field_audio_supply_transcripts_create_one_supply_candidate() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    brightwash = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-brightwash",
            area="Restrooms",
            phase="issue",
            audio_asset_id="fca_brightwash",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="Okay, at Summit Wire, we need to order BrightWash bathroom cleaner.",
        )
    )
    mop_heads = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-mop-heads",
            area="Janitorial",
            phase="issue",
            audio_asset_id="fca_mop_heads",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="We are currently out of fresh mop heads. All mop heads are soiled.",
        )
    )

    assert brightwash.issue_type == "supplies"
    assert brightwash.action_candidates == ["Review supply/order follow-up."]
    assert mop_heads.issue_type == "supplies"
    assert mop_heads.action_candidates == ["Review supply/order follow-up."]


def test_classify_visit_true_for_completion_language() -> None:
    assert audio_semantics.classify_visit("all done, looks good") is True
    assert audio_semantics.classify_visit("The area is finished.") is True
    assert audio_semantics.classify_visit("Everything is good on site.") is True


def test_classify_visit_false_for_issue_language() -> None:
    assert audio_semantics.classify_visit("paper towels are out in bathroom 2") is False
    assert audio_semantics.classify_visit("the sink is leaking") is False


def test_classify_visit_false_for_empty_or_junk() -> None:
    assert audio_semantics.classify_visit("") is False
    assert audio_semantics.classify_visit("test") is False


def test_classify_qc_visit_true_for_inspection_language() -> None:
    assert audio_semantics.classify_qc_visit("just completed the quality check at Citizens Trust Bank") is True
    assert audio_semantics.classify_qc_visit("walked the branch and spoke with the manager") is True
    assert audio_semantics.classify_qc_visit("conducting a qc walk of the facility") is True


def test_classify_qc_visit_false_for_unrelated_language() -> None:
    assert audio_semantics.classify_qc_visit("all done, looks good") is False
    assert audio_semantics.classify_qc_visit("paper towels are out in bathroom 2") is False
    assert audio_semantics.classify_qc_visit("") is False


def test_semantic_result_visit_proposed_true_for_completion_note() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-complete",
            area="Mill floor",
            phase="completion",
            audio_asset_id="fca_complete",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="Summit Wire mill floor all done looks good",
        )
    )

    assert result.visit_proposed is True
    assert result.issue_type == "other"


def test_semantic_result_visit_proposed_true_for_qc_language_despite_quality_issue_type() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-qc",
            area="Branch",
            phase="inspection",
            audio_asset_id="fca_qc",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="just completed the quality check, everything looked great",
        )
    )

    assert result.issue_type == "quality"
    assert result.visit_proposed is True
    assert result.visit_type == "qc_inspection"


def test_semantic_result_visit_type_is_service_completion_for_completion_language() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-complete",
            area="Branch",
            phase="completion",
            audio_asset_id="fca_complete",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="all done, looks good, no issues",
        )
    )

    assert result.visit_type == "service_completion"
    assert result.visit_proposed is True


def test_semantic_result_visit_type_empty_for_issue_language() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-issue",
            area="Restrooms",
            phase="issue",
            audio_asset_id="fca_issue",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="the sink is leaking badly",
        )
    )

    assert result.visit_type == ""
    assert result.visit_proposed is False


def test_semantic_result_includes_submitter_person_id() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-person",
            area="Branch",
            phase="completion",
            audio_asset_id="fca_person",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="all done, looks good",
            person_id="per_test003",
        )
    )

    assert result.submitter_person_id == "per_test003"


def test_semantic_result_submitter_person_id_empty_when_transcript_lacks_it() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-no-person",
            area="Branch",
            phase="completion",
            audio_asset_id="fca_no_person",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="all done, looks good",
        )
    )

    assert result.submitter_person_id == ""


def test_semantic_result_visit_proposed_false_when_issue_detected() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-supplies",
            area="Restrooms",
            phase="issue",
            audio_asset_id="fca_supplies",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="Paper towels are out",
        )
    )

    assert result.visit_proposed is False
    assert result.issue_type == "supplies"


def test_employee_note_no_longer_classified() -> None:
    assert audio_semantics.classify_issue("staff member had an issue today") == "other"


def test_field_audio_after_photo_requires_correction_workflow() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-quality",
            area="Restrooms",
            phase="issue",
            audio_asset_id="fca_quality",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="The restroom was missed. Review the area and capture an after photo once corrected.",
        )
    )

    assert result.issue_type == "quality"
    assert result.action_candidates == ["Review the area and capture an after photo once corrected."]


def test_field_audio_junk_or_test_transcript_produces_no_action_candidates() -> None:
    engine = audio_semantics.LocalRuleSemanticEngine()

    result = engine(
        audio_semantics.FieldAudioTranscript(
            site_id="7050",
            upload_id="cap-test",
            area="Restrooms",
            phase="issue",
            audio_asset_id="fca_test_audio",
            source_transcript_path=Path("/runtime/transcript.json"),
            raw_text="This is just a test recording. I'm recording a note for Summit Wire.",
        )
    )

    assert result.action_candidates == []


def test_already_summarized_transcript_is_skipped(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    write_transcript(transcript_dir)
    first = FakeSemanticEngine()
    audio_semantics.process_semantics(transcript_dir, semantic_dir, first, runtime_root=runtime_root)
    second = FakeSemanticEngine()

    counts = audio_semantics.process_semantics(transcript_dir, semantic_dir, second, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert second.calls == []


def test_failed_semantic_processing_writes_terminal_failed_status(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    write_transcript(transcript_dir)

    class FailingEngine:
        engine_name = "stub-semantic"

        def __call__(self, _transcript: audio_semantics.FieldAudioTranscript) -> audio_semantics.SemanticResult:
            raise RuntimeError("semantic parse failed")

    counts = audio_semantics.process_semantics(transcript_dir, semantic_dir, FailingEngine(), runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    payload = json.loads((semantic_dir / "fca_test.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["error"]["type"] == "RuntimeError"
    assert payload["error"]["message"] == "semantic parse failed"


def test_raw_transcript_is_not_overwritten(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    transcript_path = write_transcript(transcript_dir)
    before = transcript_path.read_text(encoding="utf-8")

    audio_semantics.process_semantics(transcript_dir, semantic_dir, FakeSemanticEngine(), runtime_root=runtime_root)

    assert transcript_path.read_text(encoding="utf-8") == before


def test_semantic_dir_must_stay_under_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    unsafe_semantic_dir = tmp_path / "outside" / "audio_semantics"
    write_transcript(transcript_dir)

    with pytest.raises(ValueError):
        audio_semantics.process_semantics(transcript_dir, unsafe_semantic_dir, FakeSemanticEngine(), runtime_root=runtime_root)

    assert not unsafe_semantic_dir.exists()


def write_capture_for_viewer(queue_dir: Path, upload_dir: Path) -> tuple[str, Path]:
    audio_path = upload_dir / "2026-05-02" / "cap-audio" / "voice.webm"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"audio")
    asset_id = audio_asset_id("cap-audio", "voice.webm", str(audio_path.resolve()))
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / "audio.json").write_text(
        json.dumps(
            {
                "job_id": "audio",
                "job_type": "photo_capture",
                "metadata": {"capture_id": "cap-audio", "site_id": "7050"},
                "payload": {
                    "site": "Summit Wire",
                    "qc_category": "Restrooms",
                    "phase": "issue",
                    "captured_at": "2026-05-02T23:31:41-04:00",
                    "exported_at": "2026-05-02T23:31:41-04:00",
                    "photos": [],
                    "audio": [
                        {
                            "filename": "voice.webm",
                            "mime_type": "audio/webm",
                            "stored_path": str(audio_path),
                            "upload_id": "2026-05-02/cap-audio/voice.webm",
                            "size_bytes": audio_path.stat().st_size,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return asset_id, audio_path


def test_viewer_displays_semantic_layer_when_present(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    queue_dir = runtime_root / "queue"
    upload_dir = runtime_root / "uploads"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    asset_id, _audio_path = write_capture_for_viewer(queue_dir, upload_dir)
    write_transcript(transcript_dir, asset_id=asset_id, raw_text="Bathroom two has water under the sink and no towels.")
    audio_semantics.process_semantics(transcript_dir, semantic_dir, FakeSemanticEngine(), runtime_root=runtime_root)

    payload = build_site_payload("7050", capture_docs_from_queue(queue_dir), upload_dir, transcript_dir, semantic_dir)
    html = render_site_page("7050", payload)

    semantic = payload["dates"][0]["uploads"][0]["audio"][0]["semantic"]
    assert semantic["status"] == "complete"
    assert semantic["issue_type"] == "water"
    assert "AI-assisted semantic cleanup - review needed" in html
    assert "Internal cleaned note" in html
    assert "Client-safe prepared note" in html
    assert "Suggested actions" in html


def test_field_capture_action_candidates_emit_review_artifacts_without_changing_semantic_json(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_transcript(transcript_dir)
    audio_semantics.process_semantics(transcript_dir, semantic_dir, FakeSemanticEngine(), runtime_root=runtime_root)
    semantic_path = semantic_dir / "fca_test.json"
    semantic_before = semantic_path.read_text(encoding="utf-8")

    counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert semantic_path.read_text(encoding="utf-8") == semantic_before
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    review_paths = sorted(candidate_dir.glob("*.json"))
    assert len(review_paths) == 2
    payload = json.loads(review_paths[0].read_text(encoding="utf-8"))
    assert payload["type"] == "action_candidate_review"
    assert payload["status"] == "pending_review"
    assert payload["candidate_type"] == "field_capture_follow_up"
    assert payload["summary"] in {
        "Restock Bathroom 2 paper towels.",
        "Inspect sink area for leak or moisture source.",
    }
    assert payload["provenance"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))
    assert payload["provenance"]["source_transcript_path"] == str(transcript_dir / "fca_test.json")
    assert payload["channel_metadata"]["channel"] == "field_capture"
    assert payload["channel_metadata"]["site_id"] == "7050"
    assert payload["error"] is None


def test_channel_metadata_carries_submitter_person_id() -> None:
    metadata = field_action_candidates.semantic_channel_metadata(
        {
            "site_id": "7050",
            "submitter_person_id": "per_test004",
            "suggested_tags": [],
        }
    )

    assert metadata["submitter_person_id"] == "per_test004"


def test_collect_action_candidates_preserves_reviewed_existing_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    write_transcript(transcript_dir)
    audio_semantics.process_semantics(transcript_dir, semantic_dir, FakeSemanticEngine(), runtime_root=runtime_root)
    first_counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)
    [candidate_path, *_other_paths] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate_id = candidate["candidate_id"]

    review_result = field_action_candidates.review_candidate(
        candidate_dir,
        candidate_id=candidate_id,
        status=CANDIDATE_STATUS_APPROVED,
        reviewer="Jordan",
        rationale="Looks right",
        runtime_root=runtime_root,
    )
    # 308c: candidates are sole-canonical in CouchDB; the approval (review_candidate)
    # mutates the CouchDB doc, and the collector re-run must NOT clobber a decided
    # status back to pending_review (the exists-skip fix). Read from CouchDB.
    approved_before = dict(couchdb_review.docs[f"action_candidate_{candidate_id}"])
    second_counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)
    approved_after = dict(couchdb_review.docs[f"action_candidate_{candidate_id}"])

    assert first_counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert review_result["status"] == CANDIDATE_STATUS_APPROVED
    assert second_counts == {"discovered": 2, "skipped": 2, "completed": 0, "failed": 0}
    # The re-run skipped both existing candidates without touching the approved doc.
    assert approved_after == approved_before
    assert approved_after["status"] == CANDIDATE_STATUS_APPROVED
    assert approved_after["reviewer"] == "Jordan"
    assert approved_after["review_rationale"] == "Looks right"
    assert not (runtime_root / "queue").exists()


def test_field_capture_candidate_collection_writes_failed_record_for_malformed_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_dir.mkdir(parents=True)
    semantic_path = semantic_dir / "fca_test.json"
    semantic_path.write_text(
        json.dumps(
            {
                "type": "field_audio_semantic_summary",
                "site_id": "7050",
                "upload_id": "cap-audio",
                "area": "Restrooms",
                "phase": "issue",
                "audio_asset_id": "fca_test",
                "source_transcript_path": str(runtime_root / "field_capture" / "audio_transcripts" / "fca_test.json"),
                "created_at": "2026-05-02T23:32:00+00:00",
                "semantic_engine": "stub-semantic",
                "raw_text_hash": "abc123",
                "status": "complete",
                "cleaned_internal_note": "Bathroom 2 has water under the sink.",
                "client_safe_note": "Bathroom 2 requires follow-up.",
                "operational_summary": "Possible water issue.",
                "issue_detected": True,
                "issue_type": "water",
                "urgency": "normal",
                "suggested_tags": ["field-audio/water"],
                "action_candidates": [""],
                "error": None,
            }
        ),
        encoding="utf-8",
    )

    counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    assert not (runtime_root / "queue").exists()
    [review_path] = sorted(candidate_dir.glob("*.json"))
    payload = json.loads(review_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["candidate_type"] == "field_capture_follow_up"
    assert payload["summary"] == ""
    assert payload["error"] == {"type": "ValueError", "message": "summary is required"}
    assert payload["provenance"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))


def approved_candidate_payload() -> dict[str, object]:
    payload = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area for leak or moisture source.",
        rationale="Possible water issue.",
        confidence="unknown",
        source_text="Bathroom 2 has water under the sink.",
        source_context="Bathroom 2 requires follow-up.",
        provenance={
            "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json",
            "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
            "audio_asset_id": "fca_test",
        },
        channel_metadata={"channel": "field_capture", "site_id": "7050"},
        status=CANDIDATE_STATUS_APPROVED,
        created_at="2026-05-03T10:00:00+00:00",
    )
    payload["reviewer"] = "manager"
    payload["approved_at"] = "2026-05-03T10:05:00+00:00"
    payload["approval_metadata"] = {
        "proposed_queue_job": {
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/ExampleAccount/Locations/7050 - Summit Wire/about.md",
                "destination": "site_note",
                "content": "Inspect sink area for leak or moisture source.\n",
            },
        }
    }
    return payload


def approved_field_capture_candidate_without_explicit_job() -> dict[str, object]:
    payload = approved_candidate_payload()
    payload.pop("approval_metadata")
    channel_metadata = dict(payload["channel_metadata"])
    channel_metadata.update({"area": "Restrooms", "phase": "issue", "upload_id": "cap-audio"})
    payload["channel_metadata"] = channel_metadata
    return payload


def approved_site_reference_candidate_without_explicit_job() -> dict[str, object]:
    candidate = approved_field_capture_candidate_without_explicit_job()
    candidate["summary"] = "Preserve administrative office map photo for site reference."
    candidate["rationale"] = "Useful site documentation note."
    candidate["source_text"] = "Administrative office map photo should be preserved for site reference."
    candidate["source_context"] = "Office map documentation photo."
    return candidate


def approved_completion_candidate_without_explicit_job() -> dict[str, object]:
    candidate = approved_field_capture_candidate_without_explicit_job()
    candidate["summary"] = "Review the field audio note and decide whether follow-up is needed."
    candidate["rationale"] = "Field audio completion note. Area complete."
    candidate["source_text"] = "Summit Wire mill floor all done looks good."
    candidate["source_context"] = "Summit Wire mill floor completion note."
    candidate["provenance"] = {
        **dict(candidate["provenance"]),
        "audio_asset_id": "audio-abc123",
        "site_id": "7050",
    }
    candidate["channel_metadata"] = {
        **dict(candidate["channel_metadata"]),
        "site_id": "7050",
        "audio_asset_id": "audio-abc123",
        "issue_type": "other",
        "visit_proposed": True,
    }
    return candidate


def approved_summit_drain_candidate_without_explicit_job() -> dict[str, object]:
    candidate = approved_field_capture_candidate_without_explicit_job()
    candidate["candidate_id"] = "ac_386bdf44bf4f08764e5a7bb7"
    candidate["summary"] = "Review the field audio note and decide whether follow-up is needed."
    candidate["rationale"] = "Restroom maintenance follow-up needed."
    candidate["source_text"] = (
        "Drain backed up. Sink drain pushed water onto floor. Missing mop. "
        "Metal stall inoperable. Dusty wire drawing area."
    )
    candidate["source_context"] = (
        "Summit Wire restroom drain backed up, the sink drain pushed water onto the floor, "
        "a mop is missing, the metal stall is inoperable, and maintenance follow-up is needed."
    )
    candidate["review_rationale"] = (
        "Maintenance issue: drain backed up, sink drain pushed water onto floor, "
        "missing mop, metal stall inoperable, dusty wire drawing area, maintenance follow-up needed."
    )
    candidate["channel_metadata"] = {
        **dict(candidate["channel_metadata"]),
        "site_id": "7050",
        "area": "Restrooms",
        "phase": "issue",
        "upload_id": "cap-photo-summit-drain",
        "captured_at": "2026-05-06T18:27:03-04:00",
    }
    return candidate


def write_intake_submitter(runtime_root: Path, *, capture_id: str, person_name: str) -> None:
    intake_dir = runtime_root / "field_capture" / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)
    (intake_dir / "2026-05-06T18-27-03-04-00__photo-capture-summit-wire.json").write_text(
        json.dumps(
            {
                "job_type": "photo_capture",
                "metadata": {
                    "capture_id": capture_id,
                    "site_id": "7050",
                    "person_id": "per_james",
                    "person_name": person_name,
                    "field_capture_token_id": "fct_secretish",
                },
                "payload": {
                    "captured_at": "2026-05-06T18:27:03-04:00",
                    "area": "Restrooms",
                    "phase": "issue",
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_field_capture_approved_candidate_writes_approved_job_draft_only(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    candidate = approved_candidate_payload()
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    counts = field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    [draft_path] = sorted(draft_dir.glob("*.json"))
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["type"] == "approved_queue_job_draft"
    assert payload["status"] == "approved_draft"
    assert payload["candidate_id"] == candidate["candidate_id"]
    assert payload["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert payload["provenance"]["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert payload["provenance"]["semantic_artifact_path"] == "/runtime/field_capture/audio_semantics/fca_test.json"
    assert payload["provenance"]["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"
    assert payload["proposed_job_type"] == "append_to_note"
    assert payload["proposed_payload"] == candidate["approval_metadata"]["proposed_queue_job"]["payload"]
    assert payload["reviewer"] == "manager"
    assert payload["approved_at"] == "2026-05-03T10:05:00+00:00"
    assert payload["error"] is None


def test_field_capture_non_approved_candidates_do_not_create_drafts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    for status in (STATUS_PENDING_REVIEW, STATUS_REJECTED, CANDIDATE_STATUS_FAILED):
        write_action_candidate_review(
            candidate_dir,
            action_candidate_payload(
                candidate_type="field_capture_follow_up",
                summary=f"Review candidate {status}.",
                status=status,
                created_at=f"2026-05-03T10:0{len(list(candidate_dir.glob('*.json')))}:00+00:00",
            ),
        )

    counts = field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 0, "skipped": 3, "completed": 0, "failed": 0}
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_malformed_approved_candidate_writes_failed_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_field_capture_candidate_without_explicit_job()
    channel_metadata = dict(candidate["channel_metadata"])
    channel_metadata.pop("site_id")
    candidate["channel_metadata"] = channel_metadata
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    counts = field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    assert not (runtime_root / "queue").exists()
    [draft_path] = sorted(draft_dir.glob("*.json"))
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert payload["proposed_job_type"] == ""
    assert payload["proposed_payload"] == {}
    assert payload["error"] == {"type": "ValueError", "message": "field_capture site_id is required for default append-to-site-note draft"}


def test_field_capture_default_approved_candidate_maps_to_site_note_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    candidate = approved_site_reference_candidate_without_explicit_job()
    candidate["review_rationale"] = "Reviewed against uploaded photo and field note."
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "would_create"
    draft = result["draft"]
    assert draft["status"] == "approved_draft"
    assert draft["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert draft["proposed_job_type"] == "append_to_note"
    proposed_payload = draft["proposed_payload"]
    assert proposed_payload["path"] == "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
    assert proposed_payload["destination"] == "site_note"
    assert validate_job({"job_type": draft["proposed_job_type"], "payload": proposed_payload})
    assert proposed_payload["content"].startswith("---\n\n## Field Capture Reviews\n\n### Field Capture Review - ")
    assert "- site_id: 7050" in proposed_payload["content"]
    assert "- area: Restrooms" in proposed_payload["content"]
    assert "- capture_id: cap-audio" in proposed_payload["content"]
    assert "- audio_asset_id: fca_test" in proposed_payload["content"]
    assert "Summary: Reviewed against uploaded photo and field note." in proposed_payload["content"]
    assert "Review rationale: Reviewed against uploaded photo and field note." in proposed_payload["content"]
    assert "/runtime/field_capture/audio_semantics/fca_test.json" in proposed_payload["content"]
    assert "/runtime/field_capture/audio_transcripts/fca_test.json" in proposed_payload["content"]
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_field_capture_completion_candidate_maps_to_visit_create_draft() -> None:
    candidate = approved_completion_candidate_without_explicit_job()

    draft = field_approved_job_drafts.draft_payload_from_candidate(Path("/runtime/reviews/action_candidates/ac.json"), candidate)

    assert draft is not None
    assert draft["proposed_job_type"] == "visit_create"
    proposed_payload = draft["proposed_payload"]
    assert proposed_payload["site"] == "7050"
    assert proposed_payload["confidence"] == "medium"
    assert isinstance(proposed_payload["source"], str) and proposed_payload["source"]
    assert isinstance(proposed_payload["evidence"], str) and proposed_payload["evidence"]


def test_field_capture_completion_candidate_without_site_id_falls_through() -> None:
    candidate = approved_completion_candidate_without_explicit_job()
    candidate["channel_metadata"] = {
        **dict(candidate["channel_metadata"]),
        "site_id": "",
    }

    draft = field_approved_job_drafts.draft_payload_from_candidate(Path("/runtime/reviews/action_candidates/ac.json"), candidate)

    assert draft is not None
    assert draft["proposed_job_type"] == "append_to_note"
    assert draft["proposed_payload"]["destination"] == "site_note"


def test_field_capture_non_completion_candidate_does_not_propose_visit() -> None:
    candidate = approved_completion_candidate_without_explicit_job()
    candidate["channel_metadata"] = {
        **dict(candidate["channel_metadata"]),
        "visit_proposed": False,
        "issue_type": "other",
    }

    draft = field_approved_job_drafts.draft_payload_from_candidate(Path("/runtime/reviews/action_candidates/ac.json"), candidate)

    assert draft is not None
    assert draft["proposed_job_type"] == "append_to_note"


def test_field_capture_summit_drain_candidate_maps_to_log_site_issue_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_summit_drain_candidate_without_explicit_job()
    candidate_path = write_action_candidate_review(candidate_dir, candidate)
    write_intake_submitter(runtime_root, capture_id="cap-photo-summit-drain", person_name="Tom Walsh")

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "would_create"
    draft = result["draft"]
    assert draft["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert draft["proposed_job_type"] == "log_site_issue"
    proposed_payload = draft["proposed_payload"]
    assert validate_job({"job_type": "log_site_issue", "payload": proposed_payload})
    assert proposed_payload["site_id"] == "7050"
    assert proposed_payload["title"] == "Restroom drain backup and inoperable stall"
    assert proposed_payload["category"] == "maintenance"
    assert proposed_payload["priority"] == "high"
    assert proposed_payload["status"] == "open"
    assert proposed_payload["observed_at"] == "2026-05-06T18:27:03-04:00"
    assert proposed_payload["reported_by"] == "Tom Walsh"
    assert proposed_payload["source"] == "field_capture"
    assert proposed_payload["client_notified"] is False
    assert proposed_payload["resolution_trigger"] == "Maintenance confirms the drain is clear and the stall is operable."
    assert proposed_payload["related_capture_ids"] == ["cap-photo-summit-drain"]
    assert proposed_payload["related_candidate_ids"] == ["ac_386bdf44bf4f08764e5a7bb7"]
    assert "Drain backed up." in proposed_payload["observations"]
    assert "Sink drain pushed water onto the floor." in proposed_payload["observations"]
    assert "Metal stall is inoperable." in proposed_payload["observations"]
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_log_site_issue_client_notified_uses_notification_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    notification_dir = runtime_root / "reviews" / "client_notifications" / "field_capture"
    candidate = approved_summit_drain_candidate_without_explicit_job()
    write_action_candidate_review(candidate_dir, candidate)
    notification_dir.mkdir(parents=True)
    (notification_dir / f"{candidate['candidate_id']}.json").write_text(
        json.dumps(
            {
                "type": "field_capture_client_notification",
                "channel": "field_capture",
                "candidate_id": candidate["candidate_id"],
                "capture_id": "cap-photo-summit-drain",
                "site_id": "7050",
                "client_informed": True,
                "client_informed_at": "2026-05-08T15:10:00-04:00",
                "client_informed_by": "Jordan",
                "client_informed_method": "email",
                "client_informed_note": "Emailed client with photo/context.",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    [result] = report["results"]
    proposed_payload = result["draft"]["proposed_payload"]
    assert proposed_payload["client_notified"] is True
    assert proposed_payload["client_notified_method"] == "email"
    assert proposed_payload["client_notified_by"] == "Jordan"
    assert proposed_payload["client_notified_note"] == "Emailed client with photo/context."
    assert validate_job({"job_type": result["draft"]["proposed_job_type"], "payload": proposed_payload})
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_default_draft_uses_review_rationale_over_generic_candidate_label(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_field_capture_candidate_without_explicit_job()
    candidate["summary"] = "Capture after photo once corrected."
    candidate["review_rationale"] = "Operational supply note: Summit Wire is out of fresh mop heads and needs supply follow-up."
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    proposed_payload = result["draft"]["proposed_payload"]
    assert validate_job({"job_type": result["draft"]["proposed_job_type"], "payload": proposed_payload})
    content = proposed_payload["content"]
    assert "Summary: Summit Wire is out of fresh mop heads and needs supply follow-up." in content
    assert "Review rationale: Operational supply note: Summit Wire is out of fresh mop heads and needs supply follow-up." in content
    assert "Candidate label:" not in content
    assert "Summary: Capture after photo once corrected." not in content
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_default_draft_falls_back_to_candidate_summary_without_review_rationale(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_site_reference_candidate_without_explicit_job()
    candidate.pop("review_rationale", None)
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    proposed_payload = result["draft"]["proposed_payload"]
    assert validate_job({"job_type": result["draft"]["proposed_job_type"], "payload": proposed_payload})
    assert "Summary: Preserve administrative office map photo for site reference." in proposed_payload["content"]
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_default_draft_uses_unknown_area_when_area_is_missing(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_site_reference_candidate_without_explicit_job()
    channel_metadata = dict(candidate["channel_metadata"])
    channel_metadata.pop("area")
    candidate["channel_metadata"] = channel_metadata
    candidate["review_rationale"] = "Useful site documentation note: Summit Wire administrative office map photo should be preserved for site reference."
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    [result] = report["results"]
    content = result["draft"]["proposed_payload"]["content"]
    assert "- area: Unknown" in content
    assert validate_job({"job_type": result["draft"]["proposed_job_type"], "payload": result["draft"]["proposed_payload"]})
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_default_draft_does_not_carry_supply_area_for_reference_notes(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_field_capture_candidate_without_explicit_job()
    channel_metadata = dict(candidate["channel_metadata"])
    channel_metadata["area"] = "Supply Levels"
    candidate["channel_metadata"] = channel_metadata
    candidate["summary"] = "Review the field audio note and decide whether follow-up is needed."
    candidate["review_rationale"] = "Useful site documentation note: Summit Wire administrative office map photo should be preserved for site reference."
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    [result] = report["results"]
    content = result["draft"]["proposed_payload"]["content"]
    assert "- area: Unknown" in content
    assert "- area: Supply Levels" not in content
    assert "Candidate label:" not in content
    assert "Summary: Summit Wire administrative office map photo should be preserved for site reference." in content
    assert validate_job({"job_type": result["draft"]["proposed_job_type"], "payload": result["draft"]["proposed_payload"]})
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_generated_notes_have_separators_when_combined(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    first = approved_site_reference_candidate_without_explicit_job()
    first["candidate_id"] = "ac_first"
    first["review_rationale"] = "First reviewed note."
    second = approved_site_reference_candidate_without_explicit_job()
    second["candidate_id"] = "ac_second"
    second["review_rationale"] = "Second reviewed note."
    write_action_candidate_review(candidate_dir, first)
    write_action_candidate_review(candidate_dir, second)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    contents = [result["draft"]["proposed_payload"]["content"] for result in report["results"]]
    combined = "".join(contents)
    assert combined.count("---\n\n## Field Capture Reviews") == 2
    assert "First reviewed note." in combined
    assert "---\n\n## Field Capture Reviews" in combined.split("First reviewed note.", 1)[1]
    assert "Second reviewed note." in combined
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_default_approved_candidate_without_site_id_fails_closed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_field_capture_candidate_without_explicit_job()
    channel_metadata = dict(candidate["channel_metadata"])
    channel_metadata.pop("site_id")
    candidate["channel_metadata"] = channel_metadata
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    [result] = report["results"]
    assert result["status"] == "failed"
    assert result["reason"] == "field_capture site_id is required for default append-to-site-note draft"
    assert result["draft"]["status"] == "failed"
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_explicit_proposed_queue_job_overrides_default_mapping(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_candidate_payload()
    explicit_payload = candidate["approval_metadata"]["proposed_queue_job"]["payload"]
    write_action_candidate_review(candidate_dir, candidate)

    report = field_approved_job_drafts.approved_job_drafts_report(
        candidate_dir,
        draft_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    [result] = report["results"]
    assert result["status"] == "would_create"
    assert result["draft"]["proposed_job_type"] == "append_to_note"
    assert result["draft"]["proposed_payload"] == explicit_payload
    assert explicit_payload["path"] == "Accounts/ExampleAccount/Locations/7050 - Summit Wire/about.md"
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_stage_approved_draft_writes_runtime_queue_without_touching_vault(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    counts = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    job = json.loads(queue_path.read_text(encoding="utf-8"))
    assert job["job_type"] == "append_to_note"
    assert job["payload"] == candidate["approval_metadata"]["proposed_queue_job"]["payload"]
    assert job["metadata"]["source"] == "approved_job_draft"
    assert job["metadata"]["candidate_id"] == candidate["candidate_id"]
    assert job["metadata"]["semantic_artifact_path"] == "/runtime/field_capture/audio_semantics/fca_test.json"
    assert job["metadata"]["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"
    [status_path] = sorted(status_dir.glob("*.json"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "staged"
    assert status["queue_path"] == str(queue_path)


def test_field_capture_stage_non_approved_or_failed_drafts_does_not_stage(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    approved = approved_candidate_payload()
    valid_draft = field_approved_job_drafts.draft_payload_from_candidate(Path("/runtime/reviews/action_candidates/ac.json"), approved)
    assert valid_draft is not None
    valid_draft["status"] = "failed"
    valid_draft["draft_id"] = "ajd_failed"
    draft_dir.mkdir(parents=True)
    (draft_dir / "failed.json").write_text(json.dumps(valid_draft), encoding="utf-8")

    counts = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)

    assert counts == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert not (runtime_root / "queue").exists()
    [status_path] = sorted(status_dir.glob("*.json"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["status"] == "skipped"
    assert status["reason"] == "draft status is not approved: failed"


def test_field_capture_stage_same_draft_does_not_duplicate_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    first = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)
    second = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)

    assert first == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert second == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert len(sorted((runtime_root / "queue").glob("*.json"))) == 1


def test_field_capture_stage_approved_drafts_dry_run_reports_would_stage_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    called = {"queue_processor": False}

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        called["queue_processor"] = True
        raise AssertionError("queue processor must not run")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_called)
    write_action_candidate_review(candidate_dir, approved_candidate_payload())
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    exit_code = btq.run(
        [
            "stage-approved-drafts",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--status-dir",
            str(status_dir),
            "--dry-run",
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["dry_run"] is True
    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "would_stage"
    assert result["computed_job_id"]
    assert result["queue_path"].startswith(str(runtime_root / "queue"))
    assert result["job"]["job_type"] == "append_to_note"
    assert not (runtime_root / "queue").exists()
    assert not status_dir.exists()
    assert called["queue_processor"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_field_capture_stage_approved_drafts_dry_run_reports_invalid_job_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["proposed_payload"] = {"path": "Accounts/ExampleAccount/Locations/7050 - Summit Wire/about.md"}
    draft_path.write_text(json.dumps(draft, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert btq.run(["stage-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    assert report["results"][0]["status"] == "failed"
    assert report["results"][0]["reason"] == "proposed queue job does not match queue_spec"
    assert not (runtime_root / "queue").exists()
    assert not status_dir.exists()


def test_field_capture_stage_approved_drafts_dry_run_detects_existing_queue_duplicate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    write_action_candidate_review(candidate_dir, approved_candidate_payload())
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)
    for path in status_dir.glob("*.json"):
        path.unlink()

    assert btq.run(["stage-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert report["results"][0]["status"] == "skipped"
    assert report["results"][0]["reason"] == "already present in runtime/queue"
    assert len(sorted((runtime_root / "queue").glob("*.json"))) == 1
    assert not any(status_dir.glob("*.json"))


@pytest.mark.parametrize("duplicate_root", ["processed", "failed"])
def test_field_capture_stage_approved_drafts_dry_run_detects_processed_or_failed_duplicate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duplicate_root: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    write_action_candidate_review(candidate_dir, approved_candidate_payload())
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=status_dir)
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    duplicate_dir = runtime_root / duplicate_root
    duplicate_dir.mkdir(parents=True)
    duplicate_path = duplicate_dir / queue_path.name
    queue_path.replace(duplicate_path)
    for path in status_dir.glob("*.json"):
        path.unlink()

    assert btq.run(["stage-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert report["results"][0]["status"] == "skipped"
    assert report["results"][0]["reason"] == f"already present in runtime/{duplicate_root}"
    assert not list((runtime_root / "queue").glob("*.json"))
    assert sorted(duplicate_dir.glob("*.json")) == [duplicate_path]
    assert not any(status_dir.glob("*.json"))


def write_semantic_artifact(semantic_dir: Path, asset_id: str = "fca_test", proposed_note_path: str | None = None) -> Path:
    semantic_dir.mkdir(parents=True, exist_ok=True)
    path = semantic_dir / f"{asset_id}.json"
    payload = {
        "type": "field_audio_semantic_summary",
        "site_id": "7050",
        "upload_id": "cap-audio",
        "area": "Restrooms",
        "phase": "issue",
        "audio_asset_id": asset_id,
        "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
        "created_at": "2026-05-02T23:32:00+00:00",
        "semantic_engine": "stub-semantic",
        "raw_text_hash": "abc123",
        "status": "complete",
        "cleaned_internal_note": "Bathroom 2 has water under the sink.",
        "client_safe_note": "Bathroom 2 requires follow-up.",
        "operational_summary": "Possible water issue.",
        "issue_detected": True,
        "issue_type": "water",
        "urgency": "normal",
        "suggested_tags": ["field-audio/water"],
        "action_candidates": ["Inspect sink area."],
        "error": None,
    }
    if proposed_note_path is not None:
        payload["proposed_note_path"] = proposed_note_path
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


def review_candidate(status: str, semantic_path: Path, summary: str) -> dict[str, object]:
    return action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=summary,
        provenance={
            "semantic_artifact_path": str(semantic_path),
            "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
        },
        channel_metadata={"channel": "field_capture", "site_id": "7050"},
        status=status,
        created_at=f"2026-05-03T10:00:0{len(summary)}+00:00",
    )


def test_field_capture_review_status_reports_counts_and_job_states(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    pending = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Pending review.")
    approved = approved_candidate_payload()
    approved["provenance"]["semantic_artifact_path"] = str(semantic_path)
    rejected = review_candidate(STATUS_REJECTED, semantic_path, "Rejected review.")
    failed = review_candidate(CANDIDATE_STATUS_FAILED, semantic_path, "Failed review.")
    for candidate in (pending, approved, rejected, failed):
        write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    failed_draft = field_approved_job_drafts.draft_payload_from_candidate(candidate_dir / "missing-approved.json", approved)
    assert failed_draft is not None
    failed_draft["draft_id"] = "ajd_failed_report"
    failed_draft["status"] = "failed"
    write_approved_job_draft(draft_dir, failed_draft)
    field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=staging_dir)
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    queued_job = json.loads(queue_path.read_text(encoding="utf-8"))
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    processed_dir.mkdir()
    failed_dir.mkdir()
    processed_job = dict(queued_job)
    processed_job["metadata"] = {**processed_job["metadata"], "draft_id": "ajd_processed_report"}
    failed_job = dict(queued_job)
    failed_job["metadata"] = {**failed_job["metadata"], "draft_id": "ajd_failed_job_report"}
    failed_job["payload"] = {**failed_job["payload"], "content": "Unresolved failed job evidence."}
    replayed_failed_job = dict(queued_job)
    replayed_failed_job["metadata"] = {**replayed_failed_job["metadata"], "draft_id": "ajd_processed_report"}
    (processed_dir / "processed.json").write_text(json.dumps(processed_job), encoding="utf-8")
    (failed_dir / "failed.json").write_text(json.dumps(failed_job), encoding="utf-8")
    (failed_dir / "replayed-then-processed.json").write_text(json.dumps(replayed_failed_job), encoding="utf-8")
    (runtime_root / "processed_index.jsonl").write_text(
        json.dumps({"computed_job_id": "idx-test", "job_type": "append_to_note", "target_path": "Accounts/Example/about.md"}) + "\n",
        encoding="utf-8",
    )

    report = field_review_status.review_status_report(runtime_root=runtime_root)

    counts = report["counts"]
    assert counts["semantic_with_action_candidates"] == 1
    assert counts["candidates_pending_review"] == 1
    assert counts["candidates_approved"] == 1
    assert counts["candidates_rejected"] == 1
    assert counts["candidates_failed"] == 1
    assert counts["approved_job_drafts"] == 1
    assert counts["failed_job_drafts"] == 1
    assert counts["staging_staged"] == 1
    assert counts["staging_skipped"] == 1
    assert counts["staged_queue_jobs_runtime_queue"] == 1
    assert counts["staged_jobs_processed"] == 1
    assert counts["staged_jobs_failed"] == 1
    assert counts["staged_jobs_failed_unresolved"] == 1
    assert counts["staged_jobs_failed_replayed_successfully"] == 1
    assert counts["staged_jobs_failed_archive_total"] == 2
    assert counts["processed_index_records"] == 1


def test_field_capture_review_status_detects_lineage_gaps(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    candidate = approved_candidate_payload()
    candidate["provenance"]["semantic_artifact_path"] = str(runtime_root / "field_capture" / "audio_semantics" / "missing.json")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)
    draft = field_approved_job_drafts.draft_payload_from_candidate(candidate_path, candidate)
    assert draft is not None
    draft["candidate_artifact_path"] = str(candidate_dir / "missing-candidate.json")
    draft["provenance"]["candidate_artifact_path"] = str(candidate_dir / "missing-candidate.json")
    write_approved_job_draft(draft_dir, draft)
    staging_dir.mkdir(parents=True)
    (staging_dir / f"{draft['draft_id']}.json").write_text(
        json.dumps(
            {
                "type": "approved_draft_staging_result",
                "draft_id": draft["draft_id"],
                "candidate_id": candidate["candidate_id"],
                "status": "staged",
                "created_at": "2026-05-03T10:00:00+00:00",
                "reason": "staged approved draft into runtime queue",
                "draft_artifact_path": str(draft_dir / "missing-draft.json"),
                "queue_path": str(runtime_root / "queue" / "missing.json"),
                "computed_job_id": "missing-computed",
                "job": None,
            }
        ),
        encoding="utf-8",
    )
    queue_dir = runtime_root / "queue"
    queue_dir.mkdir()
    queue_job = queue_job_from_draft(draft, draft_dir / "missing-draft.json")
    (queue_dir / "queued.json").write_text(json.dumps(queue_job), encoding="utf-8")

    report = field_review_status.review_status_report(runtime_root=runtime_root)

    gap_types = {gap["type"] for gap in report["lineage_gaps"]}
    assert "candidate_missing_semantic" in gap_types
    assert "draft_missing_candidate_artifact" in gap_types
    assert "staging_missing_draft_artifact" in gap_types
    assert "queue_file_missing_draft_artifact" in gap_types


def test_field_capture_review_status_is_read_only_and_json_cli_is_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    write_action_candidate_review(
        runtime_root / "reviews" / "action_candidates" / "field_capture",
        review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Pending review."),
    )

    exit_code = btq.run(["review-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["channel"] == "field_capture"
    assert output["counts"]["candidates_pending_review"] == 1
    assert output["counts"]["approved_job_drafts"] == 0
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_review_maintenance_status_reports_stale_unresolved_orphaned_and_disk_usage_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    queue_dir = runtime_root / "queue"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")

    stale_pending = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Stale pending.")
    stale_pending["created_at"] = "2000-01-01T00:00:00+00:00"
    approved_without_draft = approved_candidate_payload()
    approved_without_draft["candidate_id"] = "ac_without_draft"
    old_rejected = review_candidate(STATUS_REJECTED, semantic_path, "Old rejected.")
    old_rejected["created_at"] = "2000-01-02T00:00:00+00:00"
    failed_candidate = action_candidate_payload(candidate_type="field_capture_follow_up", summary="")
    draft_candidate = approved_candidate_payload()
    draft_candidate["candidate_id"] = "ac_draft_without_staging"
    draft_candidate["approval_metadata"]["proposed_queue_job"]["payload"]["content"] = "Draft without staging.\n"
    staged_candidate = approved_candidate_payload()
    staged_candidate["candidate_id"] = "ac_staged_without_evidence"
    staged_candidate["approval_metadata"]["proposed_queue_job"]["payload"]["content"] = "Staged without evidence.\n"
    failed_draft_candidate = approved_candidate_payload()
    failed_draft_candidate["candidate_id"] = "ac_failed_draft"
    failed_draft_candidate.pop("approval_metadata")
    failed_channel_metadata = dict(failed_draft_candidate["channel_metadata"])
    failed_channel_metadata.pop("site_id")
    failed_draft_candidate["channel_metadata"] = failed_channel_metadata
    for candidate in (
        stale_pending,
        approved_without_draft,
        old_rejected,
        failed_candidate,
        draft_candidate,
        staged_candidate,
        failed_draft_candidate,
    ):
        if candidate.get("candidate_id"):
            write_action_candidate_review(candidate_dir, candidate)
        else:
            candidate_dir.mkdir(parents=True, exist_ok=True)
            (candidate_dir / "failed-missing-id.json").write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    for path in draft_dir.glob("*.json"):
        draft_payload = json.loads(path.read_text(encoding="utf-8"))
        if draft_payload.get("candidate_id") == "ac_without_draft":
            path.unlink()
    drafts = {json.loads(path.read_text(encoding="utf-8"))["candidate_id"]: json.loads(path.read_text(encoding="utf-8")) for path in draft_dir.glob("*.json")}
    staged_draft = drafts["ac_staged_without_evidence"]
    staging_dir.mkdir(parents=True)
    (staging_dir / f"{staged_draft['draft_id']}.json").write_text(
        json.dumps(
            {
                "type": "approved_draft_staging_result",
                "draft_id": staged_draft["draft_id"],
                "candidate_id": staged_draft["candidate_id"],
                "status": "staged",
                "created_at": "2026-05-03T10:00:00+00:00",
                "reason": "staged approved draft into runtime queue",
                "draft_artifact_path": str(draft_dir / f"{staged_draft['draft_id']}.json"),
                "queue_path": str(queue_dir / "missing.json"),
                "computed_job_id": "missing-computed",
                "job": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (staging_dir / "orphan.json").write_text(
        json.dumps(
            {
                "type": "approved_draft_staging_result",
                "draft_id": "ajd_orphan",
                "candidate_id": "ac_orphan",
                "status": "skipped",
                "draft_artifact_path": str(draft_dir / "missing.json"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    queue_dir.mkdir()
    (queue_dir / "missing-draft.json").write_text(
        json.dumps(
            {
                "job_id": "ajd_missing_queue",
                "job_type": "append_to_note",
                "payload": {"path": "Accounts/Example/about.md", "destination": "site_note", "content": "queued\n"},
                "metadata": {"draft_id": "ajd_missing_queue", "draft_artifact_path": str(draft_dir / "missing-queue-draft.json")},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    before_candidates = {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))}
    before_drafts = {path: path.read_text(encoding="utf-8") for path in sorted(draft_dir.glob("*.json"))}
    before_staging = {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))}
    before_queue = {path: path.read_text(encoding="utf-8") for path in sorted(queue_dir.glob("*.json"))}

    assert btq.run(["review-maintenance-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--stale-days", "1", "--include-paths", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["channel"] == "field_capture"
    assert report["stale_days"] == 1
    counts = report["counts"]
    assert counts["candidates_total"] == 7
    assert counts["stale_pending_candidates"] == 1
    assert counts["approved_candidates_without_draft"] == 1
    assert counts["old_rejected_candidates"] == 1
    assert counts["candidates_failed"] == 1
    assert counts["drafts_total"] == 3
    assert counts["drafts_approved"] == 2
    assert counts["drafts_failed"] == 1
    assert counts["approved_drafts_without_staging_status"] == 1
    assert counts["staged_drafts_without_queue_processed_failed_index_evidence"] == 1
    assert counts["orphaned_staging_statuses"] == 1
    assert counts["queue_files_missing_drafts"] == 1
    finding_types = {item["type"] for item in report["findings"]}
    assert {
        "stale_pending_candidate",
        "approved_candidate_missing_draft",
        "old_rejected_candidate",
        "failed_candidate",
        "approved_draft_missing_staging_status",
        "failed_draft",
        "staged_draft_missing_queue_processed_failed_evidence",
        "orphaned_staging_status",
        "queue_file_missing_draft",
    }.issubset(finding_types)
    assert all("artifact_path" in item for item in report["findings"])
    assert report["disk_usage"]["action_candidates"]["files"] == 7
    assert report["disk_usage"]["approved_job_drafts"]["files"] == 3
    assert report["disk_usage"]["staging"]["files"] == 2
    assert report["oldest"]["candidate_dir"]
    assert report["newest"]["draft_dir"]
    assert {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))} == before_candidates
    assert {path: path.read_text(encoding="utf-8") for path in sorted(draft_dir.glob("*.json"))} == before_drafts
    assert {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))} == before_staging
    assert {path: path.read_text(encoding="utf-8") for path in sorted(queue_dir.glob("*.json"))} == before_queue
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_review_maintenance_status_human_output_omits_paths_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    pending = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Stale pending.")
    pending["created_at"] = "2000-01-01T00:00:00+00:00"
    candidate_path = write_action_candidate_review(candidate_dir, pending)

    assert btq.run(["review-maintenance-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--stale-days", "1"]) == 0
    output = capsys.readouterr().out

    assert "field-capture review maintenance" in output
    assert "stale_pending_candidates: 1" in output
    assert "stale_pending_candidate" in output
    assert str(candidate_path) not in output


def test_review_dashboard_reports_pending_candidates_limits_preview_and_suggests_review(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    first = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "First pending.")
    second = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Second pending.")
    write_action_candidate_review(candidate_dir, first)
    write_action_candidate_review(candidate_dir, second)
    before_candidates = {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))}

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--limit", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["channel"] == "field_capture"
    assert report["candidate_summary"]["pending_review"] == 2
    assert len(report["candidate_preview"]) == 1
    assert report["candidate_preview"][0]["candidate_id"] == min(str(first["candidate_id"]), str(second["candidate_id"]))
    assert report["draft_preview"] == []
    assert report["suggested_next_command"] == "./scripts/btq list-action-candidates --channel field_capture --status pending_review"
    assert {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))} == before_candidates
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_review_dashboard_suggests_collect_when_semantics_exist_without_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["review_status"]["counts"]["semantic_with_action_candidates"] == 1
    assert report["candidate_summary"]["pending_review"] == 0
    assert report["suggested_next_command"] == "./scripts/btq collect-action-candidates --channel field_capture --dry-run"
    assert not (runtime_root / "reviews" / "action_candidates").exists()


def test_review_dashboard_reports_approved_candidate_without_draft_and_suggests_generation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["candidate_summary"]["approved"] == 1
    assert report["maintenance"]["finding_count"] >= 1
    assert report["maintenance"]["orphan_or_lineage_issue_count"] >= 1
    assert report["suggested_next_command"] == "./scripts/btq generate-approved-drafts --channel field_capture --dry-run"
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "queue").exists()


def test_review_dashboard_reports_not_staged_draft_and_suggests_staging_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["draft_summary"]["approved_draft"] == 1
    assert report["draft_summary"]["queue_state_counts"]["not_staged"] == 1
    assert len(report["draft_preview"]) == 1
    assert report["draft_preview"][0]["candidate_id"] == candidate["candidate_id"]
    assert report["suggested_next_command"] == "./scripts/btq stage-approved-drafts --channel field_capture --dry-run"
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()


def test_review_dashboard_counts_replayed_failure_as_historical_not_current(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    processed_dir.mkdir(parents=True)
    failed_dir.mkdir(parents=True)
    candidate = approved_candidate_payload()
    candidate_path = write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    job = {
        "job_type": draft["proposed_job_type"],
        "metadata": {
            "draft_id": draft["draft_id"],
            "draft_artifact_path": str(draft_path),
            "candidate_id": candidate["candidate_id"],
            "candidate_artifact_path": str(candidate_path),
        },
        "payload": draft["proposed_payload"],
    }
    (failed_dir / "failed-before-replay.json").write_text(json.dumps(job), encoding="utf-8")
    (processed_dir / "processed-after-replay.json").write_text(json.dumps(job), encoding="utf-8")

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    counts = report["review_status"]["counts"]
    assert counts["staged_jobs_failed"] == 0
    assert counts["staged_jobs_failed_unresolved"] == 0
    assert counts["staged_jobs_failed_replayed_successfully"] == 1
    assert counts["staged_jobs_failed_archive_total"] == 1
    assert report["draft_summary"]["queue_state_counts"] == {"processed": 1}
    assert report["draft_preview"][0]["queue_state"] == "processed"
    assert not (runtime_root / "queue").exists()


def test_review_dashboard_includes_maintenance_findings_and_suggests_maintenance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    failed = action_candidate_payload(candidate_type="field_capture_follow_up", summary="")
    write_action_candidate_review(candidate_dir, failed)

    assert btq.run(["review-dashboard", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--include-paths", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["candidate_summary"]["failed"] == 1
    assert report["maintenance"]["finding_count"] == 1
    assert report["maintenance"]["findings"][0]["type"] == "failed_candidate"
    assert "artifact_path" in report["maintenance"]["findings"][0]
    assert report["suggested_next_command"] == "./scripts/btq review-maintenance-status --channel field_capture"
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_field_capture_review_pipeline_fixture_exercises_semantic_to_staged_queue_without_vault_mutation(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    semantic_path = write_semantic_artifact(semantic_dir)

    candidate_counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert candidate_counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert candidate["status"] == STATUS_PENDING_REVIEW
    assert candidate["provenance"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))

    approved_candidate = dict(candidate)
    approved_candidate["status"] = CANDIDATE_STATUS_APPROVED
    approved_candidate["reviewer"] = "fixture-reviewer"
    approved_candidate["approved_at"] = "2026-05-03T10:05:00+00:00"
    approved_candidate["approval_metadata"] = {
        "proposed_queue_job": {
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/ExampleAccount/Locations/7050 - Summit Wire/about.md",
                "destination": "site_note",
                "content": "Inspect sink area.\n",
            },
        }
    }
    candidate_path.write_text(json.dumps(approved_candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    draft_counts = field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft_counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert draft["status"] == "approved_draft"
    assert draft["candidate_id"] == approved_candidate["candidate_id"]
    assert draft["provenance"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))
    assert draft["proposed_payload"] == approved_candidate["approval_metadata"]["proposed_queue_job"]["payload"]

    first_stage = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=staging_dir)
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    queue_job = json.loads(queue_path.read_text(encoding="utf-8"))
    assert first_stage == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert len(sorted((runtime_root / "queue").glob("*.json"))) == 1
    assert queue_job["job_type"] == "append_to_note"
    assert queue_job["payload"] == approved_candidate["approval_metadata"]["proposed_queue_job"]["payload"]
    assert queue_job["metadata"]["draft_id"] == draft["draft_id"]
    assert queue_job["metadata"]["candidate_id"] == approved_candidate["candidate_id"]
    assert queue_job["metadata"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))

    report = field_review_status.review_status_report(runtime_root=runtime_root)

    assert report["counts"]["semantic_with_action_candidates"] == 1
    assert report["counts"]["candidates_pending_review"] == 0
    assert report["counts"]["candidates_approved"] == 1
    assert report["counts"]["approved_job_drafts"] == 1
    assert report["counts"]["staging_staged"] == 1
    assert report["counts"]["staging_skipped"] == 0
    assert report["counts"]["staged_queue_jobs_runtime_queue"] == 1
    assert report["lineage_gaps"] == []

    second_stage = field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=staging_dir)
    assert second_stage == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert len(sorted((runtime_root / "queue").glob("*.json"))) == 1
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_collect_action_candidates_cli_writes_review_artifacts_without_queue_or_vault(tmp_path: Path, capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(semantic_dir)
    semantic_before = semantic_path.read_text(encoding="utf-8")

    exit_code = btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"])
    first_output = json.loads(capsys.readouterr().out)
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate_before = candidate_path.read_text(encoding="utf-8")
    second_exit = btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"])
    second_output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert second_exit == 0
    assert first_output == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert second_output == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    assert semantic_path.read_text(encoding="utf-8") == semantic_before
    assert len(sorted(candidate_dir.glob("*.json"))) == 1
    assert candidate_path.read_text(encoding="utf-8") == candidate_before
    candidate = json.loads(candidate_before)
    assert candidate["status"] == STATUS_PENDING_REVIEW
    assert candidate["provenance"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_collect_action_candidates_dry_run_reports_would_create_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(semantic_dir)
    semantic_before = semantic_path.read_text(encoding="utf-8")
    called = {"queue_processor": False}

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        called["queue_processor"] = True
        raise AssertionError("queue processor must not run")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_called)

    exit_code = btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["dry_run"] is True
    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "would_create"
    assert result["reason"] == "would write action candidate to CouchDB when configured"
    assert result["candidate"]["status"] == STATUS_PENDING_REVIEW
    assert result["candidate"]["summary"] == "Inspect sink area."
    assert result["candidate_id"] == result["candidate"]["candidate_id"]
    assert result["candidate_path"].startswith(str(candidate_dir))
    assert semantic_path.read_text(encoding="utf-8") == semantic_before
    assert not candidate_dir.exists()
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()
    assert called["queue_processor"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_collect_action_candidates_suppresses_supply_after_photo_duplicate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_path = write_semantic_artifact(semantic_dir)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "cleaned_internal_note": "Okay, at Summit Wire, we need to order BrightWash bathroom cleaner.",
            "client_safe_note": "Summit Wire requires supply follow-up for bathroom cleaner.",
            "operational_summary": "Supply issue identified from field audio.",
            "issue_type": "supplies",
            "suggested_tags": ["field-audio/supplies"],
            "action_candidates": [
                "Review the field audio note and decide whether follow-up is needed.",
                "Capture after photo once corrected.",
            ],
        }
    )
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]
    assert [candidate["summary"] for candidate in candidates] == ["Review supply/order follow-up."]
    assert not (runtime_root / "queue").exists()


def test_collect_action_candidates_keeps_after_photo_for_correction_workflow(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_path = write_semantic_artifact(semantic_dir)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "cleaned_internal_note": "Restroom quality was missed. Capture an after photo once corrected.",
            "client_safe_note": "Restroom quality requires correction and an after photo.",
            "operational_summary": "Quality issue requires corrected after photo.",
            "issue_type": "quality",
            "suggested_tags": ["field-audio/quality"],
            "action_candidates": [
                "Review the area and capture an after photo once corrected.",
                "Capture after photo once corrected.",
            ],
        }
    )
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    counts = field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    summaries = [json.loads(path.read_text(encoding="utf-8"))["summary"] for path in sorted(candidate_dir.glob("*.json"))]
    assert summaries == [
        "Review the area and capture an after photo once corrected.",
    ]
    assert not (runtime_root / "queue").exists()


def test_collect_action_candidates_suppresses_junk_test_audio(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_path = write_semantic_artifact(semantic_dir)
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "cleaned_internal_note": "A note, I'm at Summit Wire, blah, blah, blah, blah, blah.",
            "client_safe_note": "A note, I'm at Summit Wire, blah, blah, blah, blah, blah.",
            "operational_summary": "A note, I'm at Summit Wire, blah, blah, blah.",
            "issue_type": "other",
            "issue_detected": False,
            "suggested_tags": ["field-audio/other"],
            "action_candidates": [
                "Review the field audio note and decide whether follow-up is needed.",
                "Capture after photo once corrected.",
            ],
        }
    )
    semantic_path.write_text(json.dumps(payload), encoding="utf-8")

    report = field_action_candidates.collect_action_candidates_report(
        semantic_dir,
        candidate_dir,
        runtime_root=runtime_root,
        dry_run=True,
    )

    assert report["counts"] == {"discovered": 0, "skipped": 1, "completed": 0, "failed": 0}
    assert report["results"][0]["reason"] == "semantic action candidates suppressed by quality rules"
    assert not candidate_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_collect_action_candidates_dry_run_reports_existing_equivalent_candidate_as_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    write_semantic_artifact(semantic_dir)
    field_action_candidates.collect_action_candidates(semantic_dir, candidate_dir, runtime_root=runtime_root)
    existing_candidates = sorted(candidate_dir.glob("*.json"))

    assert btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "skipped"
    assert result["reason"] == "candidate already exists in CouchDB"
    assert sorted(candidate_dir.glob("*.json")) == existing_candidates
    assert not (runtime_root / "queue").exists()


def test_collect_action_candidates_dry_run_reports_malformed_semantic_candidate_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    payload = json.loads(semantic_path.read_text(encoding="utf-8"))
    payload["action_candidates"] = [""]
    semantic_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    semantic_before = semantic_path.read_text(encoding="utf-8")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"

    assert btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    [result] = report["results"]
    assert result["status"] == "failed"
    assert result["reason"] == "summary is required"
    assert result["candidate"]["status"] == "failed"
    assert semantic_path.read_text(encoding="utf-8") == semantic_before
    assert not candidate_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_collect_action_candidates_dry_run_skips_empty_or_incomplete_semantic_artifacts_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    empty_path = write_semantic_artifact(semantic_dir, asset_id="fca_empty")
    empty_payload = json.loads(empty_path.read_text(encoding="utf-8"))
    empty_payload["action_candidates"] = []
    empty_path.write_text(json.dumps(empty_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    incomplete_path = write_semantic_artifact(semantic_dir, asset_id="fca_incomplete")
    incomplete_payload = json.loads(incomplete_path.read_text(encoding="utf-8"))
    incomplete_payload["status"] = "failed"
    incomplete_path.write_text(json.dumps(incomplete_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 0, "skipped": 2, "completed": 0, "failed": 0}
    assert [result["status"] for result in report["results"]] == ["skipped", "skipped"]
    assert {result["reason"] for result in report["results"]} == {
        "semantic artifact has no action candidates",
        "semantic status is not complete: 'failed'",
    }
    assert not candidate_dir.exists()
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "queue").exists()


def test_list_action_candidates_cli_shows_pending_candidate_id_status_and_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Review sink area.")
    candidate["source_context"] = "Bathroom 2 requires follow-up."
    write_action_candidate_review(candidate_dir, candidate)

    exit_code = btq.run(["list-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "field action candidates: total=1 pending_review=1 approved=0 rejected=0 failed=0" in output
    assert str(candidate["candidate_id"]) in output
    assert "[pending_review]" in output
    assert "Review sink area." in output
    assert "context:" in output
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "queue").exists()


def test_list_action_candidates_json_filters_review_metadata_and_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    approved = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Approved sink review.")
    pending = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Pending sink review.")
    rejected = review_candidate(STATUS_REJECTED, semantic_path, "Rejected sink review.")
    failed = action_candidate_payload(candidate_type="field_capture_follow_up", summary="")
    for candidate in (approved, pending, rejected, failed):
        write_action_candidate_review(candidate_dir, candidate)

    assert (
        btq.run(
            [
                "review-candidate",
                "--channel",
                "field_capture",
                "--runtime-root",
                str(runtime_root),
                "--candidate-id",
                str(approved["candidate_id"]),
                "--status",
                "approved",
                "--reviewer",
                "Jordan",
                "--rationale",
                "Looks right.",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    before = {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))}

    exit_code = btq.run(
        [
            "list-action-candidates",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--status",
            "approved",
            "--include-source",
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["channel"] == "field_capture"
    assert report["counts"]["total"] == 4
    assert report["counts"]["pending_review"] == 1
    assert report["counts"]["approved"] == 1
    assert report["counts"]["rejected"] == 1
    assert report["counts"]["failed"] == 1
    [item] = report["candidates"]
    assert item["candidate_id"] == approved["candidate_id"]
    assert item["status"] == CANDIDATE_STATUS_APPROVED
    assert item["summary"] == "Approved sink review."
    assert item["reviewer"] == "Jordan"
    assert item["reviewed_at"]
    assert item["review_rationale"] == "Looks right."
    assert item["semantic_artifact_path"] == str(semantic_path)
    # 308c: artifact_path is the CouchDB pseudo-path (sole canonical store).
    assert item["artifact_path"].endswith(f"action_candidate_{approved['candidate_id']}")
    assert "source_text" in item
    assert "source_context" in item
    after = {path: path.read_text(encoding="utf-8") for path in sorted(candidate_dir.glob("*.json"))}
    assert after == before
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_generate_approved_drafts_cli_only_processes_approved_candidates_without_queue_or_vault(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    approved = approved_candidate_payload()
    approved["provenance"]["semantic_artifact_path"] = str(semantic_path)
    for candidate in (
        approved,
        review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Pending review."),
        review_candidate(STATUS_REJECTED, semantic_path, "Rejected review."),
        review_candidate(CANDIDATE_STATUS_FAILED, semantic_path, "Failed review."),
    ):
        write_action_candidate_review(candidate_dir, candidate)

    exit_code = btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"])
    first_output = json.loads(capsys.readouterr().out)
    second_exit = btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"])
    second_output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert second_exit == 0
    assert first_output == {"discovered": 1, "skipped": 3, "completed": 1, "failed": 0}
    assert second_output == {"discovered": 1, "skipped": 3, "completed": 1, "failed": 0}
    assert len(sorted(draft_dir.glob("*.json"))) == 1
    draft = json.loads(next(draft_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert draft["status"] == "approved_draft"
    assert draft["candidate_id"] == approved["candidate_id"]
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_generate_approved_drafts_dry_run_reports_would_create_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    called = {"queue_processor": False}

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        called["queue_processor"] = True
        raise AssertionError("queue processor must not run")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_called)
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)

    exit_code = btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["dry_run"] is True
    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [result] = report["results"]
    assert result["candidate_id"] == candidate["candidate_id"]
    assert result["status"] == "would_create"
    assert result["reason"] == "would create approved queue-job draft"
    assert result["draft"]["status"] == "approved_draft"
    assert result["draft"]["candidate_id"] == candidate["candidate_id"]
    assert result["draft_path"].startswith(str(draft_dir))
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()
    assert called["queue_processor"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_generate_approved_drafts_dry_run_skips_non_approved_candidates_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    for status in (STATUS_PENDING_REVIEW, STATUS_REJECTED, CANDIDATE_STATUS_FAILED):
        write_action_candidate_review(candidate_dir, review_candidate(status, semantic_path, f"Candidate {status}."))

    assert btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 0, "skipped": 3, "completed": 0, "failed": 0}
    assert [result["status"] for result in report["results"]] == ["skipped", "skipped", "skipped"]
    assert {result["reason"] for result in report["results"]} == {
        "candidate status is not approved: pending_review",
        "candidate status is not approved: rejected",
        "candidate status is not approved: failed",
    }
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


@pytest.mark.parametrize(
    ("mutator", "expected_reason"),
    [
        (lambda candidate: candidate.pop("candidate_id"), "candidate_id is required"),
        (
            lambda candidate: (candidate.pop("approval_metadata"), candidate["channel_metadata"].pop("site_id")),
            "field_capture site_id is required for default append-to-site-note draft",
        ),
    ],
)
def test_generate_approved_drafts_dry_run_reports_malformed_or_unmapped_approved_candidate_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutator: object,
    expected_reason: str,
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_candidate_payload()
    assert callable(mutator)
    mutator(candidate)
    if candidate.get("candidate_id"):
        write_action_candidate_review(candidate_dir, candidate)
    else:
        candidate_dir.mkdir(parents=True)
        (candidate_dir / "missing-candidate-id.json").write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    [result] = report["results"]
    assert result["status"] == "failed"
    assert result["reason"] == expected_reason
    assert result["draft"]["status"] == "failed"
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()


def test_generate_approved_drafts_dry_run_reports_existing_equivalent_draft_as_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    existing_drafts = sorted(draft_dir.glob("*.json"))

    assert btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--dry-run", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["counts"] == {"discovered": 1, "skipped": 1, "completed": 0, "failed": 0}
    [result] = report["results"]
    assert result["status"] == "skipped"
    assert result["reason"] == "approved draft already exists"
    assert sorted(draft_dir.glob("*.json")) == existing_drafts
    assert not (runtime_root / "queue").exists()


def test_list_approved_drafts_cli_shows_draft_candidate_status_and_job_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    exit_code = btq.run(["list-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "field approved drafts: total=1 approved_draft=1 failed_draft=0 other=0" in output
    assert draft["draft_id"] in output
    assert candidate["candidate_id"] in output
    assert "[approved_draft]" in output
    assert "job_type=append_to_note" in output
    assert "queue_state=not_staged" in output
    assert "payload:" in output
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()


def test_list_approved_drafts_json_filters_failed_drafts_and_includes_payload_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    approved = approved_candidate_payload()
    malformed = approved_candidate_payload()
    malformed.pop("approval_metadata")
    malformed["candidate_id"] = "ac_missing_mapping"
    malformed["summary"] = "Missing mapping."
    malformed_channel_metadata = dict(malformed["channel_metadata"])
    malformed_channel_metadata.pop("site_id")
    malformed["channel_metadata"] = malformed_channel_metadata
    write_action_candidate_review(candidate_dir, approved)
    write_action_candidate_review(candidate_dir, malformed)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    before = {path: path.read_text(encoding="utf-8") for path in sorted(draft_dir.glob("*.json"))}

    exit_code = btq.run(
        [
            "list-approved-drafts",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--status",
            "failed_draft",
            "--include-payload",
            "--include-source",
            "--json",
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["channel"] == "field_capture"
    assert report["counts"] == {"total": 2, "approved_draft": 1, "failed_draft": 1, "other": 0}
    [item] = report["drafts"]
    assert item["status"] == "failed_draft"
    assert item["candidate_id"] == "ac_missing_mapping"
    assert item["proposed_job_type"] == ""
    assert item["proposed_payload"] == {}
    assert item["queue_state"]["state"] == "not_staged"
    assert "source_context" in item
    assert "provenance" in item
    after = {path: path.read_text(encoding="utf-8") for path in sorted(draft_dir.glob("*.json"))}
    assert after == before
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_list_approved_drafts_reports_queued_state_after_staging_without_mutating_more(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=staging_dir)
    queue_before = {path: path.read_text(encoding="utf-8") for path in sorted((runtime_root / "queue").glob("*.json"))}
    staging_before = {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))}

    assert btq.run(["list-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    [item] = report["drafts"]
    assert item["status"] == "approved_draft"
    assert item["queue_state"]["state"] == "queued"
    assert item["queue_state"]["evidence_path"].startswith(str(runtime_root / "queue"))
    assert item["queue_state"]["computed_job_id"]
    assert {path: path.read_text(encoding="utf-8") for path in sorted((runtime_root / "queue").glob("*.json"))} == queue_before
    assert {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))} == staging_before
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_list_approved_drafts_reports_failed_processed_and_replayed_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    processed_dir = runtime_root / "processed"
    failed_dir = runtime_root / "failed"
    processed_dir.mkdir(parents=True)
    failed_dir.mkdir(parents=True)
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")

    failed_candidate = approved_candidate_payload()
    failed_candidate["candidate_id"] = "ac_failed_only"
    failed_candidate["summary"] = "Failed only."
    failed_draft = field_approved_job_drafts.draft_payload_from_candidate(Path("failed.json"), failed_candidate)
    assert failed_draft is not None
    failed_draft["draft_id"] = "ajd_failed_only"
    failed_draft["candidate_id"] = "ac_failed_only"
    failed_draft["provenance"]["semantic_artifact_path"] = str(semantic_path)
    write_approved_job_draft(draft_dir, failed_draft)

    processed_candidate = approved_candidate_payload()
    processed_candidate["candidate_id"] = "ac_processed_only"
    processed_candidate["summary"] = "Processed only."
    processed_draft = field_approved_job_drafts.draft_payload_from_candidate(Path("processed.json"), processed_candidate)
    assert processed_draft is not None
    processed_draft["draft_id"] = "ajd_processed_only"
    processed_draft["candidate_id"] = "ac_processed_only"
    processed_draft["provenance"]["semantic_artifact_path"] = str(semantic_path)
    write_approved_job_draft(draft_dir, processed_draft)

    replayed_candidate = approved_candidate_payload()
    replayed_candidate["candidate_id"] = "ac_replayed"
    replayed_candidate["summary"] = "Failed then replayed."
    replayed_draft = field_approved_job_drafts.draft_payload_from_candidate(Path("replayed.json"), replayed_candidate)
    assert replayed_draft is not None
    replayed_draft["draft_id"] = "ajd_replayed"
    replayed_draft["candidate_id"] = "ac_replayed"
    replayed_draft["provenance"]["semantic_artifact_path"] = str(semantic_path)
    write_approved_job_draft(draft_dir, replayed_draft)

    def queue_evidence(draft: dict[str, object], *, marker: str) -> dict[str, object]:
        payload = dict(draft["proposed_payload"])
        payload["content"] = f"{payload.get('content', '')}\n\n{marker}"
        return {
            "job_type": draft["proposed_job_type"],
            "metadata": {
                "draft_id": draft["draft_id"],
                "draft_artifact_path": str(draft_dir / f"{draft['draft_id']}.json"),
            },
            "payload": payload,
        }

    (failed_dir / "failed-only.json").write_text(json.dumps(queue_evidence(failed_draft, marker="failed-only")), encoding="utf-8")
    (processed_dir / "processed-only.json").write_text(json.dumps(queue_evidence(processed_draft, marker="processed-only")), encoding="utf-8")
    (failed_dir / "replayed-failed.json").write_text(json.dumps(queue_evidence(replayed_draft, marker="replayed")), encoding="utf-8")
    (processed_dir / "replayed-processed.json").write_text(json.dumps(queue_evidence(replayed_draft, marker="replayed")), encoding="utf-8")
    before_runtime_files = {
        path: path.read_text(encoding="utf-8")
        for path in sorted(runtime_root.glob("**/*.json"))
    }

    assert btq.run(["list-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    states = {item["draft_id"]: item["queue_state"] for item in report["drafts"]}
    assert states["ajd_failed_only"]["state"] == "failed"
    assert states["ajd_processed_only"]["state"] == "processed"
    assert states["ajd_processed_only"]["historical_failed"] is False
    assert states["ajd_replayed"]["state"] == "processed"
    assert states["ajd_replayed"]["historical_failed"] is True
    assert states["ajd_replayed"]["historical_failed_count"] == 1
    assert {path: path.read_text(encoding="utf-8") for path in sorted(runtime_root.glob("**/*.json"))} == before_runtime_files
    assert not (runtime_root / "queue").exists()


def test_show_review_item_candidate_displays_full_detail_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Review sink area.")
    candidate["source_text"] = "Bathroom 2 has water under the sink."
    candidate["source_context"] = "Bathroom 2 requires follow-up."
    candidate_path = write_action_candidate_review(candidate_dir, candidate)
    assert (
        btq.run(
            [
                "review-candidate",
                "--channel",
                "field_capture",
                "--runtime-root",
                str(runtime_root),
                "--candidate-id",
                str(candidate["candidate_id"]),
                "--status",
                "approved",
                "--reviewer",
                "Jordan",
                "--rationale",
                "Verified.",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    before = candidate_path.read_text(encoding="utf-8")

    exit_code = btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--candidate-id", str(candidate["candidate_id"])])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"field review candidate {candidate['candidate_id']}" in output
    assert "status: approved" in output
    assert "summary: Review sink area." in output
    assert "source_text: Bathroom 2 has water under the sink." in output
    assert "source_context: Bathroom 2 requires follow-up." in output
    assert "reviewer: Jordan" in output
    assert "review_history:" in output
    assert candidate_path.read_text(encoding="utf-8") == before
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_show_review_item_candidate_json_shape_is_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Review sink area.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    assert btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--candidate-id", str(candidate["candidate_id"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["channel"] == "field_capture"
    assert report["item_type"] == "candidate"
    item = report["item"]
    assert item["candidate_id"] == candidate["candidate_id"]
    assert item["status"] == STATUS_PENDING_REVIEW
    assert item["candidate_type"] == "field_capture_follow_up"
    assert item["summary"] == "Review sink area."
    assert item["provenance"]["semantic_artifact_path"] == str(semantic_path)
    assert item["artifact_path"] == "couchdb/btq_field_captures/action_candidate_" + str(candidate["candidate_id"])


def test_show_review_item_draft_displays_payload_and_queue_state_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    staging_dir = runtime_root / "reviews" / "staging" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    candidate = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, candidate)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    field_draft_staging.stage_field_capture_drafts(runtime_root=runtime_root, draft_dir=draft_dir, status_dir=staging_dir)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    queue_before = {path: path.read_text(encoding="utf-8") for path in sorted((runtime_root / "queue").glob("*.json"))}
    staging_before = {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))}

    assert btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--draft-id", str(draft["draft_id"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["channel"] == "field_capture"
    assert report["item_type"] == "draft"
    item = report["item"]
    assert item["draft_id"] == draft["draft_id"]
    assert item["status"] == "approved_draft"
    assert item["candidate_id"] == candidate["candidate_id"]
    assert item["proposed_job_type"] == "append_to_note"
    assert item["proposed_payload"] == candidate["approval_metadata"]["proposed_queue_job"]["payload"]
    assert item["queue_state"]["state"] == "queued"
    assert item["staging_status_artifact_path"].startswith(str(staging_dir))
    assert item["staging_status"]["status"] == "staged"
    assert {path: path.read_text(encoding="utf-8") for path in sorted((runtime_root / "queue").glob("*.json"))} == queue_before
    assert {path: path.read_text(encoding="utf-8") for path in sorted(staging_dir.glob("*.json"))} == staging_before
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_show_review_item_fails_closed_for_missing_and_duplicate_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Duplicate candidate.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)
    duplicate_candidate_path = candidate_dir / "duplicate-candidate.json"
    duplicate_candidate_path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    approved = approved_candidate_payload()
    write_action_candidate_review(candidate_dir, approved)
    field_approved_job_drafts.create_approved_job_drafts(candidate_dir, draft_dir, runtime_root=runtime_root)
    [draft_path] = sorted(draft_dir.glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    duplicate_draft_path = draft_dir / "duplicate-draft.json"
    duplicate_draft_path.write_text(json.dumps(draft, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    missing_exit = btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--candidate-id", "ac_missing", "--json"])
    missing_report = json.loads(capsys.readouterr().out)
    duplicate_candidate_exit = btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--candidate-id", str(candidate["candidate_id"]), "--json"])
    duplicate_candidate_report = json.loads(capsys.readouterr().out)
    duplicate_draft_exit = btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--draft-id", str(draft["draft_id"]), "--json"])
    duplicate_draft_report = json.loads(capsys.readouterr().out)

    assert missing_exit == 1
    assert missing_report == {"channel": "field_capture", "error": "candidate not found: ac_missing"}
    # 308c: candidates are sole-canonical in CouchDB keyed by a unique _id, so a
    # second filesystem artifact for the same candidate_id is NOT a duplicate the
    # reader can ever see -- show-review-item resolves the single CouchDB doc and
    # succeeds. (Drafts are still filesystem-based, so the draft-duplicate path
    # below still fails closed.)
    assert duplicate_candidate_exit == 0
    assert duplicate_candidate_report["item"]["candidate_id"] == candidate["candidate_id"]
    assert duplicate_draft_exit == 1
    assert duplicate_draft_report == {
        "channel": "field_capture",
        "error": f"multiple draft artifacts found for draft_id: {draft['draft_id']}",
    }
    assert candidate_path.exists()
    assert duplicate_candidate_path.exists()
    assert draft_path.exists()
    assert duplicate_draft_path.exists()
    assert not (runtime_root / "reviews" / "staging").exists()
    assert not (runtime_root / "queue").exists()


def test_show_review_item_requires_exactly_one_id(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    with pytest.raises(SystemExit) as neither:
        btq.run(["show-review-item", "--channel", "field_capture", "--runtime-root", str(runtime_root)])
    with pytest.raises(SystemExit) as both:
        btq.run(
            [
                "show-review-item",
                "--channel",
                "field_capture",
                "--runtime-root",
                str(runtime_root),
                "--candidate-id",
                "ac_test",
                "--draft-id",
                "ajd_test",
            ]
        )

    assert neither.value.code == 2
    assert both.value.code == 2


def test_review_candidate_cli_approves_one_pending_candidate_and_preserves_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Review sink area.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    exit_code = btq.run(
        [
            "review-candidate",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            str(candidate["candidate_id"]),
            "--status",
            "approved",
            "--reviewer",
            "Jordan",
            "--rationale",
            "Verified from call.",
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "candidate_id": candidate["candidate_id"],
        "candidate_path": "couchdb/btq_field_captures/action_candidate_" + str(candidate["candidate_id"]),
        "changed": True,
        "prior_status": "",
        "reviewer": "Jordan",
        "status": CANDIDATE_STATUS_APPROVED,
    }
    # 308c: the CLI mutates the CouchDB doc (sole canonical store); read it back.
    updated = couchdb_review.review_payload_for(output["candidate_path"])
    assert updated["status"] == CANDIDATE_STATUS_APPROVED
    assert updated["reviewer"] == "Jordan"
    assert updated["review_rationale"] == "Verified from call."
    assert updated["prior_status"] == STATUS_PENDING_REVIEW
    assert updated["review_history"][-1]["status"] == CANDIDATE_STATUS_APPROVED
    assert updated["provenance"] == candidate["provenance"]
    assert updated["source_text"] == candidate["source_text"]
    assert updated["source_context"] == candidate["source_context"]
    assert updated["confidence"] == candidate["confidence"]
    assert updated["channel_metadata"] == candidate["channel_metadata"]
    assert not draft_dir.exists()
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"


def test_review_candidate_cli_rejects_one_pending_candidate(tmp_path: Path, capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    semantic_path = write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics")
    candidate = review_candidate(STATUS_PENDING_REVIEW, semantic_path, "Reject sink action.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    exit_code = btq.run(
        [
            "review-candidate",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            str(candidate["candidate_id"]),
            "--status",
            "rejected",
            "--reviewer",
            "Jordan",
            "--rationale",
            "Already handled.",
            "--json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == STATUS_REJECTED
    # 308c: read the mutated candidate from CouchDB (sole canonical store).
    updated = couchdb_review.review_payload_for(output["candidate_path"])
    assert updated["status"] == STATUS_REJECTED
    assert updated["review_history"][-1]["review_rationale"] == "Already handled."


def test_review_candidate_cli_fails_closed_for_failed_missing_or_duplicate_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str], couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    failed = action_candidate_payload(candidate_type="field_capture_follow_up", summary="")
    failed_path = write_action_candidate_review(candidate_dir, failed)

    failed_exit = btq.run(
        [
            "review-candidate",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            str(failed["candidate_id"]),
            "--status",
            "approved",
            "--reviewer",
            "Jordan",
            "--rationale",
            "Nope.",
            "--json",
        ]
    )
    failed_output = json.loads(capsys.readouterr().out)

    missing_exit = btq.run(
        [
            "review-candidate",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            "ac_missing",
            "--status",
            "approved",
            "--reviewer",
            "Jordan",
            "--rationale",
            "Nope.",
            "--json",
        ]
    )
    missing_output = json.loads(capsys.readouterr().out)

    duplicate = review_candidate(STATUS_PENDING_REVIEW, write_semantic_artifact(runtime_root / "field_capture" / "audio_semantics"), "Duplicate.")
    duplicate_path = write_action_candidate_review(candidate_dir, duplicate)
    duplicate_copy_path = candidate_dir / "duplicate-copy.json"
    duplicate_copy_path.write_text(json.dumps(duplicate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    duplicate_exit = btq.run(
        [
            "review-candidate",
            "--channel",
            "field_capture",
            "--runtime-root",
            str(runtime_root),
            "--candidate-id",
            str(duplicate["candidate_id"]),
            "--status",
            "approved",
            "--reviewer",
            "Jordan",
            "--rationale",
            "Nope.",
            "--json",
        ]
    )
    duplicate_output = json.loads(capsys.readouterr().out)

    assert failed_exit == 1
    assert failed_output == {"candidate_id": failed["candidate_id"], "changed": False, "error": "candidate status is not pending_review: failed"}
    assert json.loads(failed_path.read_text(encoding="utf-8"))["status"] == "failed"
    assert missing_exit == 1
    assert missing_output == {"candidate_id": "ac_missing", "changed": False, "error": "candidate not found: ac_missing"}
    # 308c: candidates are sole-canonical in CouchDB keyed by a unique _id, so a
    # second filesystem copy is NOT a duplicate the reviewer can ever see -- the
    # review resolves the single CouchDB doc and succeeds. The CouchDB status
    # flips to approved; the inert legacy filesystem artifacts are NOT mutated.
    assert duplicate_exit == 0
    assert duplicate_output["status"] == CANDIDATE_STATUS_APPROVED
    assert couchdb_review.status_of(duplicate["candidate_id"]) == CANDIDATE_STATUS_APPROVED
    assert json.loads(duplicate_path.read_text(encoding="utf-8"))["status"] == STATUS_PENDING_REVIEW
    assert json.loads(duplicate_copy_path.read_text(encoding="utf-8"))["status"] == STATUS_PENDING_REVIEW
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()


def test_field_capture_full_review_workflow_smoke_uses_cli_dispatch_without_queue_processor_or_vault_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    sentinel = vault_root / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    proposed_note_path = "Accounts/ExampleAccount/Locations/7050 - Summit Wire/about.md"
    semantic_path = write_semantic_artifact(
        runtime_root / "field_capture" / "audio_semantics",
        proposed_note_path=proposed_note_path,
    )
    queue_processor_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run during review workflow smoke test")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)

    assert btq.run(["collect-action-candidates", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    assert btq.run(["review-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    pending_status = json.loads(capsys.readouterr().out)
    assert pending_status["counts"]["semantic_with_action_candidates"] == 1
    assert pending_status["counts"]["candidates_pending_review"] == 1
    assert pending_status["counts"]["candidates_approved"] == 0

    [candidate_path] = sorted((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate_id = candidate["candidate_id"]

    assert (
        btq.run(
            [
                "review-candidate",
                "--channel",
                "field_capture",
                "--runtime-root",
                str(runtime_root),
                "--candidate-id",
                candidate_id,
                "--status",
                "approved",
                "--reviewer",
                "Jordan",
                "--rationale",
                "Verified against the field note.",
                "--json",
            ]
        )
        == 0
    )
    review_output = json.loads(capsys.readouterr().out)
    assert review_output["candidate_id"] == candidate_id
    # 308c: the CLI review summary emits an empty prior_status (the prior status
    # is preserved in the CouchDB doc's review_history, not the summary).
    assert review_output["prior_status"] == ""
    assert review_output["status"] == CANDIDATE_STATUS_APPROVED

    # 308c: review now flips the CouchDB candidate; the Pro-side staging watcher
    # materializes the approved candidate back onto the filesystem before the
    # (still filesystem-based) draft/staging stages run. Mirror that production
    # step here so the CLI draft pipeline sees an approved candidate -- exactly
    # what action_candidate_staging_watcher does on a real approve.
    from field_capture.action_candidate_staging_watcher import (
        materialize_candidate_for_existing_staging,
    )
    materialize_candidate_for_existing_staging(
        couchdb_review.docs[f"action_candidate_{candidate_id}"], runtime_root
    )

    assert btq.run(["review-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    approved_status = json.loads(capsys.readouterr().out)
    assert approved_status["counts"]["candidates_pending_review"] == 0
    assert approved_status["counts"]["candidates_approved"] == 1

    assert btq.run(["generate-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    assert btq.run(["stage-approved-drafts", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    assert btq.run(["review-status", "--channel", "field_capture", "--runtime-root", str(runtime_root), "--json"]) == 0
    final_status = json.loads(capsys.readouterr().out)
    assert final_status["counts"]["semantic_with_action_candidates"] == 1
    assert final_status["counts"]["candidates_approved"] == 1
    assert final_status["counts"]["approved_job_drafts"] == 1
    assert final_status["counts"]["staging_staged"] == 1
    assert final_status["counts"]["staged_queue_jobs_runtime_queue"] == 1
    assert final_status["lineage_gaps"] == []

    [draft_path] = sorted((runtime_root / "reviews" / "approved_job_drafts" / "field_capture").glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    queue_job = json.loads(queue_path.read_text(encoding="utf-8"))
    assert draft["proposed_payload"]["path"] == proposed_note_path
    assert draft["proposed_payload"]["destination"] == "site_note"
    draft_content = draft["proposed_payload"]["content"]
    assert "## Field Capture Reviews" in draft_content
    assert "- site_id: 7050" in draft_content
    assert "- area: Restrooms" in draft_content
    assert "- capture_id: cap-audio" in draft_content
    assert "- audio_asset_id: fca_test" in draft_content
    assert "Summary: Verified against the field note." in draft_content
    assert "Review rationale: Verified against the field note." in draft_content
    assert str(semantic_path.resolve(strict=False)) in draft_content
    assert queue_job["payload"] == draft["proposed_payload"]
    assert queue_job["metadata"]["candidate_id"] == candidate_id
    assert queue_job["metadata"]["draft_id"] == draft["draft_id"]
    assert queue_job["metadata"]["draft_artifact_path"] == str(draft_path.resolve(strict=False))
    assert queue_job["metadata"]["candidate_artifact_path"] == str(candidate_path.resolve(strict=False))
    assert queue_job["metadata"]["semantic_artifact_path"] == str(semantic_path.resolve(strict=False))

    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert queue_processor_called["called"] is False
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
