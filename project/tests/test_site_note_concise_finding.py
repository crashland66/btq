"""Gating tests for prompt 379: concise, path-free field-capture site-note content.

`draft_note_content(candidate)` must:
  1. Return "" for empty/generic findings (no genuine action item).
  2. For a real non-generic finding, emit EXACTLY one line:
       ## <YYYY-MM-DD> - Field capture: <primary_summary>\n
     with the date taken from captured_at/created_at. No verbose
     "## Field Capture Reviews" block, no "### Field Capture Review -"
     header, no field-list bullets, and CRITICALLY no absolute
     filesystem paths (the /runtime/ and /btq_runtime/ leak regression).
  3. The two job builders (proposed_queue_job ~:210 and
     default_field_capture_append_job ~:510) must NOT emit an
     append_to_note job when draft_note_content is empty, and must emit
     the concise content when it is a real finding.

Independent verifier author: these tests were written against the
specification, not the implementation, and are mutation-checked against
the pre-379 verbose builder.
"""

from __future__ import annotations

import field_capture.approved_job_drafts as m
from field_capture.action_candidates import GENERIC_REVIEW_ACTION
from queue_spec import JOB_APPEND_TO_NOTE


# A site_id + note path that resolves via SITES under the test registry that
# conftest.py pins (BTQ_SITE_REGISTRY_PATH -> example registry). Used by the
# append builder, which looks the note path up from SITES.
REAL_SITE_ID = "7020"
REAL_NOTE_PATH = "Accounts/RHN/Locations/7020 - Lakeshore Community Health Center - RHN/about.md"

# Absolute paths that MUST NEVER leak into note content under any input.
LEAKY_SEMANTIC_PATH = "/runtime/gregstoltz/development/btq/btq_runtime/reviews/x/semantic.json"
LEAKY_TRANSCRIPT_PATH = "/runtime/reviews/x/transcript.json"

REAL_SUMMARY = "Replaced the broken paper towel dispenser bracket in the east restroom."


def _base_candidate(**overrides) -> dict:
    """A field_capture_follow_up candidate that travels the proposed_note_path
    builder branch of proposed_queue_job (~:210). Carries leaky absolute paths
    in provenance so every test exercises the path-leak surface."""
    candidate = {
        "type": "action_candidate_review",
        "candidate_type": "field_capture_follow_up",
        "candidate_id": "cand-379",
        "channel_metadata": {
            "proposed_note_path": REAL_NOTE_PATH,
            "site_id": REAL_SITE_ID,
            "captured_at": "2026-06-15T09:30:00Z",
            "upload_id": "cap-xyz",
            "area": "Restrooms",
            "phase": "post",
        },
        "summary": REAL_SUMMARY,
        "review_rationale": "",
        "source_context": "",
        "rationale": "",
        "created_at": "2026-06-15T09:30:00Z",
        "reviewer": "Greg",
        "provenance": {
            "semantic_artifact_path": LEAKY_SEMANTIC_PATH,
            "source_transcript_path": LEAKY_TRANSCRIPT_PATH,
            "audio_asset_id": "aud-1",
            "site_id": REAL_SITE_ID,
        },
    }
    candidate.update(overrides)
    return candidate


def _assert_no_path_leak(content: str) -> None:
    assert "/runtime/" not in content, f"absolute /runtime/ path leaked into note content: {content!r}"
    assert "/btq_runtime/" not in content, f"runtime path leaked into note content: {content!r}"


def _assert_no_verbose_block(content: str) -> None:
    assert "## Field Capture Reviews" not in content, content
    assert "### Field Capture Review -" not in content, content
    assert "Source semantic artifact" not in content, content
    assert "Source transcript artifact" not in content, content
    # No field-list bullets from the old verbose form.
    for bullet in ("- site_id:", "- area:", "- capture_id:", "- audio_asset_id:", "- reviewer:", "- field_capture_timestamp:"):
        assert bullet not in content, f"verbose bullet {bullet!r} leaked: {content!r}"


# --------------------------------------------------------------------------
# 1. generic / empty summary -> draft_note_content returns ""
# --------------------------------------------------------------------------

def test_generic_review_action_summary_returns_empty() -> None:
    cand = _base_candidate(summary=GENERIC_REVIEW_ACTION)
    assert m.draft_note_content(cand) == ""


def test_reviewed_context_complete_summary_returns_empty() -> None:
    cand = _base_candidate(summary="Reviewed context: Complete")
    assert m.draft_note_content(cand) == ""


def test_complete_summary_returns_empty() -> None:
    cand = _base_candidate(summary="Complete")
    assert m.draft_note_content(cand) == ""


def test_generic_candidate_label_summary_returns_empty() -> None:
    # "Review supply/order follow-up." is in GENERIC_CANDIDATE_LABELS.
    cand = _base_candidate(summary="Review supply/order follow-up.")
    assert m.draft_note_content(cand) == ""


def test_empty_summary_and_no_other_source_returns_empty() -> None:
    cand = _base_candidate(summary="", review_rationale="", source_context="", rationale="")
    assert m.draft_note_content(cand) == ""


