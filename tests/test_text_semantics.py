from field_capture.action_candidates import ACCEPTED_SEMANTIC_TYPES
from field_capture.audio_semantics import GENERIC_REVIEW_ACTION, SUPPLY_REVIEW_ACTION
from field_capture.text_semantics import ARTIFACT_TYPE, run_text_semantic_pipeline


def test_run_text_semantic_pipeline_returns_artifact():
    artifact = run_text_semantic_pipeline(
        "soap and paper towels are low",
        site_id="999",
        upload_id="up_abc",
        area="restroom",
    )
    assert artifact["type"] == ARTIFACT_TYPE
    assert artifact["issue_type"] == "supplies"
    assert artifact["site_id"] == "999"


def test_run_text_semantic_pipeline_includes_action_candidates_for_classified_text():
    artifact = run_text_semantic_pipeline(
        "the towels in the breakroom need restocked",
        site_id="999",
        upload_id="up_abc",
    )

    assert isinstance(artifact["action_candidates"], list)
    assert artifact["action_candidates"]
    assert SUPPLY_REVIEW_ACTION in artifact["action_candidates"]


def test_run_text_semantic_pipeline_generic_text_includes_generic_review_action():
    artifact = run_text_semantic_pipeline(
        "I will be speaking to David Pearson today about the Hawthorne center incident.",
        site_id="999",
        upload_id="up_abc",
    )

    assert artifact["action_candidates"] == [GENERIC_REVIEW_ACTION]


def test_run_text_semantic_pipeline_blank_text_action_candidates_empty_list():
    artifact = run_text_semantic_pipeline(
        "   ",
        site_id="999",
        upload_id="up_abc",
    )

    assert artifact["action_candidates"] == []


def test_accepted_semantic_types_includes_text_type():
    assert ARTIFACT_TYPE in ACCEPTED_SEMANTIC_TYPES
    assert "field_audio_semantic_summary" in ACCEPTED_SEMANTIC_TYPES
