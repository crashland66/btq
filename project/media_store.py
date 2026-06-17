from __future__ import annotations

from pathlib import Path
from typing import Protocol

from instance_config import DEFAULT_MEDIA_STORE, get_instance_config


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


def get_media_store(upload_root: Path, instance_config: object | None = None) -> MediaStore:
    config = get_instance_config() if instance_config is None else instance_config
    media_store = getattr(config, "media_store", DEFAULT_MEDIA_STORE) or DEFAULT_MEDIA_STORE
    if media_store == "local":
        return LocalFilesystemStore(upload_root)
    if media_store == "s3":
        raise NotImplementedError("media_store 's3' not available until 391")
    raise NotImplementedError(f"media_store {media_store!r} is not supported")
