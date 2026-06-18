"""INDEPENDENT VERIFIER gating tests for prompt 396: the /swipe quick-review
card surfaces the HUMAN source (the cleaner's actual words from the originating
capture's semantic artifact) ADDITIVELY -- WITHOUT overwriting the card's
existing model-derived ``message`` / ``summary`` / ``evidence`` fields.

Sandbox identity only (capture ``cap-sandbox-*``, site ``SANDBOX``, person
``sandbox-user``). No real names.

The resolution is exercised end-to-end: ``swipe_card`` is called with
``human_sources=None`` so it must resolve the human source itself via
``runtime_root`` + ``source_capture_id`` against a tmp
``field_capture/semantics/{cid}.json``. The additive-invariant test
(``card["message"] == payload["message"]``) is the critical regression guard:
the prior attempt regressed by reassigning ``message`` to the human source.
"""
from __future__ import annotations

import json
from pathlib import Path

from ops_dashboard.sections import swipe


# The cleaner's actual words vs the model's proposed-action text. These MUST
# stay distinct so a regression that conflates them is caught.
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
        "job_type": "append_to_note",
        "message": MODEL_MESSAGE,
        "submitter_name": "sandbox-user",
        "confidence": "high",
        "created_at": "2026-06-17T00:00:00Z",
        "payload": {
            "path": "Accounts/SANDBOX.md",
            "content": "order paper towels",
            "destination": "site_note",
        },
    }


def _card_resolved_via_runtime(runtime_root: Path, payload: dict) -> dict:
    # human_sources=None forces swipe_card to resolve via runtime_root +
    # source_capture_id -- the production path collect_cards exercises.
    return swipe.swipe_card(
        runtime_root / "draft.json",
        payload,
        runtime_root=runtime_root,
        submitters={},
        human_sources=None,
    )


def test_resolved_card_exposes_human_source_text_additively(tmp_path: Path) -> None:
    """ACCEPTANCE 1: the human source_text is exposed (cleaner's words) +
    source_is_human True, IN ADDITION to the existing fields."""
    capture_id = "cap-sandbox-resolved"
    _write_semantic(tmp_path, capture_id, HUMAN_WORDS)
    payload = _job_draft_payload(capture_id)

    card = _card_resolved_via_runtime(tmp_path, payload)

    # The human words are surfaced...
    assert card["source_text"] == HUMAN_WORDS
    assert card["source_is_human"] is True
    # ...and are genuinely the cleaner's words, NOT the model message.
    assert card["source_text"] != MODEL_MESSAGE


def test_additive_invariant_message_stays_model_message_even_when_human_resolves(
    tmp_path: Path,
) -> None:
    """ACCEPTANCE 2 (CRITICAL): even when a human source resolves, the card's
    message/summary/evidence MUST remain payload["message"] (the proposed-action
    text). This is the exact regression the prior attempt introduced."""
    capture_id = "cap-sandbox-additive"
    _write_semantic(tmp_path, capture_id, HUMAN_WORDS)
    payload = _job_draft_payload(capture_id)

    card = _card_resolved_via_runtime(tmp_path, payload)

    # The additive invariant, asserted three ways.
    assert card["message"] == payload["message"]
    assert card["message"] == MODEL_MESSAGE
    assert card["summary"] == payload["message"]
    assert card["evidence"] == payload["message"]
    # And the human source did NOT leak into the model fields.
    assert card["message"] != HUMAN_WORDS
    assert card["summary"] != HUMAN_WORDS
    assert card["evidence"] != HUMAN_WORDS
    # The human source still resolved alongside (proves the message stayed put
    # DESPITE a human source being present, not because none was found).
    assert card["source_text"] == HUMAN_WORDS
    assert card["source_is_human"] is True


