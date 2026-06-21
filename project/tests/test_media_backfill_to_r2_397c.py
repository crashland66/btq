"""Verifier-owned tests for the 397c FS->R2 media backfill CLI.

Drives ``backfill_media_to_store`` against an in-memory stub store and a temp
``uploads/`` tree. Covers the contract: relative-posix keys matching the
capture-ingest scheme; present-and-matching -> skip; missing -> write+verify;
mismatch -> re-copy; ``--dry-run`` writes nothing; non-destructive; idempotent;
write/verify failures counted (run continues, non-zero exit); ``--limit`` bounds
the scan; symlinks/non-files skipped; keys-only logging.
"""
from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from btq_cli import media_backfill_to_r2
from btq_cli.media_backfill_to_r2 import (
    backfill_media_to_store,
    handle_backfill_media_to_r2,
)


class StubStore:
    """In-memory MediaStore stub.

    No ``client``/``bucket`` attributes, so ``_single_part_etag`` returns None
    and integrity always falls back to a read-back byte compare (the path the
    contract asks the verifier to exercise).
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.write_calls: list[str] = []
        self.write_count = 0

    def write(self, key: str, data: bytes) -> None:
        self.write_calls.append(key)
        self.write_count += 1
        self.objects[key] = bytes(data)

    def read(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class FailingWriteStore(StubStore):
    """Accepts write() calls but never persists -> verify-after-write fails."""

    def write(self, key: str, data: bytes) -> None:
        self.write_calls.append(key)
        self.write_count += 1
        # Intentionally drop the bytes: object stays absent.


class RaisingWriteStore(StubStore):
    """write() raises -> counted as write-failed, run continues."""

    def __init__(self, fail_key: str) -> None:
        super().__init__()
        self.fail_key = fail_key

    def write(self, key: str, data: bytes) -> None:
        self.write_calls.append(key)
        if key == self.fail_key:
            raise OSError("simulated upload failure")
        super().write(key, data)


def _make_uploads(root: Path, files: dict[str, bytes]) -> None:
    uploads = root / "uploads"
    for rel, data in files.items():
        target = uploads / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


CORPUS = {
    "2026-06-16/cap-a/photo1.jpg": b"\x89PNG-bytes-a",
    "2026-06-16/cap-a/photo2.jpg": b"second-photo-bytes",
    "2026-06-17/cap-b/note.jpg": b"third-photo-bytes",
}


class KeyDerivationTests(unittest.TestCase):
    def test_keys_are_relative_posix_and_every_file_offered(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(report.total_scanned, 3)
            # Keys match the capture-ingest relative-to-uploads-root posix scheme.
            self.assertEqual(set(store.objects), set(CORPUS))
            self.assertEqual(set(store.write_calls), set(CORPUS))


class WriteVerifyTests(unittest.TestCase):
    def test_missing_keys_written_then_verified(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(report.copied, 3)
            self.assertEqual(report.verified, 3)
            self.assertEqual(report.skipped_present, 0)
            self.assertEqual(report.verify_failed, 0)
            self.assertEqual(report.errors, [])
            for key, data in CORPUS.items():
                self.assertEqual(store.objects[key], data)

    def test_present_and_matching_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            # Pre-populate the store with identical bytes.
            for key, data in CORPUS.items():
                store.objects[key] = bytes(data)
            report = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(report.skipped_present, 3)
            self.assertEqual(report.copied, 0)
            self.assertEqual(store.write_count, 0)
            self.assertEqual(store.write_calls, [])

    def test_mismatched_remote_is_recopied(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            # Present but WRONG bytes for one key -> must be re-copied.
            mismatch_key = "2026-06-16/cap-a/photo1.jpg"
            store.objects[mismatch_key] = b"stale-wrong-bytes"
            # The other two already match -> skipped.
            store.objects["2026-06-16/cap-a/photo2.jpg"] = CORPUS["2026-06-16/cap-a/photo2.jpg"]
            store.objects["2026-06-17/cap-b/note.jpg"] = CORPUS["2026-06-17/cap-b/note.jpg"]
            report = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(report.copied, 1)
            self.assertEqual(report.skipped_present, 2)
            self.assertEqual(store.write_calls, [mismatch_key])
            # Re-copy fixed the bytes.
            self.assertEqual(store.objects[mismatch_key], CORPUS[mismatch_key])


class DryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing_reports_would_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store, dry_run=True)
            self.assertEqual(store.write_calls, [])
            self.assertEqual(store.objects, {})
            self.assertEqual(report.would_copy, 3)
            self.assertEqual(report.would_skip_present, 0)
            self.assertEqual(report.copied, 0)

    def test_dry_run_reports_would_skip_for_present(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            for key, data in CORPUS.items():
                store.objects[key] = bytes(data)
            report = backfill_media_to_store(runtime_root=root, store=store, dry_run=True)
            self.assertEqual(report.would_skip_present, 3)
            self.assertEqual(report.would_copy, 0)
            self.assertEqual(store.write_calls, [])

    def test_dry_run_mixed_present_and_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            present_key = "2026-06-17/cap-b/note.jpg"
            store.objects[present_key] = CORPUS[present_key]
            report = backfill_media_to_store(runtime_root=root, store=store, dry_run=True)
            self.assertEqual(report.would_skip_present, 1)
            self.assertEqual(report.would_copy, 2)
            self.assertEqual(store.write_calls, [])


class NonDestructiveTests(unittest.TestCase):
    def test_local_tree_byte_identical_after_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            before = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in (root / "uploads").rglob("*")
                if p.is_file()
            }
            store = StubStore()
            backfill_media_to_store(runtime_root=root, store=store)
            after = {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in (root / "uploads").rglob("*")
                if p.is_file()
            }
            self.assertEqual(before, after)


class IdempotencyTests(unittest.TestCase):
    def test_second_run_copies_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            first = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(first.copied, 3)
            second = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(second.copied, 0)
            self.assertEqual(second.skipped_present, 3)
            self.assertEqual(second.verify_failed, 0)
            # No new writes on the second pass.
            self.assertEqual(len(store.write_calls), 3)


class FailureHandlingTests(unittest.TestCase):
    def test_verify_after_write_failure_counted_and_continues(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = FailingWriteStore()
            report = backfill_media_to_store(runtime_root=root, store=store)
            # Every key write was attempted (run did not abort on first failure).
            self.assertEqual(len(store.write_calls), 3)
            self.assertEqual(report.copied, 0)
            self.assertEqual(report.verified, 0)
            self.assertEqual(report.verify_failed, 3)
            self.assertEqual(len(report.errors), 3)
            self.assertTrue(all(e.reason == "verify-after-write-failed" for e in report.errors))

    def test_write_exception_counted_and_run_continues(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            fail_key = "2026-06-16/cap-a/photo1.jpg"
            store = RaisingWriteStore(fail_key=fail_key)
            report = backfill_media_to_store(runtime_root=root, store=store)
            # The two good keys still copied; the failing one is recorded.
            self.assertEqual(report.copied, 2)
            self.assertEqual(report.verify_failed, 1)
            self.assertEqual([e.key for e in report.errors], [fail_key])
            self.assertEqual(report.errors[0].reason, "write-failed")

    def test_handle_returns_nonzero_exit_on_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = FailingWriteStore()
            args = argparse.Namespace(
                runtime_root=root, dry_run=False, limit=None, verbose=False
            )
            # Patch store construction so we exercise the real handler logic
            # without boto3/instance config.
            original = media_backfill_to_r2.build_s3_store
            media_backfill_to_r2.build_s3_store = lambda *a, **k: store
            try:
                rc = handle_backfill_media_to_r2(args)
            finally:
                media_backfill_to_r2.build_s3_store = original
            self.assertEqual(rc, 1)

    def test_handle_returns_zero_on_clean_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            args = argparse.Namespace(
                runtime_root=root, dry_run=False, limit=None, verbose=False
            )
            original = media_backfill_to_r2.build_s3_store
            media_backfill_to_r2.build_s3_store = lambda *a, **k: store
            try:
                rc = handle_backfill_media_to_r2(args)
            finally:
                media_backfill_to_r2.build_s3_store = original
            self.assertEqual(rc, 0)


class LimitAndScanTests(unittest.TestCase):
    def test_limit_bounds_the_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store, limit=2)
            self.assertEqual(report.total_scanned, 2)
            self.assertEqual(report.copied, 2)

    def test_limit_zero_scans_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store, limit=0)
            self.assertEqual(report.total_scanned, 0)
            self.assertEqual(store.write_calls, [])

    def test_negative_limit_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            with self.assertRaises(ValueError):
                backfill_media_to_store(runtime_root=root, store=store, limit=-1)

    def test_symlinks_and_dirs_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            uploads = root / "uploads"
            # A symlink to a real file must NOT be offered.
            real = uploads / "2026-06-16/cap-a/photo1.jpg"
            link = uploads / "2026-06-16/cap-a/link.jpg"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unsupported on this platform")
            # An empty subdir must not be counted either.
            (uploads / "2026-06-18" / "empty-cap").mkdir(parents=True)
            store = StubStore()
            report = backfill_media_to_store(runtime_root=root, store=store)
            self.assertEqual(report.total_scanned, 3)
            self.assertNotIn("2026-06-16/cap-a/link.jpg", store.objects)

    def test_missing_uploads_dir_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)  # no uploads/ created
            store = StubStore()
            with self.assertRaises(FileNotFoundError):
                backfill_media_to_store(runtime_root=root, store=store)


class LoggingHygieneTests(unittest.TestCase):
    def test_verbose_logging_is_keys_only(self) -> None:
        import io
        from contextlib import redirect_stdout

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_uploads(root, CORPUS)
            store = StubStore()
            buf = io.StringIO()
            with redirect_stdout(buf):
                backfill_media_to_store(
                    runtime_root=root, store=store, verbose=True
                )
            out = buf.getvalue()
            # Keys appear; no creds, no presigned URL scheme, no secrets.
            for key in CORPUS:
                self.assertIn(key, out)
            lowered = out.lower()
            self.assertNotIn("secret", lowered)
            self.assertNotIn("access_key", lowered)
            self.assertNotIn("x-amz-signature", lowered)
            self.assertNotIn("https://", lowered)


if __name__ == "__main__":
    unittest.main()
