"""A note with no actionable content must not become a review candidate.

The rule engine emits a generic "Review the field audio note and decide whether
follow-up is needed." action for captures it can't classify (status notes like
"complete" / "area done"). Those are not action items and must never reach the
pending-review queue. Regression for the 1024-boilerplate-candidate flood
(2026-06-10). Covers both collector emission paths: the structured
``extracted_actions`` path (what the rule engine actually produces) and the
legacy ``action_candidates`` summary path.
"""

from __future__ import annotations

import json
from pathlib import Path

import field_capture.action_candidates as fc
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import GENERIC_REVIEW_ACTION, RuleCaptureEngine


def _artifact(text: str, upload_id: str, area: str = "") -> dict:
    artifact = run_text_semantic_pipeline(
        text,
        site_id="SANDBOX",
        upload_id=upload_id,
        area=area,
        engine=RuleCaptureEngine(),
    )
    # Mimic the real pipeline's write_json_object -> read round-trip (the collector
    # reads artifacts from disk; the JSON pass normalizes tuples -> lists so the
    # extracted-actions validation matches production).
    return json.loads(json.dumps(artifact))


def test_completion_note_produces_no_candidate() -> None:
    # A pure status note classifies to the generic review fallback only.
    artifact = _artifact("Area done. Complete.", "cap-test-complete")
    assert fc.payloads_from_semantic(Path("ignored.json"), artifact) == []


def test_generic_only_summary_path_filtered() -> None:
    payload = {"action_candidates": [GENERIC_REVIEW_ACTION]}
    assert fc.quality_filtered_summaries(payload) == []


def test_generic_dropped_but_real_action_kept_summary_path() -> None:
    payload = {"action_candidates": [GENERIC_REVIEW_ACTION, "Inspect the affected floor area for moisture."]}
    summaries = fc.quality_filtered_summaries(payload)
    assert summaries == ["Inspect the affected floor area for moisture."]


def test_real_supply_note_still_produces_a_candidate() -> None:
    # Positive control: a genuine supply need must still surface, and never as the
    # generic boilerplate.
    artifact = _artifact(
        "Need to order a few things. A new vacuum and lint brushes are the required supplies.",
        "cap-test-supply",
        area="Supply Levels",
    )
    payloads = fc.payloads_from_semantic(Path("ignored.json"), artifact)
    assert payloads, "a real supply note should still yield a candidate"
    assert not any(fc.is_generic_summary(str(p.get("summary") or "")) for p in payloads)
