"""Gating tests for the no-vision-origination invariant (393).

Operator policy: the vision model may ENRICH a capture for review, but it must
NEVER originate a proposed job. A proposed job (action candidate) may originate
ONLY from human input -- a voice transcript or a typed note. In the code that
means a candidate must carry a non-empty human-text basis under ``source_text``
(or ``source_excerpt``); a candidate with no such basis (a vision-only capture)
must be suppressed at origination and blocked at staging.

393 locks that invariant behind a reversible, default-OFF flag
``BTQ_ALLOW_VISION_ORIGINATED_JOBS`` (mirroring ``keyword_action_fallback_enabled``).

Contract pinned here (independent verification for 393):

1. flag OFF (default) + vision-only / no-human-text artifact -> 0 candidates
   from payloads_from_semantic AND structured_payloads_from_semantic.
2. a normal human-originated capture (structured action carrying a non-empty
   source_excerpt) STILL produces its candidate -- the guard drops NOTHING
   legitimate (the critical non-regression).
3. a basis-less candidate is BLOCKED from staging (raises CandidateReviewError),
   never silently staged, with the flag off.
4. flag ON -> pre-393 behavior restored (guard suppresses nothing): reversible.
5. flag parse matches keyword_action_fallback_enabled (default off;
   0/false/no/off/disabled off, anything else on).

Sandbox identity only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import field_capture.action_candidates as fc
from field_capture import candidate_staging
from event_pipeline import couchdb_config
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import RuleCaptureEngine


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _human_originated_structured_artifact() -> dict:
    """A REAL human-originated capture run through the semantic pipeline. It
    populates extracted_actions, each carrying a non-empty source_excerpt -- the
    legitimate, source-grounded shape the guard must never suppress."""
    art = run_text_semantic_pipeline(
        "The mop sink in the back is broken and leaking water everywhere.",
        site_id="SANDBOX",
        upload_id="cap-393-human-1",
        area="",
        engine=RuleCaptureEngine(),
    )
    return json.loads(json.dumps(art))


def _vision_only_artifact() -> dict:
    """A vision-only / no-human-text capture: no transcript, no typed note, no
    structured extracted_actions, no cleaned_internal_note. The crude keyword
    action_candidates field still carries a string (as a vision/keyword path
    might emit), but there is NO human-text basis behind it."""
    return {
        "extracted_actions": [],
        "action_candidates": ["Inspect the affected area."],
        # No cleaned_internal_note / transcript / typed note => no human basis.
        "cleaned_internal_note": "",
        "operational_summary": "Possible spill visible in image.",
        "client_safe_note": "",
        "upload_id": "cap-393-vision-1",
        "site_id": "SANDBOX",
    }


def _vision_only_structured_artifact() -> dict:
    """A structured artifact whose single extracted action carries an EMPTY
    source_excerpt -- i.e. a job proposed with no human-text basis. The schema
    requires source_excerpt to be non-empty, so a truly basis-less structured
    candidate is constructed directly via action_candidate_payload below; this
    helper instead exercises the keyword path. Kept for clarity of intent."""
    return _vision_only_artifact()


def _basisless_record() -> dict:
    """A candidate record built exactly as the production code builds one, but
    with empty human-text basis (source_text/source_context blank) -- the shape
    the staging guard must reject."""
    from processing_core.action_candidates import action_candidate_payload

    return action_candidate_payload(
        candidate_type=fc.FIELD_CAPTURE_CANDIDATE_TYPE,
        summary="Inspect the affected area.",
        rationale="",
        source_text="",
        source_context="",
        provenance={"capture_id": "cap-393-vision-staging-1"},
        channel_metadata={"site_id": "SANDBOX"},
    )


# --------------------------------------------------------------------------- #
# Criterion 5: flag parse mirrors keyword_action_fallback_enabled.
# --------------------------------------------------------------------------- #
def test_vision_flag_default_unset_is_off(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    assert fc.vision_originated_jobs_enabled() is False


@pytest.mark.parametrize("falsey", ["0", "false", "no", "off", "disabled", "FALSE", "Off"])
def test_vision_flag_falsey_values_off(monkeypatch, falsey) -> None:
    monkeypatch.setenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", falsey)
    assert fc.vision_originated_jobs_enabled() is False


@pytest.mark.parametrize("truthy", ["1", "true", "YES", "On", "enabled", "anything"])
def test_vision_flag_truthy_values_on(monkeypatch, truthy) -> None:
    monkeypatch.setenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", truthy)
    assert fc.vision_originated_jobs_enabled() is True


# --------------------------------------------------------------------------- #
# Criterion 1: flag OFF (default) + vision-only/no-human-text -> 0 candidates.
# --------------------------------------------------------------------------- #
def test_vision_only_keyword_path_suppressed_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    # Allow the keyword fallback so origination is even attempted -- this proves
    # the 393 human-basis guard (not 395's keyword gate) is what suppresses.
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", "1")
    payload = _vision_only_artifact()
    assert fc.has_extracted_actions(payload) is False
    assert fc.payloads_from_semantic(Path("ignored.json"), payload) == []


def test_vision_only_structured_path_suppressed_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    # Build a structured artifact by hand whose extracted action carries an
    # EMPTY source_excerpt is impossible (schema rejects it), so we assert the
    # guard at the record level: a basis-less record is suppressed.
    record = _basisless_record()
    assert fc.candidate_has_human_text_basis(record) is False
    assert fc.suppress_candidate_without_human_text_basis(record) is True


def test_vision_only_keyword_path_suppressed_even_with_both_gates_open(monkeypatch) -> None:
    # 393 must hold independently of 395. With the keyword gate OPEN and the
    # vision gate CLOSED, a vision-only (no human basis) capture still yields 0.
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", "1")
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    assert fc.structured_payloads_from_semantic(
        Path("ignored.json"), _vision_only_artifact()
    ) == []
    assert fc.payloads_from_semantic(Path("ignored.json"), _vision_only_artifact()) == []


# --------------------------------------------------------------------------- #
# Criterion 2: human-originated capture STILL produces its candidate (the guard
# drops NOTHING legitimate). This is the most important non-regression.
# --------------------------------------------------------------------------- #
def test_human_originated_structured_candidate_survives_guard(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    art = _human_originated_structured_artifact()
    assert fc.has_extracted_actions(art) is True

    structured = fc.structured_payloads_from_semantic(Path("x.json"), art)
    payloads = fc.payloads_from_semantic(Path("x.json"), art)

    assert structured, "structured human candidate must NOT be suppressed"
    assert payloads, "payloads_from_semantic must keep the human candidate"
    # Each surviving candidate carries a non-empty human-text basis.
    for record in structured:
        assert fc.candidate_has_human_text_basis(record) is True
        assert str(record.get("source_text") or "").strip()


def test_human_originated_candidate_identical_regardless_of_flag(monkeypatch) -> None:
    art = _human_originated_structured_artifact()

    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    off = fc.structured_payloads_from_semantic(Path("x.json"), art)

    monkeypatch.setenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", "1")
    on = fc.structured_payloads_from_semantic(Path("x.json"), art)

    assert off, "legitimate human candidate present with flag off"
    assert [p.get("summary") for p in off] == [p.get("summary") for p in on]
    assert [p.get("candidate_id") for p in off] == [p.get("candidate_id") for p in on]


# --------------------------------------------------------------------------- #
# Criterion 4: flag ON -> guard suppresses nothing (reversible / pre-393).
# --------------------------------------------------------------------------- #
def test_flag_on_restores_vision_origination(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", "1")
    monkeypatch.setenv("BTQ_ALLOW_KEYWORD_ACTION_FALLBACK", "1")
    payload = _vision_only_artifact()
    # With the vision flag ON the basis-less keyword candidate is restored.
    payloads = fc.payloads_from_semantic(Path("ignored.json"), payload)
    assert payloads, "flag-on must restore pre-393 vision/keyword origination"


def test_flag_on_record_not_suppressed(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", "1")
    record = _basisless_record()
    assert fc.candidate_has_human_text_basis(record) is False
    # Flag ON: the guard is disabled, so the basis-less record is NOT suppressed.
    assert fc.suppress_candidate_without_human_text_basis(record) is False
    assert fc.candidate_missing_human_text_basis_reason(record) == ""


# --------------------------------------------------------------------------- #
# Criterion 3: a basis-less candidate is BLOCKED from staging (raises), never
# silently staged, with the flag off.
# --------------------------------------------------------------------------- #
def test_require_human_text_basis_raises_for_basisless_record(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    record = _basisless_record()
    with pytest.raises(fc.CandidateReviewError):
        fc.require_candidate_human_text_basis(record, context="unit")


def test_require_human_text_basis_passes_for_human_record(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)
    from processing_core.action_candidates import action_candidate_payload

    record = action_candidate_payload(
        candidate_type=fc.FIELD_CAPTURE_CANDIDATE_TYPE,
        summary="Order more soap.",
        source_text="The dispenser in restroom 2 is empty, please reorder soap.",
        provenance={"capture_id": "cap-393-human-staging-1"},
    )
    # Must NOT raise for a human-grounded record.
    fc.require_candidate_human_text_basis(record, context="unit")


def test_staging_materialization_blocks_basisless_candidate(monkeypatch, tmp_path) -> None:
    """Drive materialize_candidate_for_direct_staging with a CouchDB doc that has
    no human-text basis; the staging guard must raise CandidateReviewError and
    NOT write an approved artifact."""
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)

    candidate_id = "cap-393-vision-staging-1"
    # A CouchDB candidate doc whose review payload will carry NO source_text.
    couch_doc = {
        "_id": f"action_candidate:{candidate_id}",
        "candidate_id": candidate_id,
        "candidate_type": fc.FIELD_CAPTURE_CANDIDATE_TYPE,
        "summary": "Inspect the affected area.",
        "status": "pending_review",
        "source": {"source_text": "", "source_context": ""},
        "site_id": "SANDBOX",
    }

    monkeypatch.setattr(
        candidate_staging.field_action_candidates,
        "couchdb_candidate_config_or_none",
        lambda: object(),  # non-None config so we proceed to the doc fetch
    )
    monkeypatch.setattr(
        candidate_staging.field_action_candidates,
        "get_action_candidate",
        lambda config, db, cid: couch_doc,
    )
    monkeypatch.setattr(couchdb_config, "field_captures_database", lambda: "field_captures")

    with pytest.raises(fc.CandidateReviewError):
        candidate_staging.materialize_candidate_for_direct_staging(tmp_path, candidate_id)

    # No approved candidate artifact should have been written.
    candidate_dir = fc.default_candidate_dir(tmp_path)
    written = list(candidate_dir.glob("**/*.json")) if candidate_dir.exists() else []
    assert written == [], "basis-less candidate must not be materialized for staging"


def test_staging_materialization_allows_human_candidate(monkeypatch, tmp_path) -> None:
    """A human-grounded CouchDB candidate (non-empty source_text) materializes
    fine -- the guard blocks nothing legitimate at staging."""
    monkeypatch.delenv("BTQ_ALLOW_VISION_ORIGINATED_JOBS", raising=False)

    candidate_id = "cap-393-human-staging-2"
    couch_doc = {
        "_id": f"action_candidate:{candidate_id}",
        "candidate_id": candidate_id,
        "candidate_type": fc.FIELD_CAPTURE_CANDIDATE_TYPE,
        "summary": "Reorder soap.",
        "status": "pending_review",
        "source": {
            "source_text": "The dispenser in restroom 2 is empty, please reorder soap.",
            "source_context": "Restroom supplies low.",
        },
        "site_id": "SANDBOX",
    }

    monkeypatch.setattr(
        candidate_staging.field_action_candidates,
        "couchdb_candidate_config_or_none",
        lambda: object(),
    )
    monkeypatch.setattr(
        candidate_staging.field_action_candidates,
        "get_action_candidate",
        lambda config, db, cid: couch_doc,
    )
    monkeypatch.setattr(couchdb_config, "field_captures_database", lambda: "field_captures")

    result = candidate_staging.materialize_candidate_for_direct_staging(tmp_path, candidate_id)
    assert result is not None, "human-grounded candidate must materialize for staging"
    assert result.exists()
