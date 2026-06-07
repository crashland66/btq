from __future__ import annotations

import json
from pathlib import Path

import pytest

from processing_core.artifacts import read_json_object, resolve_within_root, write_json_object, write_json_value
from processing_core.action_candidates import (
    STATUS_APPROVED as CANDIDATE_STATUS_APPROVED,
    STATUS_FAILED as CANDIDATE_STATUS_FAILED,
    STATUS_PENDING_REVIEW,
    action_candidate_id,
    action_candidate_payload,
    write_action_candidate_review,
)
from processing_core.approved_job_drafts import (
    DRAFT_STATUS_APPROVED,
    DRAFT_STATUS_FAILED,
    approved_job_draft_id,
    approved_job_draft_payload,
    write_approved_job_draft,
)
from processing_core.draft_staging import queue_filename_for, queue_job_from_draft, stage_approved_drafts
from processing_core.hashing import text_sha256
from processing_core.ids import deterministic_artifact_id
from processing_core.json import canonical_json
from processing_core.results import result_counts, semantic_result_counts
from processing_core.semantics import semantic_base_payload, semantic_failed_payload, semantic_success_payload
from processing_core.semantic_transform import SemanticTransformSpec, semantic_engine_name, transform_semantic_payload
from processing_core.status import STATUS_COMPLETE, STATUS_FAILED, is_terminal_status
from processing_core.transcripts import (
    transcript_base_payload,
    transcript_failed_payload,
    transcript_metadata_payload,
    transcript_success_payload,
)


def test_deterministic_artifact_id_uses_newline_joined_parts_and_prefix() -> None:
    first = deterministic_artifact_id("fca", "cap-audio", "voice.webm", "/tmp/voice.webm")
    second = deterministic_artifact_id("fca", "cap-audio", "voice.webm", "/tmp/voice.webm")
    changed = deterministic_artifact_id("fca", "cap-audio", "other.webm", "/tmp/voice.webm")

    assert first == second
    assert first.startswith("fca_")
    assert len(first) == len("fca_") + 24
    assert first != changed


def test_canonical_json_shared_helper_preserves_compact_sorted_default_str_behavior() -> None:
    class Custom:
        def __str__(self) -> str:
            return "custom-value"

    assert canonical_json({"z": 1, "a": [Custom()]}) == '{"a":["custom-value"],"z":1}'


def test_terminal_status_helper() -> None:
    assert STATUS_COMPLETE == "complete"
    assert STATUS_FAILED == "failed"
    assert is_terminal_status("complete")
    assert is_terminal_status("failed")
    assert not is_terminal_status("pending")
    assert not is_terminal_status(None)


