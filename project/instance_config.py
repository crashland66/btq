from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


CONFIG_PATH_ENV = "BT_PIPELINE_CONFIG_PATH"
DEFAULT_CONFIG_NAME = "config.json"
EXAMPLE_CONFIG_NAME = "config.example.json"

DEFAULT_COUCHDB_URL = "http://127.0.0.1:5984"
DEFAULT_FIELD_CAPTURES_DB = "btq_field_captures"
DEFAULT_PHOTO_VISION_DB = "btq_photo_vision"
DEFAULT_QUEUE_DB = "btq_queue"
DEFAULT_VAULT_DB = "btq_vault"
DEFAULT_PERSONAL_JOURNAL_DB = "btq_personal_journal"
DEFAULT_VOICE_MEMOS_DB = "btq_voice_memos"

DEFAULT_MEDIA_STORE = "local"


@dataclass(frozen=True)
class InstanceConfig:
    couchdb_url: str
    couchdb_user: str
    couchdb_password: str
    couchdb_field_captures_db: str
    couchdb_photo_vision_db: str
    couchdb_queue_db: str
    couchdb_vault_db: str
    couchdb_personal_journal_db: str
    couchdb_voice_memos_db: str
    site_registry_path: Path
    brand_keywords_path: Path
    media_store: str
    r2_endpoint_url: str
    r2_bucket: str
    r2_region: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_secret_prefix: str


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    env_value = os.environ.get(CONFIG_PATH_ENV)
    if env_value:
        return Path(env_value).expanduser()
    real = repo_root() / DEFAULT_CONFIG_NAME
    if real.exists():
        return real
    example = repo_root() / EXAMPLE_CONFIG_NAME
    if example.exists():
        return example
    return real


def default_site_registry_real_path() -> Path:
    return repo_root() / "project" / "event_pipeline" / "site_registry.json"


def default_site_registry_example_path() -> Path:
    return repo_root() / "project" / "event_pipeline" / "site_registry.example.json"


def default_brand_keywords_real_path() -> Path:
    return repo_root() / "project" / "event_pipeline" / "brand_keywords.json"


def default_brand_keywords_example_path() -> Path:
    return repo_root() / "project" / "event_pipeline" / "brand_keywords.example.json"


def _instance_raw(config_path: Path | None) -> dict[str, Any]:
    resolved = default_config_path() if config_path is None else config_path.expanduser()
    if not resolved.exists():
        return {}
    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Config file must contain a JSON object: {resolved}")
    instance = raw.get("instance", {})
    if instance is None:
        return {}
    if not isinstance(instance, dict):
        raise ValueError(f"Config key 'instance' must be a JSON object: {resolved}")
    return instance


def _dict_from(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Instance config key '{key}' must be a JSON object")
    return value


def _string_from(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"Instance config key '{key}' must be a string")
    return value


def _env_or_config(env_name: str, raw: dict[str, Any], key: str, default: str) -> str:
    if env_name in os.environ:
        return os.environ[env_name]
    return _string_from(raw, key, default)


def _configured_path(raw: dict[str, Any], key: str) -> Path | None:
    value = raw.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Instance config key '{key}' must be a string")
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return repo_root() / path


def _data_path(
    *,
    env_name: str,
    raw: dict[str, Any],
    key: str,
    real_path: Path,
    example_path: Path,
    allow_configured_path: bool,
) -> Path:
    override = os.environ.get(env_name, "").strip()
    if override:
        return Path(override)
    if real_path.exists():
        return real_path
    configured = _configured_path(raw, key) if allow_configured_path else None
    if configured is not None:
        return configured
    return example_path


def load_instance_config(
    config_path: Path | None = None,
    *,
    site_registry_real_path: Path | None = None,
    site_registry_example_path: Path | None = None,
    brand_keywords_real_path: Path | None = None,
    brand_keywords_example_path: Path | None = None,
) -> InstanceConfig:
    raw = _instance_raw(config_path)
    couchdb = _dict_from(raw, "couchdb")
    r2 = _dict_from(raw, "r2")

    site_real = site_registry_real_path or default_site_registry_real_path()
    site_example = site_registry_example_path or default_site_registry_example_path()
    brand_real = brand_keywords_real_path or default_brand_keywords_real_path()
    brand_example = brand_keywords_example_path or default_brand_keywords_example_path()
    use_configured_data_paths = (
        site_registry_real_path is None
        and site_registry_example_path is None
        and brand_keywords_real_path is None
        and brand_keywords_example_path is None
    )

    return InstanceConfig(
        couchdb_url=_env_or_config("BTQ_COUCHDB_URL", couchdb, "url", DEFAULT_COUCHDB_URL),
        couchdb_user=_env_or_config("BTQ_COUCHDB_USER", couchdb, "user", ""),
        couchdb_password=_env_or_config("BTQ_COUCHDB_PASSWORD", couchdb, "password", ""),
        couchdb_field_captures_db=_env_or_config(
            "BTQ_COUCHDB_FIELD_CAPTURES_DB",
            couchdb,
            "field_captures_db",
            DEFAULT_FIELD_CAPTURES_DB,
        ),
        couchdb_photo_vision_db=_env_or_config(
            "BTQ_COUCHDB_PHOTO_VISION_DB",
            couchdb,
            "photo_vision_db",
            DEFAULT_PHOTO_VISION_DB,
        ),
        couchdb_queue_db=_env_or_config("BTQ_COUCHDB_QUEUE_DB", couchdb, "queue_db", DEFAULT_QUEUE_DB),
        couchdb_vault_db=_env_or_config("BTQ_COUCHDB_VAULT_DB", couchdb, "vault_db", DEFAULT_VAULT_DB),
        couchdb_personal_journal_db=_env_or_config(
            "BTQ_COUCHDB_PERSONAL_JOURNAL_DB",
            couchdb,
            "personal_journal_db",
            DEFAULT_PERSONAL_JOURNAL_DB,
        ),
        couchdb_voice_memos_db=_env_or_config(
            "BTQ_COUCHDB_VOICE_MEMOS_DB",
            couchdb,
            "voice_memos_db",
            DEFAULT_VOICE_MEMOS_DB,
        ),
        site_registry_path=_data_path(
            env_name="BTQ_SITE_REGISTRY_PATH",
            raw=raw,
            key="site_registry_path",
            real_path=site_real,
            example_path=site_example,
            allow_configured_path=use_configured_data_paths,
        ),
        brand_keywords_path=_data_path(
            env_name="BTQ_BRAND_KEYWORDS_PATH",
            raw=raw,
            key="brand_keywords_path",
            real_path=brand_real,
            example_path=brand_example,
            allow_configured_path=use_configured_data_paths,
        ),
        media_store=_env_or_config("BTQ_MEDIA_STORE", raw, "media_store", DEFAULT_MEDIA_STORE),
        r2_endpoint_url=_env_or_config("BTQ_R2_ENDPOINT_URL", r2, "endpoint_url", ""),
        r2_bucket=_env_or_config("BTQ_R2_BUCKET", r2, "bucket", ""),
        r2_region=_env_or_config("BTQ_R2_REGION", r2, "region", ""),
        r2_access_key_id=_env_or_config("BTQ_R2_ACCESS_KEY_ID", r2, "access_key_id", ""),
        r2_secret_access_key=_env_or_config("BTQ_R2_SECRET_ACCESS_KEY", r2, "secret_access_key", ""),
        r2_secret_prefix=_env_or_config("BTQ_R2_SECRET_PREFIX", r2, "secret_prefix", ""),
    )


@lru_cache(maxsize=1)
def get_instance_config() -> InstanceConfig:
    return load_instance_config()
