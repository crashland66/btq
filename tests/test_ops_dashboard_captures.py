from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from field_capture import approved_job_drafts
from processing_core.action_candidates import STATUS_APPROVED, action_candidate_payload, write_action_candidate_review
from processing_core.approved_job_drafts import write_approved_job_draft
from processing_core.artifacts import write_json_object
from tests.test_ops_dashboard import request_text


def seed_capture(runtime_root: Path, capture_id: str, *, date: str = "2026-05-03", site_id: str = "7050", photo: bool = True, audio: bool = False) -> None:
    upload_dir = runtime_root / "uploads" / date / capture_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    if photo:
        (upload_dir / "photo.jpg").write_bytes(b"jpeg")
    if audio:
        (upload_dir / "voice.webm").write_bytes(b"audio")
    write_json_object(
        runtime_root / "field_capture" / "intake" / f"{capture_id}.json",
        {
            "job_type": "photo_capture",
            "metadata": {"capture_id": capture_id, "site_id": site_id, "person_name": "Alice Example"},
            "payload": {
                "area": "Restrooms",
                "captured_at": f"{date}T12:00:00-04:00",
                "photos": [{"stored_path": f"{date}/{capture_id}/photo.jpg", "filename": "photo.jpg"}] if photo else [],
                "audio": [{"stored_path": f"{date}/{capture_id}/voice.webm", "filename": "voice.webm"}] if audio else [],
            },
        },
    )


def seed_links(runtime_root: Path, capture_id: str) -> tuple[str, str]:
    sidecar = {
        "type": "field_capture_photo_vision",
        "status": "completed",
        "photo_asset_id": "fcp_linked",
        "capture_id": capture_id,
        "site_id": "7050",
        "area_guess": "restroom",
    }
    write_json_object(runtime_root / "field_capture" / "photo_vision" / "fcp_linked.json", sidecar)
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Linked candidate summary.",
        rationale="Linked candidate summary.",
        source_text="Linked candidate summary.",
        source_context="Linked candidate summary.",
        channel_metadata={"upload_id": capture_id, "site_id": "7050", "channel": "field_capture"},
        status=STATUS_APPROVED,
    )
    write_action_candidate_review(approved_job_drafts.default_candidate_dir(runtime_root), candidate)
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": "ajd_linked",
        "status": "approved_draft",
        "candidate_id": candidate["candidate_id"],
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"path": "Accounts/Test/Site.md", "destination": "site_note", "content": "Linked\n"},
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)
    return str(candidate["candidate_id"]), "ajd_linked"


def test_captures_filter_by_site_and_date_range(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_keep", date="2026-05-03", site_id="7050")
    seed_capture(runtime_root, "cap_drop", date="2026-05-08", site_id="9999")

    status, _content_type, body = request_text("GET", "/captures?site=7050&date_from=2026-05-01&date_to=2026-05-04", runtime_root)

    assert status == HTTPStatus.OK
    assert "cap_keep" in body
    assert "cap_drop" not in body


def test_captures_table_uses_data_table_class(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_table")

    status, _content_type, body = request_text("GET", "/captures", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="data-table"' in body
    assert "<table><tr><th>capture_id" not in body


def test_captures_filter_has_photo_and_has_audio(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_both", photo=True, audio=True)
    seed_capture(runtime_root, "cap_photo_only", photo=True, audio=False)

    status, _content_type, body = request_text("GET", "/captures?has_photo=1&has_audio=1", runtime_root)

    assert status == HTTPStatus.OK
    assert "cap_both" in body
    assert "cap_photo_only" not in body


def test_captures_filter_has_candidate_excludes_uncandidated(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_with_candidate")
    seed_capture(runtime_root, "cap_without_candidate")
    seed_links(runtime_root, "cap_with_candidate")

    status, _content_type, body = request_text("GET", "/captures?has_candidate=1", runtime_root)

    assert status == HTTPStatus.OK
    assert "cap_with_candidate" in body
    assert "cap_without_candidate" not in body


def test_capture_detail_lists_uploads_with_safe_media_urls(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_media", photo=True, audio=True)

    status, _content_type, body = request_text("GET", "/captures?capture_id=cap_media", runtime_root)

    assert status == HTTPStatus.OK
    assert "/media/2026-05-03/cap_media/photo.jpg" in body
    assert "/media/2026-05-03/cap_media/voice.webm" in body
    assert "../" not in body


def test_capture_detail_links_to_candidates_drafts_and_queue_state(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_linked")
    candidate_id, draft_id = seed_links(runtime_root, "cap_linked")

    status, _content_type, body = request_text("GET", "/captures?capture_id=cap_linked", runtime_root)

    assert status == HTTPStatus.OK
    assert f"/candidates?candidate_id={candidate_id}" in body
    assert f"/drafts?draft_id={draft_id}" in body
    assert "Not Staged" in body
    assert "fcp_linked" in body


def test_capture_detail_handles_capture_with_no_sidecars(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_no_sidecar")

    status, _content_type, body = request_text("GET", "/captures?capture_id=cap_no_sidecar", runtime_root)

    assert status == HTTPStatus.OK
    # Photo card renders inline as "Awaiting analysis." when no sidecar exists yet (363).
    assert "Awaiting analysis." in body


def test_capture_detail_handles_capture_with_no_drafts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_no_drafts")

    status, _content_type, body = request_text("GET", "/captures?capture_id=cap_no_drafts", runtime_root)

    assert status == HTTPStatus.OK
    assert "No drafts." in body
