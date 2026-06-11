from __future__ import annotations

import io
import json
import os
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import ops_dashboard.app as ops_app
from btq import parse_args
from event_pipeline import couchdb_config
from field_capture import action_candidates as field_action_candidates
from field_capture import approved_job_drafts
from field_capture import photo_vision as field_photo_vision
from ops_dashboard.app import build_status, route_response, startup_lines
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos
from ops_dashboard.sections import candidates as candidates_section
from ops_dashboard.sections.candidates import _handle_review_post, candidate_detail, filter_candidates, render_candidate_card, render_candidate_context, render_candidate_groups
from processing_core.action_candidates import action_candidate_payload, write_action_candidate_review
from processing_core.artifacts import write_json_object
from queue_processor import health as queue_health
from queue_processor import main as queue_processor_main


def request_text(method: str, path: str, runtime_root: Path, body: str = "") -> tuple[HTTPStatus, str, str]:
    status, content_type, body = route_response(method, path, runtime_root, body.encode("utf-8"))
    return status, content_type, body.decode("utf-8")


class RecordingRFile:
    def __init__(self, body: bytes = b"", *, fail_on_read: bool = False) -> None:
        self.body = body
        self.fail_on_read = fail_on_read
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.fail_on_read:
            raise AssertionError("rfile.read should not be called")
        return self.body[:size] if size >= 0 else self.body


def do_post_response(
    runtime_root: Path,
    *,
    path: str,
    content_length: object,
    body: bytes = b"",
    content_type: str = "",
    fail_on_read: bool = False,
) -> tuple[HTTPStatus, dict[str, str], bytes, RecordingRFile]:
    handler = object.__new__(ops_app.OpsDashboardHandler)
    headers = {}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if content_type:
        headers["Content-Type"] = content_type
    rfile = RecordingRFile(body, fail_on_read=fail_on_read)
    response_headers: dict[str, str] = {}
    response_status: list[HTTPStatus] = []
    handler.path = path
    handler.headers = headers
    handler.rfile = rfile
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(runtime_root=runtime_root)
    handler.send_response = lambda status: response_status.append(status)
    handler.send_header = lambda key, value: response_headers.__setitem__(key, value)
    handler.end_headers = lambda: None

    handler.do_POST()

    return response_status[0], response_headers, handler.wfile.getvalue(), rfile


def test_do_post_rejects_oversize_content_length_with_413(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_MAX_REQUEST_BYTES", 10)

    status, headers, body, rfile = do_post_response(
        tmp_path / "runtime",
        path="/field-capture/review/approve",
        content_length=11,
        fail_on_read=True,
    )

    assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert b"request_too_large" in body
    assert rfile.read_calls == []


def test_do_post_batch_route_allows_within_batch_cap(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_route_response_with_headers(method: str, path: str, runtime_root: Path, body: bytes = b"", content_type: str = ""):
        captured["method"] = method
        captured["path"] = path
        captured["runtime_root"] = runtime_root
        captured["body"] = body
        captured["content_type"] = content_type
        return HTTPStatus.OK, "application/json; charset=utf-8", b'{"ok": true}', {}

    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_MAX_REQUEST_BYTES", 5)
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES", 20)
    monkeypatch.setattr(ops_app, "route_response_with_headers", fake_route_response_with_headers)
    runtime_root = tmp_path / "runtime"

    status, _headers, body, rfile = do_post_response(
        runtime_root,
        path="/batch-images/upload",
        content_length=10,
        body=b"0123456789",
        content_type="multipart/form-data; boundary=test",
    )

    assert status == HTTPStatus.OK
    assert body == b'{"ok": true}'
    assert rfile.read_calls == [10]
    assert captured == {
        "method": "POST",
        "path": "/batch-images/upload",
        "runtime_root": runtime_root,
        "body": b"0123456789",
        "content_type": "multipart/form-data; boundary=test",
    }


def test_do_post_batch_route_rejects_above_batch_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_MAX_REQUEST_BYTES", 5)
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES", 20)

    status, _headers, body, rfile = do_post_response(
        tmp_path / "runtime",
        path="/batch-images/upload",
        content_length=21,
        fail_on_read=True,
    )

    assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert b"request_too_large" in body
    assert rfile.read_calls == []


def test_do_post_non_integer_content_length_is_400(tmp_path: Path) -> None:
    status, _headers, body, rfile = do_post_response(
        tmp_path / "runtime",
        path="/field-capture/review/approve",
        content_length="abc",
        fail_on_read=True,
    )

    assert status == HTTPStatus.BAD_REQUEST
    assert b"invalid_content_length" in body
    assert rfile.read_calls == []


def test_do_post_within_default_cap_dispatches_normally(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_MAX_REQUEST_BYTES", 20)

    status, _headers, body, rfile = do_post_response(
        tmp_path / "runtime",
        path="/not-a-post-route",
        content_length=4,
        body=b"test",
    )

    assert status == HTTPStatus.METHOD_NOT_ALLOWED
    assert b"read_only" in body
    assert rfile.read_calls == [4]


def test_max_request_bytes_for_path_selects_batch_cap(monkeypatch) -> None:
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_MAX_REQUEST_BYTES", 11)
    monkeypatch.setattr(ops_app, "OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES", 22)

    assert ops_app.max_request_bytes_for_path("/batch-images/upload") == 22
    assert ops_app.max_request_bytes_for_path("/batch-images/upload?site_id=7050") == 22
    assert ops_app.max_request_bytes_for_path("/field-capture/review/approve") == 11


def section_ctx(runtime_root: Path, vault_root: Path | None = None) -> SectionContext:
    vault = vault_root or runtime_root / "vault"
    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=vault, vault_root=vault))


# 337a: the review surfaces read job_draft docs (review_status pending_approval)
# and approve/reject via the 335 write path. These helpers build job_draft
# fixtures into the in-memory ``couchdb_job_draft_review`` double and produce the
# draft-shaped review payload the unit-level card renderer consumes.
def draft_review_payload(draft_id: str, **overrides) -> dict[str, object]:
    payload = {
        "type": "job_draft_review",
        "draft_id": draft_id,
        "review_status": "pending_approval",
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/7050.md",
            "content": "Staffing risk: Bruce no-showed.",
            "destination": "site_note",
        },
        "message": "Review test note.",
        "site_id": "7050",
        "submitter_name": "Greg",
        "source_capture_id": f"cap-{draft_id}",
        "confidence": "high",
        "created_at": "2026-06-11T00:00:00Z",
        "_rev": "1-fake",
    }
    payload.update(overrides)
    return payload


def seed_review_draft(couchdb_job_draft_review, draft_id: str, **overrides) -> dict:
    draft = {
        "draft_id": draft_id,
        "job_type": overrides.pop("job_type", "append_to_note"),
        "payload": overrides.pop("payload", {
            "path": "Accounts/7050.md",
            "content": "Staffing risk: Bruce no-showed.",
            "destination": "site_note",
        }),
        "message": overrides.pop("message", "Review test note."),
        "site_id": overrides.pop("site_id", "7050"),
        "submitter_name": overrides.pop("submitter_name", "Greg"),
        "source_capture_id": overrides.pop("source_capture_id", f"cap-{draft_id}"),
        "confidence": overrides.pop("confidence", "high"),
        "created_at": overrides.pop("created_at", "2026-06-11T00:00:00Z"),
    }
    draft.update(overrides)
    couchdb_job_draft_review.seed_draft(draft)
    return couchdb_job_draft_review.doc(draft_id)


