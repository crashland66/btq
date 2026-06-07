from __future__ import annotations

import pytest

from processing_core.action_candidates import (
    action_candidate_payload,
    patch_candidate_resolution,
    write_action_candidate_review,
)
from processing_core.artifacts import read_json_object


def test_patch_candidate_resolution_open(tmp_path):
    candidate_dir = tmp_path / "reviews" / "action_candidates"
    candidate = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    patch_candidate_resolution(candidate_dir, str(candidate["candidate_id"]), resolution_status="open", by="Jordan")

    updated = read_json_object(candidate_path)
    assert updated is not None
    resolution = updated["resolution"]
    assert resolution["status"] == "open"
    assert resolution["opened_by"] == "Jordan"


def test_patch_candidate_resolution_resolved_updates_status(tmp_path):
    candidate_dir = tmp_path / "reviews" / "action_candidates"
    candidate = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    candidate_path = write_action_candidate_review(candidate_dir, candidate)

    patch_candidate_resolution(candidate_dir, str(candidate["candidate_id"]), resolution_status="open", by="Jordan")
    patch_candidate_resolution(candidate_dir, str(candidate["candidate_id"]), resolution_status="resolved", by="Jordan", note="Fixed")

    updated = read_json_object(candidate_path)
    assert updated is not None
    resolution = updated["resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["resolved_note"] == "Fixed"


def test_patch_candidate_resolution_invalid_status_raises(tmp_path):
    candidate_dir = tmp_path / "reviews" / "action_candidates"
    candidate = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    write_action_candidate_review(candidate_dir, candidate)

    with pytest.raises(ValueError):
        patch_candidate_resolution(candidate_dir, str(candidate["candidate_id"]), resolution_status="unknown")
