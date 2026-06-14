"""Gating tests for 377 — per-photo notes in the unified Field Capture PWA.

The PWA JS is not executed in the pytest suite, so these gate the static
capture/upload contract the way the other SPA-stage tests do (assert on the
served asset content). The backend side (photo_notes_json -> photos[].note) is
already covered by test_unified_capture.py; these prove the unified PWA emits
that exact contract and exposes an accessible per-photo note control.
"""

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_asset(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


class UnifiedPwaPerPhotoNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = read_asset("unified_capture/public/index.html")
        self.app_js = read_asset("unified_capture/public/app.js")

    # --- capture UI (present + accessible) ------------------------------- #

    def test_per_photo_note_template_present(self) -> None:
        self.assertIn('<template id="photoNoteTemplate">', self.html)
        self.assertIn('class="photo-note-input"', self.html)

    def test_per_photo_note_input_has_accessibility_label(self) -> None:
        # the lazy-greybeard floor: accessibility is never trimmed
        self.assertIn('aria-label="Note for this photo"', self.html)

    # --- offline persistence --------------------------------------------- #

    def test_note_persisted_into_queued_record(self) -> None:
        # the per-photo note rides the queued record.photos[] entry
        self.assertIn("note: photo.note", self.app_js)

    # --- upload contract (must mirror the native client exactly) ---------- #

    def test_upload_builds_native_photo_notes_json_shape(self) -> None:
        # native CaptureAPIClient emits [{index, filename, note}]; mirror it
        self.assertIn("notes.push({ index, filename: photo.filename, note })", self.app_js)
        self.assertIn('form.append("photo_notes_json"', self.app_js)

    def test_upload_omits_empty_notes(self) -> None:
        # only non-empty trimmed notes are sent
        self.assertIn('(photo.note || "").trim()', self.app_js)


if __name__ == "__main__":
    unittest.main()