def test_json_artifact_helpers_round_trip_sorted_indented_payload(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"

    write_json_object(path, {"z": 1, "a": {"nested": True}})

    assert read_json_object(path) == {"z": 1, "a": {"nested": True}}
    assert path.read_text(encoding="utf-8").startswith('{\n  "a":')
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_json_value_helper_writes_list_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "corrections.json"

    write_json_value(path, [{"from": "bct", "to": "VCT"}])

    assert json.loads(path.read_text(encoding="utf-8")) == [{"from": "bct", "to": "VCT"}]
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_read_json_object_returns_none_for_missing_invalid_or_non_object(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    array = tmp_path / "array.json"
    invalid.write_text("{", encoding="utf-8")
    array.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert read_json_object(missing) is None
    assert read_json_object(invalid) is None
    assert read_json_object(array) is None


def test_resolve_within_root_returns_resolved_path_and_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    inside = root / "child" / "artifact.json"
    outside = tmp_path / "outside.json"

    assert resolve_within_root(inside, root) == inside.resolve(strict=False)
    with pytest.raises(ValueError):
        resolve_within_root(outside, root)


def test_result_count_helpers() -> None:
    assert result_counts("pending", "transcribed", "failed", "skipped") == {
        "pending": 0,
        "transcribed": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert semantic_result_counts() == {"discovered": 0, "skipped": 0, "completed": 0, "failed": 0}


def test_transcript_payload_helpers_preserve_channel_provenance_and_failure_shape() -> None:
    base = transcript_base_payload(
        artifact_type="field_audio_transcript",
        artifact_id_field="audio_asset_id",
        artifact_id="fca_test",
        engine_field="transcription_engine",
        engine_name="stub",
        provenance={"site_id": "7050", "upload_id": "cap-audio"},
        created_at="2026-05-02T23:31:00+00:00",
    )

    success = transcript_success_payload(base, raw_text="  Raw note.\n", transcription_payload={"model": "stub"})
    failure = transcript_failed_payload(base, RuntimeError("mic noise"))

    assert base == {
        "type": "field_audio_transcript",
        "site_id": "7050",
        "upload_id": "cap-audio",
        "audio_asset_id": "fca_test",
        "created_at": "2026-05-02T23:31:00+00:00",
        "transcription_engine": "stub",
    }
    assert success["status"] == "complete"
    assert success["raw_text"] == "Raw note."
    assert success["error"] is None
    assert success["transcription"] == {"model": "stub"}
    assert failure["status"] == "failed"
    assert failure["raw_text"] == ""
    assert failure["error"] == {"type": "RuntimeError", "message": "mic noise"}
    assert failure["transcription"] is None


def test_transcript_metadata_payload_preserves_voice_inbox_shape() -> None:
    payload = transcript_metadata_payload(
        capture_id="cap-test",
        audio_file="/runtime/audio/note.m4a",
        source_fingerprint={"path": "/inbox/note.m4a", "size": 10, "mtime_ns": 123},
        transcript_file="/runtime/audio/note.m4a.whisper.txt",
        created_at="2026-05-03T12:00:00+00:00",
    )

    assert payload == {
        "capture_id": "cap-test",
        "audio_file": "/runtime/audio/note.m4a",
        "source_fingerprint": {"path": "/inbox/note.m4a", "size": 10, "mtime_ns": 123},
        "transcript_file": "/runtime/audio/note.m4a.whisper.txt",
        "created_at": "2026-05-03T12:00:00+00:00",
    }


def test_semantic_payload_helpers_hash_text_and_preserve_channel_fields() -> None:
    base = semantic_base_payload(
        artifact_type="field_audio_semantic_summary",
        artifact_id_field="audio_asset_id",
        artifact_id="fca_test",
        source_transcript_path="/runtime/transcripts/fca_test.json",
        engine_field="semantic_engine",
        engine_name="stub-semantic",
        raw_text="Raw sink note.",
        provenance={"site_id": "7050", "upload_id": "cap-audio", "area": "Restrooms"},
        created_at="2026-05-02T23:32:00+00:00",
    )
    success = semantic_success_payload(base, {"cleaned_internal_note": "Cleaned.", "issue_detected": True})
    failure = semantic_failed_payload(
        base,
        error=ValueError("unsupported issue_type"),
        failure_fields={"cleaned_internal_note": "", "issue_detected": False},
    )

    assert base["type"] == "field_audio_semantic_summary"
    assert base["site_id"] == "7050"
    assert base["area"] == "Restrooms"
    assert base["audio_asset_id"] == "fca_test"
    assert base["source_transcript_path"] == "/runtime/transcripts/fca_test.json"
    assert base["semantic_engine"] == "stub-semantic"
    assert base["raw_text_hash"] == text_sha256("Raw sink note.")
    assert success["status"] == "complete"
    assert success["cleaned_internal_note"] == "Cleaned."
    assert success["error"] is None
    assert failure["status"] == "failed"
    assert failure["error"] == {"type": "ValueError", "message": "unsupported issue_type"}


class StubSemanticEngine:
    engine_name = "stub-semantic"

    def __call__(self, transcript: dict[str, object]) -> dict[str, object]:
        return {
            "cleaned_internal_note": "Bathroom 2 has water under the sink.",
            "issue_detected": True,
            "issue_type": "water",
        }


def test_semantic_transform_success_preserves_field_capture_shape_without_field_awareness() -> None:
    transcript = {"raw_text": "Bathroom two has water under the sink."}

    outcome = transform_semantic_payload(
        SemanticTransformSpec(
            artifact_type="field_audio_semantic_summary",
            artifact_id_field="audio_asset_id",
            artifact_id="fca_test",
            source_transcript_path="/runtime/field_capture/audio_transcripts/fca_test.json",
            engine_field="semantic_engine",
            engine_name=semantic_engine_name(StubSemanticEngine()),
            raw_text=str(transcript["raw_text"]),
            provenance={"site_id": "7050", "upload_id": "cap-audio", "area": "Restrooms", "phase": "issue"},
            engine_input=transcript,
            engine=StubSemanticEngine(),
            result_fields=lambda result: {
                **result,
                "client_safe_note": "Bathroom 2 requires follow-up.",
                "operational_summary": "Possible water issue.",
                "urgency": "normal",
                "suggested_tags": ["field-audio/water"],
                "action_candidates": ["Review the field audio note."],
            },
            failure_fields={
                "cleaned_internal_note": "",
                "client_safe_note": "",
                "operational_summary": "",
                "issue_detected": False,
                "issue_type": "other",
                "urgency": "low",
                "suggested_tags": [],
                "action_candidates": [],
            },
        )
    )

    assert outcome.error is None
    assert outcome.payload["type"] == "field_audio_semantic_summary"
    assert outcome.payload["site_id"] == "7050"
    assert outcome.payload["upload_id"] == "cap-audio"
    assert outcome.payload["area"] == "Restrooms"
    assert outcome.payload["phase"] == "issue"
    assert outcome.payload["audio_asset_id"] == "fca_test"
    assert outcome.payload["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"
    assert outcome.payload["semantic_engine"] == "stub-semantic"
    assert outcome.payload["raw_text_hash"] == text_sha256("Bathroom two has water under the sink.")
    assert outcome.payload["status"] == "complete"
    assert outcome.payload["error"] is None
    assert outcome.payload["cleaned_internal_note"] == "Bathroom 2 has water under the sink."
    assert outcome.payload["issue_type"] == "water"


def test_semantic_transform_failure_preserves_failed_shape_and_opaque_provenance() -> None:
    class FailingEngine:
        engine_name = "stub-semantic"

        def __call__(self, _transcript: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("semantic parse failed")

    outcome = transform_semantic_payload(
        SemanticTransformSpec(
            artifact_type="field_audio_semantic_summary",
            artifact_id_field="audio_asset_id",
            artifact_id="fca_test",
            source_transcript_path="/runtime/field_capture/audio_transcripts/fca_test.json",
            engine_field="semantic_engine",
            engine_name=semantic_engine_name(FailingEngine()),
            raw_text="Raw text",
            provenance={"site_id": "7050", "opaque_channel_key": "kept"},
            engine_input={"raw_text": "Raw text"},
            engine=FailingEngine(),
            result_fields=dict,
            failure_fields={
                "cleaned_internal_note": "",
                "client_safe_note": "",
                "operational_summary": "",
                "issue_detected": False,
                "issue_type": "other",
                "urgency": "low",
                "suggested_tags": [],
                "action_candidates": [],
            },
        )
    )

    assert isinstance(outcome.error, RuntimeError)
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["site_id"] == "7050"
    assert outcome.payload["opaque_channel_key"] == "kept"
    assert outcome.payload["raw_text_hash"] == text_sha256("Raw text")
    assert outcome.payload["cleaned_internal_note"] == ""
    assert outcome.payload["action_candidates"] == []
    assert outcome.payload["error"] == {"type": "RuntimeError", "message": "semantic parse failed"}


def test_semantic_transform_validation_failure_uses_failed_payload() -> None:
    outcome = transform_semantic_payload(
        SemanticTransformSpec(
            artifact_type="field_audio_semantic_summary",
            artifact_id_field="audio_asset_id",
            artifact_id="fca_test",
            source_transcript_path="/runtime/transcripts/fca_test.json",
            engine_field="semantic_engine",
            engine_name="stub-semantic",
            raw_text="Raw text",
            provenance={},
            engine_input={"raw_text": "Raw text"},
            engine=lambda _value: {"issue_type": "unsupported"},
            result_fields=dict,
            failure_fields={"issue_type": "other"},
            validate_result=lambda result: (_ for _ in ()).throw(ValueError(f"unsupported issue_type: {result['issue_type']}")),
        )
    )

    assert isinstance(outcome.error, ValueError)
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["issue_type"] == "other"
    assert outcome.payload["error"] == {"type": "ValueError", "message": "unsupported issue_type: unsupported"}


def test_action_candidate_id_is_deterministic_for_equivalent_content_and_provenance() -> None:
    provenance = {
        "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json",
        "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
    }
    first = action_candidate_id(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        provenance=provenance,
        source_text="Bathroom 2 has water under the sink.",
        channel_metadata={"site_id": "7050", "channel": "field_capture"},
    )
    second = action_candidate_id(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        provenance=dict(reversed(list(provenance.items()))),
        source_text="Bathroom 2 has water under the sink.",
        channel_metadata={"channel": "field_capture", "site_id": "7050"},
    )

    assert first == second
    assert first.startswith("ac_")


def test_action_candidate_payload_defaults_to_pending_review_and_preserves_provenance() -> None:
    payload = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Restock Bathroom 2 paper towels.",
        rationale="Supply issue and possible sink leak in Bathroom 2.",
        source_text="Bathroom 2 has water under the sink and paper towels are out.",
        source_context="Bathroom 2 requires follow-up.",
        provenance={
            "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json",
            "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
            "raw_text_hash": "abc123",
        },
        channel_metadata={"channel": "field_capture", "site_id": "7050"},
        created_at="2026-05-03T10:00:00+00:00",
    )

    assert payload["type"] == "action_candidate_review"
    assert payload["status"] == STATUS_PENDING_REVIEW
    assert payload["created_at"] == "2026-05-03T10:00:00+00:00"
    assert payload["summary"] == "Restock Bathroom 2 paper towels."
    assert payload["confidence"] == "unknown"
    assert payload["provenance"]["semantic_artifact_path"] == "/runtime/field_capture/audio_semantics/fca_test.json"
    assert payload["provenance"]["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"
    assert payload["channel_metadata"] == {"channel": "field_capture", "site_id": "7050"}
    assert payload["error"] is None


def test_malformed_action_candidate_payload_is_failed_not_approved() -> None:
    payload = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="",
        provenance={"semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json"},
        status=STATUS_PENDING_REVIEW,
        created_at="2026-05-03T10:00:00+00:00",
    )

    assert payload["status"] == CANDIDATE_STATUS_FAILED
    assert payload["summary"] == ""
    assert payload["error"] == {"type": "ValueError", "message": "summary is required"}


def test_write_action_candidate_review_uses_candidate_id_filename(tmp_path: Path) -> None:
    payload = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Review field audio.",
        created_at="2026-05-03T10:00:00+00:00",
    )

    path = write_action_candidate_review(tmp_path / "reviews" / "action_candidates", payload)

    assert path.name == f"{payload['candidate_id']}.json"
    assert read_json_object(path) == payload


def test_approved_job_draft_id_is_deterministic_for_candidate_and_proposed_job() -> None:
    payload = {"path": "Accounts/Example/about.md", "destination": "site_note", "content": "Review sink.\n"}
    first = approved_job_draft_id(
        candidate_id="ac_test",
        proposed_job_type="append_to_note",
        proposed_payload=payload,
    )
    second = approved_job_draft_id(
        candidate_id="ac_test",
        proposed_job_type="append_to_note",
        proposed_payload={"content": "Review sink.\n", "destination": "site_note", "path": "Accounts/Example/about.md"},
    )

    assert first == second
    assert first.startswith("ajd_")


def test_approved_job_draft_payload_preserves_candidate_and_source_provenance() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        source_context="Bathroom 2 requires follow-up.",
        provenance={
            "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json",
            "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
        },
        status=CANDIDATE_STATUS_APPROVED,
        created_at="2026-05-03T10:00:00+00:00",
    )
    candidate["reviewer"] = "manager"
    candidate["approved_at"] = "2026-05-03T10:05:00+00:00"
    candidate["approval_metadata"] = {"reason": "confirmed from call"}

    payload = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path="/runtime/reviews/action_candidates/field_capture/ac_test.json",
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md", "destination": "site_note", "content": "Inspect sink area.\n"},
        created_at="2026-05-03T10:06:00+00:00",
    )

    assert payload["type"] == "approved_queue_job_draft"
    assert payload["status"] == DRAFT_STATUS_APPROVED
    assert payload["candidate_id"] == candidate["candidate_id"]
    assert payload["candidate_artifact_path"] == "/runtime/reviews/action_candidates/field_capture/ac_test.json"
    assert payload["provenance"]["candidate_artifact_path"] == "/runtime/reviews/action_candidates/field_capture/ac_test.json"
    assert payload["provenance"]["semantic_artifact_path"] == "/runtime/field_capture/audio_semantics/fca_test.json"
    assert payload["provenance"]["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"
    assert payload["proposed_job_type"] == "append_to_note"
    assert payload["proposed_payload"]["destination"] == "site_note"
    assert payload["reviewer"] == "manager"
    assert payload["approved_at"] == "2026-05-03T10:05:00+00:00"
    assert payload["approval_metadata"] == {"reason": "confirmed from call"}
    assert payload["source_context"] == "Bathroom 2 requires follow-up."
    assert payload["error"] is None


def test_approved_job_draft_payload_fails_closed_for_unapproved_or_malformed_candidate() -> None:
    pending = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        status=STATUS_PENDING_REVIEW,
    )
    pending_payload = approved_job_draft_payload(
        candidate=pending,
        candidate_artifact_path="/runtime/reviews/action_candidates/field_capture/ac_test.json",
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md"},
    )
    approved_missing_job = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        status=CANDIDATE_STATUS_APPROVED,
    )
    malformed_payload = approved_job_draft_payload(
        candidate=approved_missing_job,
        candidate_artifact_path="/runtime/reviews/action_candidates/field_capture/ac_test.json",
        proposed_job_type="",
        proposed_payload={},
    )

    assert pending_payload["status"] == DRAFT_STATUS_FAILED
    assert pending_payload["error"]["message"] == "candidate status must be approved, got pending_review"
    assert malformed_payload["status"] == DRAFT_STATUS_FAILED
    assert malformed_payload["error"]["message"] == "proposed_job_type is required"


