"""Gating tests for the keyword `action_candidates` fallback (395).

`payloads_from_semantic` originates candidates from a capture's semantic
artifact. When the LLM/rule engine populates ``extracted_actions`` we take the
source-grounded structured path. When it does NOT (empty/absent
``extracted_actions``) the OLD code fell back to the crude keyword
``action_candidates`` field (e.g. the moisture/"sink" rule) and MANUFACTURED a
candidate -- a false candidate when no action was actually found.

The change gates that keyword fallback behind ``BTQ_ALLOW_KEYWORD_ACTION_FALLBACK``
(default OFF). These tests pin the contract:

1. empty extracted_actions + populated keyword action_candidates -> 0 with flag OFF
2. same payload + flag ON -> the OLD keyword candidate(s) reappear (reversible)
3. extracted_actions path -> unchanged regardless of the flag (LLM path independent)
4. malformed action_candidates -> no crash, 0 approvable candidates with flag OFF

Independent verification for 395 (BTQ_ALLOW_KEYWORD_ACTION_FALLBACK).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import field_capture.action_candidates as fc
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import RuleCaptureEngine

MOISTURE_RULE_SUMMARY = "Inspect the affected sink or floor area for moisture source."


def _moisture_shape_payload() -> dict:
    """An LLM-style semantic artifact that found NO action (empty
    extracted_actions) but whose crude keyword field carries the moisture/"sink"
    fallback string. This is exactly the false-candidate shape 395 suppresses."""
    return {
        "extracted_actions": [],
        "action_candidates": [MOISTURE_RULE_SUMMARY],
        "cleaned_internal_note": "The mop sink in the back is broken and leaking water everywhere.",
        "operational_summary": "Sink reported leaking.",
        "client_safe_note": "Maintenance follow-up.",
        "upload_id": "cap-gate-moisture-1",
        "site_id": "SANDBOX",
    }


# --------------------------------------------------------------------------- #
# Criterion 1: flag OFF (default) -> the keyword fallback originates NOTHING.
# --------------------------------------------------------------------------- #
def test_moisture_keyword_fallback_suppressed_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", raising=False)
    payload = _moisture_shape_payload()
    assert fc.has_extracted_actions(payload) is False
    assert fc.keyword_action_fallback_enabled() is False
    assert fc.payloads_from_semantic(Path("ignored.json"), payload) == []


# --------------------------------------------------------------------------- #
# Criterion 2: flag ON -> the OLD keyword candidate(s) reappear (reversibility).
# --------------------------------------------------------------------------- #
def test_moisture_keyword_fallback_restored_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", "1")
    payload = _moisture_shape_payload()
    assert fc.keyword_action_fallback_enabled() is True
    payloads = fc.payloads_from_semantic(Path("ignored.json"), payload)
    assert payloads, "flag-on must reproduce the old keyword fallback candidate"
    summaries = [str(p.get("summary") or "") for p in payloads]
    assert MOISTURE_RULE_SUMMARY in summaries


@pytest.mark.parametrize("truthy", ["1", "true", "YES", "On", "enabled"])
def test_flag_truthy_values_enable_fallback(monkeypatch, truthy) -> None:
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", truthy)
    assert fc.keyword_action_fallback_enabled() is True
    payloads = fc.payloads_from_semantic(Path("ignored.json"), _moisture_shape_payload())
    assert payloads


@pytest.mark.parametrize("falsey", ["0", "false", "no", "off", "disabled"])
def test_flag_falsey_values_keep_fallback_off(monkeypatch, falsey) -> None:
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", falsey)
    assert fc.keyword_action_fallback_enabled() is False
    assert fc.payloads_from_semantic(Path("ignored.json"), _moisture_shape_payload()) == []


def test_flag_default_unset_is_off(monkeypatch) -> None:
    # The contract that matters: with the env var UNSET, the fallback is OFF
    # (default "0"). Matches the existing os.environ.get(ENV, "0") pattern.
    monkeypatch.delenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", raising=False)
    assert fc.keyword_action_fallback_enabled() is False


# --------------------------------------------------------------------------- #
# Criterion 3: the extracted_actions (LLM/structured) path is unchanged by the
# flag. Built from the real semantic pipeline so it carries source_excerpt.
# --------------------------------------------------------------------------- #
def _issue_artifact() -> dict:
    art = run_text_semantic_pipeline(
        "The mop sink in the back is broken and leaking water everywhere.",
        site_id="SANDBOX",
        upload_id="cap-gate-issue-1",
        area="",
        engine=RuleCaptureEngine(),
    )
    return json.loads(json.dumps(art))


def test_extracted_actions_path_independent_of_flag(monkeypatch) -> None:
    art = _issue_artifact()
    # Precondition: this artifact takes the structured path.
    assert fc.has_extracted_actions(art) is True

    monkeypatch.delenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", raising=False)
    off = fc.payloads_from_semantic(Path("x.json"), art)

    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", "1")
    on = fc.payloads_from_semantic(Path("x.json"), art)

    assert off, "structured path must originate its grounded candidate"
    assert [p.get("summary") for p in off] == [p.get("summary") for p in on]
    # The grounded candidate carries the source excerpt (structured path marker).
    assert any(str(p.get("source_text") or "") for p in off)


# --------------------------------------------------------------------------- #
# Criterion 4: malformed action_candidates (not a list) -> no crash, and with
# the flag OFF no approvable candidate is originated.
# --------------------------------------------------------------------------- #
def _approvable(payloads) -> list:
    return [p for p in payloads if isinstance(p.get("approval_metadata"), dict) and p["approval_metadata"]]


def test_malformed_action_candidates_no_crash_no_candidate_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", raising=False)
    payload = {
        "extracted_actions": [],
        "action_candidates": "not-a-list",  # malformed
        "cleaned_internal_note": "garbled",
        "operational_summary": "",
        "upload_id": "cap-gate-malformed-1",
        "site_id": "SANDBOX",
    }
    # Must not raise.
    payloads = fc.payloads_from_semantic(Path("ignored.json"), payload)
    assert payloads == []
    assert _approvable(payloads) == []


def test_malformed_action_candidates_dict_shape_no_crash(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", raising=False)
    payload = {
        "extracted_actions": [],
        "action_candidates": {"unexpected": "shape"},
        "upload_id": "cap-gate-malformed-2",
        "site_id": "SANDBOX",
    }
    payloads = fc.payloads_from_semantic(Path("ignored.json"), payload)
    assert payloads == []
    assert _approvable(payloads) == []
