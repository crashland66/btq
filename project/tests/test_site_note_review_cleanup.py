"""Gating tests for 380 — strip_review_blocks (site-note review-block cleanup).

Validated separately byte-for-byte against the real polluted location_1337 content;
this fixture is the future regression guard: a mixed chronological log with operator
entries interleaved among BOTH auto-block formats and a voice-memo raw_transcript leak.
"""

import unittest

from field_capture.site_note_review_cleanup import strip_review_blocks

MIXED_LOG = """# Liberty Wire

## Operational Notes

### Internal Notes
⚠️ Filthy place — keep this operator note.

## Field Capture Review
- field_capture_timestamp: 2026-05-01T00:00:00Z
- site_id: 1337
Source semantic artifact: /Users/gstoltz/btq_runtime/field_capture/audio_semantics/a.json
Source transcript artifact: /Users/gstoltz/btq_runtime/field_capture/audio_transcripts/a.json

### 2026-05-13T22:21:24Z — voice memo

note:
QC completed in locker rooms

raw_transcript: /Users/gstoltz/btq/local/audio_processing/vm-1.webm

transcript:
> Real operator transcript that must survive.

## 2026-06-04 - Recognition: Ishmael holding the site

Keep this recognition entry.

## 2026-06-04 - Staffing: Scott reassigned

## Field Capture Review
- field_capture_timestamp: 2026-05-04T00:00:00Z
- site_id: 1337
Source semantic artifact: /Users/gstoltz/btq_runtime/field_capture/semantics/c.json

---
type: visit_gap
site: Liberty Wire
date: 2026-05-05
reason: "event_without_visit"
---

## Field Capture Reviews

### Field Capture Review - 2026-05-16T04:10:54Z
- site_id: 1337
- area: Restrooms
Summary: Review the field audio note and decide whether follow-up is needed.
Source semantic artifact: /Users/gstoltz/btq_runtime/field_capture/semantics/b.json
"""


class StripReviewBlocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cleaned = strip_review_blocks(MIXED_LOG)

    def test_all_review_blocks_removed_both_formats(self) -> None:
        self.assertNotIn("Field Capture Review", self.cleaned)  # singular AND plural

    def test_no_filesystem_path_leak_remains(self) -> None:
        self.assertNotIn("/Users/", self.cleaned)
        self.assertNotIn("raw_transcript:", self.cleaned)
        self.assertNotIn("Source semantic artifact", self.cleaned)

    def test_all_operator_content_preserved(self) -> None:
        for kept in [
            "## Operational Notes",
            "Filthy place — keep this operator note.",
            "QC completed in locker rooms",
            "Real operator transcript that must survive.",
            "— voice memo",
            "## 2026-06-04 - Recognition: Ishmael holding the site",
            "Keep this recognition entry.",
            "## 2026-06-04 - Staffing: Scott reassigned",
        ]:
            self.assertIn(kept, self.cleaned, f"operator content lost: {kept!r}")

    def test_visit_gap_between_review_blocks_preserved(self) -> None:
        # regression: a headingless visit_gap frontmatter sandwiched between two review
        # blocks must NOT be swallowed (the dry-run caught this on real 1337).
        self.assertIn("type: visit_gap", self.cleaned)
        self.assertIn('reason: "event_without_visit"', self.cleaned)
        self.assertIn("date: 2026-05-05", self.cleaned)

    def test_idempotent(self) -> None:
        self.assertEqual(strip_review_blocks(self.cleaned), self.cleaned)

    def test_clean_doc_unchanged(self) -> None:
        clean = "# Site\n\n## Operational Notes\n\nNothing to strip.\n"
        self.assertEqual(strip_review_blocks(clean), clean)


if __name__ == "__main__":
    unittest.main()