def write_field_capture_fixture(runtime_root: Path, *, include_candidate: bool = True, submitter: bool = False) -> None:
    semantic_dir = runtime_root / "field_capture" / "audio_semantics"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    intake_dir = runtime_root / "field_capture" / "intake"
    upload_dir = runtime_root / "uploads" / "2026-05-03" / "cap-photo-2026-05-03T18-25-20-04-00"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    upload_dir.mkdir(parents=True)
    (upload_dir / "voice.webm").write_bytes(b"audio")
    photo_path = upload_dir / "photo-1.jpg"
    photo_path.write_bytes(b"fake jpeg")
    write_json_object(
        intake_dir / "2026-05-03T18-25-20-04-00__photo-capture-summit-wire.json",
        {
            "job_type": "photo_capture",
            "metadata": {
                "capture_id": "cap-photo-2026-05-03T18-25-20-04-00",
                "site_id": "7050",
            }
            | (
                {
                    "person_id": "per_alice",
                    "person_name": "Alice Example",
                    "field_capture_token_id": "fct_secretish",
                    "field_capture_token_label": "Alice phone",
                }
                if submitter
                else {}
            ),
            "payload": {
                "area": "Restrooms",
                "phase": "reset",
                "captured_at": "2026-05-03T18:25:20-04:00",
                "audio": [],
                "photos": [
                    {
                        "stored_path": "2026-05-03/cap-photo-2026-05-03T18-25-20-04-00/photo-1.jpg",
                        "filename": "photo-1.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 9,
                    }
                ],
            },
        },
    )
    write_json_object(
        transcript_dir / "fca_test.json",
        {
            "type": "field_audio_transcript",
            "status": "complete",
            "audio_asset_id": "fca_test",
            "upload_id": "cap-photo-2026-05-03T18-25-20-04-00",
            "raw_text": "Test note.",
        },
    )
    semantic_path = semantic_dir / "fca_test.json"
    write_json_object(
        semantic_path,
        {
            "type": "field_audio_semantic_summary",
            "status": "complete",
            "audio_asset_id": "fca_test",
            "upload_id": "cap-photo-2026-05-03T18-25-20-04-00",
            "action_candidates": [{"summary": "Review test note.", "rationale": "Test note."}],
        },
    )
    if include_candidate:
        candidate = action_candidate_payload(
            candidate_type="field_capture_follow_up",
            summary="Review test note.",
            rationale="Test note.",
            source_text="Test note.",
            source_context="Test note.",
            provenance={
                "semantic_artifact_path": str(semantic_path),
                "source_transcript_path": str(transcript_dir / "fca_test.json"),
                "audio_asset_id": "fca_test",
            },
            channel_metadata={
                "channel": "field_capture",
                "site_id": "7050",
                "area": "Restrooms",
                "upload_id": "cap-photo-2026-05-03T18-25-20-04-00",
            },
        )
        write_action_candidate_review(candidate_dir, candidate)


def write_voice_status_candidate(runtime_root: Path, *, site_id: str = "7030") -> dict[str, object]:
    candidate = action_candidate_payload(
        candidate_type="voice_memo_operator_action",
        summary=f"Set site {site_id} inactive.",
        rationale="The voice memo requested that the site be made inactive.",
        source_text=f"Please make site {site_id} inactive.",
        source_context="voice memo",
        provenance={"semantic_artifact_path": str(runtime_root / "field_capture" / "semantics" / "voice-status.json")},
        channel_metadata={"channel": "voice_memo", "intent": "site_status_change", "action": "set_inactive", "target_id": site_id, "site_id": site_id},
    )
    write_action_candidate_review(runtime_root / "reviews" / "action_candidates" / "field_capture", candidate)
    return candidate


def first_photo_asset(runtime_root: Path) -> field_photo_vision.FieldPhotoAsset:
    assets = field_photo_vision.discover_photo_assets(
        field_photo_vision.default_intake_dir(runtime_root),
        field_photo_vision.default_upload_dir(runtime_root),
    )
    assert len(assets) == 1
    return assets[0]


def write_photo_vision_sidecar(runtime_root: Path, *, status: str = "completed") -> Path:
    asset = first_photo_asset(runtime_root)
    sidecar_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    sidecar_path = sidecar_dir / f"{asset.photo_asset_id}.json"
    write_json_object(
        sidecar_path,
        {
            "artifact_type": "field_capture_photo_vision",
            "type": "field_capture_photo_vision",
            "status": status,
            "capture_id": asset.capture_id,
            "photo_id": asset.photo_id,
            "photo_asset_id": asset.photo_asset_id,
            "source_image_path": str(asset.image_path),
            "source_image_hash": "sha256-test",
            "site_id": asset.site_id,
            "submitted_area": asset.area,
            "submitted_phase": asset.phase,
            "model_name": "ollama:qwen2.5vl:7b",
            "model_provider": "ollama",
            "site_context_used": True,
            "site_context_id": "7050",
            "site_context_name": "Summit Wire",
            "site_context_summary": "Summit Wire; industrial manufacturing / wire facility",
            "generated_at": "2026-05-06T12:00:00Z",
            "description": "" if status == "failed" else "A restroom area appears visible with stalls and paper supplies.",
            "area_guess": "" if status == "failed" else "restroom",
            "visible_objects": ["stall doors", "paper supply dispenser"],
            "possible_conditions": ["paper supplies visible"],
            "possible_issues": [],
            "confidence": 0.82,
            "needs_human_review": True,
            "warnings": ["Vision timeout"] if status == "failed" else ["Vision output is advisory interpretation only."],
            "error": {"type": "timeout", "message": "local Ollama vision request timed out", "can_retry": True} if status == "failed" else {},
            "provenance": {
                "intake_json_path": str(field_photo_vision.default_intake_dir(runtime_root) / "2026-05-03T18-25-20-04-00__photo-capture-summit-wire.json"),
                "capture_id": asset.capture_id,
                "photo_id": asset.photo_id,
                "photo_asset_id": asset.photo_asset_id,
                "source_image_path": str(asset.image_path),
                "image_media_url": asset.image_media_url,
            },
        },
    )
    return sidecar_path


def write_latest_photo_sidecar(
    runtime_root: Path,
    name: str,
    *,
    site_id: str = "7050",
    target_type: str = "",
    target_id: str = "",
    description: str | None = None,
    mtime: int = 1,
) -> Path:
    sidecar_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    sidecar_path = sidecar_dir / f"{name}.json"
    write_json_object(
        sidecar_path,
        {
            "artifact_type": "field_capture_photo_vision",
            "type": "field_capture_photo_vision",
            "status": "completed",
            "capture_id": f"cap-{name}",
            "photo_id": f"photo-{name}",
            "photo_asset_id": name,
            "site_id": site_id,
            "target_type": target_type,
            "target_id": target_id,
            "generated_at": f"2026-05-0{mtime}T12:00:00Z",
            "description": description if description is not None else f"Photo {name}",
            "area_guess": "restroom",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": 0.8,
            "provenance": {"image_media_url": f"/media/{name}.jpg"},
        },
    )
    os.utime(sidecar_path, (mtime, mtime))
    return sidecar_path


def write_vault_site_issue(vault_root: Path) -> Path:
    path = vault_root / "Accounts" / "Summitsteel" / "Locations" / "7050 - Summit Wire" / "Issues" / "iss_drain__issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
type: site_issue
issue_id: iss_drain
site_id: "7050"
site: Summit Wire
account: Summitsteel
title: Restroom drain backup
status: open
priority: high
category: maintenance
client_notified: true
client_notified_method: email
reported_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
resolution_trigger: Maintenance confirms the drain is clear.
related_capture_ids:
  - cap-photo-drain
related_candidate_ids:
  - ac_drain
---
# Restroom drain backup

