"""INDEPENDENT VERIFIER gating tests for prompt 394: the /candidates job_draft
review card grounds the approver in the HUMAN source (the cleaner's words from
the originating capture's semantic artifact), NOT the model's proposed-action
``message``.

Sandbox identity only (capture ``cap-sandbox-*``, site ``SANDBOX``, person
``sandbox-user``). No real names.

These exercise the resolution end-to-end: ``candidate_detail`` is called with
``human_source=None`` so it must resolve the human source via ``runtime_root``
and ``source_capture_id`` against a tmp ``field_capture/semantics/{cid}.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_dashboard.common import human_source_by_capture, human_source_details_by_capture
from ops_dashboard.sections.candidates import (
    candidate_detail,
    render_evidence_summary,
    render_source_pill,
)


HUMAN_WORDS = "Restocked paper towels and emptied the bins in the SANDBOX lobby."
MODEL_MESSAGE = "Create consumables job: order paper towels for SANDBOX."


def _write_semantic(runtime_root: Path, capture_id: str, source_text: str) -> Path:
    semantic_dir = runtime_root / "field_capture" / "semantics"
    semantic_dir.mkdir(parents=True, exist_ok=True)
    path = semantic_dir / f"{capture_id}.json"
    path.write_text(
        json.dumps(
            {
                "capture_id": capture_id,
                "source_text": source_text,
                "source_transcript_path": f"/runtime/transcripts/{capture_id}.json",
            }
        ),
        encoding="utf-8",
    )
    return path


def _job_draft_payload(capture_id: str) -> dict:
    return {
        "type": "job_draft_review",
        "draft_id": "draft-sandbox-1",
        "review_status": "pending_approval",
        "source_capture_id": capture_id,
        "site_id": "SANDBOX",
        "job_type": "consumables_order",
        "message": MODEL_MESSAGE,
        "submitter_name": "sandbox-user",
        "payload": {"site_id": "SANDBOX", "items": ["paper towels"]},
    }


def test_resolved_case_card_uses_human_source_not_model_message(tmp_path: Path) -> None:
    capture_id = "cap-sandbox-resolved"
    _write_semantic(tmp_path, capture_id, HUMAN_WORDS)
    payload = _job_draft_payload(capture_id)

    # human_source=None forces resolution via runtime_root + source_capture_id.
    card = candidate_detail(tmp_path / "draft.json", payload, tmp_path)

    # ACCEPTANCE 1: source_text is the HUMAN words, never the model message.
    assert card["source_text"] == HUMAN_WORDS
    assert card["source_text"] != MODEL_MESSAGE
    # The model's action text is retained as summary (proposed action).
    assert card["summary"] == MODEL_MESSAGE

    # ACCEPTANCE 2: source_is_human True + badge renders.
    assert card["source_is_human"] is True
    assert render_source_pill(card) == '<span class="pill source-human">source: human</span>'

    # ACCEPTANCE 4: the grounding shown is the human words, labelled as the
    # cleaner's report (not "Why"), and the model message is NOT in the evidence.
    evidence = render_evidence_summary(card)
    assert HUMAN_WORDS in evidence
    assert MODEL_MESSAGE not in evidence
    assert "Reported by cleaner" in evidence
    assert f"<blockquote>{HUMAN_WORDS}</blockquote>" in evidence


def test_no_source_case_no_badge_and_model_message_not_shown_as_human(tmp_path: Path) -> None:
    # No semantic artifact for this capture -> no human source resolves.
    capture_id = "cap-sandbox-no-source"
    payload = _job_draft_payload(capture_id)

    card = candidate_detail(tmp_path / "draft.json", payload, tmp_path)

    # ACCEPTANCE 2: no human source -> source_is_human False, no badge.
    assert card["source_is_human"] is False
    assert render_source_pill(card) == ""
    # source_text is NOT the model message mislabelled as human.
    assert card["source_text"] != MODEL_MESSAGE
    assert not card["source_text"]
    # A clear "no human source" state is carried.
    assert "No human source found" in str(card["source_missing_message"])

    # The rendered evidence shows the no-human-source state, NOT the model
    # message presented as the cleaner's words.
    evidence = render_evidence_summary(card)
    assert "No human source found for originating capture." in evidence
    assert MODEL_MESSAGE not in evidence


def test_human_source_by_capture_maps_and_missing_dir_is_empty(tmp_path: Path) -> None:
    # ACCEPTANCE 3: missing field_capture/semantics dir -> {} no raise.
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    assert human_source_by_capture(empty_root) == {}
    assert human_source_details_by_capture(empty_root) == {}

    # Correctly maps capture_id -> source_text from semantic JSONs.
    cid_a = "cap-sandbox-a"
    cid_b = "cap-sandbox-b"
    _write_semantic(tmp_path, cid_a, "Mopped the SANDBOX hallway.")
    _write_semantic(tmp_path, cid_b, "Reported a leaking tap in SANDBOX restroom.")

    mapping = human_source_by_capture(tmp_path)
    assert mapping[cid_a] == "Mopped the SANDBOX hallway."
    assert mapping[cid_b] == "Reported a leaking tap in SANDBOX restroom."

    details = human_source_details_by_capture(tmp_path)
    assert details[cid_a]["source_text"] == "Mopped the SANDBOX hallway."
    assert details[cid_a]["semantic_artifact_path"].endswith(f"{cid_a}.json")
