from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice_memo import couchdb_watcher


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class FakeListener:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        self.states: list[str] = [str(doc.get("processing_state") or "") for doc in docs]
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str]] = []

    def listen(self):
        yield from self.docs

    def mark_processing(self, doc: dict, state: str = couchdb_watcher.PROCESSING_STATE) -> dict:
        self.states.append(state)
        updated = dict(doc)
        updated["processing_state"] = state
        updated["_rev"] = "2-claimed"
        return updated

    def mark_complete(self, doc: dict, state: str = couchdb_watcher.COMPLETE_STATE) -> None:
        self.states.append(state)
        self.completed.append((str(doc.get("capture_id") or doc.get("_id")), state))

    def mark_failed(self, doc: dict, reason: str, state: str = couchdb_watcher.FAILED_STATE) -> None:
        self.states.append(state)
        self.failed.append((str(doc.get("capture_id") or doc.get("_id")), reason, state))


class FakeFindListener(FakeListener):
    def __init__(self, docs: list[dict]) -> None:
        super().__init__(docs)
        self.find_calls: list[tuple[str, str, dict]] = []

    def _request_json(self, method: str, doc_id: str, payload: dict) -> dict:
        self.find_calls.append((method, doc_id, payload))
        return {"docs": self.docs[:1]}

    def stop(self) -> None:
        pass


def sample_doc(**overrides) -> dict:
    doc = {
        "_id": "vm-2026-05-10T17-20-23-2bb626",
        "_rev": "1-a",
        "capture_id": "vm-2026-05-10T17-20-23-2bb626",
        "processing_state": "pending",
        "audio_path": "2026/05/vm-2026-05-10T17-20-23-2bb626.webm",
        "audio_filename": "memo.webm",
        "routing_flag": "personal_journal",
        "mode": "personal",
        "captured_at": "2026-05-10T17:20:23Z",
        "duration_seconds": 43,
        "note": "private note",
        "site_id": None,
        "employee_slugs": [],
        "employee_names": [],
        "geolocation": {"lat": 40.41, "lng": -78.83, "accuracy_m": 12},
    }
    doc.update(overrides)
    return doc


class VoiceMemoCouchDBWatcherTests(unittest.TestCase):
    def test_default_database_is_btq_voice_memos(self) -> None:
        self.assertEqual(couchdb_watcher.DEFAULT_DATABASE, "btq_voice_memos")

    def test_watcher_marks_pending_doc_intake_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = FakeListener([sample_doc()])
            scp_calls: list[list[str]] = []

            def runner(command, **_kwargs):
                scp_calls.append(command)
                Path(command[-1]).write_bytes(b"audio")
                return FakeCompletedProcess()

            count = couchdb_watcher.run_listener(
                listener=listener,
                inbox_dir=Path(tmp),
                remote_host="deploy@example",
                runner=runner,
                dry_run=False,
                json_output=False,
                logger=couchdb_watcher.configure_logger(Path(tmp) / "watch.log"),
            )

            self.assertEqual(count, 1)
            self.assertEqual(listener.states, ["pending", "claimed", "intake_done"])
            self.assertEqual(listener.completed, [("vm-2026-05-10T17-20-23-2bb626", "intake_done")])
            self.assertEqual(len(scp_calls), 1)

    def test_watcher_writes_sidecar_with_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = FakeListener([sample_doc()])

            def runner(command, **_kwargs):
                Path(command[-1]).write_bytes(b"audio")
                return FakeCompletedProcess()

            couchdb_watcher.run_listener(
                listener=listener,
                inbox_dir=Path(tmp),
                remote_host="deploy@example",
                runner=runner,
                dry_run=False,
                json_output=False,
                logger=couchdb_watcher.configure_logger(Path(tmp) / "watch.log"),
            )

            sidecar = Path(tmp) / "vm-2026-05-10T17-20-23-2bb626.metadata.json"
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "voice_memo")
            self.assertEqual(payload["capture_id"], "vm-2026-05-10T17-20-23-2bb626")
            self.assertEqual(payload["routing_flag"], "personal_journal")
            self.assertEqual(payload["mode"], "personal")
            self.assertEqual(payload["captured_at"], "2026-05-10T17:20:23Z")
            self.assertEqual(payload["duration_seconds"], 43)
            self.assertEqual(payload["geolocation"]["accuracy_m"], 12)

    def test_sidecar_payload_includes_person_id_when_present(self) -> None:
        payload = couchdb_watcher.sidecar_payload(sample_doc(person_id="per_test001"))

        self.assertEqual(payload["person_id"], "per_test001")

    def test_sidecar_payload_omits_person_id_when_empty(self) -> None:
        payload = couchdb_watcher.sidecar_payload(sample_doc())

        self.assertNotIn("person_id", payload)

    def test_watcher_idempotent_on_existing_inbox_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp)
            (inbox / "vm-2026-05-10T17-20-23-2bb626.webm").write_bytes(b"audio")
            listener = FakeListener([sample_doc()])
            scp_calls: list[list[str]] = []

            def runner(command, **_kwargs):
                scp_calls.append(command)
                return FakeCompletedProcess()

            couchdb_watcher.run_listener(
                listener=listener,
                inbox_dir=inbox,
                remote_host="deploy@example",
                runner=runner,
                dry_run=False,
                json_output=False,
                logger=couchdb_watcher.configure_logger(inbox / "watch.log"),
            )

            self.assertEqual(scp_calls, [])
            self.assertEqual(listener.completed, [("vm-2026-05-10T17-20-23-2bb626", "intake_done")])

    def test_watcher_marks_failed_on_scp_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = FakeListener([sample_doc()])

            def runner(_command, **_kwargs):
                return FakeCompletedProcess(returncode=1, stderr="no such file")

            couchdb_watcher.run_listener(
                listener=listener,
                inbox_dir=Path(tmp),
                remote_host="deploy@example",
                runner=runner,
                dry_run=False,
                json_output=False,
                logger=couchdb_watcher.configure_logger(Path(tmp) / "watch.log"),
            )

            self.assertEqual(listener.failed[0][2], "intake_failed")
            self.assertIn("scp failed", listener.failed[0][1])

    def test_watcher_skips_doc_missing_audio_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            listener = FakeListener([sample_doc(audio_path="")])
            scp_calls: list[list[str]] = []

            def runner(command, **_kwargs):
                scp_calls.append(command)
                return FakeCompletedProcess()

            couchdb_watcher.run_listener(
                listener=listener,
                inbox_dir=Path(tmp),
                remote_host="deploy@example",
                runner=runner,
                dry_run=False,
                json_output=False,
                logger=couchdb_watcher.configure_logger(Path(tmp) / "watch.log"),
            )

            self.assertEqual(scp_calls, [])
            self.assertEqual(listener.failed[0][2], "intake_failed")
            self.assertIn("audio_path", listener.failed[0][1])

    def test_pending_once_listener_uses_find_without_waiting_for_changes_feed(self) -> None:
        listener = FakeFindListener([])
        active = couchdb_watcher._PendingOnceListener(listener, "processing_state")

        self.assertEqual(list(active.listen()), [])
        self.assertEqual(
            listener.find_calls,
            [("POST", "_find", {"selector": {"processing_state": "pending"}, "limit": 1})],
        )

    def test_pending_once_listener_yields_one_pending_doc(self) -> None:
        doc = sample_doc()
        listener = FakeFindListener([doc, sample_doc(capture_id="vm-other")])
        active = couchdb_watcher._PendingOnceListener(listener, "processing_state")

        self.assertEqual(list(active.listen()), [doc])


if __name__ == "__main__":
    unittest.main()
