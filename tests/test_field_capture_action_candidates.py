from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import queue_spec
from field_capture import action_candidates, approved_job_drafts
from field_capture.text_semantics import run_text_semantic_pipeline


@pytest.fixture
def vault_named_bt(monkeypatch) -> None:
    """Make get_config().vault_dir.name return "OperationalVault" deterministically."""
    fake_config = SimpleNamespace(vault_dir=Path("/some/path/OperationalVault"))
    # normalize_vault_relative_path lives in queue_spec; action_candidates
    # only re-exports it. Patch at the source so the configured vault name
    # is what the function actually reads.
    monkeypatch.setattr(queue_spec, "get_config", lambda: fake_config)


def test_normalize_strips_doubled_bt_prefix(vault_named_bt: None) -> None:
    assert (
        action_candidates.normalize_vault_relative_path("OperationalVault/Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md")
        == "Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md"
    )


def test_normalize_strips_repeated_bt_prefix(vault_named_bt: None) -> None:
    # Defense in depth: a producer that doubles up its own bug shouldn't
    # leak through.
    assert (
        action_candidates.normalize_vault_relative_path("OperationalVault/OperationalVault/People/Derry, Albert.md")
        == "People/Derry, Albert.md"
    )


def test_normalize_passes_through_clean_path(vault_named_bt: None) -> None:
    assert (
        action_candidates.normalize_vault_relative_path("Accounts/Citizenstb/Locations/7090 - Citizens Trust Bank - Ridgeway/about.md")
        == "Accounts/Citizenstb/Locations/7090 - Citizens Trust Bank - Ridgeway/about.md"
    )


def test_normalize_strips_leading_slashes_and_dot(vault_named_bt: None) -> None:
    assert action_candidates.normalize_vault_relative_path("/OperationalVault/People/Smith.md") == "People/Smith.md"
    assert action_candidates.normalize_vault_relative_path("./OperationalVault/People/Smith.md") == "People/Smith.md"
    assert action_candidates.normalize_vault_relative_path("./People/Smith.md") == "People/Smith.md"


def test_normalize_handles_non_string_inputs(vault_named_bt: None) -> None:
    assert action_candidates.normalize_vault_relative_path(None) == ""
    assert action_candidates.normalize_vault_relative_path(123) == ""
    assert action_candidates.normalize_vault_relative_path("") == ""
    assert action_candidates.normalize_vault_relative_path("   ") == ""


def test_normalize_uses_configured_vault_name(monkeypatch) -> None:
    # If the vault gets renamed to something else, the normalizer should
    # strip the new name — proves we're not hardcoded to one vault prefix.
    fake_config = SimpleNamespace(vault_dir=Path("/other/path/CustomVaultName"))
    monkeypatch.setattr(queue_spec, "get_config", lambda: fake_config)
    assert (
        action_candidates.normalize_vault_relative_path("CustomVaultName/Accounts/X/about.md")
        == "Accounts/X/about.md"
    )
    # An "OperationalVault/" prefix is no longer the vault name, so it must NOT be stripped.
    assert (
        action_candidates.normalize_vault_relative_path("OperationalVault/Accounts/X/about.md")
        == "OperationalVault/Accounts/X/about.md"
    )


def test_quality_filter_suppresses_non_field_operation_actions() -> None:
    payload = {
        "action_candidates": [
            "Add journal entry",
            "Review and test the new transcription process",
            "Inspect the sink area for moisture source.",
        ],
        "cleaned_internal_note": "Field note requested a journal entry during a test.",
        "operational_summary": "Test of field-capture semantics.",
        "client_safe_note": "Test note.",
        "issue_type": "water",
    }

    assert action_candidates.quality_filtered_summaries(payload) == [
        "Inspect the sink area for moisture source."
    ]


def test_semantic_channel_metadata_strips_doubled_prefix(vault_named_bt: None) -> None:
    payload = {
        "site_id": "7050",
        "proposed_note_path": "OperationalVault/Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md",
    }
    metadata = action_candidates.semantic_channel_metadata(payload)
    assert metadata["proposed_note_path"] == "Accounts/Hillcrest/Locations/7091 - Hillcrest Corporation/about.md"