def test_write_approved_job_draft_uses_draft_id_filename(tmp_path: Path) -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        status=CANDIDATE_STATUS_APPROVED,
    )
    payload = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path="/runtime/reviews/action_candidates/field_capture/ac_test.json",
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md", "destination": "site_note", "content": "Inspect sink area.\n"},
        created_at="2026-05-03T10:00:00+00:00",
    )

    path = write_approved_job_draft(tmp_path / "reviews" / "approved_job_drafts", payload)

    assert path.name == f"{payload['draft_id']}.json"
    assert read_json_object(path) == payload


def test_queue_job_from_draft_preserves_provenance_as_metadata_and_payload_unchanged() -> None:
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        provenance={
            "semantic_artifact_path": "/runtime/field_capture/audio_semantics/fca_test.json",
            "source_transcript_path": "/runtime/field_capture/audio_transcripts/fca_test.json",
        },
        status=CANDIDATE_STATUS_APPROVED,
    )
    draft = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path="/runtime/reviews/action_candidates/field_capture/ac_test.json",
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md", "destination": "site_note", "content": "Inspect sink area.\n"},
        created_at="2026-05-03T10:00:00+00:00",
    )

    job = queue_job_from_draft(draft, Path("/runtime/reviews/approved_job_drafts/field_capture/ajd_test.json"))

    assert job["job_id"] == draft["draft_id"]
    assert job["job_type"] == "append_to_note"
    assert job["payload"] == draft["proposed_payload"]
    assert job["metadata"]["source"] == "approved_job_draft"
    assert job["metadata"]["draft_id"] == draft["draft_id"]
    assert job["metadata"]["candidate_id"] == candidate["candidate_id"]
    assert job["metadata"]["semantic_artifact_path"] == "/runtime/field_capture/audio_semantics/fca_test.json"
    assert job["metadata"]["source_transcript_path"] == "/runtime/field_capture/audio_transcripts/fca_test.json"