def test_no_human_source_no_badge_unchanged_message_no_crash(tmp_path: Path) -> None:
    """ACCEPTANCE 3: a capture with no resolvable human source -> source_is_human
    False, unchanged message, a missing-source note, and no crash."""
    capture_id = "cap-sandbox-no-source"
    # No semantic artifact written for this capture -> no human source resolves.
    (tmp_path / "field_capture" / "semantics").mkdir(parents=True, exist_ok=True)
    payload = _job_draft_payload(capture_id)

    card = _card_resolved_via_runtime(tmp_path, payload)

    assert card["source_is_human"] is False
    assert not card["source_text"]
    # The model message is untouched.
    assert card["message"] == MODEL_MESSAGE
    assert card["summary"] == MODEL_MESSAGE
    assert card["evidence"] == MODEL_MESSAGE
    # A clear missing-source state is carried (consumed by the JS to show the
    # muted note instead of the cleaner blockquote).
    assert "No human source found" in str(card["source_missing_message"])


def test_missing_semantics_dir_resolver_empty_card_renders_as_before(tmp_path: Path) -> None:
    """ACCEPTANCE 5: missing field_capture/semantics dir -> resolver returns {}
    -> the card still builds, no human source, unchanged message, no raise."""
    capture_id = "cap-sandbox-missing-dir"
    # Deliberately do NOT create field_capture/semantics.
    payload = _job_draft_payload(capture_id)

    from ops_dashboard.common import human_source_details_by_capture

    assert human_source_details_by_capture(tmp_path) == {}

    card = _card_resolved_via_runtime(tmp_path, payload)
    assert card["source_is_human"] is False
    assert not card["source_text"]
    assert card["message"] == MODEL_MESSAGE


def test_swipe_body_shows_cleaner_markup_only_when_human_source_resolves(tmp_path: Path) -> None:
    """ACCEPTANCE 4: the 'Reported by cleaner' / 'source: human' markup the JS
    bootstrap consumes appears (the snippet builder is present in render_body),
    and a card dict with source_is_human True carries exactly the fields that JS
    keys off, while a non-human card carries none of the human text."""
    from types import SimpleNamespace

    body = swipe.render_body(
        SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault")),
        payload={"cards": [], "counts": {}},
    )
    # The JS that renders the badge/snippet keys off c.source_is_human and
    # c.source_text and emits the cleaner markup. Assert that wiring exists.
    assert "source_is_human" in body
    assert "source-human" in body
    assert "Reported by cleaner" in body
    assert "source: human" in body

    # The card dict the JS consumes: human resolves -> the fields the markup
    # branches on are populated.
    capture_id = "cap-sandbox-markup"
    _write_semantic(tmp_path, capture_id, HUMAN_WORDS)
    human_card = _card_resolved_via_runtime(tmp_path, _job_draft_payload(capture_id))
    assert human_card["source_is_human"] is True
    assert human_card["source_text"] == HUMAN_WORDS

    # No human source -> the JS branch that emits the cleaner blockquote is not
    # taken (source_is_human False, empty source_text).
    no_source_card = _card_resolved_via_runtime(
        tmp_path, _job_draft_payload("cap-sandbox-markup-absent")
    )
    assert no_source_card["source_is_human"] is False
    assert not no_source_card["source_text"]


def test_explicit_human_sources_arg_also_resolves(tmp_path: Path) -> None:
    """The collect_cards path passes a prebuilt human_sources map; verify the
    same additive behavior when human_sources is supplied explicitly (not None)."""
    capture_id = "cap-sandbox-explicit"
    payload = _job_draft_payload(capture_id)

    card = swipe.swipe_card(
        tmp_path / "draft.json",
        payload,
        runtime_root=tmp_path,
        submitters={},
        human_sources={capture_id: {"source_text": HUMAN_WORDS}},
    )
    assert card["source_text"] == HUMAN_WORDS
    assert card["source_is_human"] is True
    # Additive invariant holds on the explicit path too.
    assert card["message"] == MODEL_MESSAGE
