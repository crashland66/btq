"""Independent verifier gating tests for prompt 376 — surfacing human-entered
per-photo notes on the ops-dashboard capture photo cards.

These tests were authored by the INDEPENDENT VERIFIER (not the implementer, who
shipped no tests). They cover four behaviours:

  1. render_photo_card with a record carrying ``note`` renders the escaped note
     under a "Photo note" label.
  2. A record with no note renders no note block / label.
  3. A note containing ``<script>...</script>`` is HTML-escaped, not raw.
  4. INTEGRATION: build the filesystem intake artifact via the REAL writer
     (event_pipeline.couchdb_capture_adapter.import_couchdb_capture — the
     CouchDB -> filesystem projection that materialises ``field_capture/intake``
     ``photo_capture`` artifacts), with a CouchDB doc whose ``photos[].note`` is
     set the way a native-app submission stores it, then assert
     photo_card_records_for_capture() resolves that note onto the right photo's
     render record.

Mutation note: tests 1-3 fail if ``_photo_note_html`` is stubbed to return "";
test 4 fails if ``_photo_note_lookup`` / ``_photo_note_for_asset`` are stubbed.
Confirmed by the verifier against a reverted baseline.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from event_pipeline.couchdb_capture_adapter import import_couchdb_capture
from ops_dashboard.sections import captures


# ---------------------------------------------------------------------------
# Unit: render_photo_card note rendering
# ---------------------------------------------------------------------------

def test_render_photo_card_renders_note_with_label() -> None:
    record = {
        "status": "completed",
        "submitted_area": "Restrooms",
        "captured_at": "2026-05-09T14:30:00Z",
        "description": "Floor mopped.",
        "note": "Overflowing restroom trash",
    }
    html_out = captures.render_photo_card(record)
    assert "Photo note" in html_out
    assert "Overflowing restroom trash" in html_out
    # The note label sits ABOVE the machine vision description prose.
    assert html_out.index("Photo note") < html_out.index("Floor mopped.")


def test_render_photo_card_multiline_note_uses_br() -> None:
    record = {"status": "completed", "note": "line one\nline two", "description": "x"}
    html_out = captures.render_photo_card(record)
    assert "line one<br>line two" in html_out


def test_render_photo_card_without_note_renders_no_label() -> None:
    record = {
        "status": "completed",
        "submitted_area": "Restrooms",
        "captured_at": "2026-05-09T14:30:00Z",
        "description": "Floor mopped.",
    }
    html_out = captures.render_photo_card(record)
    assert "Photo note" not in html_out


def test_render_photo_card_blank_note_renders_no_label() -> None:
    record = {"status": "completed", "note": "   ", "description": "x"}
    html_out = captures.render_photo_card(record)
    assert "Photo note" not in html_out


def test_render_photo_card_escapes_script_injection_in_note() -> None:
    payload = '<script>alert("xss")</script>'
    record = {"status": "completed", "note": payload, "description": "x"}
    html_out = captures.render_photo_card(record)
    # Raw script tag must NOT survive into the output.
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


# ---------------------------------------------------------------------------
# Integration: realistic capture written by the REAL intake-artifact writer
# ---------------------------------------------------------------------------

REMOTE_PHOTO_A = "/srv/btq/runtime/uploads/cap-note/photo_001.jpg"
REMOTE_PHOTO_B = "/srv/btq/runtime/uploads/cap-note/photo_002.jpg"


class _CopyingRunner:
    """Stands in for ssh/scp; materialises the copied photo bytes locally so the
    adapter's idempotency + media checks pass, exactly like the production path.
    """

    def __init__(self) -> None:
        self.sizes = {REMOTE_PHOTO_A: 5, REMOTE_PHOTO_B: 5}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "ssh":
            return subprocess.CompletedProcess(args, 0, stdout="5\n", stderr="")
        if args[0] == "scp":
            local_path = Path(args[-1])
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"photo")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)


class _FakeRegistry:
    def resolve_canonical(self, site_id: str) -> str:
        return "Summit Wire"


def _couch_doc_with_per_photo_notes() -> dict[str, Any]:
    """A field_capture CouchDB document shaped the way a native-app submission
    persists it: unified_capture.apply_photo_notes() set ``note`` on the
    per-photo records that capture_ingest stored at doc["photos"][i]["note"].
    """
    return {
        "_id": "cap-note",
        "_rev": "2-claimed",
        "type": "field_capture",
        "capture_id": "cap-note",
        "site_id": "7050",
        "person_id": "person-1",
        "person_name": "Person One",
        "captured_at": "2026-05-09T14:30:00Z",
        "qc_category": "completed_area",
        "note": "Capture-level note.",
        "photos": [
            {
                "filename": "photo_001.jpg",
                "mime_type": "image/jpeg",
                "stored_path": REMOTE_PHOTO_A,
                "note": "Overflowing restroom trash",
            },
            {
                "filename": "photo_002.jpg",
                "mime_type": "image/jpeg",
                "stored_path": REMOTE_PHOTO_B,
                # second photo intentionally has NO note
            },
        ],
        "processing_state": "claimed",
    }


def _materialise_capture(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime"
    import_couchdb_capture(
        doc=_couch_doc_with_per_photo_notes(),
        runtime_root=runtime_root,
        remote_host="vps.example",
        registry=_FakeRegistry(),
        runner=_CopyingRunner(),
    )
    return runtime_root.expanduser().resolve(strict=False)


def test_real_intake_artifact_carries_per_photo_note(tmp_path: Path) -> None:
    """Pre-condition for the feature: the per-photo note must actually reach the
    on-disk intake artifact. If this regresses, 376 renders nothing for real
    native-app captures.
    """
    runtime_root = _materialise_capture(tmp_path)
    intake = json.loads(
        (runtime_root / "field_capture" / "intake" / "cap-note.json").read_text(encoding="utf-8")
    )
    photos = intake["payload"]["photos"]
    by_name = {p["filename"]: p for p in photos}
    assert by_name["photo_001.jpg"].get("note") == "Overflowing restroom trash"
    assert "note" not in by_name["photo_002.jpg"]


def test_photo_card_records_resolve_per_photo_note_from_real_artifact(tmp_path: Path) -> None:
    runtime_root = _materialise_capture(tmp_path)
    records = captures.photo_card_records_for_capture(runtime_root, "cap-note")

    by_filename: dict[str, dict[str, object]] = {}
    for record in records:
        source = str(record.get("source_image_path") or record.get("image_media_url") or "")
        by_filename[Path(source).name] = record

    assert "photo_001.jpg" in by_filename, f"records: {records}"
    assert by_filename["photo_001.jpg"].get("note") == "Overflowing restroom trash"
    # The note must land on the RIGHT photo only.
    assert "photo_002.jpg" in by_filename
    assert not str(by_filename["photo_002.jpg"].get("note") or "").strip()


def test_rendered_card_shows_note_for_real_artifact(tmp_path: Path) -> None:
    runtime_root = _materialise_capture(tmp_path)
    records = captures.photo_card_records_for_capture(runtime_root, "cap-note")
    rendered = "".join(captures.render_photo_card(record) for record in records)
    assert "Photo note" in rendered
    assert "Overflowing restroom trash" in rendered