def test_semantic_channel_metadata_omits_proposed_when_empty_after_strip(vault_named_bt: None) -> None:
    payload = {"site_id": "7050", "proposed_note_path": "OperationalVault/"}
    metadata = action_candidates.semantic_channel_metadata(payload)
    assert "proposed_note_path" not in metadata


def semantic_payload(*, semantic_type: str, status: str = "complete") -> dict[str, object]:
    return {
        "type": semantic_type,
        "status": status,
        "source_transcript_path": "/tmp/transcripts/cowork-note.txt",
        "audio_asset_id": "asset-123",
        "raw_text_hash": "hash-123",
        "site_id": "7080",
        "upload_id": "upload-123",
        "area": "Cowork",
        "phase": "operations",
        "semantic_engine": "test",
        "issue_detected": True,
        "issue_type": "maintenance",
        "urgency": "normal",
        "visit_proposed": False,
        "visit_type": "",
        "submitter_person_id": "person-123",
        "suggested_tags": ["cowork"],
        "proposed_note_path": "Accounts/Apexco/Locations/7080 - Apex/about.md",
        "cleaned_internal_note": "Pearson and Hawthorne need follow-up after the Cowork voice memo.",
        "operational_summary": "Cowork memo identifies a follow-up for site 7080.",
        "client_safe_note": "Follow-up is needed for the site.",
        "action_candidates": ["Review the Pearson and Hawthorne Cowork follow-up."],
    }


