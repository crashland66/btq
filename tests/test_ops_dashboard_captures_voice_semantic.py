"""Behavioral tests for prompt 293: the read-only "Voice note & transcript"
and "Semantic results" sections on the ops-dashboard capture detail page.

These are authored independently of the implementation under test
(captures.render_detail). They drive render_detail against a temp runtime
seeded with a real audio asset plus transcript/semantic artifacts and assert
on the rendered HTML, including graceful degradation when artifacts are
missing or there is no audio at all.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from field_capture import audio_semantics, audio_transcription
from ops_dashboard.sections import captures
from processing_core.artifacts import write_json_object

# Reuse the existing capture-seeding helper so the fixture matches the real
# intake/upload layout that discover_audio_assets expects.
from tests.test_ops_dashboard_captures import seed_capture, seed_links


def _make_ctx(runtime_root: Path) -> SimpleNamespace:
    """Minimal ctx: render_detail only reads ctx.runtime_root."""
    return SimpleNamespace(runtime_root=runtime_root.expanduser().resolve(strict=False))


def _discover_audio_asset_id(runtime_root: Path, capture_id: str) -> str:
    """Resolve the real deterministic audio_asset_id the pipeline would key
    transcript/semantic artifacts under, by discovering the asset the same way
    the section code does."""
    assets = captures.audio_assets_for_capture(runtime_root.expanduser().resolve(strict=False), capture_id)
    assert assets, "expected seed_capture(audio=True) to produce a discoverable audio asset"
    return assets[0].audio_asset_id


def _write_transcript(runtime_root: Path, audio_asset_id: str, raw_text: str) -> None:
    root = runtime_root.expanduser().resolve(strict=False)
    transcript_dir = audio_transcription.default_transcript_dir(root)
    path = audio_transcription.transcript_path_for(transcript_dir, audio_asset_id)
    write_json_object(
        path,
        {
            "type": "field_audio_transcript",
            "audio_asset_id": audio_asset_id,
            "status": "completed",
            "raw_text": raw_text,
        },
    )


def _write_semantic(runtime_root: Path, audio_asset_id: str) -> None:
    root = runtime_root.expanduser().resolve(strict=False)
    semantic_dir = audio_semantics.default_semantic_dir(root)
    path = audio_semantics.semantic_path_for(semantic_dir, audio_asset_id)
    write_json_object(
        path,
        {
            "type": "field_audio_semantics",
            "audio_asset_id": audio_asset_id,
            "status": "completed",
            "operational_summary": "SUMMARY_OPERATIONAL_MARKER toilet overflowing in north restroom",
            "cleaned_internal_note": "CLEANED_INTERNAL_MARKER note body",
            "client_safe_note": "CLIENT_SAFE_MARKER note body",
            "extracted_actions": [
                {"action": "ACTION_DISPATCH_MARKER plumber", "priority": "high"},
            ],
            "issue_detected": True,
            "issue_type": "ISSUE_TYPE_PLUMBING_MARKER",
            "urgency": "URGENCY_HIGH_MARKER",
            "visit_proposed": True,
            "visit_type": "VISIT_EMERGENCY_MARKER",
            "suggested_tags": ["TAG_PLUMBING_MARKER", "TAG_URGENT_MARKER"],
        },
    )


# --------------------------------------------------------------------------
# Happy path: transcript + semantic artifacts present
# --------------------------------------------------------------------------


def test_capture_detail_renders_voice_transcript_section(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_voice", photo=True, audio=True)
    asset_id = _discover_audio_asset_id(runtime_root, "cap_voice")
    raw_text = "RAW_TRANSCRIPT_MARKER the toilet is overflowing & needs help <now>"
    _write_transcript(runtime_root, asset_id, raw_text)

    body = captures.render_detail(_make_ctx(runtime_root), "cap_voice")

    # Heading present.
    assert "Voice note & transcript" in body
    # Recognizable transcript text appears, HTML-escaped (note the & and <>).
    assert "RAW_TRANSCRIPT_MARKER" in body
    assert "overflowing &amp; needs help &lt;now&gt;" in body
    # The raw unescaped form must NOT appear (proves output is escaped).
    assert "needs help <now>" not in body
    # Audio player rendered from the asset media url.
    assert "<audio" in body
    assert "/media/2026-05-03/cap_voice/voice.webm" in body


def test_capture_detail_renders_semantic_results_section(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_sem", photo=True, audio=True)
    asset_id = _discover_audio_asset_id(runtime_root, "cap_sem")
    _write_transcript(runtime_root, asset_id, "transcript text body")
    _write_semantic(runtime_root, asset_id)

    body = captures.render_detail(_make_ctx(runtime_root), "cap_sem")

    assert "Semantic results" in body
    assert "SUMMARY_OPERATIONAL_MARKER" in body
    assert "ACTION_DISPATCH_MARKER plumber" in body
    assert "ISSUE_TYPE_PLUMBING_MARKER" in body
    assert "URGENCY_HIGH_MARKER" in body
    assert "VISIT_EMERGENCY_MARKER" in body
    assert "TAG_PLUMBING_MARKER" in body


# --------------------------------------------------------------------------
# Graceful degradation
# --------------------------------------------------------------------------


def test_capture_detail_no_audio_shows_no_voice_note(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_noaudio", photo=True, audio=False)

    # No crash, and both new sections render an empty/"no voice note" state.
    body = captures.render_detail(_make_ctx(runtime_root), "cap_noaudio")

    assert "Voice note & transcript" in body
    assert "Semantic results" in body
    assert "No voice note." in body
    # No audio element should be emitted when there is no audio asset.
    assert "<audio" not in body


def test_capture_detail_audio_but_missing_transcript_and_semantic(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_pending", photo=True, audio=True)
    # Confirm the asset is discoverable but DO NOT write transcript/semantic.
    _discover_audio_asset_id(runtime_root, "cap_pending")

    body = captures.render_detail(_make_ctx(runtime_root), "cap_pending")

    # Audio is present so the section renders a card + player...
    assert "Voice note & transcript" in body
    assert "<audio" in body
    # ...but degrades to pending states rather than crashing.
    assert "Transcript pending." in body
    assert "Semantic pending." in body


# --------------------------------------------------------------------------
# No regression: existing sections still render alongside the new ones
# --------------------------------------------------------------------------


def test_capture_detail_existing_sections_still_render(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_full", photo=True, audio=True)
    candidate_id, _draft_id = seed_links(runtime_root, "cap_full")
    asset_id = _discover_audio_asset_id(runtime_root, "cap_full")
    _write_transcript(runtime_root, asset_id, "transcript text body")
    _write_semantic(runtime_root, asset_id)

    body = captures.render_detail(_make_ctx(runtime_root), "cap_full")

    # Existing capture-detail sections.
    assert "<h3>Photos</h3>" in body
    assert "<h3>Action candidates</h3>" in body
    assert f"/candidates?candidate_id={candidate_id}" in body
    # New sections appear between Photos and Action candidates.
    photos_idx = body.index("<h3>Photos</h3>")
    voice_idx = body.index("Voice note & transcript")
    semantic_idx = body.index("Semantic results")
    candidates_idx = body.index("<h3>Action candidates</h3>")
    assert photos_idx < voice_idx < semantic_idx < candidates_idx


# --------------------------------------------------------------------------
# Prompt 297: "Capture details" field-group panel on the capture detail view
# --------------------------------------------------------------------------


def test_capture_detail_renders_capture_details_panel(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_panel", date="2026-05-03", site_id="7050", photo=True, audio=True)
    candidate_id, _draft_id = seed_links(runtime_root, "cap_panel")
    asset_id = _discover_audio_asset_id(runtime_root, "cap_panel")
    _write_transcript(runtime_root, asset_id, "transcript text body")
    _write_semantic(runtime_root, asset_id)

    body = captures.render_detail(_make_ctx(runtime_root), "cap_panel")

    # A "Capture details" field-group panel (record_section markup) is present.
    assert "<h3>Capture details</h3>" in body
    panel_start = body.index("<h3>Capture details</h3>")
    panel = body[panel_start : panel_start + 2000]
    assert '<dl class="fields">' in panel

    # Capture-level record fields rendered as field-rows inside the panel.
    assert '<div class="field-row"><dt>Capture Id</dt><dd>cap_panel</dd></div>' in panel
    assert '<div class="field-row"><dt>Site</dt><dd>7050</dd></div>' in panel
    assert '<div class="field-row"><dt>Area</dt><dd>Restrooms</dd></div>' in panel
    assert '<div class="field-row"><dt>Submitter</dt><dd>Alice Example</dd></div>' in panel
    assert '<div class="field-row"><dt>Captured At</dt><dd>2026-05-03T12:00:00-04:00</dd></div>' in panel
    # Counts (including zero-safe ints) appear.
    assert "<dt>Photo Count</dt>" in panel
    assert "<dt>Candidate Count</dt>" in panel
    assert "<dt>Sidecar Count</dt>" in panel


def test_capture_details_panel_precedes_other_sections(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_order2", photo=True, audio=True)
    asset_id = _discover_audio_asset_id(runtime_root, "cap_order2")
    _write_transcript(runtime_root, asset_id, "transcript text body")
    _write_semantic(runtime_root, asset_id)

    body = captures.render_detail(_make_ctx(runtime_root), "cap_order2")

    # Panel sits above Photos / Voice / Semantic, all of which still render.
    details_idx = body.index("<h3>Capture details</h3>")
    photos_idx = body.index("<h3>Photos</h3>")
    voice_idx = body.index("Voice note & transcript")
    semantic_idx = body.index("Semantic results")
    candidates_idx = body.index("<h3>Action candidates</h3>")
    assert details_idx < photos_idx < voice_idx < semantic_idx < candidates_idx
    # Section headers normalized to <h3>, none left as <h2>.
    assert "<h2>Photos</h2>" not in body
    assert "<h2>Voice note &amp; transcript</h2>" not in body
    assert "<h2>Semantic results</h2>" not in body
