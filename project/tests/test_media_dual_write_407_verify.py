"""Independent verifier tests for 407 — capture media dual-write.

Contract: write_capture_media must ALWAYS write to local disk (source of truth),
and mirror_capture_media_from_disk best-effort mirrors the same key+bytes to R2
from the persisted local file.
A mirror that raises must never propagate, never skip the local write, never
fail the capture. Local mode must be byte-identical to prior behavior (no
mirror attempted). get_mirror_store returns an S3 store in s3 mode, None in
local mode.

Patch point note: mirror_capture_media_from_disk re-imports get_mirror_store
on each call, so the live binding is media_store.get_mirror_store — that is
what we patch.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BTQ_MEDIA_STORE", "local")

import capture_ingest
import media_store
from capture_ingest import UploadedFile, mirror_capture_media_from_disk, write_capture_media
from media_store import LocalFilesystemStore, get_mirror_store


def _uploaded(content: bytes, filename: str = "photo.jpg") -> UploadedFile:
    return UploadedFile(
        field_name="photos",
        filename=filename,
        content_type="image/jpeg",
        content=content,
    )


class _SpyStore:
    """Records (key, data) writes; optionally raises on write."""

    def __init__(self, raise_on_write: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.raise_on_write = raise_on_write

    def write(self, key: str, data: bytes) -> None:
        self.calls.append((key, data))
        if self.raise_on_write:
            raise RuntimeError("simulated R2 outage")

    # Unused protocol methods (mirror only writes)
    def read(self, key):  # pragma: no cover
        raise AssertionError("mirror.read should not be called")

    def exists(self, key):  # pragma: no cover
        return False

    def url_for(self, key):  # pragma: no cover
        return ""


# ---------------------------------------------------------------------------
# get_mirror_store
# ---------------------------------------------------------------------------
class GetMirrorStoreTests(unittest.TestCase):
    def test_local_mode_returns_none(self) -> None:
        cfg = SimpleNamespace(media_store="local")
        self.assertIsNone(get_mirror_store(Path("/tmp"), instance_config=cfg))

    def test_default_config_returns_none(self) -> None:
        # No media_store attr -> falls back to DEFAULT_MEDIA_STORE ("local") -> None.
        cfg = SimpleNamespace()
        self.assertIsNone(get_mirror_store(Path("/tmp"), instance_config=cfg))

    def test_blank_media_store_returns_none(self) -> None:
        cfg = SimpleNamespace(media_store="")
        self.assertIsNone(get_mirror_store(Path("/tmp"), instance_config=cfg))

    def test_s3_mode_returns_s3_store(self) -> None:
        cfg = SimpleNamespace(media_store="s3")
        sentinel = object()
        with mock.patch.object(
            media_store, "build_s3_store", return_value=sentinel
        ) as build:
            out = get_mirror_store(Path("/tmp"), instance_config=cfg)
        self.assertIs(out, sentinel)
        build.assert_called_once_with(cfg)

    def test_s3_mode_does_not_need_real_boto3(self) -> None:
        # boto3 forcibly absent; with build_s3_store patched we never construct
        # a real client — proves no eager boto3/cred requirement.
        cfg = SimpleNamespace(media_store="s3")
        with mock.patch.dict(sys.modules, {"boto3": None, "botocore": None}):
            with mock.patch.object(
                media_store, "build_s3_store", return_value=object()
            ):
                self.assertIsNotNone(get_mirror_store(Path("/tmp"), instance_config=cfg))


# ---------------------------------------------------------------------------
# write_capture_media — LOCAL mode
# ---------------------------------------------------------------------------
class LocalModeTests(unittest.TestCase):
    def test_local_writes_each_file_and_returns_mirror_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            date, cap = "2026-06-21", "cap-local"
            files = {
                "a.jpg": b"\xff\xd8bytes-A\x00",
                "b.jpg": b"bytes-B-\x01\x02",
            }
            records = []
            uploads = []
            for name, content in files.items():
                sp = upload_root / date / cap / name
                records.append({"stored_path": str(sp)})
                uploads.append(_uploaded(content, name))

            with mock.patch.object(
                media_store,
                "get_mirror_store",
                side_effect=AssertionError("mirror store must not load during local write"),
            ):
                candidates = write_capture_media(records, uploads, upload_root)

            for name, content in files.items():
                landed = upload_root / date / cap / name
                self.assertTrue(landed.is_file(), name)
                self.assertEqual(landed.read_bytes(), content)
            self.assertEqual(
                candidates,
                [(f"{date}/{cap}/{name}", upload_root / date / cap / name) for name in files],
            )

    def test_local_write_never_loads_mirror_store(self) -> None:
        # Local write no longer resolves any mirror store on the request path.
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            sp = upload_root / "2026-06-21" / "cap" / "p.jpg"
            with mock.patch.object(
                media_store,
                "get_mirror_store",
                side_effect=AssertionError("mirror store must not load during local write"),
            ):
                write_capture_media(
                    [{"stored_path": str(sp)}], [_uploaded(b"x")], upload_root
                )
            self.assertTrue(sp.is_file())
            self.assertEqual(sp.read_bytes(), b"x")


# ---------------------------------------------------------------------------
# write_capture_media — S3 mode (mirror happy path)
# ---------------------------------------------------------------------------
class S3MirrorModeTests(unittest.TestCase):
    def test_s3_mirror_reads_local_disk_and_mirrors_same_key_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            date, cap = "2026-06-21", "cap-s3"
            files = {
                "one.jpg": b"\xff\xd8one\x00",
                "two.jpg": b"two-\x01\x02\x03",
                "three.jpg": b"three-bytes",
            }
            records, uploads, expected = [], [], []
            for name, content in files.items():
                sp = upload_root / date / cap / name
                records.append({"stored_path": str(sp)})
                uploads.append(_uploaded(content, name))
                expected.append((f"{date}/{cap}/{name}", content))

            spy = _SpyStore()
            with mock.patch.object(media_store, "get_mirror_store", return_value=spy):
                candidates = write_capture_media(records, uploads, upload_root)
                for _key, stored_path in candidates:
                    Path(stored_path).write_bytes(Path(stored_path).read_bytes() + b"-disk")
                mirror_capture_media_from_disk(candidates, upload_root)

            # Local present + byte-identical.
            for name, content in files.items():
                landed = upload_root / date / cap / name
                self.assertTrue(landed.is_file(), name)
                self.assertEqual(landed.read_bytes(), content + b"-disk")
            # Mirror received the SAME keys, with bytes re-read from disk.
            self.assertEqual(spy.calls, [(key, data + b"-disk") for key, data in expected])

    def test_mirror_loaded_once_not_per_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            date, cap = "2026-06-21", "cap-once"
            records, uploads = [], []
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                sp = upload_root / date / cap / name
                records.append({"stored_path": str(sp)})
                uploads.append(_uploaded(b"d-" + name.encode(), name))

            spy = _SpyStore()
            with mock.patch.object(
                media_store, "get_mirror_store", return_value=spy
            ) as gms:
                candidates = write_capture_media(records, uploads, upload_root)
                mirror_capture_media_from_disk(candidates, upload_root)
            # get_mirror_store resolved lazily exactly once for 3 files.
            self.assertEqual(gms.call_count, 1)
            self.assertEqual(len(spy.calls), 3)


# ---------------------------------------------------------------------------
# Mirror failure must never break the capture
# ---------------------------------------------------------------------------
class MirrorFailureTests(unittest.TestCase):
    def test_mirror_write_raises_does_not_propagate_local_still_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            date, cap = "2026-06-21", "cap-boom"
            files = {"x.jpg": b"x-bytes", "y.jpg": b"y-bytes"}
            records, uploads = [], []
            for name, content in files.items():
                sp = upload_root / date / cap / name
                records.append({"stored_path": str(sp)})
                uploads.append(_uploaded(content, name))

            spy = _SpyStore(raise_on_write=True)
            with mock.patch.object(media_store, "get_mirror_store", return_value=spy):
                # Must NOT raise.
                candidates = write_capture_media(records, uploads, upload_root)
                mirror_capture_media_from_disk(candidates, upload_root)

            # Local source-of-truth files present despite every mirror raising.
            for name, content in files.items():
                landed = upload_root / date / cap / name
                self.assertTrue(landed.is_file(), name)
                self.assertEqual(landed.read_bytes(), content)
            # Mirror was attempted for every file (it raised each time).
            self.assertEqual(len(spy.calls), 2)

    def test_get_mirror_store_raising_does_not_break_capture(self) -> None:
        # Even if resolving the mirror itself blows up, local write survives.
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            sp = upload_root / "2026-06-21" / "cap" / "p.jpg"
            with mock.patch.object(
                media_store,
                "get_mirror_store",
                side_effect=RuntimeError("config blew up"),
            ):
                candidates = write_capture_media(
                    [{"stored_path": str(sp)}], [_uploaded(b"survives")], upload_root
                )
                mirror_capture_media_from_disk(candidates, upload_root)
            self.assertTrue(sp.is_file())
            self.assertEqual(sp.read_bytes(), b"survives")


# ---------------------------------------------------------------------------
# Validation paths unchanged
# ---------------------------------------------------------------------------
class ValidationPathsTests(unittest.TestCase):
    def test_length_mismatch_raises_invalid_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            with self.assertRaises(capture_ingest.SubmissionError) as cm:
                write_capture_media(
                    [{"stored_path": str(upload_root / "a.jpg")}],
                    [_uploaded(b"1"), _uploaded(b"2")],
                    upload_root,
                )
            self.assertEqual(cm.exception.code, "invalid_media")

    def test_non_dict_record_raises_invalid_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            with self.assertRaises(capture_ingest.SubmissionError) as cm:
                write_capture_media(["not-a-dict"], [_uploaded(b"x")], upload_root)
            self.assertEqual(cm.exception.code, "invalid_photo")

    def test_out_of_root_path_raises_invalid_upload_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = (Path(tmp) / "uploads").resolve()
            upload_root.mkdir()
            outside = Path(tmp) / "escape.jpg"
            with self.assertRaises(capture_ingest.SubmissionError) as cm:
                write_capture_media(
                    [{"stored_path": str(outside)}], [_uploaded(b"x")], upload_root
                )
            self.assertEqual(cm.exception.code, "invalid_upload_path")
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