def write_semantic_artifact(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def test_collect_action_candidates_walks_audio_text_and_voice_dirs(tmp_path: Path) -> None:
    audio_dir = tmp_path / "field_capture" / "audio_semantics"
    text_dir = tmp_path / "field_capture" / "semantics"
    voice_dir = tmp_path / "voice_memo" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    write_semantic_artifact(audio_dir / "audio.json", semantic_payload(semantic_type="field_audio_semantic_summary"))
    write_semantic_artifact(text_dir / "text.json", semantic_payload(semantic_type="field_text_semantic_summary"))
    write_semantic_artifact(voice_dir / "voice.json", semantic_payload(semantic_type="voice_memo_semantic_summary"))

    counts = action_candidates.collect_action_candidates([audio_dir, text_dir, voice_dir], candidate_dir)

    assert counts == {"discovered": 3, "skipped": 0, "completed": 3, "failed": 0}
    candidates = sorted(candidate_dir.glob("*.json"))
    assert len(candidates) == 3
    semantic_paths = {
        json.loads(candidate.read_text(encoding="utf-8"))["provenance"]["semantic_artifact_path"]
        for candidate in candidates
    }
    assert semantic_paths == {
        str((audio_dir / "audio.json").resolve(strict=False)),
        str((text_dir / "text.json").resolve(strict=False)),
        str((voice_dir / "voice.json").resolve(strict=False)),
    }


def test_collect_action_candidates_accepts_scalar_path_for_back_compat(tmp_path: Path) -> None:
    semantic_dir = tmp_path / "field_capture" / "audio_semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    write_semantic_artifact(semantic_dir / "audio.json", semantic_payload(semantic_type="field_audio_semantic_summary"))

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    candidates = sorted(candidate_dir.glob("*.json"))
    assert len(candidates) == 1
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    assert payload["provenance"]["semantic_artifact_path"] == str((semantic_dir / "audio.json").resolve(strict=False))


def test_default_semantic_dirs_returns_audio_then_text_then_voice_in_order(tmp_path: Path) -> None:
    assert action_candidates.default_semantic_dirs(tmp_path) == (
        tmp_path / "field_capture" / "audio_semantics",
        tmp_path / "field_capture" / "semantics",
        tmp_path / "voice_memo" / "semantics",
    )


def test_collect_action_candidates_handles_missing_text_semantics_dir(tmp_path: Path) -> None:
    audio_dir = tmp_path / "field_capture" / "audio_semantics"
    text_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    write_semantic_artifact(audio_dir / "audio.json", semantic_payload(semantic_type="field_audio_semantic_summary"))

    counts = action_candidates.collect_action_candidates([audio_dir, text_dir], candidate_dir)

    assert not text_dir.exists()
    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert len(sorted(candidate_dir.glob("*.json"))) == 1


def test_collect_action_candidates_interim_text_semantic_legacy_strings_still_emit_candidate(tmp_path: Path) -> None:
    semantic_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    artifact = run_text_semantic_pipeline("soap is low", site_id="7050", upload_id="typed-note-interim")
    write_semantic_artifact(semantic_dir / "typed-note-interim.json", artifact)

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    assert counts == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    assert candidate["summary"] == "Review supply/order follow-up."
    assert candidate["status"] == "pending_review"


def test_plan_candidates_from_semantic_accepts_text_semantic(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_text_semantic_summary"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_WOULD_CREATE


def test_plan_candidates_from_semantic_accepts_audio_semantic(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_audio_semantic_summary"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_WOULD_CREATE


def structured_semantic_payload(*, extracted_actions: list[dict[str, object]]) -> dict[str, object]:
    payload = semantic_payload(semantic_type="field_text_semantic_summary")
    payload.update(
        {
            "upload_id": "typed-note-254",
            "source_kind": "typed_note",
            "cleaned_internal_note": "Bruce Keller was late. Summit Wire is missing supplies.",
            "operational_summary": "Typed note contains personnel and supply follow-up.",
            "action_candidates": [],
            "employees": [{"id": "emp-damon", "name": "Damon Wrong"}],
            "extracted_actions": extracted_actions,
        }
    )
    return payload


def personnel_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "action_key": "attendance:bruce-keller:late",
        "candidate_type": "personnel_attendance",
        "job_type": "log_personnel_event",
        "target_type": "employee",
        "target_id": "emp-bruce-keller",
        "target_label": "Bruce Keller",
        "summary": "Review Bruce Keller attendance note.",
        "rationale": "Typed note says Bruce Keller was late.",
        "confidence": "high",
        "payload_fields": {"event_type": "attendance"},
        "source_excerpt": "Bruce Keller was late.",
        "evidence_terms": ["late", "Bruce Keller"],
    }
    action.update(overrides)
    return action


def supply_action(**overrides: object) -> dict[str, object]:
    action: dict[str, object] = {
        "action_key": "supply-shrinkage:summit-wire:paper-products",
        "candidate_type": "supply_equipment_shrinkage",
        "target_type": "site",
        "target_id": "7050",
        "target_label": "Summit Wire",
        "summary": "Review Summit Wire supply or equipment shrinkage.",
        "rationale": "Typed note reports missing supply inventory.",
        "confidence": "medium",
        "source_excerpt": "Summit Wire is missing supplies.",
        "evidence_terms": ["Summit Wire", "missing supplies"],
    }
    action.update(overrides)
    return action


def test_collect_action_candidates_fans_out_structured_actions_with_per_action_targets(tmp_path: Path) -> None:
    semantic_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    semantic_path = semantic_dir / "typed-note-254.json"
    write_semantic_artifact(
        semantic_path,
        structured_semantic_payload(extracted_actions=[personnel_action(), supply_action()]),
    )

    first_counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    first_candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]
    first_ids = {candidate["candidate_id"] for candidate in first_candidates}
    second_counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    second_candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]

    assert first_counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert second_counts == {"discovered": 2, "skipped": 2, "completed": 0, "failed": 0}
    assert len(second_candidates) == 2
    assert {candidate["candidate_id"] for candidate in second_candidates} == first_ids

    personnel = next(candidate for candidate in first_candidates if candidate["candidate_type"] == "personnel_attendance")
    assert personnel["channel_metadata"]["target_type"] == "employee"
    assert personnel["channel_metadata"]["target_id"] == "emp-bruce-keller"
    assert personnel["channel_metadata"]["target_label"] == "Bruce Keller"
    assert personnel["channel_metadata"]["target_label"] != "Damon Wrong"

    supply = next(candidate for candidate in first_candidates if candidate["candidate_type"] == "supply_equipment_shrinkage")
    assert supply["status"] == "pending_review"
    assert supply["channel_metadata"]["target_type"] == "site"
    assert supply["channel_metadata"]["target_label"] == "Summit Wire"
    assert "approval_metadata" not in supply


