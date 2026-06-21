from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

from instance_config import DEFAULT_MEDIA_STORE, get_instance_config

_R2_ATTR_PREFIX = "r2_"
_R2_ENDPOINT_ATTR = _R2_ATTR_PREFIX + "endpoint_url"
_R2_BUCKET_ATTR = _R2_ATTR_PREFIX + "bucket"
_R2_REGION_ATTR = _R2_ATTR_PREFIX + "region"
_R2_ACCESS_KEY_ATTR = _R2_ATTR_PREFIX + "access_key_id"
_R2_SECRET_ACCESS_KEY_ATTR = _R2_ATTR_PREFIX + "secret_access_key"
_R2_SECRET_PREFIX_ATTR = _R2_ATTR_PREFIX + "secret_prefix"


class MediaStore(Protocol):
    def write(self, key: str, data: bytes) -> None:
        ...

    def read(self, key: str) -> bytes:
        ...

    def exists(self, key: str) -> bool:
        ...

    def url_for(self, key: str) -> str:
        ...


class LocalFilesystemStore:
    def __init__(self, upload_root: Path) -> None:
        self.upload_root = upload_root.expanduser().resolve(strict=False)

    def write(self, key: str, data: bytes) -> None:
        from capture_ingest import atomic_write_bytes

        atomic_write_bytes(self._path_for_key(key), data)

    def read(self, key: str) -> bytes:
        return self._path_for_key(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path_for_key(key).is_file()

    def url_for(self, key: str) -> str:
        self._path_for_key(key)
        return f"/media/{key}"

    def _path_for_key(self, key: str) -> Path:
        candidate = (self.upload_root / key).expanduser().resolve(strict=False)
        try:
            candidate.relative_to(self.upload_root)
        except ValueError as exc:
            raise ValueError(f"Media key escapes upload root: {key}") from exc
        return candidate


class MediaStoreConfigError(ValueError, NotImplementedError):
    pass


def _guard_s3_key(key: str) -> str:
    if not key:
        raise ValueError("Media key must not be empty")
    if key.startswith("/"):
        raise ValueError(f"Media key must be relative: {key}")
    if ".." in PurePosixPath(key).parts:
        raise ValueError(f"Media key escapes media store: {key}")
    return key


def media_key_from_stored_path(stored_path: object, upload_root: Path) -> str:
    """Return the media-store key represented by persisted capture metadata.

    Older CouchDB capture docs store absolute local paths under runtime/uploads;
    newer records also carry upload_id. This read-only helper accepts either
    shape and returns the canonical store key: date/capture_id/filename.
    """
    text = str(stored_path or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("Media stored_path must not be empty")

    root = upload_root.expanduser().resolve(strict=False)
    if text.startswith("/media/"):
        return _guard_s3_key(text.removeprefix("/media/"))

    path = Path(text).expanduser()
    if path.is_absolute():
        resolved = path.resolve(strict=False)
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Media stored_path escapes upload root: {stored_path}") from exc

    parts = PurePosixPath(text).parts
    key = ""
    if "runtime" in parts and "uploads" in parts:
        index = parts.index("uploads")
        key = PurePosixPath(*parts[index + 1 :]).as_posix() if index + 1 < len(parts) else ""
    elif parts and parts[0] == "uploads":
        key = PurePosixPath(*parts[1:]).as_posix() if len(parts) > 1 else ""
    else:
        key = PurePosixPath(text).as_posix()
    if key in {"", "."}:
        raise ValueError(f"Media stored_path has no key suffix: {stored_path}")
    return _guard_s3_key(key)


class S3Store:
    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_access_key: str,
        region: str = "auto",
    ) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError("boto3 not installed; install the R2 optional dependency with: pip install .[r2]") from exc

        self.bucket = bucket
        self._client_error_type = ClientError
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_access_key,
            region_name=region or "auto",
        )

    def write(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=_guard_s3_key(key), Body=data)

    def read(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=_guard_s3_key(key))
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=_guard_s3_key(key))
            return True
        except self._client_error_type as exc:
            error = exc.response.get("Error", {})
            metadata = exc.response.get("ResponseMetadata", {})
            code = str(error.get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"} or metadata.get("HTTPStatusCode") == 404:
                return False
            raise

    def url_for(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": _guard_s3_key(key)},
            ExpiresIn=300,
        )


def _load_r2_credentials(
    secrets_path: str | Path = "secrets/cloudflareR2", key_prefix: str = ""
) -> dict[str, str]:
    path = Path(secrets_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if not path.exists():
        return {"access_key": "", "secret_access_key": ""}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        # Accept either `key=value` (env-style) or `key: value` (colon-style),
        # splitting on whichever separator appears first so the key is delimited
        # correctly even if the value itself contains the other separator.
        candidates = [i for i in (stripped.find("="), stripped.find(":")) if i != -1]
        if not candidates:
            continue
        idx = min(candidates)
        key, value = stripped[:idx], stripped[idx + 1 :]
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    prefix = key_prefix.strip().strip("_")
    if prefix:
        access_key = values.get(f"{prefix}_access_key", "")
        secret_access_key = values.get(f"{prefix}_secret_access_key", "")
        if access_key and secret_access_key:
            return {"access_key": access_key, "secret_access_key": secret_access_key}
    return {
        "access_key": values.get("access_key", ""),
        "secret_access_key": values.get("secret_access_key", ""),
    }


def _config_string(config: object, attr: str) -> str:
    value = getattr(config, attr, "")
    return value.strip() if isinstance(value, str) else ""


def build_s3_store(instance_config: object | None = None) -> S3Store:
    config = get_instance_config() if instance_config is None else instance_config
    endpoint_url = _config_string(config, _R2_ENDPOINT_ATTR)
    bucket = _config_string(config, _R2_BUCKET_ATTR)
    region = _config_string(config, _R2_REGION_ATTR) or "auto"
    access_key = _config_string(config, _R2_ACCESS_KEY_ATTR)
    secret_access_key = _config_string(config, _R2_SECRET_ACCESS_KEY_ATTR)
    if not access_key or not secret_access_key:
        secrets = _load_r2_credentials(key_prefix=_config_string(config, _R2_SECRET_PREFIX_ATTR))
        access_key = access_key or secrets["access_key"]
        secret_access_key = secret_access_key or secrets["secret_access_key"]

    missing = [
        name
        for name, value in (
            (_R2_ENDPOINT_ATTR, endpoint_url),
            (_R2_BUCKET_ATTR, bucket),
            (f"{_R2_ACCESS_KEY_ATTR}/access_key", access_key),
            (f"{_R2_SECRET_ACCESS_KEY_ATTR}/secret_access_key", secret_access_key),
        )
        if not value
    ]
    if missing:
        raise MediaStoreConfigError(f"media_store 's3' requires configured R2 values: {', '.join(missing)}")
    return S3Store(endpoint_url, bucket, access_key, secret_access_key, region)


def get_media_store(upload_root: Path, instance_config: object | None = None) -> MediaStore:
    config = get_instance_config() if instance_config is None else instance_config
    media_store = getattr(config, "media_store", DEFAULT_MEDIA_STORE) or DEFAULT_MEDIA_STORE
    if media_store == "local":
        return LocalFilesystemStore(upload_root)
    if media_store == "s3":
        return build_s3_store(config)
    raise NotImplementedError(f"media_store {media_store!r} is not supported")
