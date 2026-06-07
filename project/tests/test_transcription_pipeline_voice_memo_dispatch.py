from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transcription_pipeline import main as pipeline


def write_event(output_root: Path, payload: dict, name: str = "event.json") -> Path:
    event_dir = output_root / "events_valid"
    event_dir.mkdir(parents=True, exist_ok=True)
    path = event_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def sidecar(**overrides) -> dict:
    payload = {
        "source": "voice_memo",
        "capture_id": "vm-test-123",
        "routing_flag": "site_tagged",
        "site_id": "7060",
        "site_account": "Contworks",
        "site_location": "Continental Metalworks",
        "employee_slugs": [],
        "employee_names": [],
    }
    payload.update(overrides)
    return payload


class VoiceMemoDispatchTests(unittest.TestCase):
    def test_site_tagged_augments_extracted_events_with_site_id(self) -> None:
        event = {"event_id": "evt-1", "type": "site_observation", "site": None, "details": "check", "source_excerpt": "check"}
        augmented = pipeline.augment_event_with_voice_memo_sidecar(
            event,
            sidecar(),
            transcript_text="full transcript",
            transcript_path=Path("/tmp/transcript.txt"),
            audio_file_name="vm-test-123.webm",
        )
        self.assertEqual(augmented["site_id"], "7060")
        self.assertEqual(augmented["site"], "Contworks - Continental Metalworks")
        self.assertEqual(augmented["site_source"], "voice_memo_picker")

    def test_employee_tagged_attaches_employees_array_to_events(self) -> None:
        augmented = pipeline.augment_event_with_voice_memo_sidecar(
            {"event_id": "evt-1", "type": "interview_note"},
            sidecar(routing_flag="employee_tagged", site_id="", employee_slugs=["hutton-maria"], employee_names=["Maria Hutton"]),
            transcript_text="full transcript",
            transcript_path=Path("/tmp/transcript.txt"),
            audio_file_name="vm-test-123.webm",
        )
        self.assertEqual(
            augmented["employees"],
            [{"slug": "hutton-maria", "name": "Maria Hutton", "source": "voice_memo_picker"}],
        )

    def test_geolocation_passthrough(self) -> None:
        geo = {"lat": 40.41, "lng": -78.83, "accuracy_m": 12}
        augmented = pipeline.augment_event_with_voice_memo_sidecar(
            {"event_id": "evt-1", "type": "site_observation"},
            sidecar(geolocation=geo),
            transcript_text="full transcript",
            transcript_path=Path("/tmp/transcript.txt"),
            audio_file_name="vm-test-123.webm",
        )
        self.assertEqual(augmented["geolocation"], geo)

    def test_extractor_produces_no_events_synthesizes_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "memo.webm"
            audio.write_bytes(b"audio")

            def fake_process(_transcript_path, _output_root, capture_id=None):
                self.assertEqual(capture_id, "vm-test-123")
                return [], [], []

            with patch.object(pipeline, "process_transcript", side_effect=fake_process):
                status, _transcript, events, jobs = pipeline.process_audio_file(
                    pipeline.build_fingerprint(audio),
                    root / "local",
                    logging.getLogger("test.vm.dispatch.fallback"),
                    lambda _path: "dispatch fallback transcript",
                    archive_dir=root / "archive",
                    local_runtime_dir=root / "runtime",
                    sidecar_metadata=sidecar(),
                )

            self.assertEqual(status, "success")
            self.assertEqual(events, 1)
            self.assertEqual(jobs, 1)
            [event_path] = list((root / "local" / "events_valid").glob("*.json"))
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(event["type"], "voice_memo_observation")
            self.assertEqual(event["site_id"], "7060")
            self.assertEqual(event["voice_memo_capture_id"], "vm-test-123")

    def test_personal_journal_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "memo.webm"
            audio.write_bytes(b"audio")
            with patch.object(pipeline, "process_transcript") as fake_process:
                status, _transcript, events, jobs = pipeline.process_audio_file(
                    pipeline.build_fingerprint(audio),
                    root / "local",
                    logging.getLogger("test.vm.dispatch.personal"),
                    lambda _path: "this should be full body",
                    archive_dir=root / "archive",
                    local_runtime_dir=root / "runtime",
                    sidecar_metadata=sidecar(routing_flag="personal_journal"),
                )

            fake_process.assert_not_called()
            self.assertEqual(status, "success")
            self.assertEqual(events, 0)
            self.assertEqual(jobs, 1)

    def test_no_sidecar_path_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "memo.m4a"
            audio.write_bytes(b"audio")

            def fake_process(_transcript_path, output_root, capture_id=None):
                event_path = write_event(
                    output_root,
                    {"event_id": "evt-1", "type": "interview_note", "details": "regular note", "source_excerpt": "regular note", "timestamp": "2026-05-10T00:00:00Z"},
                )
                return [event_path], [], []

            with patch.object(pipeline, "process_transcript", side_effect=fake_process):
                pipeline.process_audio_file(
                    pipeline.build_fingerprint(audio),
                    root / "local",
                    logging.getLogger("test.vm.dispatch.nosidecar"),
                    lambda _path: "regular note",
                    archive_dir=root / "archive",
                    local_runtime_dir=root / "runtime",
                )

            [event_path] = list((root / "local" / "events_valid").glob("*.json"))
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertNotIn("voice_memo_capture_id", event)
            self.assertNotIn("site_id", event)


if __name__ == "__main__":
    unittest.main()