def test_collect_action_candidates_does_not_collapse_same_type_same_target_structured_actions(tmp_path: Path) -> None:
    semantic_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    write_semantic_artifact(
        semantic_dir / "typed-note-254.json",
        structured_semantic_payload(
            extracted_actions=[
                personnel_action(
                    action_key="attendance:bruce-keller:late",
                    source_excerpt="Bruce Keller was late.",
                    summary="Review Bruce Keller lateness.",
                ),
                personnel_action(
                    action_key="attendance:bruce-keller:left-early",
                    source_excerpt="Bruce Keller left early.",
                    summary="Review Bruce Keller leaving early.",
                ),
            ]
        ),
    )

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]

    assert counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    assert len(candidates) == 2
    assert len({candidate["candidate_id"] for candidate in candidates}) == 2
    assert {candidate["channel_metadata"]["target_id"] for candidate in candidates} == {"emp-bruce-keller"}


def test_collect_action_candidates_carries_only_valid_structured_proposed_queue_jobs(tmp_path: Path) -> None:
    semantic_dir = tmp_path / "field_capture" / "semantics"
    candidate_dir = tmp_path / "reviews" / "action_candidates" / "field_capture"
    valid_job = {
        "job_type": "log_personnel_event",
        "payload": {
            "employee": "Bruce Keller",
            "event_type": "attendance",
            "summary": "Bruce Keller was late for the Summit Wire opening.",
            "occurred_at": "2026-06-03T09:00:00-04:00",
            "reported_by": "Jordan",
        },
    }
    invalid_job = {
        "job_type": "log_personnel_event",
        "payload": {
            "employee": "Bruce Keller",
            "event_type": "not-a-valid-event-type",
        },
    }
    write_semantic_artifact(
        semantic_dir / "typed-note-254.json",
        structured_semantic_payload(
            extracted_actions=[
                personnel_action(action_key="attendance:bruce-keller:valid", proposed_queue_job=valid_job),
                personnel_action(
                    action_key="attendance:bruce-keller:invalid",
                    source_excerpt="Bruce Keller had an invalid structured job.",
                    summary="Review invalid structured job.",
                    proposed_queue_job=invalid_job,
                ),
            ]
        ),
    )

    counts = action_candidates.collect_action_candidates(semantic_dir, candidate_dir)
    candidates = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(candidate_dir.glob("*.json"))]

    assert counts == {"discovered": 2, "skipped": 0, "completed": 2, "failed": 0}
    with_job = next(candidate for candidate in candidates if candidate["channel_metadata"]["action_key"] == "attendance:bruce-keller:valid")
    assert with_job["approval_metadata"]["proposed_queue_job"] == valid_job
    assert approved_job_drafts.proposed_queue_job(with_job) == ("log_personnel_event", valid_job["payload"], "")

    without_job = next(candidate for candidate in candidates if candidate["channel_metadata"]["action_key"] == "attendance:bruce-keller:invalid")
    assert without_job["status"] == "pending_review"
    assert "approval_metadata" not in without_job


def test_plan_candidates_from_semantic_rejects_unknown_semantic_type(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_made_up_semantic"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_SKIPPED
    assert "semantic type not accepted" in results[0]["reason"]


def test_plan_candidates_from_semantic_rejects_incomplete_status(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_text_semantic_summary", status="pending"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_SKIPPED
    assert "semantic status is not complete" in results[0]["reason"]
