from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_adapter import CaptureAdapterError
from event_pipeline.couchdb_listener import CouchDBListenerError
from field_capture import couchdb_watcher


def null_logger() -> logging.Logger:
    logger = logging.getLogger("test.couchdb_watcher")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


class FakeListener:
    def __init__(self, docs: list[dict[str, object]]) -> None:
        self.docs = docs
        self.events: list[str] = []
        self.mark_processing_error: Exception | None = None

    def listen(self):
        return iter(self.docs)

    def mark_processing(self, doc: dict[str, object]) -> dict[str, object]:
        self.events.append(f"mark_processing:{doc['_id']}")
        if self.mark_processing_error is not None:
            error = self.mark_processing_error
            self.mark_processing_error = None
            raise error
        updated = dict(doc)
        updated["_rev"] = "2-processing"
        return updated

    def mark_complete(self, doc: dict[str, object]) -> None:
        self.events.append(f"mark_complete:{doc['_id']}")

    def mark_failed(self, doc: dict[str, object], reason: str) -> None:
        self.events.append(f"mark_failed:{doc['_id']}:{reason}")


class CouchDBWatcherTests(unittest.TestCase):
    def test_process_one_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            counts = {"copied": 1, "skipped": 0, "failed": 0, "would_copy": 0}
            with mock.patch(
                "field_capture.couchdb_watcher.import_couchdb_capture",
                return_value={"capture_id": "cap1", "ok": True, "counts": counts},
            ) as import_mock:
                result = couchdb_watcher.process_one(
                    doc={"_id": "cap1", "capture_id": "cap1"},
                    runtime_root=Path(tmp),
                    remote_host="btq-vps",
                    registry=mock.Mock(),
                    runner=mock.Mock(),
                    dry_run=False,
                    logger=null_logger(),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["counts"], counts)
            import_mock.assert_called_once()

    def test_process_one_adapter_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "field_capture.couchdb_watcher.import_couchdb_capture",
                side_effect=CaptureAdapterError("copy failed"),
            ):
                result = couchdb_watcher.process_one(
                    doc={"_id": "cap1", "capture_id": "cap1"},
                    runtime_root=Path(tmp),
                    remote_host="btq-vps",
                    registry=mock.Mock(),
                    runner=mock.Mock(),
                    dry_run=False,
                    logger=null_logger(),
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "copy failed")

    def test_run_listener_success_path(self) -> None:
        listener = FakeListener([{"_id": "cap1", "_rev": "1-claimed", "capture_id": "cap1"}])

        def fake_process_one(**_kwargs: object) -> dict[str, object]:
            listener.events.append("process_one:cap1")
            return {"capture_id": "cap1", "ok": True, "counts": {"copied": 1}, "error": ""}

        with mock.patch("field_capture.couchdb_watcher.process_one", side_effect=fake_process_one):
            couchdb_watcher.run_listener(
                listener=listener,
                runtime_root=Path("/tmp/runtime"),
                remote_host="btq-vps",
                registry=mock.Mock(),
                runner=mock.Mock(),
                dry_run=False,
                json_output=False,
                logger=null_logger(),
            )

        self.assertEqual(listener.events, ["mark_processing:cap1", "process_one:cap1", "mark_complete:cap1"])

    def test_run_listener_marks_failed_on_error(self) -> None:
        listener = FakeListener([{"_id": "cap1", "_rev": "1-claimed", "capture_id": "cap1"}])

        with mock.patch(
            "field_capture.couchdb_watcher.process_one",
            return_value={"capture_id": "cap1", "ok": False, "counts": {}, "error": "bad document"},
        ):
            couchdb_watcher.run_listener(
                listener=listener,
                runtime_root=Path("/tmp/runtime"),
                remote_host="btq-vps",
                registry=mock.Mock(),
                runner=mock.Mock(),
                dry_run=False,
                json_output=False,
                logger=null_logger(),
            )

        self.assertEqual(listener.events, ["mark_processing:cap1", "mark_failed:cap1:bad document"])
        self.assertNotIn("mark_complete:cap1", listener.events)

    def test_run_listener_retries_after_mark_processing_error(self) -> None:
        listener = FakeListener(
            [
                {"_id": "cap1", "_rev": "1-claimed", "capture_id": "cap1"},
                {"_id": "cap2", "_rev": "1-claimed", "capture_id": "cap2"},
            ]
        )
        listener.mark_processing_error = CouchDBListenerError("conflict")

        with mock.patch(
            "field_capture.couchdb_watcher.process_one",
            side_effect=lambda **kwargs: {
                "capture_id": kwargs["doc"]["capture_id"],
                "ok": True,
                "counts": {},
                "error": "",
            },
        ) as process_mock:
            couchdb_watcher.run_listener(
                listener=listener,
                runtime_root=Path("/tmp/runtime"),
                remote_host="btq-vps",
                registry=mock.Mock(),
                runner=mock.Mock(),
                dry_run=False,
                json_output=False,
                logger=null_logger(),
            )

        self.assertEqual(process_mock.call_count, 2)
        self.assertEqual(
            listener.events,
            [
                "mark_processing:cap1",
                "mark_processing:cap1",
                "mark_complete:cap1",
                "mark_processing:cap2",
                "mark_complete:cap2",
            ],
        )

    def test_run_exits_1_when_no_couchdb_url(self) -> None:
        with mock.patch.dict(os.environ, {"BTQ_COUCHDB_URL": ""}, clear=False):
            with mock.patch("field_capture.couchdb_watcher.configure_logger", return_value=null_logger()):
                self.assertEqual(couchdb_watcher.run([]), 1)

    def test_run_exits_on_bad_credentials(self) -> None:
        class BadConfig:
            def assert_can_authenticate(self) -> None:
                raise couchdb_config.CouchDBConfigError("bad credentials")

        with mock.patch.dict(os.environ, {"BTQ_COUCHDB_URL": "http://couchdb.test"}, clear=False):
            with mock.patch("field_capture.couchdb_watcher.configure_logger", return_value=null_logger()):
                with mock.patch("field_capture.couchdb_watcher.couchdb_config.from_env", return_value=BadConfig()):
                    with mock.patch("field_capture.couchdb_watcher.CouchDBChangesListener") as listener_mock:
                        with self.assertRaises(SystemExit) as context:
                            couchdb_watcher.run(["--json"])

        self.assertEqual(context.exception.code, 2)
        listener_mock.assert_not_called()

    def test_run_exits_0_on_normal_stop(self) -> None:
        listener = mock.Mock()
        listener.listen.return_value = iter([])
        with mock.patch.dict(os.environ, {"BTQ_COUCHDB_URL": "http://couchdb.test"}, clear=False):
            with mock.patch("field_capture.couchdb_watcher.configure_logger", return_value=null_logger()):
                with mock.patch("field_capture.couchdb_watcher.CouchDBChangesListener", return_value=listener):
                    with mock.patch("field_capture.couchdb_watcher.CouchDBSiteRegistry", return_value=mock.Mock()):
                        self.assertEqual(couchdb_watcher.run(["--json"]), 0)

    def test_run_uses_remote_tunnel_when_loopback_couchdb_is_unavailable(self) -> None:
        listener = mock.Mock()
        listener.listen.return_value = iter([])

        class FakeTunnel:
            def __enter__(self) -> str:
                return "http://127.0.0.1:15984"

            def __exit__(self, *_args: object) -> None:
                return None

        with mock.patch.dict(os.environ, {"BTQ_COUCHDB_URL": "http://127.0.0.1:5984"}, clear=False):
            with mock.patch("field_capture.couchdb_watcher.configure_logger", return_value=null_logger()):
                with mock.patch("field_capture.couchdb_watcher.couchdb_url_needs_remote_tunnel", return_value=True):
                    with mock.patch("field_capture.couchdb_watcher.couchdb_tunnel", return_value=FakeTunnel()) as tunnel_mock:
                        with mock.patch("field_capture.couchdb_watcher.CouchDBChangesListener", return_value=listener):
                            with mock.patch("field_capture.couchdb_watcher.CouchDBSiteRegistry", return_value=mock.Mock()):
                                self.assertEqual(couchdb_watcher.run(["--json", "--remote-host", "vps.example"]), 0)
                                self.assertEqual(os.environ["BTQ_COUCHDB_URL"], "http://127.0.0.1:5984")

        tunnel_mock.assert_called_once_with("vps.example")

    def test_couchdb_url_needs_remote_tunnel_ignores_non_loopback_urls(self) -> None:
        self.assertFalse(couchdb_watcher.couchdb_url_needs_remote_tunnel("http://couchdb.example:5984"))


if __name__ == "__main__":
    unittest.main()