def test_generic_inputs_never_leak_paths_even_when_empty() -> None:
    # Empty return obviously carries no path, but assert the regression invariant
    # directly: under a generic finding nothing leaks.
    cand = _base_candidate(summary=GENERIC_REVIEW_ACTION)
    _assert_no_path_leak(m.draft_note_content(cand))


# --------------------------------------------------------------------------
# 2. real non-generic finding -> exactly one concise line, correct date,
#    no verbose block, no path leak
# --------------------------------------------------------------------------

def test_real_finding_is_single_concise_line_with_correct_date() -> None:
    content = m.draft_note_content(_base_candidate())
    assert content == f"## 2026-06-15 - Field capture: {REAL_SUMMARY}\n"
    # Exactly one line of body content.
    assert content.count("\n") == 1
    assert len(content.rstrip("\n").splitlines()) == 1


def test_real_finding_no_verbose_block_no_path_leak() -> None:
    content = m.draft_note_content(_base_candidate())
    _assert_no_verbose_block(content)
    _assert_no_path_leak(content)


def test_date_from_created_at_when_captured_at_absent() -> None:
    cand = _base_candidate()
    cand["channel_metadata"].pop("captured_at")
    cand["created_at"] = "2026-05-27T14:00:00Z"
    content = m.draft_note_content(cand)
    assert content == f"## 2026-05-27 - Field capture: {REAL_SUMMARY}\n"


def test_review_rationale_drives_summary_concise_and_pathfree() -> None:
    # review_rationale takes precedence over summary in note_primary_summary;
    # the "operational note:" prefix is stripped/cased by the operational helper.
    cand = _base_candidate(
        summary="",
        review_rationale="Operational note: drain backed up onto the floor in restroom 2.",
    )
    content = m.draft_note_content(cand)
    assert content == "## 2026-06-15 - Field capture: Drain backed up onto the floor in restroom 2.\n"
    _assert_no_verbose_block(content)
    _assert_no_path_leak(content)


def test_no_path_leak_across_many_inputs() -> None:
    # Sweep a matrix of summary sources to prove /runtime/ and /btq_runtime/ can
    # never reach the note content via any field, generic or real.
    summaries = [
        REAL_SUMMARY,
        GENERIC_REVIEW_ACTION,
        "Reviewed context: Complete",
        "Complete",
        "",
        "Sink leak requires maintenance follow-up.",
    ]
    rationales = ["", "Useful site documentation note: photographed the new lockbox code placard."]
    for s in summaries:
        for rr in rationales:
            cand = _base_candidate(summary=s, review_rationale=rr)
            _assert_no_path_leak(m.draft_note_content(cand))


# --------------------------------------------------------------------------
# 3. job-builder suppression
# --------------------------------------------------------------------------

def test_proposed_queue_job_real_finding_emits_concise_append() -> None:
    job_type, payload, error = m.proposed_queue_job(_base_candidate())
    assert job_type == JOB_APPEND_TO_NOTE
    assert error == ""
    assert isinstance(payload, dict)
    assert payload["destination"] == "site_note"
    assert payload["content"] == f"## 2026-06-15 - Field capture: {REAL_SUMMARY}\n"
    _assert_no_verbose_block(str(payload["content"]))
    _assert_no_path_leak(str(payload["content"]))


def test_proposed_queue_job_generic_emits_no_append() -> None:
    job_type, payload, _error = m.proposed_queue_job(_base_candidate(summary=GENERIC_REVIEW_ACTION))
    assert job_type != JOB_APPEND_TO_NOTE
    # No append_to_note payload content is produced.
    if isinstance(payload, dict):
        assert payload.get("destination") != "site_note"


def test_proposed_queue_jobs_generic_yields_no_append_job() -> None:
    jobs = m.proposed_queue_jobs(_base_candidate(summary=GENERIC_REVIEW_ACTION))
    assert not any(jt == JOB_APPEND_TO_NOTE for jt, _payload, _err in jobs)


def test_append_builder_real_finding_emits_concise_content() -> None:
    # Drive the default_field_capture_append_job builder (~:510) directly:
    # no proposed_note_path, rely on site_id -> SITES note path resolution.
    cand = _base_candidate()
    cand["channel_metadata"] = {"site_id": REAL_SITE_ID, "captured_at": "2026-06-15T09:30:00Z"}
    job_type, payload, error = m.default_field_capture_append_job(cand)
    assert job_type == JOB_APPEND_TO_NOTE
    assert error == ""
    assert payload["content"] == f"## 2026-06-15 - Field capture: {REAL_SUMMARY}\n"
    _assert_no_verbose_block(str(payload["content"]))
    _assert_no_path_leak(str(payload["content"]))


def test_append_builder_generic_emits_no_job() -> None:
    cand = _base_candidate(summary=GENERIC_REVIEW_ACTION)
    cand["channel_metadata"] = {"site_id": REAL_SITE_ID, "captured_at": "2026-06-15T09:30:00Z"}
    job_type, payload, error = m.default_field_capture_append_job(cand)
    assert job_type == ""
    assert payload == {}
    assert "no non-generic site-note finding" in error
