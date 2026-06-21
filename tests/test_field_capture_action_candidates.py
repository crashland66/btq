from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import queue_spec
from field_capture import action_candidates


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


def test_default_semantic_dirs_returns_audio_then_text_then_voice_in_order(tmp_path: Path) -> None:
    assert action_candidates.default_semantic_dirs(tmp_path) == (
        tmp_path / "field_capture" / "audio_semantics",
        tmp_path / "field_capture" / "semantics",
        tmp_path / "voice_memo" / "semantics",
    )


def test_plan_candidates_from_semantic_skips_text_semantic_without_extracted_actions(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_text_semantic_summary"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_SKIPPED
    assert results[0]["reason"] == "semantic action candidates suppressed by quality rules"
    assert results[0]["candidate"] is None


def test_plan_candidates_from_semantic_skips_audio_semantic_without_extracted_actions(tmp_path: Path) -> None:
    results = action_candidates.plan_candidates_from_semantic(
        tmp_path / "semantic.json",
        semantic_payload(semantic_type="field_audio_semantic_summary"),
        tmp_path / "candidates",
    )

    assert results[0]["status"] == action_candidates.CANDIDATE_PLAN_SKIPPED
    assert results[0]["reason"] == "semantic action candidates suppressed by quality rules"
    assert results[0]["candidate"] is None


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
