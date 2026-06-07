from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from transcription_pipeline.main import build_fingerprint, process_audio_file


class TranscriptionPipelineSidecarTests(unittest.TestCase):
    def test_personal_journal_with_sidecar_uses_full_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "vm-2026-05-10T17-20-23-2bb626.webm"
            audio.write_bytes(b"audio")
            local_root = root / "local"
            archive_dir = root / "archive"
            runtime_dir = root / "runtime"
            sidecar = {
                "source": "voice_memo",
                "capture_id": "vm-2026-05-10T17-20-23-2bb626",
                "routing_flag": "personal_journal",
                "mode": "personal",
            }

            status, _transcript, _events, jobs = process_audio_file(
                build_fingerprint(audio),
                local_root,
                logging.getLogger("test.sidecar.personal"),
                lambda _path: "anything I want to record",
                archive_dir=archive_dir,
                local_runtime_dir=runtime_dir,
                sidecar_metadata=sidecar,
            )

            self.assertEqual(status, "success")
            self.assertEqual(jobs, 1)
            [job_path] = list((local_root / "queue_jobs").glob("job_personal_*.json"))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["payload"]["body"], "anything I want to record")
            self.assertEqual(job["metadata"]["capture_id"], "vm-2026-05-10T17-20-23-2bb626")

    def test_no_sidecar_existing_trigger_path_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "legacy.m4a"
            audio.write_bytes(b"audio")
            local_root = root / "local"
            archive_dir = root / "archive"
            runtime_dir = root / "runtime"

            status, _transcript, _events, jobs = process_audio_file(
                build_fingerprint(audio),
                local_root,
                logging.getLogger("test.sidecar.legacy"),
                lambda _path: "personal journal: today I wrote the old trigger path",
                archive_dir=archive_dir,
                local_runtime_dir=runtime_dir,
            )

            self.assertEqual(status, "success")
            self.assertEqual(jobs, 1)
            [job_path] = list((local_root / "queue_jobs").glob("job_personal_*.json"))
            job = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(job["payload"]["body"], "today I wrote the old trigger path")
            self.assertTrue(job["metadata"]["capture_id"].startswith("cap-"))


if __name__ == "__main__":
    unittest.main()
