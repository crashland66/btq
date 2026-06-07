from __future__ import annotations

from dataclasses import asdict

from processing_core.capture_semantics import CaptureSemanticInput, RuleCaptureEngine, build_semantic_engine
from processing_core.status import STATUS_COMPLETE

ARTIFACT_TYPE = "field_text_semantic_summary"

__all__ = ["ARTIFACT_TYPE", "run_text_semantic_pipeline"]


def run_text_semantic_pipeline(
    text: str,
    *,
    site_id: str,
    upload_id: str,
    area: str = "",
    person_id: str = "",
    site_label: str = "",
    selected_employees: list[dict[str, object]] | None = None,
    captured_at: str = "",
    engine: object | None = None,
) -> dict:
    """Classify typed note text and return a semantic artifact dict.

    Produces the same schema as ``field_audio_semantic_summary`` artifacts but
    with ``type`` set to ``ARTIFACT_TYPE`` (``"field_text_semantic_summary"``).
    """
    if not text.strip():
        return {
            "type": ARTIFACT_TYPE,
            "status": STATUS_COMPLETE,
            "site_id": site_id,
            "upload_id": upload_id,
            "capture_id": upload_id,
            "source_kind": "ops_dashboard_text",
            "area": area,
            "person_id": person_id,
            "source_text": text,
            "issue_detected": False,
            "visit_proposed": False,
            "action_candidates": [],
            "extracted_actions": [],
        }

    capture_input = CaptureSemanticInput(
        capture_id=upload_id,
        source_kind="ops_dashboard_text",
        source_text=text,
        site_id=site_id,
        site_label=site_label,
        selected_employees=selected_employees or [],
        submitter_person_id=person_id,
        captured_at=captured_at,
        area=area,
    )
    semantic_engine = engine or build_semantic_engine()
    result = semantic_engine(capture_input)
    if not hasattr(result, "extracted_actions"):
        result = RuleCaptureEngine()(capture_input)

    return {
        "type": ARTIFACT_TYPE,
        "status": STATUS_COMPLETE,
        "site_id": site_id,
        "upload_id": upload_id,
        "capture_id": upload_id,
        "source_kind": "ops_dashboard_text",
        "area": area,
        "person_id": person_id,
        "source_text": text,
        "issue_type": result.issue_type,
        "issue_detected": result.issue_detected,
        "urgency": result.urgency,
        "visit_proposed": result.visit_proposed,
        "visit_type": result.visit_type,
        "cleaned_internal_note": result.cleaned_internal_note,
        "client_safe_note": result.client_safe_note,
        "operational_summary": result.operational_summary,
        "suggested_tags": list(result.suggested_tags),
        "action_candidates": list(result.action_candidates),
        "extracted_actions": [asdict(action) for action in (result.extracted_actions or [])],
        "action_summary": getattr(result, "action_summary", ""),
    }
