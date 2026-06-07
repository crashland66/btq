from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.common import SectionContext
from ops_dashboard.sections import inbox
from processing_core.action_candidates import action_candidate_payload, write_action_candidate_review


def write_pending_candidate(
    runtime_root: Path,
    candidate_id: str,
    *,
    source_transcript_path: str = "",
    capture_id: str | None = None,
) -> None:
    upload_id = capture_id or f"capture-{candidate_id}"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=f"Review {candidate_id}.",
        rationale="Needs operator triage.",
        source_text="Field capture note.",
        source_context="Field capture note.",
        provenance={
            "source_transcript_path": source_transcript_path,
            "semantic_artifact_path": str(runtime_root / "field_capture" / "audio_semantics" / f"{candidate_id}.json"),
        },
        channel_metadata={
            "channel": "field_capture",
            "site_id": "7050",
            "area": "Restrooms",
            "upload_id": upload_id,
            "captured_at": f"2026-05-27T12:0{candidate_id[-1]}:00+00:00",
        },
    )
    candidate["candidate_id"] = candidate_id
    write_action_candidate_review(runtime_root / "reviews" / "action_candidates" / "field_capture", candidate)


def card_by_id(cards: list[dict[str, object]], card_id: str) -> dict[str, object]:
    return next(card for card in cards if card.get("id") == card_id)


def section_ctx(runtime_root: Path, vault_root: Path | None = None) -> SectionContext:
    vault = vault_root or runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


def test_inbox_cards_includes_pipeline_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        inbox,
        "pipeline_status",
        lambda _runtime_root: {
            "summary": {
                "ok": True,
                "failing": [],
            }
        },
    )

    cards = inbox.inbox_cards(section_ctx(tmp_path / "runtime"))

    card = next(item for item in cards if item.get("id") == "pipeline_health")
    assert card["see_all"] == "/health/pipeline"


def test_inbox_cards_partition_captures_with_note_and_pending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    write_pending_candidate(runtime_root, "ac_note_1", source_transcript_path=str(runtime_root / "transcripts" / "note-1.json"))
    write_pending_candidate(runtime_root, "ac_note_2", source_transcript_path=str(runtime_root / "transcripts" / "note-2.json"))
    write_pending_candidate(runtime_root, "ac_no_note_3")

    cards = inbox.inbox_cards(section_ctx(runtime_root, vault_root))

    captures_with_note = card_by_id(cards, "captures_with_note")
    pending_candidates = card_by_id(cards, "pending_candidates")
    note_ids = {str(row.get("candidate_id") or "") for row in captures_with_note["top"]}
    pending_ids = {str(row.get("candidate_id") or "") for row in pending_candidates["top"]}
    assert captures_with_note["count"] == 2
    assert pending_candidates["count"] == 1
    assert note_ids.isdisjoint(pending_ids)
    assert int(captures_with_note["count"]) + int(pending_candidates["count"]) == 3


def test_inbox_renders_multi_candidate_capture_signal(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    write_pending_candidate(runtime_root, "ac_multi_1", capture_id="cap-multi")
    write_pending_candidate(runtime_root, "ac_multi_2", capture_id="cap-multi")
    write_pending_candidate(runtime_root, "ac_single_3", capture_id="cap-single")

    body = inbox.render(section_ctx(runtime_root, vault_root))

    assert "2 pending candidates from this capture" in body
    assert "1 pending candidates from this capture" not in body


def test_inbox_cards_pending_candidates_title_reflects_no_note(tmp_path: Path) -> None:
    cards = inbox.inbox_cards(section_ctx(tmp_path / "runtime"))

    assert card_by_id(cards, "pending_candidates")["title"] == "Pending candidates without a note"


def test_candidate_inbox_row_summary_prefers_source_text() -> None:
    candidate = {
        "source_text": "the actual note content",
        "summary": "Review the field audio note...",
    }

    row = inbox.candidate_inbox_row(candidate)

    assert row["summary"] == "the actual note content"


def test_candidate_inbox_row_summary_falls_back_to_action_label() -> None:
    candidate = {
        "summary": "Review the field audio note...",
    }

    row = inbox.candidate_inbox_row(candidate)

    assert row["summary"] == "Review the field audio note..."


def test_candidate_inbox_row_summary_truncates_long_note() -> None:
    candidate = {
        "source_text": "x" * 300,
        "summary": "Review the field audio note...",
    }

    row = inbox.candidate_inbox_row(candidate)

    assert len(str(row["summary"])) <= 200
    assert str(row["summary"]).endswith("...")