def test_queue_filename_for_is_deterministic() -> None:
    assert queue_filename_for("ajd_test", "abc1234567890abcdef") == "ajd_test__abc1234567890abc.json"


def test_stage_approved_drafts_validates_and_writes_queue_plus_status(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        status=CANDIDATE_STATUS_APPROVED,
    )
    draft = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path=str(runtime_root / "reviews" / "action_candidates" / "field_capture" / "ac_test.json"),
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md", "destination": "site_note", "content": "Inspect sink area.\n"},
        created_at="2026-05-03T10:00:00+00:00",
    )
    write_approved_job_draft(draft_dir, draft)

    counts = stage_approved_drafts(draft_dir, runtime_root=runtime_root, status_dir=status_dir)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    job = read_json_object(queue_path)
    assert job["payload"] == draft["proposed_payload"]
    assert job["metadata"]["draft_id"] == draft["draft_id"]
    [status_path] = sorted(status_dir.glob("*.json"))
    status = read_json_object(status_path)
    assert status["status"] == "staged"
    assert status["queue_path"] == str(queue_path)
    assert status["job"] == job


def test_stage_approved_drafts_rejects_invalid_job_without_queue_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    draft_dir = runtime_root / "reviews" / "approved_job_drafts" / "field_capture"
    status_dir = runtime_root / "reviews" / "staging" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Inspect sink area.",
        status=CANDIDATE_STATUS_APPROVED,
    )
    draft = approved_job_draft_payload(
        candidate=candidate,
        candidate_artifact_path=str(runtime_root / "reviews" / "action_candidates" / "field_capture" / "ac_test.json"),
        proposed_job_type="append_to_note",
        proposed_payload={"path": "Accounts/Example/about.md", "destination": "site_note"},
        created_at="2026-05-03T10:00:00+00:00",
    )
    write_approved_job_draft(draft_dir, draft)

    counts = stage_approved_drafts(draft_dir, runtime_root=runtime_root, status_dir=status_dir)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 0, "failed": 1}
    assert not (runtime_root / "queue").exists()
    [status_path] = sorted(status_dir.glob("*.json"))
    status = read_json_object(status_path)
    assert status["status"] == "failed"
    assert status["reason"] == "proposed queue job does not match queue_spec"
