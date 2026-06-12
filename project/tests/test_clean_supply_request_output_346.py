"""Verifier tests for btq prompt 346 — clean supply-request output for the operator card.

Covers the 346 changes in ``project/processing_core/capture_semantics.py``:
  * ``clean_supply_item_phrase`` truncates a trailing commentary/sentence tail
    (helper ``_truncate_supply_sentence_tail``) so a rambling voice note tightens
    to the items only, and never returns "".
  * ``supply_need_action_payload`` summary is ``"Supply request: {item_name}."``
    (was ``"Log supply need: {item_name}."``), which drives the operator card
    ``message`` via ``job_draft_emission._message``.
  * ``_build_semantic_prompt`` nudges the model to exclude commentary after items.
"""

from __future__ import annotations

from field_capture.job_draft_emission import _message
from processing_core.capture_semantics import (
    CaptureSemanticInput,
    _build_semantic_prompt,
    supply_item_name_from_text,
    supply_need_action_payload,
)

# A rambling voice note: the real request ("mop heads") followed by a spoken
# commentary tail that 346 must drop.
RAMBLING_SUPPLY_TEXT = (
    "we need mop heads. So I would like to see a job that says, "
    "here are the supplies that were requested"
)


def _supply_source(text: str = "we need mop heads") -> CaptureSemanticInput:
    return CaptureSemanticInput(
        capture_id="cap_346",
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id="SANDBOX",
        site_label="SANDBOX",
        area="supplies",
    )


# 1. Tightened item_name — the voice-tail must be stripped to the items only.
def test_item_name_strips_voice_commentary_tail():
    assert supply_item_name_from_text(RAMBLING_SUPPLY_TEXT) == "mop heads"


# 2. 340 regressions still hold (the clean multi-item goldens are untouched).
def test_340_goldens_still_hold():
    assert (
        supply_item_name_from_text(
            "Need to order a few things. A new vacuum and lint brushes."
        )
        == "vacuum, lint brushes"
    )
    assert (
        supply_item_name_from_text("We're low on paper towels and soap")
        == "paper towels, soap"
    )
    assert supply_item_name_from_text("out of trash bags") == "trash bags"
    assert (
        supply_item_name_from_text("a couple of items - gloves and degreaser")
        == "gloves, degreaser"
    )
    assert supply_item_name_from_text("need to restock mop heads") == "mop heads"


def test_item_name_never_empty_guard():
    # "order some" would strip the leading verb AND the article, which would
    # otherwise tighten to "" — the never-empty guard must keep a non-"" value.
    result = supply_item_name_from_text("order some")
    assert result != ""
    assert result.strip()


# 3. Summary text — the payload summary is the new "Supply request:" form.
def test_supply_need_action_payload_summary_text():
    payload = supply_need_action_payload(
        _supply_source(),
        RAMBLING_SUPPLY_TEXT,
        "field_capture_supply_need",
    )
    assert payload["summary"] == "Supply request: mop heads."


# 4. End-to-end operator message — the emitted card message is driven by the
#    action summary through job_draft_emission._message. Proving the card text
#    = asserting _message({"summary": <the payload summary>}) returns it
#    verbatim (no transcript tail leaks through).
def test_operator_card_message_is_clean_supply_request():
    payload = supply_need_action_payload(
        _supply_source(),
        RAMBLING_SUPPLY_TEXT,
        "field_capture_supply_need",
    )
    summary = payload["summary"]
    message = _message({"summary": summary})
    assert message == "Supply request: mop heads."
    # Guard: the rambling transcript tail must not leak into the card.
    assert "would like" not in message
    assert "job that says" not in message


# 5. Prompt contract — the model is told to exclude commentary after the items.
def test_semantic_prompt_excludes_commentary_after_items():
    prompt = _build_semantic_prompt(_supply_source())
    assert "exclude any commentary or speech AFTER the items" in prompt
