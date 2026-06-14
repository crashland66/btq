"""Gating tests for the capture-detail redesign (prompt 374).

Verifier-authored invariant tests driving captures.render_detail() against a
data-rich seeded capture and a sparse/unprocessed one. The contract:

  * "site — submitter" header + a compact meta-chip row (photo count · date ·
    status).
  * A photos-FIRST gallery: image + area + the vision description AS PROSE + a
    status chip. NO render_kv raw-key dump and NO raw ids on the card FACE.
  * A voice-note/transcript block (graceful "No voice note.").
  * A one-line friendly extraction summary (derived from candidates/drafts, not
    raw id lists).
  * Pipeline internals (Capture details / Action candidates / Drafts & queue /
    Semantic) inside a collapsed "Processing details" <details> — still present,
    one click away (don't-lose-info).
  * An unprocessed capture renders clean states (no 500).

Shared invariants: UI-only, don't-lose-information, degrade-gracefully (no 500),
names-not-ids.

Mutation checks (tamper the impl -> a gating test fails -> restore):
  1. Hoist the render_kv pipeline dump onto the card FACE (make render_photo_card
     append render_photo_processing_details) ->
     test_photo_card_is_prose_no_raw_kv_dump fails.
  2. Move the pipeline internals OUT of the collapsed <details> (have
     render_detail append the processing body bare instead of via _details_block)
     -> test_pipeline_internals_live_inside_collapsed_details fails.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import captures
from tests.test_ops_dashboard_captures import seed_capture, seed_links


def _ctx(runtime_root: Path) -> SimpleNamespace:
    return SimpleNamespace(runtime_root=runtime_root.expanduser().resolve(strict=False))


def _rich(tmp_path: Path) -> str:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_rich", date="2026-05-03", site_id="7050", photo=True, audio=True)
    seed_links(runtime_root, "cap_rich")
    return captures.render_detail(_ctx(runtime_root), "cap_rich")


# ---------------------------------------------------------------------------
# Header: "site — submitter" + compact meta-chip row
# ---------------------------------------------------------------------------

def test_header_site_submitter_and_meta_chip_row(tmp_path: Path) -> None:
    body = _rich(tmp_path)
    header = body[body.index("<header>"):body.index("</header>")]
    # site — submitter title (em-dash joined).
    assert "&mdash;" in header
    assert "Alice Example" in header  # submitter name in header
    # Compact meta-chip row: photo count · date · status pills.
    assert 'class="pill"' in header
    assert "photo" in header  # "N photo(s)" chip


# ---------------------------------------------------------------------------
# Photos-first gallery — prose, status chip, NO raw kv dump / NO raw ids on face
# ---------------------------------------------------------------------------

def test_photos_render_first(tmp_path: Path) -> None:
    body = _rich(tmp_path)
    photos_idx = body.index("<h3>Photos</h3>")
    voice_idx = body.index("Voice note & transcript")
    # Photos lead, before the voice block and before pipeline internals.
    assert photos_idx < voice_idx
    assert photos_idx < body.index("Processing details")


def test_photo_card_is_prose_no_raw_kv_dump(tmp_path: Path) -> None:
    # MUTATION GUARD: the card FACE shows prose (description + status chip), never
    # a render_kv raw-key dump and never raw ids.
    body = _rich(tmp_path)
    photos_section = body[body.index("<h3>Photos</h3>"):body.index("</section>", body.index("<h3>Photos</h3>"))]
    # Prose card present.
    assert "site-gallery-card" in photos_section
    assert 'class="pill' in photos_section  # status chip on the card
    # No render_kv table on the card face.
    assert "kv-table" not in photos_section
    # No raw photo_asset_id surfaced on the card face.
    assert "photo_asset_id" not in photos_section


# ---------------------------------------------------------------------------
# Voice / transcript block (graceful when absent)
# ---------------------------------------------------------------------------

def test_voice_block_present(tmp_path: Path) -> None:
    body = _rich(tmp_path)
    assert "Voice note & transcript" in body


def test_voice_block_degrades_gracefully_without_audio(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_noaudio", photo=True, audio=False)
    body = captures.render_detail(_ctx(runtime_root), "cap_noaudio")
    assert "Voice note & transcript" in body
    assert "No voice note." in body
    assert "<audio" not in body


# ---------------------------------------------------------------------------
# Friendly one-line extraction summary (not raw id lists)
# ---------------------------------------------------------------------------

def test_friendly_extraction_summary_one_line(tmp_path: Path) -> None:
    body = _rich(tmp_path)
    assert "<h3>Extraction summary</h3>" in body
    summary = body[body.index("<h3>Extraction summary</h3>"):body.index("</section>", body.index("<h3>Extraction summary</h3>"))]
    # A human sentence ("Extracted: N follow-up(s) proposed -> ..."), not a bare
    # candidate-id list.
    assert "Extracted:" in summary
    assert "proposed" in summary


# ---------------------------------------------------------------------------
# Pipeline internals live inside the collapsed "Processing details" <details>
# ---------------------------------------------------------------------------

def test_pipeline_internals_live_inside_collapsed_details(tmp_path: Path) -> None:
    # MUTATION GUARD: Capture details / Action candidates / render_kv dump must be
    # inside the collapsed Processing details <details>, one click away (still
    # present: don't-lose-info), not on the page surface.
    body = _rich(tmp_path)
    details_open = body.index('<details class="detail-block">')
    summary_idx = body.index("Processing details")
    assert details_open < summary_idx
    details_close = body.index("</details>", summary_idx)
    inner = body[details_open:details_close]
    # The pipeline internals are all inside the block.
    for marker in ("<h3>Capture details</h3>", "<h3>Action candidates</h3>", "Drafts and queue state", "kv-table"):
        assert marker in inner, f"{marker!r} must live inside Processing details"
    # And NOT bare above the collapsed block.
    above = body[:details_open]
    assert "<h3>Capture details</h3>" not in above
    assert "kv-table" not in above


def test_pipeline_internals_preserve_capture_fields(tmp_path: Path) -> None:
    # don't-lose-info: the raw capture fields are preserved (just relocated).
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_fields", date="2026-05-03", site_id="7050", photo=True, audio=True)
    seed_links(runtime_root, "cap_fields")
    body = captures.render_detail(_ctx(runtime_root), "cap_fields")
    details = body[body.index('<details class="detail-block">'):]
    assert "<dt>Capture Id</dt><dd>cap_fields</dd>" in details or '<th>capture_id</th>' in details
    assert "7050" in details
    assert "Photo Count" in details or "photo_count" in details


# ---------------------------------------------------------------------------
# Unprocessed capture renders clean states (no 500)
# ---------------------------------------------------------------------------

def test_unprocessed_capture_renders_clean_states_no_500(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_bare", photo=False, audio=False)
    body = captures.render_detail(_ctx(runtime_root), "cap_bare")
    # Degrade-gracefully: clean empty states, no crash.
    assert "No photos in this capture." in body
    assert "No voice note." in body
    # Friendly summary still renders its no-issues line.
    assert "<h3>Extraction summary</h3>" in body
    assert "No issues flagged" in body
    # Processing details block still present (empty-but-present), one click away.
    assert "Processing details" in body
