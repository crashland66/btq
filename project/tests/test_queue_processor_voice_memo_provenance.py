from __future__ import annotations

import unittest

from queue_processor.idempotency import parse_frontmatter_text, upsert_job_id_frontmatter
from queue_processor.main import (
    render_personal_journal_entry,
    upsert_voice_memo_capture_id_frontmatter,
    voice_memo_capture_id,
)


class QueueProcessorVoiceMemoProvenanceTests(unittest.TestCase):
    def test_journal_entry_includes_voice_memo_capture_ids(self) -> None:
        payload = {
            "date": "2026-05-10",
            "timestamp": "2026-05-10T17:20:23+00:00",
            "audio_file": "vm-2026-05-10T17-20-23-2bb626.webm",
            "raw_transcript_path": "/tmp/vm-2026-05-10T17-20-23-2bb626.webm.whisper.txt",
            "body": "journal body",
        }
        rendered = render_personal_journal_entry(payload)
        with_job = upsert_job_id_frontmatter(rendered, "job-123")
        final = upsert_voice_memo_capture_id_frontmatter(with_job, "vm-2026-05-10T17-20-23-2bb626")

        frontmatter, body, has_frontmatter = parse_frontmatter_text(final)
        self.assertTrue(has_frontmatter)
        self.assertEqual(frontmatter["btq_job_ids"], ["job-123"])
        self.assertEqual(frontmatter["voice_memo_capture_ids"], ["vm-2026-05-10T17-20-23-2bb626"])
        self.assertIn("source_audio: vm-2026-05-10T17-20-23-2bb626.webm", body)

    def test_journal_entry_omits_voice_memo_field_for_legacy_capture_id(self) -> None:
        self.assertIsNone(voice_memo_capture_id({"capture_id": "cap-20260510T172023Z-abc"}))


if __name__ == "__main__":
    unittest.main()
