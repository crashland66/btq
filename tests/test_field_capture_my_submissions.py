"""Tests for field_capture.my_submissions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_capture.my_submissions import (
    collect_my_submissions,
    derive_track_b_stage,
    is_track_b,
    quality_for_capture,
    rolling_quality_summary,
)


# ─── is_track_b ─────────────────────────────────────────────────────────────


def test_is_track_b_photo_only_is_track_a():
    doc = {"photos": [{"filename": "a.jpg"}], "audio": None, "note": ""}
    assert is_track_b(doc) is False


def test_is_track_b_audio_is_track_b():
    doc = {"photos": [{"filename": "a.jpg"}], "audio": [{"filename": "voice.webm"}], "note": ""}
    assert is_track_b(doc) is True


def test_is_track_b_text_note_is_track_b():
    doc = {"photos": [{"filename": "a.jpg"}], "audio": None, "note": "Locker room done."}
    assert is_track_b(doc) is True


# ─── derive_track_b_stage ───────────────────────────────────────────────────


def _write_candidate(candidates_dir: Path, name: str, data: dict) -> Path:
    path = candidates_dir / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_derive_stage_no_candidate_is_processing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "processing"
    assert label == ""


def test_derive_stage_open_candidate_is_processing(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "open",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "processing"
    assert label == ""


def test_derive_stage_client_notified(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "client_notified",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "acted_on"
    assert label == "Client notified"


def test_derive_stage_resolved_supply(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "resolved",
        "job_type": "log_supply_need",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "acted_on"
    assert label == "Supplies ordered"


def test_derive_stage_resolved_equipment(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "resolved",
        "job_type": "log_equipment_request",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "acted_on"
    assert label == "Equipment handled"


def test_derive_stage_resolved_other(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "resolved",
        "job_type": "site_issue",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "acted_on"
    assert label == "Logged"


def test_derive_stage_dismissed(tmp_path):
    candidates_dir = tmp_path / "candidates"
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "dismissed",
    })
    stage, label = derive_track_b_stage("cap-001", candidates_dir)
    assert stage == "reviewed"
    assert label == "No action needed"


# ─── quality_for_capture ────────────────────────────────────────────────────


def test_quality_for_capture_no_sidecar(tmp_path):
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()
    result = quality_for_capture("cap-001", pv_dir)
    assert result == []


def test_quality_for_capture_returns_flags(tmp_path):
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()
    # Sidecars are named by photo_asset_id (a hash), not capture_id.
    # Each file contains capture_id inside and a quality block.
    (pv_dir / "fcp-abc123.json").write_text(json.dumps({
        "capture_id": "cap-002",
        "description": "Tiled restroom with three urinals.",
        "possible_issues": [],
        "quality": {"severity": "degraded", "flags": ["too_dark"], "analyzable": True},
    }), encoding="utf-8")
    (pv_dir / "fcp-def456.json").write_text(json.dumps({
        "capture_id": "cap-002",
        "description": "Floor area near entrance.",
        "possible_issues": ["paper towel on floor"],
        "quality": {"severity": "ok", "flags": [], "analyzable": True},
    }), encoding="utf-8")
    result = quality_for_capture("cap-002", pv_dir)
    assert len(result) == 2
    severities = {r["severity"] for r in result}
    assert severities == {"degraded", "ok"}
    degraded = next(r for r in result if r["severity"] == "degraded")
    assert degraded["flags"] == ["too_dark"]
    ok_photo = next(r for r in result if r["severity"] == "ok")
    assert ok_photo["description"] == "Floor area near entrance."
    assert ok_photo["possible_issues"] == ["paper towel on floor"]


# ─── rolling_quality_summary ────────────────────────────────────────────────


_sidecar_counter = 0


def _make_sidecar(pv_dir: Path, capture_id: str, photos: list[dict]) -> None:
    """Write one sidecar file per photo using the real per-photo format.

    Each file is named by a synthetic asset_id (simulating the real
    hash-based naming). The quality block is taken from the photo dict.
    """
    global _sidecar_counter
    for photo in photos:
        _sidecar_counter += 1
        asset_id = f"fcp-test{_sidecar_counter:04d}"
        payload = {
            "capture_id": capture_id,
            "description": photo.get("description", ""),
            "possible_issues": photo.get("possible_issues", []),
            "quality": {
                "severity": photo.get("severity", "ok"),
                "flags": photo.get("flags", []),
                "analyzable": True,
            },
        }
        (pv_dir / f"{asset_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_doc(capture_id: str, captured_at: str = "2026-05-01T10:00:00Z") -> dict:
    return {
        "capture_id": capture_id,
        "_id": capture_id,
        "person_id": "p1",
        "captured_at": captured_at,
        "photos": [{"filename": "a.jpg"}],
    }


def test_my_submissions_renders_retarget_button_when_stage_not_acted_on(tmp_path):
    pv_dir = tmp_path / "pv"
    candidates_dir = tmp_path / "candidates"
    pv_dir.mkdir()
    candidates_dir.mkdir()
    rows = collect_my_submissions(
        "p1",
        [_make_doc("cap-001")],
        upload_dir=tmp_path / "uploads",
        photo_vision_dir=pv_dir,
        candidates_dir=candidates_dir,
        can_retarget=True,
    )

    assert rows[0]["retargetable"] is True


def test_my_submissions_hides_retarget_button_when_stage_acted_on(tmp_path):
    pv_dir = tmp_path / "pv"
    candidates_dir = tmp_path / "candidates"
    pv_dir.mkdir()
    candidates_dir.mkdir()
    _write_candidate(candidates_dir, "c1.json", {
        "capture_id": "cap-001",
        "resolution_status": "resolved",
        "job_type": "log_site_issue",
    })

    rows = collect_my_submissions(
        "p1",
        [_make_doc("cap-001", "2026-05-01T10:00:00Z") | {"note": "Needs follow-up"}],
        upload_dir=tmp_path / "uploads",
        photo_vision_dir=pv_dir,
        candidates_dir=candidates_dir,
        can_retarget=True,
    )

    assert rows[0]["stage"] == "acted_on"
    assert rows[0]["retargetable"] is False


def test_my_submissions_hides_retarget_button_for_cleaner_role(tmp_path):
    pv_dir = tmp_path / "pv"
    candidates_dir = tmp_path / "candidates"
    pv_dir.mkdir()
    candidates_dir.mkdir()

    rows = collect_my_submissions(
        "p1",
        [_make_doc("cap-001")],
        upload_dir=tmp_path / "uploads",
        photo_vision_dir=pv_dir,
        candidates_dir=candidates_dir,
        can_retarget=False,
    )

    assert rows[0]["retargetable"] is False


def test_rolling_quality_summary_all_clear(tmp_path):
    pv_dir = tmp_path / "pv"
    pv_dir.mkdir()
    docs = []
    for i in range(5):
        cid = f"cap-{i:03d}"
        docs.append(_make_doc(cid, f"2026-05-{i+1:02d}T10:00:00Z"))
        _make_sidecar(pv_dir, cid, [{"severity": "ok", "flags": []}])
    summary = rolling_quality_summary("p1", docs, pv_dir)
    assert summary["total_processed"] == 5
    assert summary["clear"] == 5
    assert summary["flag_counts"] == {}


def test_rolling_quality_summary_counts_flags(tmp_path):
    pv_dir = tmp_path / "pv"
    pv_dir.mkdir()
    docs = []
    # 3 ok, 2 with too_dark
    for i in range(5):
        cid = f"cap-{i:03d}"
        docs.append(_make_doc(cid, f"2026-05-{i+1:02d}T10:00:00Z"))
        if i < 3:
            _make_sidecar(pv_dir, cid, [{"severity": "ok", "flags": []}])
        else:
            _make_sidecar(pv_dir, cid, [{"severity": "degraded", "flags": ["too_dark"]}])
    summary = rolling_quality_summary("p1", docs, pv_dir)
    assert summary["total_processed"] == 5
    assert summary["clear"] == 3
    assert summary["flag_counts"] == {"too_dark": 2}


def test_rolling_quality_summary_window_limits(tmp_path):
    pv_dir = tmp_path / "pv"
    pv_dir.mkdir()
    docs = []
    # Create 40 captures, all with 1 photo each
    for i in range(40):
        cid = f"cap-{i:03d}"
        docs.append(_make_doc(cid, f"2026-04-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00Z"))
        _make_sidecar(pv_dir, cid, [{"severity": "ok", "flags": []}])
    summary = rolling_quality_summary("p1", docs, pv_dir, window=30)
    # Only the 30 most recent photos should be counted
    assert summary["total_processed"] == 30


# ─── collect_my_submissions ─────────────────────────────────────────────────


def _make_full_doc(
    capture_id: str,
    person_id: str = "p1",
    captured_at: str = "2026-05-20T10:00:00Z",
    has_note: bool = False,
    has_audio: bool = False,
) -> dict:
    doc: dict = {
        "capture_id": capture_id,
        "_id": capture_id,
        "person_id": person_id,
        "site_id": "site-1",
        "site": "Site One",
        "captured_at": captured_at,
        "photos": [{"filename": "a.jpg", "upload_id": f"2026-05-20/{capture_id}/a.jpg"}],
        "note": "Issue here" if has_note else "",
    }
    if has_audio:
        doc["audio"] = [{"filename": "voice.webm"}]
    return doc


def test_collect_my_submissions_filters_by_person_id(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [
        _make_full_doc("cap-p1", person_id="p1"),
        _make_full_doc("cap-p2", person_id="p2"),
    ]
    result = collect_my_submissions(
        "p1", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert len(result) == 1
    assert result[0]["capture_id"] == "cap-p1"


def test_collect_my_submissions_track_a_processing(tmp_path):
    """Track A with no vision sidecars → stage=processing."""
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [_make_full_doc("cap-a1", has_note=False, has_audio=False)]
    result = collect_my_submissions(
        "p1", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert len(result) == 1
    assert result[0]["track"] == "A"
    assert result[0]["stage"] == "processing"
    assert result[0]["outcome_label"] == ""


def test_prospect_capture_photo_urls_are_doc_resolved(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [
        {
            "capture_id": "cap-photo-prospect-1",
            "_id": "cap-photo-prospect-1",
            "type": "field_capture",
            "site": "KMF Birch Ave",
            "site_id": "",
            "target_type": "prospect",
            "target_id": "kmf-birch-1",
            "captured_at": "2026-05-28T12:48:00-04:00",
            "person_id": "jordan-avery",
            "photos": [{"upload_id": "2026-05-28/cap-photo-prospect-1/sink.jpg"}],
        },
    ]
    result = collect_my_submissions(
        "jordan-avery", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert len(result) == 1
    assert result[0]["photo_urls"] == ["/media/2026-05-28/cap-photo-prospect-1/sink.jpg"]
    assert result[0]["target_type"] == "prospect"
    assert result[0]["target_id"] == "kmf-birch-1"


def test_location_capture_photo_urls_have_no_query_string(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [
        {
            "capture_id": "cap-photo-loc-1",
            "_id": "cap-photo-loc-1",
            "type": "field_capture",
            "site": "Continental Metalworks",
            "site_id": "1234",
            "target_type": "location",
            "target_id": "1234",
            "captured_at": "2026-05-28T12:36:00-04:00",
            "person_id": "jordan-avery",
            "photos": [{"upload_id": "2026-05-28/cap-photo-loc-1/floor.jpg"}],
        },
    ]
    result = collect_my_submissions(
        "jordan-avery", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert result[0]["photo_urls"] == ["/media/2026-05-28/cap-photo-loc-1/floor.jpg"]
    assert result[0]["target_type"] == "location"
    assert result[0]["target_id"] == "1234"


def test_legacy_capture_without_target_fields_defaults_to_location(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    # Pre-prompt-133 docs have site_id but no target_type/target_id.
    docs = [
        {
            "capture_id": "cap-photo-legacy",
            "_id": "cap-photo-legacy",
            "type": "field_capture",
            "site": "Summit Wire",
            "site_id": "5678",
            "captured_at": "2026-04-01T10:00:00-04:00",
            "person_id": "jordan-avery",
            "photos": [{"upload_id": "2026-04-01/cap-photo-legacy/x.jpg"}],
        },
    ]
    result = collect_my_submissions(
        "jordan-avery", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert result[0]["photo_urls"] == ["/media/2026-04-01/cap-photo-legacy/x.jpg"]
    assert result[0]["target_type"] == "location"
    assert result[0]["target_id"] == "5678"


def test_collect_my_submissions_track_a_processed(tmp_path):
    """Track A with vision sidecars → stage=processed."""
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [_make_full_doc("cap-a2", has_note=False, has_audio=False)]
    _make_sidecar(pv_dir, "cap-a2", [{"severity": "ok", "flags": []}])
    result = collect_my_submissions(
        "p1", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert len(result) == 1
    assert result[0]["track"] == "A"
    assert result[0]["stage"] == "processed"


def test_collect_my_submissions_uses_couchdb_photo_vision_docs(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    docs = [_make_full_doc("cap-couch", has_note=False, has_audio=False)]
    result = collect_my_submissions(
        "p1",
        docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
        photo_vision_couchdb_docs={
            "cap-couch": [
                {
                    "capture_id": "cap-couch",
                    "photo_id": "photo-001",
                    "description": "Janitor closet shelf with unlabeled spray bottles.",
                    "possible_issues": ["unlabeled bottle", "items stored on floor"],
                    "quality": {"severity": "degraded", "flags": ["too_dark"]},
                }
            ]
        },
    )

    assert result[0]["stage"] == "processed"
    assert result[0]["per_photo_quality"] == [
        {
            "severity": "degraded",
            "flags": ["too_dark"],
            "description": "Janitor closet shelf with unlabeled spray bottles.",
            "possible_issues": ["unlabeled bottle", "items stored on floor"],
        }
    ]


def test_collect_my_submissions_prefers_couchdb_photo_vision_docs_over_filesystem(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    _make_sidecar(pv_dir, "cap-prefers-couch", [{"description": "Filesystem sidecar description."}])
    result = collect_my_submissions(
        "p1",
        [_make_full_doc("cap-prefers-couch", has_note=False, has_audio=False)],
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
        photo_vision_couchdb_docs={
            "cap-prefers-couch": [
                {
                    "capture_id": "cap-prefers-couch",
                    "photo_id": "photo-001",
                    "description": "CouchDB description wins.",
                    "possible_issues": [],
                    "quality": {"severity": "ok", "flags": []},
                }
            ]
        },
    )

    assert result[0]["per_photo_quality"][0]["description"] == "CouchDB description wins."


def test_collect_my_submissions_filesystem_photo_vision_fallback_still_works(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    _make_sidecar(pv_dir, "cap-fs", [{
        "severity": "ok",
        "flags": [],
        "description": "Floor area near entrance.",
        "possible_issues": ["paper towel on floor"],
    }])
    result = collect_my_submissions(
        "p1",
        [_make_full_doc("cap-fs", has_note=False, has_audio=False)],
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
        photo_vision_couchdb_docs=None,
    )

    assert result[0]["stage"] == "processed"
    assert result[0]["per_photo_quality"][0]["description"] == "Floor area near entrance."
    assert result[0]["per_photo_quality"][0]["possible_issues"] == ["paper towel on floor"]


def test_collect_my_submissions_track_b_acted_on(tmp_path):
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()

    # Create a note-bearing capture + resolved candidate
    docs = [_make_full_doc("cap-b1", has_note=True)]
    _write_candidate(cand_dir, "c1.json", {
        "capture_id": "cap-b1",
        "resolution_status": "client_notified",
    })

    result = collect_my_submissions(
        "p1", docs,
        upload_dir=upload_dir,
        photo_vision_dir=pv_dir,
        candidates_dir=cand_dir,
    )
    assert len(result) == 1
    assert result[0]["track"] == "B"
    assert result[0]["stage"] == "acted_on"
    assert result[0]["outcome_label"] == "Client notified"


def test_collect_my_submissions_track_a_complete_via_processing_state(tmp_path):
    """Edge deployments have no filesystem vision, but the replicated capture doc is
    complete -> stage=processed (not stuck 'processing'/'analyzing')."""
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"  # intentionally empty (no vision sidecars)
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()
    doc = _make_full_doc("cap-a3", has_note=False, has_audio=False)
    doc["processing_state"] = "complete"
    result = collect_my_submissions(
        "p1", [doc], upload_dir=upload_dir, photo_vision_dir=pv_dir, candidates_dir=cand_dir
    )
    assert len(result) == 1
    assert result[0]["track"] == "A"
    assert result[0]["stage"] == "processed"


def test_collect_my_submissions_track_a_incomplete_is_processing(tmp_path):
    """Without a complete processing_state and no vision -> still processing."""
    upload_dir = tmp_path / "uploads"
    pv_dir = tmp_path / "pv"
    cand_dir = tmp_path / "candidates"
    for d in (upload_dir, pv_dir, cand_dir):
        d.mkdir()
    doc = _make_full_doc("cap-a4", has_note=False, has_audio=False)
    doc["processing_state"] = "pending"
    result = collect_my_submissions(
        "p1", [doc], upload_dir=upload_dir, photo_vision_dir=pv_dir, candidates_dir=cand_dir
    )
    assert len(result) == 1
    assert result[0]["stage"] == "processing"
