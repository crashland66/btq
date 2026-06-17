from __future__ import annotations

import os
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

# Ensure local backend for serve path which consults instance config.
os.environ.setdefault("BTQ_MEDIA_STORE", "local")

import capture_ingest
from capture_ingest import UploadedFile, write_capture_media
from media_store import LocalFilesystemStore, get_media_store
from ops_dashboard.app import serve_media_response


def _uploaded(content: bytes) -> UploadedFile:
    # UploadedFile is a dataclass/namedtuple-ish; build it whichever way the
    # codebase defines it. It must carry a `.content` attribute of bytes.
    return UploadedFile(
        field_name="photo", filename="photo.jpg", content_type="image/jpeg", content=content
    )


class WriteByteIdenticalTests(unittest.TestCase):
    def test_write_capture_media_lands_at_keyed_path_with_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            date = "2026-06-16"
            capture_id = "cap-abc"
            filename = "photo.jpg"
            stored_path = upload_root / date / capture_id / filename
            content = b"\xff\xd8\xff\x00binary-bytes-\x01\x02\x03"

            record = {"stored_path": str(stored_path)}
            write_capture_media([record], [_uploaded(content)], upload_root)

            self.assertTrue(stored_path.is_file())
            self.assertEqual(stored_path.read_bytes(), content)
            # Atomic write leaves no stray temp file.
            tmp_leftover = stored_path.with_name(f".{filename}.tmp")
            self.assertFalse(tmp_leftover.exists())

    def test_store_write_matches_keyed_layout_and_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            store = LocalFilesystemStore(upload_root)
            key = "2026-06-16/cap-xyz/photo.jpg"
            content = b"keyed-bytes"
            store.write(key, content)
            landed = upload_root / "2026-06-16" / "cap-xyz" / "photo.jpg"
            self.assertTrue(landed.is_file())
            self.assertEqual(landed.read_bytes(), content)
            self.assertFalse((landed.with_name(".photo.jpg.tmp")).exists())

    def test_write_capture_media_rejects_out_of_root_stored_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            outside = Path(tmp) / "escape.jpg"
            record = {"stored_path": str(outside)}
            with self.assertRaises(capture_ingest.SubmissionError):
                write_capture_media([record], [_uploaded(b"x")], upload_root)
            self.assertFalse(outside.exists())


class ServeByteIdenticalTests(unittest.TestCase):
    def test_serve_returns_exact_bytes_and_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            upload_dir = runtime_root / "uploads"
            target = upload_dir / "2026-06-16" / "cap" / "photo.jpg"
            target.parent.mkdir(parents=True)
            content = b"\x89PNG-ish\x00serve-bytes"
            target.write_bytes(content)

            status, content_type, body, _ = serve_media_response(
                "2026-06-16/cap/photo.jpg", runtime_root
            )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(content_type, "image/jpeg")
            self.assertEqual(body, content)

    def test_serve_missing_key_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            (runtime_root / "uploads").mkdir()
            status, _ct, body, _ = serve_media_response(
                "2026-06-16/cap/missing.jpg", runtime_root
            )
            self.assertEqual(status, HTTPStatus.NOT_FOUND)
            self.assertIn(b"not_found", body)

    def test_serve_out_of_root_request_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            (runtime_root / "uploads").mkdir()
            # A traversal escaping the upload root must 404 (UnsafeMediaPath).
            status, _ct, _body, _ = serve_media_response(
                "../../etc/passwd", runtime_root
            )
            self.assertEqual(status, HTTPStatus.NOT_FOUND)


class StoreKeyConfinementTests(unittest.TestCase):
    def test_read_refuses_relative_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            secret = Path(tmp) / "secret.txt"
            secret.write_bytes(b"top-secret")
            store = LocalFilesystemStore(upload_root)
            with self.assertRaises(ValueError):
                store.read("../secret.txt")

    def test_write_refuses_relative_escape_and_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            store = LocalFilesystemStore(upload_root)
            escaped = Path(tmp) / "pwned.txt"
            with self.assertRaises(ValueError):
                store.write("../pwned.txt", b"evil")
            self.assertFalse(escaped.exists())

    def test_exists_refuses_absolute_out_of_root_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            upload_root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_bytes(b"data")
            store = LocalFilesystemStore(upload_root)
            with self.assertRaises(ValueError):
                store.exists(str(outside))


class FactoryTests(unittest.TestCase):
    def test_default_returns_local_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = get_media_store(Path(tmp))
            self.assertIsInstance(store, LocalFilesystemStore)

    def test_explicit_local_config_returns_local_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(media_store="local")
            store = get_media_store(Path(tmp), instance_config=cfg)
            self.assertIsInstance(store, LocalFilesystemStore)

    def test_s3_raises_clear_not_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(media_store="s3")
            with self.assertRaises(NotImplementedError):
                get_media_store(Path(tmp), instance_config=cfg)

    def test_unknown_backend_raises_clear_error_not_attributeerror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = SimpleNamespace(media_store="banana")
            with self.assertRaises(NotImplementedError):
                get_media_store(Path(tmp), instance_config=cfg)


if __name__ == "__main__":
    unittest.main()