## Summary
Drain backed up and water reached the floor.
""",
        encoding="utf-8",
    )
    return path


def test_latest_photo_cards_returns_html_string_under_limit(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    for i in range(6):
        write_latest_photo_sidecar(runtime_root, f"latest-{i}", description=f"Latest photo {i}", mtime=i + 1)

    cards_html, fallback = field_photos.latest_photo_cards(section_ctx(runtime_root), limit=4)

    assert isinstance(cards_html, str)
    assert fallback is False
    assert cards_html.count("<article") == 4
    assert "Latest photo 5" in cards_html
    assert "Latest photo 1" not in cards_html


def test_latest_photo_cards_site_scope_filters_by_site_id(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    write_latest_photo_sidecar(runtime_root, "alpha-1", site_id="alpha", description="Alpha one", mtime=1)
    write_latest_photo_sidecar(runtime_root, "beta-1", site_id="beta", description="Beta one", mtime=2)
    write_latest_photo_sidecar(runtime_root, "alpha-2", site_id="alpha", description="Alpha two", mtime=3)

    cards_html, fallback = field_photos.latest_photo_cards(section_ctx(runtime_root), limit=4, site_id="alpha")

    assert fallback is False
    assert "Alpha one" in cards_html
    assert "Alpha two" in cards_html
    assert "Beta one" not in cards_html


def test_latest_photo_cards_filters_by_target_type_and_id(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    write_latest_photo_sidecar(
        runtime_root,
        "prospect-x",
        target_type="prospect",
        target_id="x",
        description="Prospect X",
        mtime=1,
    )
    write_latest_photo_sidecar(
        runtime_root,
        "prospect-y",
        target_type="prospect",
        target_id="y",
        description="Prospect Y",
        mtime=2,
    )
    write_latest_photo_sidecar(
        runtime_root,
        "site-x",
        target_type="site",
        target_id="x",
        description="Site X",
        mtime=3,
    )

    cards_html, fallback = field_photos.latest_photo_cards(
        section_ctx(runtime_root),
        limit=4,
        target_type="prospect",
        target_id="x",
    )

    assert fallback is False
    assert "Prospect X" in cards_html
    assert "Prospect Y" not in cards_html
    assert "Site X" not in cards_html


def test_latest_photo_cards_target_kwargs_override_site_id(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    write_latest_photo_sidecar(
        runtime_root,
        "wrong-site-matching-target",
        site_id="bar",
        target_type="prospect",
        target_id="x",
        description="Target match",
        mtime=1,
    )
    write_latest_photo_sidecar(
        runtime_root,
        "site-only",
        site_id="foo",
        description="Site only",
        mtime=2,
    )

    cards_html, fallback = field_photos.latest_photo_cards(
        section_ctx(runtime_root),
        limit=4,
        site_id="foo",
        target_type="prospect",
        target_id="x",
    )

    assert fallback is False
    assert "Target match" in cards_html
    assert "Site only" not in cards_html


def test_latest_photo_cards_returns_fallback_flag_on_couchdb_failure(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr(field_photos, "_query_couchdb", lambda _config, _mango: None)

    cards_html, fallback = field_photos.latest_photo_cards(section_ctx(runtime_root), limit=4)

    assert cards_html == ""
    assert fallback is True


def test_field_photos_page_surfaces_pending_raw_photo(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    write_field_capture_fixture(runtime_root, include_candidate=False, submitter=True)

    status_code, _content_type, body = request_text("GET", "/field-photos", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Pending Photos" in body
    assert "Awaiting vision" in body
    assert "cap-photo-2026-05-03T18-25-20-04-00" in body
    assert "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00" in body


def test_photos_page_surfaces_pending_raw_photo(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    write_field_capture_fixture(runtime_root, include_candidate=False, submitter=True)

    status_code, _content_type, body = request_text("GET", "/photos", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Pending Photos" in body
    assert "Awaiting vision" in body
    assert "Photo Vision Search" in body


def test_ops_dashboard_healthz_and_status_missing_dirs_do_not_crash(tmp_path: Path) -> None:
    runtime_root = tmp_path / "missing-runtime"
    status_code, content_type, body = request_text("GET", "/healthz", runtime_root)
    assert status_code == HTTPStatus.OK
    assert "application/json" in content_type
    assert json.loads(body) == {"ok": True}

    status_code, content_type, body = request_text("GET", "/api/status", runtime_root)
    payload = json.loads(body)
    assert status_code == HTTPStatus.OK
    assert "application/json" in content_type
    assert payload["runtime_root"] == str(runtime_root.resolve(strict=False))
    assert payload["health"]["runtime_root_exists"] is False
    assert payload["health"]["queue_count"] == 0
    assert payload["field_capture"]["latest_uploads"] == []


def test_unknown_route_returns_styled_html_404(tmp_path: Path) -> None:
    status_code, content_type, body = request_text("GET", "/nonexistent_xyz_route", tmp_path / "runtime")

    assert status_code == HTTPStatus.NOT_FOUND
    assert "text/html" in content_type
    assert "Not Found" in body
    assert '{"error"' not in body


def test_ops_dashboard_html_includes_key_sections(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    status_code, content_type, body = request_text("GET", "/health", runtime_root)
    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "BTQ Ops Dashboard" in body
    assert "Runtime Health" in body
    assert "Field Capture" in body
    assert "Review Workflow" in body
    assert "Open Field Capture Review" in body
    assert "Maintenance" in body
    assert "Logs" in body


def test_health_panel_renders_trend_when_samples_exist(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    metrics_path = runtime_root / "metrics" / "pipeline_metrics.jsonl"
    metrics_path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    samples = [
        {"ts": (now - timedelta(minutes=30)).isoformat(), "queue_depth": 1, "error_rate": 0.0},
        {"ts": (now - timedelta(minutes=15)).isoformat(), "queue_depth": 4, "error_rate": 0.25},
        {"ts": now.isoformat(), "queue_depth": 2, "error_rate": 0.5},
    ]
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")

    status_code, content_type, body = request_text("GET", "/health", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "Pipeline trend" in body
    assert "50.0%" in body
    assert "▁" in body


def test_health_panel_renders_per_stage_latency(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    metrics_path = runtime_root / "metrics" / "pipeline_metrics.jsonl"
    metrics_path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    samples = [
        {
            "ts": (now - timedelta(minutes=30)).isoformat(),
            "queue_depth": 1,
            "error_rate": 0.0,
            "latency": {"queue": {"p50_ms": 100, "max_ms": 200}},
        },
        {
            "ts": (now - timedelta(minutes=15)).isoformat(),
            "queue_depth": 2,
            "error_rate": 0.0,
            "latency": {"queue": {"p50_ms": 150, "max_ms": 300}},
        },
        {
            "ts": now.isoformat(),
            "queue_depth": 3,
            "error_rate": 0.0,
            "latency": {"queue": {"p50_ms": 250, "max_ms": 400}},
        },
    ]
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")

    status_code, content_type, body = request_text("GET", "/health", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "queue latency p50 / max (ms)" in body
    assert "250 / 400" in body
    assert "▁" in body


def test_health_panel_latency_absent_is_graceful(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    metrics_path = runtime_root / "metrics" / "pipeline_metrics.jsonl"
    metrics_path.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    samples = [{"ts": now.isoformat(), "queue_depth": 1, "error_rate": 0.0}]
    metrics_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")

    status_code, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Latency p50 / max (ms)" in body
    assert "—" in body


def test_health_panel_empty_state_when_no_sidecar(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status_code, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "No samples yet" in body


def test_health_page_surfaces_latest_critical_alert(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    paths = queue_health.alert_paths(runtime_root)
    paths["latest"].parent.mkdir(parents=True)
    paths["latest"].write_text(
        json.dumps(
            {
                "alert_id": "health-test",
                "created_at": "2026-06-02T12:00:00+00:00",
                "status": "critical",
                "fingerprint": "abc123",
                "critical": ["queue backlog above threshold (101>100)"],
                "report": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    status_code, _content_type, body = request_text("GET", "/health", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Latest Critical Health Alert" in body
    assert "queue backlog above threshold (101&gt;100)" in body


def test_field_capture_review_page_renders_pending_drafts(tmp_path: Path, couchdb_job_draft_review) -> None:
    # 337a: the review page renders pending_approval job_draft docs.
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    seed_review_draft(couchdb_job_draft_review, "draft_1", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")
    status_code, content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "Captures" in body
    assert "Review test note." in body
    assert "pending_approval" in body
    assert "cap-photo-2026-05-03T18-25-20-04-00" in body
    assert "Proposed change" in body
    assert "Approve" in body
    assert "Deny" in body
    assert "<code>draft_1</code>" in body
    # The approve + reject forms POST draft_id (twice: approve form + reject form).
    assert body.count('<input type="hidden" name="draft_id" value="draft_1">') == 2
    assert 'class="review-action-form approve-form"' in body
    assert 'class="review-action-form reject-form"' in body
    assert "Stage this job for processing." in body
    assert "Leave the data unchanged." in body


def test_candidate_card_approve_form_precedes_kv_table(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    seed_review_draft(couchdb_job_draft_review, "draft_1", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")
    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    article = body[body.index("<article>") :]
    # The actionable approve form now leads; the technical proposed job moved into
    # the collapsed "Review details".
    assert article.index('class="review-action-form approve-form"') < article.index('class="proposed-job"')


def test_pending_voice_status_draft_shows_proposed_job_deck(tmp_path: Path, couchdb_job_draft_review) -> None:
    # 337a: a draft carries its own job_type+payload; the proposed-job deck
    # renders that directly (no second extraction path).
    runtime_root = tmp_path / "runtime"
    seed_review_draft(
        couchdb_job_draft_review,
        "draft_status_1",
        job_type="set_entity_status",
        payload={
            "entity_type": "site",
            "entity_id": "812",
            "status": "inactive",
            "reason": "Voice memo requested the site be made inactive.",
            "source": "voice_memo",
        },
        message="Set site 812 inactive.",
        site_id="812",
    )

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Proposed change" in body
    assert "Set entity status" in body
    assert "site" in body
    assert "812" in body
    assert "inactive" in body
    assert "Approve" in body
    assert "Deny" in body
    assert 'value="draft_status_1"' in body


@pytest.mark.skip(reason="candidate-lifecycle watcher staging (approve -> stage queue job) not ported to job_draft model; job_draft queue materialization is the 338 increment. Approve-flips-review_status is covered by test_field_capture_review_post_approve_updates_one_pending_draft.")
def test_approving_voice_status_candidate_generates_and_stages_queue_job(tmp_path: Path, couchdb_review) -> None:
    # 308b: approve flips CouchDB status (no inline staging); the watcher then
    # stages the set_entity_status job through the real draft/queue pipeline.
    runtime_root = tmp_path / "runtime"
    candidate = write_voice_status_candidate(runtime_root, site_id="812")
    couchdb_review.seed_from_fs(runtime_root)
    body = f"candidate_id={candidate['candidate_id']}&reviewer=Jordan&rationale=looks-right"

    status, _content_type, _body, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        body.encode("utf-8"),
    )

    assert status == HTTPStatus.SEE_OTHER
    # No inline staging: the redirect message is "approved" (not "and staged").
    assert "message=approved" in headers["Location"]
    assert couchdb_review.status_of(candidate["candidate_id"]) == "approved"
    assert not (runtime_root / "queue").exists()

    # Watcher stages exactly one set_entity_status queue job.
    couchdb_review.run_watcher(runtime_root, stub_stage=False)
    [draft_path] = sorted((runtime_root / "reviews" / "approved_job_drafts" / "field_capture").glob("*.json"))
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    assert draft["proposed_job_type"] == "set_entity_status"
    [queue_path] = sorted((runtime_root / "queue").glob("*.json"))
    queue_job = json.loads(queue_path.read_text(encoding="utf-8"))
    assert queue_job["job_type"] == "set_entity_status"
    assert queue_job["payload"]["entity_type"] == "site"
    assert queue_job["payload"]["entity_id"] == "812"
    assert queue_job["payload"]["status"] == "inactive"
    assert couchdb_review.staged_at_of(candidate["candidate_id"])


@pytest.mark.skip(reason="candidate-lifecycle multi-job watcher staging (one queue job per employee) not ported to job_draft model; a job_draft carries a single job_type+payload and queue materialization is the 338 increment.")
def test_approving_multi_employee_status_candidate_stages_one_job_per_employee(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate = action_candidate_payload(
        candidate_type="voice_memo_operator_action",
        summary="Set selected employees inactive.",
        rationale="Multiple employees left employment.",
        source_text="Maria and Alex are no longer employees.",
        source_context="voice memo",
        provenance={"semantic_artifact_path": str(runtime_root / "field_capture" / "semantics" / "voice-status.json")},
        channel_metadata={
            "channel": "voice_memo",
            "intent": "employee_status_change",
            "action": "set_inactive",
            "target_id": "hutton-maria,taylor-alex",
            "employee_slugs": ["hutton-maria", "taylor-alex"],
            "employee_names": ["Maria Hutton", "Alex Taylor"],
        },
    )
    write_action_candidate_review(runtime_root / "reviews" / "action_candidates" / "field_capture", candidate)
    couchdb_review.seed_from_fs(runtime_root)
    body = f"candidate_id={candidate['candidate_id']}&reviewer=Jordan&rationale=looks-right"

    status, _content_type, _body, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        body.encode("utf-8"),
    )

    assert status == HTTPStatus.SEE_OTHER
    # 308b: approve flips CouchDB status; no inline staging.
    assert "message=approved" in headers["Location"]
    assert couchdb_review.status_of(candidate["candidate_id"]) == "approved"
    assert not (runtime_root / "queue").exists()

    # The watcher stages one job per employee through the real pipeline.
    couchdb_review.run_watcher(runtime_root, stub_stage=False)
    queue_jobs = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((runtime_root / "queue").glob("*.json"))]
    assert {job["payload"]["entity_id"] for job in queue_jobs} == {"Maria Hutton", "Alex Taylor"}
    assert {job["payload"]["status"] for job in queue_jobs} == {"inactive"}


@pytest.mark.skip(reason="candidate-lifecycle watcher staging not ported to job_draft model (338). Deny-flips-review_status-to-rejected is covered by test_field_capture_review_post_reject_updates_one_pending_draft.")
def test_denying_voice_status_candidate_does_not_stage_queue_job(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate = write_voice_status_candidate(runtime_root, site_id="812")
    couchdb_review.seed_from_fs(runtime_root)
    body = f"candidate_id={candidate['candidate_id']}&reviewer=Jordan&rationale=nope"

    status, _content_type, _body, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-capture/review/reject",
        runtime_root,
        body.encode("utf-8"),
    )

    assert status == HTTPStatus.SEE_OTHER
    assert "message=denied" in headers["Location"]
    assert couchdb_review.status_of(candidate["candidate_id"]) == "rejected"
    # Nothing staged, even after a watcher pass (watcher only stages approved).
    couchdb_review.run_watcher(runtime_root, stub_stage=False)
    assert not (runtime_root / "queue").exists()


def test_candidate_internal_paths_collapsed_behind_details_summary(tmp_path: Path, couchdb_job_draft_review) -> None:
    # 337a: the draft_id is an internal field collapsed behind <summary>Internals</summary>.
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    seed_review_draft(couchdb_job_draft_review, "draft_internal_1", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "<summary>Internals</summary>" in body
    internals_start = body.index('<details class="raw-json">')
    internals_end = body.index("</details>", internals_start)
    internals_body = body[internals_start:internals_end]
    assert "draft_internal_1" in internals_body


@pytest.mark.skip(reason="candidate-lifecycle resolution surface (Mark Client Informed) not ported to job_draft model; see 338. Drafts have no approved-resolution lifecycle, so this asserts a feature that no longer exists on the review surface.")
def test_field_capture_review_page_hides_action_forms_for_non_pending_candidates(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["status"] = "approved"
    write_json_object(candidate_path, candidate)
    couchdb_review.seed_from_fs(runtime_root)

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=approved", runtime_root)

    assert status_code == HTTPStatus.OK
    assert candidate["candidate_id"] in body
    assert "Open this issue" not in body
    assert "Dismiss this candidate" not in body
    assert 'action="/field-capture/review/approve"' not in body
    assert 'action="/field-capture/review/reject"' not in body
    assert "Mark Client Informed" in body


@pytest.mark.skip(reason="candidate-lifecycle client-informed/resolve resolution surface not ported to job_draft model; see 338.")
def test_field_capture_review_page_shows_client_informed_status_for_approved_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["status"] = "approved"
    candidate["reviewer"] = "Jordan"
    candidate["review_rationale"] = "Looks right"
    write_json_object(candidate_path, candidate)
    couchdb_review.seed_from_fs(runtime_root)

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=approved", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Resolution" in body
    assert "Mark Client Informed" in body
    assert "Mark Resolved" in body
    assert 'action="/field-capture/review/client-informed"' in body
    assert f'<input type="hidden" name="candidate_id" value="{candidate["candidate_id"]}">' in body


def test_candidate_detail_exposes_resolution_dict(tmp_path: Path) -> None:
    payload = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    payload["resolution"] = {"status": "open", "opened_by": "Jordan"}
    path = tmp_path / "candidate.json"

    detail = candidate_detail(path, payload)

    assert detail["resolution"] == {"status": "open", "opened_by": "Jordan"}


def test_candidate_card_pending_review_shows_approval_controls(tmp_path: Path) -> None:
    # 337a: a pending_approval draft card shows Approve/Deny.
    payload = draft_review_payload("draft_ctrl")
    body = render_candidate_card(candidate_detail(tmp_path / "candidate.json", payload))

    assert "Approve" in body
    assert "Deny" in body


@pytest.mark.skip(reason="candidate-lifecycle triage actions (resubmit/archive/restore on rejected/failed/archived) not ported to job_draft model; see 338. The draft model renders no resubmit/archive forms.")
def test_candidate_card_failed_rejected_and_archived_triage_actions(tmp_path: Path) -> None:
    rejected = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    rejected["status"] = "rejected"
    rejected_body = render_candidate_card(candidate_detail(tmp_path / "rejected.json", rejected))

    assert 'action="/field-capture/review/resubmit"' in rejected_body
    assert 'action="/field-capture/review/archive"' in rejected_body
    assert "Resubmit" in rejected_body
    assert "Delete" in rejected_body

    failed = candidate_detail(tmp_path / "failed.json", {**rejected, "status": "failed"})
    failed["proposed_job_error"] = "missing target"
    failed["proposed_jobs"] = [{"job_type": "", "payload": {}, "error": "missing target"}]
    failed_body = render_candidate_card(failed)

    assert 'action="/field-capture/review/resubmit"' in failed_body
    assert "Cannot resubmit until the proposed job is valid" in failed_body
    assert "<span>Resubmit</span>" in failed_body
    assert "disabled" in failed_body

    archived = candidate_detail(tmp_path / "archived.json", rejected)
    archived["archived"] = True
    archived_body = render_candidate_card(archived)

    assert 'action="/field-capture/review/unarchive"' in archived_body
    assert "Restore" in archived_body
    assert 'action="/field-capture/review/resubmit"' not in archived_body
    assert 'action="/field-capture/review/archive"' not in archived_body


def test_candidate_card_context_expanded_by_default(tmp_path: Path, monkeypatch) -> None:
    payload = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    candidate = candidate_detail(tmp_path / "candidate.json", payload)

    def fake_context_text(path_value: object, *, json_pretty: bool = False) -> str:
        return "Worker transcript text" if path_value == candidate["source_transcript_path"] else ""

    monkeypatch.setattr(candidates_section, "read_candidate_context_text", fake_context_text)

    body = render_candidate_context(candidate)

    assert "Worker transcript text" in body
    assert "<details" not in body


def test_capture_group_renders_thumbs_and_card_has_none(tmp_path: Path) -> None:
    # Thumbnails now render once per capture (the group header), not per candidate
    # card — the capture IS the message+photos, the cards are its proposed actions.
    cap = "cap-thumb-1"
    photo_dir = tmp_path / "uploads" / "2026-06-10" / cap
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / "p.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    payload = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    cand = candidate_detail(tmp_path / "candidate.json", payload)
    cand["capture_id"] = cap

    group_html = render_candidate_groups([cand], tmp_path)
    assert f"/media/2026-06-10/{cap}/p.jpg" in group_html  # thumbnail at the group level
    assert "<img" not in render_candidate_card(cand, thumb_urls=[f"/media/2026-06-10/{cap}/p.jpg"])  # not per card


def test_candidate_card_renders_without_thumb_strip_when_not_provided(tmp_path: Path) -> None:
    payload = draft_review_payload("draft_nothumb", message="Review sink area.")

    body = render_candidate_card(candidate_detail(tmp_path / "candidate.json", payload))

    assert "Review sink area." in body
    assert "<img" not in body


@pytest.mark.skip(reason="candidate-lifecycle notify/resolve resolution forms not ported to job_draft model; see 338.")
def test_candidate_card_approved_open_shows_notify_and_resolve_forms(tmp_path: Path) -> None:
    payload = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    payload["status"] = "approved"
    payload["resolution"] = {"status": "open"}

    body = render_candidate_card(candidate_detail(tmp_path / "candidate.json", payload))

    assert 'action="/field-capture/review/client-informed"' in body
    assert 'action="/field-capture/review/resolve"' in body


def test_candidate_card_resolved_shows_no_action_forms(tmp_path: Path) -> None:
    payload = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    payload["status"] = "approved"
    payload["resolution"] = {
        "status": "resolved",
        "resolved_by": "Jordan",
        "resolved_at": "2026-05-22T20:00:00Z",
        "resolved_note": "Fixed",
    }

    body = render_candidate_card(candidate_detail(tmp_path / "candidate.json", payload))

    assert "Mark Resolved" not in body
    assert '<span class="pill resolution-resolved">Resolved</span>' in body


def test_candidate_filter_resolution_status_open_excludes_resolved(tmp_path: Path) -> None:
    candidates = [
        {"candidate_id": "open", "resolution": {"status": "open"}},
        {"candidate_id": "resolved", "resolution": {"status": "resolved"}},
    ]

    filtered = filter_candidates(candidates, tmp_path, {"resolution": ["open"]})

    assert [candidate["candidate_id"] for candidate in filtered] == ["open"]


def test_field_capture_review_page_shows_submitter_from_intake_without_token(tmp_path: Path, couchdb_job_draft_review) -> None:
    # The submitter is resolved from the capture intake (filesystem), keyed by the
    # draft's source_capture_id -> the token id/label must never leak into the page.
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, submitter=True)
    seed_review_draft(
        couchdb_job_draft_review, "draft_sub_1",
        source_capture_id="cap-photo-2026-05-03T18-25-20-04-00", submitter_name="",
    )

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Alice Example" in body
    assert "per_alice" in body
    assert "fct_secretish" not in body
    assert "Alice phone" not in body


def test_ops_dashboard_status_includes_photo_vision_counts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, submitter=True)
    write_photo_vision_sidecar(runtime_root)

    payload = build_status(runtime_root)
    photo_vision = payload["field_capture"]["photo_vision"]

    assert payload["field_capture"]["latest_uploads"][0]["submitter_name"] == "Alice Example"
    assert payload["field_capture"]["latest_uploads"][0]["submitter_id"] == "per_alice"
    assert photo_vision["image_count"] == 1
    assert photo_vision["sidecar_count"] == 1
    assert photo_vision["completed_count"] == 1
    assert photo_vision["failed_count"] == 0
    assert photo_vision["skipped_count"] == 0
    assert photo_vision["malformed_count"] == 0
    assert photo_vision["backlog_count"] == 0
    assert photo_vision["recent_warnings"] == []


def test_field_capture_review_page_renders_photo_vision_sidecar(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    write_photo_vision_sidecar(runtime_root)

    seed_review_draft(couchdb_job_draft_review, "draft_pv_1", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")
    status_code, content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "Vision" in body
    assert "A restroom area appears visible with stalls and paper supplies." in body
    assert "restroom" in body
    assert "stall doors" in body
    assert "paper supply dispenser" in body
    assert "ollama:qwen2.5vl:7b" in body
    assert "0.82" in body


def test_candidate_vision_items_wrapped_in_details_expander(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    sidecar_path = write_photo_vision_sidecar(runtime_root)
    second_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    second_sidecar["photo_asset_id"] = "photo-asset-second"
    second_sidecar["photo_id"] = "photo-2"
    second_sidecar["area_guess"] = "supply closet"
    second_sidecar["description"] = "A second photo shows stocked supplies."
    write_json_object(sidecar_path.with_name("photo-asset-second.json"), second_sidecar)

    seed_review_draft(couchdb_job_draft_review, "draft_pv_2", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")
    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    vision_start = body.index("<h4>Vision</h4>")
    vision_end = body.index("<h4>Reviewer History</h4>")
    vision_body = body[vision_start:vision_end]
    assert vision_body.count("<details>") >= 2
    assert "A restroom area appears visible with stalls and paper supplies." in body
    assert "A second photo shows stocked supplies." in body


def test_field_capture_review_page_handles_missing_photo_vision(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    seed_review_draft(couchdb_job_draft_review, "draft_pv_missing", source_capture_id="cap-photo-2026-05-03T18-25-20-04-00")
    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=pending_approval", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "No vision description yet" in body


def test_photo_vision_malformed_sidecar_warns_without_crashing(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    photo_vision_dir.mkdir(parents=True)
    (photo_vision_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

    payload = build_status(runtime_root)
    status_code, content_type, body = request_text("GET", "/health", runtime_root)
    review_status_code, _review_content_type, review_body = request_text("GET", "/field-capture/review", runtime_root)

    assert payload["field_capture"]["photo_vision"]["malformed_count"] == 1
    assert payload["field_capture"]["photo_vision"]["backlog_count"] == 1
    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "broken" in body
    assert "malformed" in body
    assert review_status_code == HTTPStatus.OK
    assert "Captures" in review_body


def test_capture_detail_renders_completed_photo_vision_card(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False, submitter=True)
    write_photo_vision_sidecar(runtime_root)

    status_code, content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "Capture Detail" in body
    assert "A restroom area appears visible with stalls and paper supplies." in body
    assert "Restrooms" in body
    assert "reset" in body
    assert "ollama:qwen2.5vl:7b" in body
    assert "Summit Wire; industrial manufacturing / wire facility" in body
    assert "Alice Example" in body
    assert "per_alice" in body
    assert "fct_secretish" not in body
    assert '<img src="/media/2026-05-03/cap-photo-2026-05-03T18-25-20-04-00/photo-1.jpg"' in body
    assert "Open this issue" not in body
    assert not (runtime_root / "reviews" / "action_candidates" / "field_capture").exists()


def test_capture_detail_media_route_serves_safe_preview(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)
    write_photo_vision_sidecar(runtime_root)

    status_code, content_type, body = request_text("GET", "/media/2026-05-03/cap-photo-2026-05-03T18-25-20-04-00/photo-1.jpg", runtime_root)

    assert status_code == HTTPStatus.OK
    assert content_type == "image/jpeg"
    assert body == "fake jpeg"


def test_capture_detail_handles_missing_media_url_gracefully(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)
    sidecar_path = write_photo_vision_sidecar(runtime_root)
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    payload["provenance"]["image_media_url"] = "file:///private/image.jpg"
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")

    status_code, _content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Image path available locally." in body
    # The capture detail still shows non-image uploads (voice.webm) with an
    # <img> wouldn't appear for them; ensure the *photo* card didn't fall
    # back to rendering an unsafe <img src=file://>.
    assert 'src="file:' not in body


def test_capture_detail_renders_failed_photo_error_metadata(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)
    write_photo_vision_sidecar(runtime_root, status="failed")

    status_code, _content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "vision_status" in body
    assert "failed" in body
    assert "error_type" in body
    assert "timeout" in body
    assert "can_retry" in body


def test_capture_detail_renders_missing_sidecar_inline(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)

    status_code, _content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "No vision description yet." in body
    assert "missing" in body


def test_capture_detail_unknown_submitter_fallback(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)

    status_code, _content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    assert "Unknown submitter" in body


def test_capture_detail_malformed_sidecar_does_not_crash(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root, include_candidate=False)
    photo_vision_dir = field_photo_vision.default_photo_vision_dir(runtime_root)
    photo_vision_dir.mkdir(parents=True)
    (photo_vision_dir / "broken.json").write_text("{not valid json", encoding="utf-8")

    status_code, _content_type, body = request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    assert status_code == HTTPStatus.OK
    # Malformed sidecar parsing surfaces in load_photo_vision_sidecars and
    # should not crash detail rendering. Without a capture_id match, the
    # malformed orphan is filtered out — the card section just shows the
    # discovered photo as missing. The important assertion is that the
    # page rendered at all.
    assert "Capture Detail" in body


def test_field_capture_review_post_approve_updates_one_pending_draft(tmp_path: Path, monkeypatch, couchdb_job_draft_review) -> None:
    # 337a: approving a job_draft flips its review_status pending_approval ->
    # approved via the 335 write path, recording reviewer + rationale on the doc.
    # No queue processor runs from the review UI; queue materialization (338) is
    # a separate increment, so no inline staging is asserted here.
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_field_capture_fixture(runtime_root)
    doc = seed_review_draft(couchdb_job_draft_review, "draft_approve_1")
    queue_processor_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run from review UI")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)

    status_code, content_type, body = request_text(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"draft_id=draft_approve_1&_rev={doc['_rev']}&reviewer=Jordan&rationale=Looks+right",
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert "text/html" in content_type
    assert "Return to Captures" in body
    assert couchdb_job_draft_review.review_status_of("draft_approve_1") == "approved"
    assert couchdb_job_draft_review.reviewer_of("draft_approve_1") == "Jordan"
    assert couchdb_job_draft_review.doc("draft_approve_1")["review_rationale"] == "Looks right"
    # The review UI never runs the queue processor and never touches the vault.
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert queue_processor_called["called"] is False


def test_field_capture_review_post_reject_updates_one_pending_draft(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    doc = seed_review_draft(couchdb_job_draft_review, "draft_reject_1")

    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/reject",
        runtime_root,
        f"draft_id=draft_reject_1&_rev={doc['_rev']}&reviewer=Jordan&rationale=Not+useful",
    )

    assert status_code == HTTPStatus.SEE_OTHER
    # 337a: review_status + rationale on the job_draft doc; rejected is KEPT.
    assert couchdb_job_draft_review.review_status_of("draft_reject_1") == "rejected"
    assert couchdb_job_draft_review.doc("draft_reject_1")["review_rationale"] == "Not useful"
    # Reject stages nothing in the review UI.
    assert not (runtime_root / "queue").exists()


@pytest.mark.skip(reason="candidate-lifecycle archive/unarchive not ported to job_draft model; see 338. Drafts have no archived state on the review surface.")
def test_field_capture_review_archive_excludes_candidate_from_counts_and_restores(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(candidate_type="field_capture_follow_up", summary="Review sink area.")
    candidate["status"] = "rejected"
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    doc = couchdb_review.docs[f"action_candidate_{candidate_id}"]

    before = field_action_candidates.list_action_candidates_report(candidate_dir, runtime_root=runtime_root)
    assert before["counts"]["rejected"] == 1

    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/archive",
        runtime_root,
        urlencode({"candidate_id": candidate_id, "_rev": doc["_rev"], "reviewer": "Jordan"}),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert couchdb_review.archived_of(candidate_id) is True
    after = field_action_candidates.list_action_candidates_report(candidate_dir, runtime_root=runtime_root)
    assert after["counts"]["rejected"] == 0
    assert after["candidates"] == []
    assert field_action_candidates.couchdb_candidate_payloads(status="rejected") == []
    archived_payloads = field_action_candidates.couchdb_candidate_payloads(status="archived")
    assert [payload["candidate_id"] for _path, payload in archived_payloads] == [candidate_id]

    status_code, _content_type, body = request_text("GET", "/field-capture/review?status=archived", runtime_root)
    assert status_code == HTTPStatus.OK
    assert "Restore" in body
    assert 'action="/field-capture/review/unarchive"' in body

    restored_rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]
    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/unarchive",
        runtime_root,
        urlencode({"candidate_id": candidate_id, "_rev": restored_rev, "reviewer": "Jordan"}),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert couchdb_review.archived_of(candidate_id) is False
    restored = field_action_candidates.list_action_candidates_report(candidate_dir, runtime_root=runtime_root)
    assert restored["counts"]["rejected"] == 1


def test_field_capture_review_resubmit_stages_valid_rejected_candidate(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Staffing risk at site 7050",
    )
    candidate["approval_metadata"] = {
        "proposed_queue_job": {
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/7050.md",
                "content": "Staffing risk: Bruce no-showed.",
                "destination": "site_note",
            },
        }
    }
    candidate["status"] = "rejected"
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]

    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/resubmit",
        runtime_root,
        urlencode({"candidate_id": candidate_id, "_rev": rev, "reviewer": "Jordan"}),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert couchdb_review.status_of(candidate_id) == "approved"
    assert len(list((runtime_root / "queue").glob("*.json"))) == 1
    history = couchdb_review.docs[f"action_candidate_{candidate_id}"]["review_history"]
    assert history[-1]["reason"] == "resubmit"
    assert history[-1]["prior_status"] == "rejected"


def test_field_capture_review_resubmit_invalid_failed_candidate_flashes_without_staging(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Broken payload",
    )
    candidate["approval_metadata"] = {"proposed_queue_job": {"job_type": "append_to_note", "payload": {}}}
    candidate["status"] = "failed"
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]

    status_code, _content_type, _body, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-capture/review/resubmit",
        runtime_root,
        urlencode({"candidate_id": candidate_id, "_rev": rev, "reviewer": "Jordan"}).encode("utf-8"),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert "the+proposed+job+isn%27t+valid+yet" in headers["Location"]
    assert couchdb_review.status_of(candidate_id) == "failed"
    assert not (runtime_root / "queue").exists()


def test_patch_action_candidate_fields_deep_merges_channel_metadata_and_rejects_disallowed_keys(
    tmp_path: Path,
    couchdb_review,
) -> None:
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Original summary",
        source_text="Original source",
        channel_metadata={"site_id": "bad-site", "area": "Lobby", "upload_id": "cap-1"},
        status="failed",
    )
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    cfg = couchdb_config.from_env()
    db = couchdb_config.field_captures_database()
    original_rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]

    updated = field_action_candidates.patch_action_candidate_fields(
        cfg,
        db,
        candidate_id,
        fields={"channel_metadata": {"site_id": "7010"}, "summary": "Fixed summary", "source_text": "Fixed source"},
        expected_rev=original_rev,
    )

    metadata = couchdb_review.docs[f"action_candidate_{candidate_id}"]["source_detail"]["channel_metadata"]
    assert updated["_rev"] != original_rev
    assert metadata["site_id"] == "7010"
    assert metadata["area"] == "Lobby"
    assert metadata["upload_id"] == "cap-1"
    assert couchdb_review.docs[f"action_candidate_{candidate_id}"]["summary"] == "Fixed summary"
    assert couchdb_review.docs[f"action_candidate_{candidate_id}"]["source_detail"]["source_text"] == "Fixed source"

    with pytest.raises(field_action_candidates.CouchDBCandidateWriterError):
        field_action_candidates.patch_action_candidate_fields(
            cfg,
            db,
            candidate_id,
            fields={"status": "approved"},
            expected_rev=updated["_rev"],
        )
    with pytest.raises(field_action_candidates.AlreadyDecided):
        field_action_candidates.patch_action_candidate_fields(
            cfg,
            db,
            candidate_id,
            fields={"area": "Kitchen"},
            expected_rev=original_rev,
        )


@pytest.mark.skip(reason="candidate-lifecycle fix/resubmit form (Fix & resubmit + site dropdown for failed candidates) not ported to job_draft model; see 338. Drafts have no 'failed' review_status and no fix form on the review surface.")
def test_candidate_card_failed_with_proposed_error_shows_fix_form_and_site_dropdown(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        candidates_section,
        "SITES",
        [{"site_id": "fix-site", "canonical": "Fix Site", "note_path": "Accounts/fix-site.md"}],
    )
    payload = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Review sink area.",
        channel_metadata={"site_id": "fix-site", "area": "Lobby", "visit_proposed": True, "visit_type": "follow_up"},
        status="failed",
    )
    failed = candidate_detail(tmp_path / "failed.json", payload)
    failed["proposed_job_error"] = "missing target"
    failed["proposed_jobs"] = [{"job_type": "", "payload": {}, "error": "missing target"}]

    body = render_candidate_card(failed)

    assert 'action="/field-capture/review/fix"' in body
    assert "Fix &amp; resubmit" in body
    assert "missing target" in body
    assert '<option value="fix-site" selected>Fix Site (fix-site)</option>' in body
    assert 'name="candidate_id"' in body
    assert 'name="_rev"' in body
    assert 'name="status"' not in body
    assert 'name="provenance"' not in body


def test_field_capture_review_fix_valid_payload_resubmits_candidate(tmp_path: Path, monkeypatch, couchdb_review) -> None:
    monkeypatch.setattr(
        approved_job_drafts,
        "SITES",
        [{"site_id": "fix-site", "canonical": "Fix Site", "note_path": "Accounts/fix-site.md"}],
    )
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Broken site payload",
        source_text="Please add this to the site note.",
        channel_metadata={"site_id": "missing-site", "area": "Lobby", "upload_id": "cap-1"},
        status="failed",
    )
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]

    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/fix",
        runtime_root,
        urlencode(
            {
                "candidate_id": candidate_id,
                "_rev": rev,
                "reviewer": "Jordan",
                "site_id": "fix-site",
                "area": "Lobby",
                "summary": "Fixed site payload",
                "visit_proposed_present": "1",
            }
        ),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert couchdb_review.status_of(candidate_id) == "approved"
    assert len(list((runtime_root / "queue").glob("*.json"))) == 1
    doc = couchdb_review.docs[f"action_candidate_{candidate_id}"]
    assert doc["source_detail"]["channel_metadata"]["site_id"] == "fix-site"
    assert doc["summary"] == "Fixed site payload"
    history = doc["review_history"]
    assert history[-1]["reason"] == "fix_resubmit"
    assert history[-1]["prior_status"] == "failed"


def test_field_capture_review_fix_still_invalid_saves_patch_without_staging(
    tmp_path: Path,
    monkeypatch,
    couchdb_review,
) -> None:
    monkeypatch.setattr(
        approved_job_drafts,
        "SITES",
        [{"site_id": "fix-site", "canonical": "Fix Site", "note_path": "Accounts/fix-site.md"}],
    )
    runtime_root = tmp_path / "runtime"
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    candidate = action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary="Still broken site payload",
        source_text="Please add this to the site note.",
        channel_metadata={"site_id": "missing-site", "area": "Lobby", "upload_id": "cap-1"},
        status="failed",
    )
    write_action_candidate_review(candidate_dir, candidate)
    couchdb_review.seed_from_fs(runtime_root)
    candidate_id = str(candidate["candidate_id"])
    rev = couchdb_review.docs[f"action_candidate_{candidate_id}"]["_rev"]

    status_code, _content_type, _body, headers = ops_app.route_response_with_headers(
        "POST",
        "/field-capture/review/fix",
        runtime_root,
        urlencode(
            {
                "candidate_id": candidate_id,
                "_rev": rev,
                "reviewer": "Jordan",
                "site_id": "still-missing",
                "area": "Lobby",
                "summary": "Saved but still invalid",
                "visit_proposed_present": "1",
            }
        ).encode("utf-8"),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert "could+not+resolve+site+note+path" in headers["Location"]
    assert couchdb_review.status_of(candidate_id) == "failed"
    doc = couchdb_review.docs[f"action_candidate_{candidate_id}"]
    assert doc["source_detail"]["channel_metadata"]["site_id"] == "still-missing"
    assert doc["summary"] == "Saved but still invalid"
    assert not (runtime_root / "queue").exists()


def test_open_issue_post_redirects_to_inbox(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    doc = seed_review_draft(couchdb_job_draft_review, "draft_open_1")

    status_code, _content_type, _body, headers = _handle_review_post(
        "approve",
        section_ctx(runtime_root),
        f"draft_id=draft_open_1&_rev={doc['_rev']}&reviewer=Jordan".encode("utf-8"),
    )

    assert status_code == HTTPStatus.SEE_OTHER
    # 337a: approve flips the draft -> approved and redirects to the inbox.
    assert headers["Location"] == "/?message=approved"
    assert couchdb_job_draft_review.review_status_of("draft_open_1") == "approved"


def test_open_issue_post_succeeds_without_rationale(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    doc = seed_review_draft(couchdb_job_draft_review, "draft_norat_1")

    status_code, _content_type, body = request_text(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"draft_id=draft_norat_1&_rev={doc['_rev']}&reviewer=Jordan&rationale=",
    )

    assert status_code != HTTPStatus.BAD_REQUEST
    assert "review rationale is required" not in body
    # 337a: approve succeeds with an empty rationale; review_status on the draft doc.
    assert couchdb_job_draft_review.review_status_of("draft_norat_1") == "approved"


def test_field_capture_review_post_reapprove_fails_closed(tmp_path: Path, couchdb_job_draft_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    doc = seed_review_draft(couchdb_job_draft_review, "draft_reapprove_1")
    request_text(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"draft_id=draft_reapprove_1&_rev={doc['_rev']}&reviewer=Jordan&rationale=Looks+right",
    )
    assert couchdb_job_draft_review.review_status_of("draft_reapprove_1") == "approved"

    # 337a: the second approve hits the review_status guard (already approved /
    # stale _rev) and does NOT re-mutate -- fails closed via the shared 335 fn.
    request_text(
        "POST",
        "/field-capture/review/approve",
        runtime_root,
        f"draft_id=draft_reapprove_1&_rev={doc['_rev']}&reviewer=Jordan&rationale=Second+approval",
    )

    after = couchdb_job_draft_review.doc("draft_reapprove_1")
    assert after["review_status"] == "approved"
    assert after["review_rationale"] == "Looks right"
    assert len(after["review_history"]) == 1


def test_field_capture_review_post_duplicate_candidate_id_fails_closed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    duplicate_path = candidate_dir / "duplicate.json"
    duplicate_path.write_text(candidate_path.read_text(encoding="utf-8"), encoding="utf-8")
    candidate_id = json.loads(candidate_path.read_text(encoding="utf-8"))["candidate_id"]

    status_code, _content_type, body = request_text(
        "POST",
        "/field-capture/review/reject",
        runtime_root,
        f"candidate_id={candidate_id}&reviewer=Jordan&rationale=Nope",
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert "Return to Captures" in body
    assert json.loads(candidate_path.read_text(encoding="utf-8"))["status"] == "pending_review"
    assert json.loads(duplicate_path.read_text(encoding="utf-8"))["status"] == "pending_review"
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "queue").exists()


def test_field_capture_review_post_client_informed_writes_notification_and_resolution(tmp_path: Path, monkeypatch, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["status"] = "approved"
    candidate["reviewer"] = "Jordan"
    candidate["review_rationale"] = "Approved"
    write_json_object(candidate_path, candidate)
    # 308c: validate_approved_candidate reads the approved candidate from CouchDB
    # (sole canonical store) before recording the client notification; the
    # resolution patch still writes the filesystem artifact. Seed both.
    couchdb_review.seed_from_fs(runtime_root)
    queue_processor_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run from client-informed UI")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)

    status_code, _content_type, body = request_text(
        "POST",
        "/field-capture/review/client-informed",
        runtime_root,
        f"candidate_id={candidate['candidate_id']}&method=email&by=Jordan&note=Emailed+client+with+photo",
    )

    notification_path = runtime_root / "reviews" / "client_notifications" / "field_capture" / f"{candidate['candidate_id']}.json"
    notification = json.loads(notification_path.read_text(encoding="utf-8"))
    assert status_code == HTTPStatus.SEE_OTHER
    assert "Return to Field Capture Review" in body
    assert notification["client_informed"] is True
    assert notification["client_informed_method"] == "email"
    assert notification["client_informed_by"] == "Jordan"
    assert notification["client_informed_note"] == "Emailed client with photo"
    updated = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert updated["status"] == "approved"
    assert updated["reviewer"] == "Jordan"
    assert updated["review_rationale"] == "Approved"
    assert updated["resolution"]["status"] == "client_notified"
    assert updated["resolution"]["client_notified_by"] == "Jordan"
    assert updated["resolution"]["client_notified_note"] == "Emailed client with photo"
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert not (runtime_root / "queue").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert queue_processor_called["called"] is False


def test_resolve_post_updates_resolution_status(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["status"] = "approved"
    write_json_object(candidate_path, candidate)

    status_code, _content_type, _body = request_text(
        "POST",
        "/field-capture/review/resolve",
        runtime_root,
        f"candidate_id={candidate['candidate_id']}&resolved_by=Jordan&resolved_note=Fixed",
    )

    updated = json.loads(candidate_path.read_text(encoding="utf-8"))
    assert status_code == HTTPStatus.SEE_OTHER
    assert updated["resolution"]["status"] == "resolved"
    assert updated["resolution"]["resolved_by"] == "Jordan"
    assert updated["resolution"]["resolved_note"] == "Fixed"


def test_field_capture_review_post_client_informed_fails_for_pending_candidate(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)
    candidate_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    [candidate_path] = sorted(candidate_dir.glob("*.json"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))

    status_code, _content_type, body = request_text(
        "POST",
        "/field-capture/review/client-informed",
        runtime_root,
        f"candidate_id={candidate['candidate_id']}&method=email&by=Jordan&note=Nope",
    )

    assert status_code == HTTPStatus.SEE_OTHER
    assert "Return to Field Capture Review" in body
    assert not (runtime_root / "reviews" / "client_notifications").exists()
    assert json.loads(candidate_path.read_text(encoding="utf-8"))["status"] == "pending_review"
    assert not (runtime_root / "queue").exists()


def test_ops_dashboard_status_includes_field_capture_review_summary(tmp_path: Path, couchdb_review) -> None:
    runtime_root = tmp_path / "runtime"
    write_field_capture_fixture(runtime_root)

    couchdb_review.seed_from_fs(runtime_root)
    payload = build_status(runtime_root)

    assert payload["health"]["field_capture_intake_count"] == 1
    assert payload["field_capture"]["transcript_count"] == 1
    assert payload["field_capture"]["semantic_count"] == 1
    assert payload["review"]["pending_candidates"] == 1
    assert payload["review"]["dashboard"]["review_status"]["counts"]["semantic_with_action_candidates"] == 1


def test_ops_dashboard_surfaces_open_site_issues_read_only(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault_root = tmp_path / "vault"
    issue_path = write_vault_site_issue(vault_root)
    monkeypatch.setattr(ops_app, "get_config", lambda: SimpleNamespace(vault_dir=vault_root))
    before_issue = issue_path.read_text(encoding="utf-8")

    payload = build_status(runtime_root)
    status_code, content_type, body = request_text("GET", "/field-capture/issues?site_id=7050", runtime_root)

    assert payload["site_issues"]["counts"]["current"] == 1
    assert payload["site_issues"]["counts"]["by_site"] == {"7050": 1}
    assert payload["site_issues"]["recent"][0]["title"] == "Restroom drain backup"
    assert status_code == HTTPStatus.OK
    assert "text/html" in content_type
    assert "Restroom drain backup" in body
    assert "client_notified" in body
    assert "Maintenance confirms the drain is clear." in body
    assert issue_path.read_text(encoding="utf-8") == before_issue
    assert not (runtime_root / "queue").exists()


def test_ops_dashboard_requests_are_read_only(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    queue_processor_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run from ops dashboard")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)
    request_text("GET", "/", runtime_root)
    request_text("GET", "/api/status", runtime_root)
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "processed").exists()
    assert not (runtime_root / "failed").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert queue_processor_called["called"] is False


def test_ops_dashboard_vision_display_does_not_call_models_or_write_artifacts(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    vault.mkdir()
    sentinel = vault / "sentinel.md"
    sentinel.write_text("do not touch\n", encoding="utf-8")
    write_field_capture_fixture(runtime_root)
    before_sidecars = sorted(field_photo_vision.default_photo_vision_dir(runtime_root).glob("*.json"))
    before_candidates = sorted((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))
    queue_processor_called = {"called": False}
    model_called = {"called": False}

    def fail_if_queue_processor_runs(*_args: object, **_kwargs: object) -> None:
        queue_processor_called["called"] = True
        raise AssertionError("queue processor must not run from ops dashboard")

    def fail_if_model_is_constructed(*_args: object, **_kwargs: object) -> None:
        model_called["called"] = True
        raise AssertionError("vision display must not construct Ollama clients")

    def fail_if_vision_processing_runs(*_args: object, **_kwargs: object) -> None:
        model_called["called"] = True
        raise AssertionError("vision display must not process photos")

    monkeypatch.setattr(queue_processor_main, "process_all", fail_if_queue_processor_runs)
    monkeypatch.setattr(field_photo_vision, "OllamaVisionClient", fail_if_model_is_constructed)
    monkeypatch.setattr(field_photo_vision, "process_photo_assets", fail_if_vision_processing_runs)

    request_text("GET", "/", runtime_root)
    request_text("GET", "/api/status", runtime_root)
    request_text("GET", "/field-capture/review", runtime_root)
    request_text("GET", "/captures?capture_id=cap-photo-2026-05-03T18-25-20-04-00", runtime_root)

    after_sidecars = sorted(field_photo_vision.default_photo_vision_dir(runtime_root).glob("*.json"))
    after_candidates = sorted((runtime_root / "reviews" / "action_candidates" / "field_capture").glob("*.json"))
    assert before_sidecars == after_sidecars
    assert before_candidates == after_candidates
    assert not (runtime_root / "queue").exists()
    assert not (runtime_root / "reviews" / "approved_job_drafts").exists()
    assert sentinel.read_text(encoding="utf-8") == "do not touch\n"
    assert queue_processor_called["called"] is False
    assert model_called["called"] is False


def test_ops_dashboard_rejects_post(tmp_path: Path) -> None:
    status_code, content_type, body = request_text("POST", "/", tmp_path / "runtime")
    assert status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert "application/json" in content_type
    assert json.loads(body) == {"error": "read_only"}

    status_code, content_type, body = request_text("POST", "/api/status", tmp_path / "runtime")
    assert status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert "application/json" in content_type
    assert json.loads(body) == {"error": "read_only"}


def test_ops_dashboard_cli_defaults_and_startup_text(tmp_path: Path) -> None:
    args = parse_args(["ops-dashboard"])
    assert args.host == "127.0.0.1"
    assert args.port == 8765

    lines = startup_lines(args.host, args.port, tmp_path / "runtime")
    text = "\n".join(lines)
    assert "http://127.0.0.1:8765/" in text
    assert "Host: 127.0.0.1" in text
    assert "Port: 8765" in text
    assert "Runtime root:" in text
    assert "Operator UI: inbox, candidates, drafts, failed jobs, captures, health, and help." in text
    assert "No direct vault writes and no queue processor invocation." in text
    assert "POST actions may stage queue jobs" in text
    assert "Press Ctrl-C to stop." in text


def test_query_processed_asset_ids_paginates_beyond_page_limit(monkeypatch) -> None:
    """btq_photo_vision past the page size must not truncate the processed set
    (else processed photos wrongly show as pending)."""
    pages = [
        {"docs": [{"photo_asset_id": f"a{i}"} for i in range(5000)], "bookmark": "bm1"},
        {"docs": [{"photo_asset_id": f"b{i}"} for i in range(12)], "bookmark": "bm2"},
    ]
    calls = {"n": 0}

    def fake_query(_config, _mango):
        i = calls["n"]
        calls["n"] += 1
        return pages[i]

    monkeypatch.setattr("field_capture.photo_vision_couchdb.query_photo_vision", fake_query)
    ids = field_photos._query_processed_asset_ids(object())
    assert calls["n"] == 2  # paginated past the first full page
    assert len(ids) == 5012
    assert "a4999" in ids and "b11" in ids
