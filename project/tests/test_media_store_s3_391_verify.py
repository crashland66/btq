from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Force local backend for any serve path that consults real instance config.
import os

os.environ.setdefault("BTQ_MEDIA_STORE", "local")

import media_store
from media_store import (
    LocalFilesystemStore,
    MediaStoreConfigError,
    S3Store,
    _load_r2_credentials,
    get_media_store,
)
import ops_dashboard.app as app


def _make_boto3_mock():
    """Return (boto3_mock, client_mock, client_error_type) for sys.modules injection."""

    class _ClientError(Exception):
        def __init__(self, response):
            super().__init__("ClientError")
            self.response = response

    client_mock = mock.MagicMock(name="s3_client")
    boto3_mock = mock.MagicMock(name="boto3")
    boto3_mock.client.return_value = client_mock

    botocore_mock = mock.MagicMock(name="botocore")
    exceptions_mock = mock.MagicMock(name="botocore.exceptions")
    exceptions_mock.ClientError = _ClientError
    botocore_mock.exceptions = exceptions_mock

    return boto3_mock, botocore_mock, client_mock, _ClientError


class _MockBoto3:
    """Context manager that injects mocked boto3/botocore into sys.modules."""

    def __init__(self):
        (
            self.boto3,
            self.botocore,
            self.client,
            self.ClientError,
        ) = _make_boto3_mock()

    def __enter__(self):
        self._patcher = mock.patch.dict(
            sys.modules,
            {
                "boto3": self.boto3,
                "botocore": self.botocore,
                "botocore.exceptions": self.botocore.exceptions,
            },
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


class ModuleImportsWithoutBoto3Tests(unittest.TestCase):
    def test_import_does_not_require_boto3(self) -> None:
        # Re-import media_store with boto3 forcibly absent; must not raise.
        with mock.patch.dict(sys.modules, {"boto3": None, "botocore": None}):
            saved = sys.modules.pop("media_store", None)
            try:
                mod = importlib.import_module("media_store")
                self.assertTrue(hasattr(mod, "S3Store"))
            finally:
                if saved is not None:
                    sys.modules["media_store"] = saved

    def test_boto3_not_imported_eagerly(self) -> None:
        # Fresh import of media_store must not pull boto3 into sys.modules.
        sys.modules.pop("boto3", None)
        saved = sys.modules.pop("media_store", None)
        try:
            importlib.import_module("media_store")
            self.assertNotIn("boto3", sys.modules)
        finally:
            if saved is not None:
                sys.modules["media_store"] = saved


class S3StoreWithMockedBoto3Tests(unittest.TestCase):
    def _store(self, mb: _MockBoto3) -> S3Store:
        return S3Store(
            endpoint_url="https://example.invalid",
            bucket="my-bucket",
            access_key="AK",
            secret_access_key="SK",
            region="auto",
        )

    def test_write_calls_put_object(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            store.write("2026-06-16/cap/photo.jpg", b"abc-bytes")
            mb.client.put_object.assert_called_once_with(
                Bucket="my-bucket", Key="2026-06-16/cap/photo.jpg", Body=b"abc-bytes"
            )

    def test_read_returns_body_bytes(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            body = mock.MagicMock()
            body.read.return_value = b"the-bytes"
            mb.client.get_object.return_value = {"Body": body}
            out = store.read("k/photo.jpg")
            self.assertEqual(out, b"the-bytes")
            mb.client.get_object.assert_called_once_with(
                Bucket="my-bucket", Key="k/photo.jpg"
            )

    def test_exists_true_via_head_object(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            mb.client.head_object.return_value = {}
            self.assertTrue(store.exists("k/photo.jpg"))
            mb.client.head_object.assert_called_once_with(
                Bucket="my-bucket", Key="k/photo.jpg"
            )

    def test_exists_false_on_404_client_error(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            mb.client.head_object.side_effect = mb.ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}
            )
            self.assertFalse(store.exists("missing.jpg"))

    def test_exists_false_on_nosuchkey(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            mb.client.head_object.side_effect = mb.ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {}}
            )
            self.assertFalse(store.exists("missing.jpg"))

    def test_exists_reraises_non_404_client_error(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            mb.client.head_object.side_effect = mb.ClientError(
                {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}
            )
            with self.assertRaises(mb.ClientError):
                store.exists("forbidden.jpg")

    def test_url_for_presigns_get_object(self) -> None:
        with _MockBoto3() as mb:
            store = self._store(mb)
            mb.client.generate_presigned_url.return_value = "https://signed.example/get"
            url = store.url_for("k/photo.jpg")
            self.assertEqual(url, "https://signed.example/get")
            args, kwargs = mb.client.generate_presigned_url.call_args
            self.assertEqual(args[0], "get_object")
            self.assertEqual(
                kwargs["Params"], {"Bucket": "my-bucket", "Key": "k/photo.jpg"}
            )

    def test_construct_without_boto3_clear_error(self) -> None:
        # boto3 forcibly absent -> RuntimeError mentioning boto3 / r2 extra.
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaises(RuntimeError) as cm:
                S3Store("https://x", "b", "AK", "SK")
            msg = str(cm.exception).lower()
            self.assertIn("boto3", msg)
            self.assertIn("r2", msg)


class GetMediaStoreS3Tests(unittest.TestCase):
    def test_uses_config_creds_when_set(self) -> None:
        with _MockBoto3() as mb:
            cfg = SimpleNamespace(
                media_store="s3",
                r2_endpoint_url="https://acct.r2.example",
                r2_bucket="bkt",
                r2_region="auto",
                r2_access_key_id="CFG_AK",
                r2_secret_access_key="CFG_SK",
            )
            # Guard: if it falls through to secrets, fail loudly.
            with mock.patch.object(
                media_store,
                "_load_r2_credentials",
                side_effect=AssertionError("should not read secrets when config creds set"),
            ):
                store = get_media_store(Path("/tmp"), instance_config=cfg)
            self.assertIsInstance(store, S3Store)
            _, kwargs = mb.boto3.client.call_args
            self.assertEqual(kwargs["aws_access_key_id"], "CFG_AK")
            self.assertEqual(kwargs["aws_secret_access_key"], "CFG_SK")
            self.assertEqual(kwargs["endpoint_url"], "https://acct.r2.example")

    def test_falls_back_to_secrets_file_creds(self) -> None:
        with _MockBoto3() as mb:
            cfg = SimpleNamespace(
                media_store="s3",
                r2_endpoint_url="https://acct.r2.example",
                r2_bucket="bkt",
                r2_region="",
                r2_access_key_id="",
                r2_secret_access_key="",
            )
            with mock.patch.object(
                media_store,
                "_load_r2_credentials",
                return_value={"access_key": "SECRET_AK", "secret_access_key": "SECRET_SK"},
            ):
                store = get_media_store(Path("/tmp"), instance_config=cfg)
            self.assertIsInstance(store, S3Store)
            _, kwargs = mb.boto3.client.call_args
            self.assertEqual(kwargs["aws_access_key_id"], "SECRET_AK")
            self.assertEqual(kwargs["aws_secret_access_key"], "SECRET_SK")

    def test_missing_endpoint_raises_clear_config_error(self) -> None:
        cfg = SimpleNamespace(
            media_store="s3",
            r2_endpoint_url="",
            r2_bucket="bkt",
            r2_access_key_id="AK",
            r2_secret_access_key="SK",
        )
        with self.assertRaises(MediaStoreConfigError) as cm:
            get_media_store(Path("/tmp"), instance_config=cfg)
        self.assertIn("endpoint", str(cm.exception))

    def test_missing_bucket_raises_clear_config_error(self) -> None:
        cfg = SimpleNamespace(
            media_store="s3",
            r2_endpoint_url="https://x",
            r2_bucket="",
            r2_access_key_id="AK",
            r2_secret_access_key="SK",
        )
        with self.assertRaises(MediaStoreConfigError) as cm:
            get_media_store(Path("/tmp"), instance_config=cfg)
        self.assertIn("bucket", str(cm.exception))

    def test_missing_creds_raises_clear_config_error(self) -> None:
        # config blank AND secrets file blank -> clear error, not broken client.
        cfg = SimpleNamespace(
            media_store="s3",
            r2_endpoint_url="https://x",
            r2_bucket="bkt",
            r2_access_key_id="",
            r2_secret_access_key="",
        )
        with mock.patch.object(
            media_store,
            "_load_r2_credentials",
            return_value={"access_key": "", "secret_access_key": ""},
        ):
            with self.assertRaises(MediaStoreConfigError):
                get_media_store(Path("/tmp"), instance_config=cfg)


class LoadR2CredentialsTests(unittest.TestCase):
    def test_absent_file_returns_blanks_no_raise(self) -> None:
        out = _load_r2_credentials("/no/such/path/cloudflareR2")
        self.assertEqual(out, {"access_key": "", "secret_access_key": ""})

    def test_parses_keys_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cloudflareR2"
            p.write_text(
                "# comment\n"
                "access_key=AKVAL\n"
                'secret_access_key="SKVAL"\n',
                encoding="utf-8",
            )
            out = _load_r2_credentials(p)
            self.assertEqual(out["access_key"], "AKVAL")
            self.assertEqual(out["secret_access_key"], "SKVAL")


class _FakeMissingS3Store:
    """A stand-in s3 store that never has the key (forces dual-read fallback)."""

    def exists(self, key: str) -> bool:
        return False

    def read(self, key: str) -> bytes:  # pragma: no cover - should not be called
        raise AssertionError("s3 read should not be reached when key absent")

    def write(self, key, data):  # pragma: no cover
        ...

    def url_for(self, key):  # pragma: no cover
        return ""


class DualReadServeTests(unittest.TestCase):
    def test_local_byte_identical_serves_real_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            target = runtime_root / "uploads" / "2026-06-16" / "cap" / "p.jpg"
            target.parent.mkdir(parents=True)
            content = b"\x89PNG\x00local-serve"
            target.write_bytes(content)
            status, ct, body, _ = app.serve_media_response(
                "2026-06-16/cap/p.jpg", runtime_root
            )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(ct, "image/jpeg")
            self.assertEqual(body, content)

    def test_local_missing_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            (runtime_root / "uploads").mkdir()
            status, _ct, body, _ = app.serve_media_response(
                "2026-06-16/cap/missing.jpg", runtime_root
            )
            self.assertEqual(status, HTTPStatus.NOT_FOUND)
            self.assertIn(b"not_found", body)

    def test_s3_missing_key_falls_back_to_local_present_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            target = runtime_root / "uploads" / "2026-06-16" / "cap" / "legacy.jpg"
            target.parent.mkdir(parents=True)
            content = b"legacy-bytes-on-disk"
            target.write_bytes(content)

            # Configure serve to use a (mocked) s3 store that lacks the key.
            with mock.patch.object(
                app, "get_media_store", return_value=_FakeMissingS3Store()
            ):
                status, ct, body, _ = app.serve_media_response(
                    "2026-06-16/cap/legacy.jpg", runtime_root
                )
            self.assertEqual(status, HTTPStatus.OK)
            self.assertEqual(ct, "image/jpeg")
            self.assertEqual(body, content)

    def test_s3_missing_and_local_missing_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            (runtime_root / "uploads").mkdir()
            with mock.patch.object(
                app, "get_media_store", return_value=_FakeMissingS3Store()
            ):
                status, _ct, body, _ = app.serve_media_response(
                    "2026-06-16/cap/gone.jpg", runtime_root
                )
            self.assertEqual(status, HTTPStatus.NOT_FOUND)
            self.assertIn(b"not_found", body)


if __name__ == "__main__":
    unittest.main()
