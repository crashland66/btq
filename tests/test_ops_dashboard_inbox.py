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


def seed_pending_draft(
    couchdb_job_draft_review,
    draft_id: str,
    *,
    capture_id: str | None = None,
) -> None:
    """337a: the inbox now projects job_draft docs (review_status
    pending_approval). Seed one into the in-memory draft review double."""
    cap = capture_id or f"capture-{draft_id}"
    couchdb_job_draft_review.seed_draft({
        "draft_id": draft_id,
        "job_type": "append_to_note",
        "payload": {"path": "Accounts/7050.md", "content": "x", "destination": "site_note"},
        "message": f"Review {draft_id}.",
        "site_id": "7050",
        "submitter_name": "Sandy",
        "source_capture_id": cap,
        "created_at": f"2026-05-27T12:00:0{draft_id[-1]}+00:00",
    })


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


def test_inbox_cards_surface_all_pending_drafts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, couchdb_job_draft_review) -> None:
    # 337a: the inbox projects job_draft docs. The candidate-model note/no-note
    # PARTITION is not ported (drafts carry no transcript distinction) -- every
    # pending_approval draft lands in the single review card and the no-note
    # bucket is empty. Retargeted to that draft reality.
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    seed_pending_draft(couchdb_job_draft_review, "ac_note_1")
    seed_pending_draft(couchdb_job_draft_review, "ac_note_2")
    seed_pending_draft(couchdb_job_draft_review, "ac_no_note_3")

    cards = inbox.inbox_cards(section_ctx(runtime_root, vault_root))

    review_card = card_by_id(cards, "captures_with_note")
    no_context = card_by_id(cards, "pending_candidates")
    draft_ids = {str(row.get("draft_id") or "") for row in review_card["top"]}
    assert review_card["count"] == 3
    assert draft_ids == {"ac_note_1", "ac_note_2", "ac_no_note_3"}
    assert no_context["count"] == 0


def test_inbox_renders_multi_candidate_capture_signal(tmp_path: Path, couchdb_job_draft_review) -> None:
    # 337a/338: retargeted to job_draft. Two pending_approval drafts sharing one
    # source_capture_id surface the multi-pending-per-capture signal on the inbox;
    # a draft on a distinct capture does not. Guards the common.py count helpers
    # now keyed on status in ("pending_review", "pending_approval").
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    seed_pending_draft(couchdb_job_draft_review, "ac_multi_1", capture_id="cap-multi")
    seed_pending_draft(couchdb_job_draft_review, "ac_multi_2", capture_id="cap-multi")
    seed_pending_draft(couchdb_job_draft_review, "ac_single_3", capture_id="cap-single")

    body = inbox.render(section_ctx(runtime_root, vault_root))

    assert "2 pending candidates from this capture" in body
    assert "1 pending candidates from this capture" not in body


def test_inbox_cards_pending_candidates_title_reflects_no_note(tmp_path: Path) -> None:
    # 337a: the secondary inbox bucket is retitled for the draft model.
    cards = inbox.inbox_cards(section_ctx(tmp_path / "runtime"))

    assert card_by_id(cards, "pending_candidates")["title"] == "Pending drafts without capture context"


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
