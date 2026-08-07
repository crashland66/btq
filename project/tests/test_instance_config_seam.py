"""INDEPENDENT VERIFIER gating tests for the InstanceConfig seam (prompt 389).

These tests assert the behavior-preserving consolidation of the instance-specific
config surface into ``instance_config.InstanceConfig``:

* byte-identical resolution: ``couchdb_config`` helpers + ``load_site_registry``
  return the same values they did before, and those values equal what
  ``InstanceConfig`` exposes (delegation, not divergence);
* delegation is real: a sentinel env var appears in BOTH ``get_instance_config()``
  and ``couchdb_config.from_env()`` -> one source;
* clean-clone: with no real ``config.json`` the example fallback resolves and
  exposes ``media_store == "local"`` + synthetic R2 slots;
* R2 slots are inert (no storage behavior wired);
* engine-agnostic settings (queue/whisper/ffmpeg) stay in ``PipelineConfig`` and
  are NOT on ``InstanceConfig``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config as pipeline_config_module
import instance_config
from event_pipeline import couchdb_config, site_registry_data

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.json"


# --------------------------------------------------------------------------- #
# BYTE-IDENTICAL RESOLUTION: couchdb helpers + load_site_registry == before, and
# equal to what InstanceConfig exposes for the same env.
# --------------------------------------------------------------------------- #
def test_couchdb_helpers_resolve_from_env_and_match_instance_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couch.sentinel:5999/")
    monkeypatch.setenv("BTQ_COUCHDB_USER", "verifier")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "verifier-pass")
    monkeypatch.setenv("BTQ_COUCHDB_SITES_DB", "sentinel_sites")
    monkeypatch.setenv("BTQ_COUCHDB_VAULT_DB", "sentinel_vault")

    # Helpers return the same values the old inline os.environ.get() did.
    assert couchdb_config.base_url() == "http://couch.sentinel:5999"  # trailing slash stripped
    assert couchdb_config.username() == "verifier"
    assert couchdb_config.password() == "verifier-pass"
    assert couchdb_config.sites_database() == "sentinel_sites"
    assert couchdb_config.vault_database() == "sentinel_vault"

    cfg = couchdb_config.from_env()
    assert cfg.base_url == "http://couch.sentinel:5999"
    assert cfg.username == "verifier"
    assert cfg.password == "verifier-pass"

    # ... and they equal what InstanceConfig exposes (delegation, not divergence).
    inst = instance_config.load_instance_config()
    assert inst.couchdb_url.rstrip("/") == couchdb_config.base_url()
    assert inst.couchdb_user == couchdb_config.username()
    assert inst.couchdb_password == couchdb_config.password()
    assert inst.couchdb_sites_db == couchdb_config.sites_database()
    assert inst.couchdb_vault_db == couchdb_config.vault_database()


def test_db_name_helpers_default_to_preserved_names() -> None:
    # The hermetic conftest deletes the couchdb url/user/pass vars but not the
    # db-name vars; with none set these must equal the historical defaults.
    assert couchdb_config.sites_database() == "btq_sites"
    assert couchdb_config.field_captures_database() == "btq_field_captures"
    assert couchdb_config.photo_vision_database() == "btq_photo_vision"
    assert couchdb_config.queue_database() == "btq_queue"
    assert couchdb_config.vault_database() == "btq_vault"
    assert couchdb_config.personal_journal_database() == "btq_personal_journal"
    assert couchdb_config.voice_memos_database() == "btq_voice_memos"


def test_db_name_defaults_equal_instance_config_defaults() -> None:
    inst = instance_config.load_instance_config()
    assert couchdb_config.sites_database() == inst.couchdb_sites_db
    assert couchdb_config.field_captures_database() == inst.couchdb_field_captures_db
    assert couchdb_config.photo_vision_database() == inst.couchdb_photo_vision_db
    assert couchdb_config.queue_database() == inst.couchdb_queue_db
    assert couchdb_config.vault_database() == inst.couchdb_vault_db
    assert couchdb_config.personal_journal_database() == inst.couchdb_personal_journal_db
    assert couchdb_config.voice_memos_database() == inst.couchdb_voice_memos_db


def test_site_registry_loads_via_instance_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the registry at the committed synthetic example explicitly and assert
    # the loader resolves through InstanceConfig to that same path + loads it.
    example = (
        REPO_ROOT / "project" / "event_pipeline" / "site_registry.example.json"
    ).resolve()
    monkeypatch.setenv("BTQ_SITE_REGISTRY_PATH", str(example))

    resolved = site_registry_data._resolve_path()
    assert resolved == example

    inst = instance_config.load_instance_config(
        site_registry_real_path=site_registry_data._REAL,
        site_registry_example_path=site_registry_data._EXAMPLE,
        brand_keywords_real_path=site_registry_data._BRAND_REAL,
        brand_keywords_example_path=site_registry_data._BRAND_EXAMPLE,
    )
    assert inst.site_registry_path == example

    data = site_registry_data.load_site_registry(force_reload=True)
    assert isinstance(data, dict)
    assert data  # non-empty synthetic registry


def test_brand_keywords_resolve_path_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    example = (
        REPO_ROOT / "project" / "event_pipeline" / "brand_keywords.example.json"
    ).resolve()
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(example))
    assert site_registry_data._resolve_brand_path() == example


# --------------------------------------------------------------------------- #
# DELEGATION IS REAL (mutation-style): one sentinel, two read paths.
# --------------------------------------------------------------------------- #
def test_sentinel_env_appears_in_both_instance_config_and_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "http://sentinel-couch.example:7777"
    monkeypatch.setenv("BTQ_COUCHDB_URL", sentinel)
    # get_instance_config() is lru-cached; clear so it re-reads the mutated env.
    instance_config.get_instance_config.cache_clear()
    try:
        inst = instance_config.get_instance_config()
        cfg = couchdb_config.from_env()
        # SAME sentinel surfaces through BOTH read paths -> single source.
        assert inst.couchdb_url.rstrip("/") == sentinel
        assert cfg.base_url == sentinel
    finally:
        instance_config.get_instance_config.cache_clear()


def test_unset_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    instance_config.get_instance_config.cache_clear()
    try:
        # No real config.json on a fresh clone -> example default url.
        assert couchdb_config.base_url() == instance_config.DEFAULT_COUCHDB_URL
    finally:
        instance_config.get_instance_config.cache_clear()


# --------------------------------------------------------------------------- #
# CLEAN-CLONE: example fallback exposes media_store=local + synthetic R2.
# --------------------------------------------------------------------------- #
def test_clean_clone_example_fallback_media_store_and_synthetic_r2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "BTQ_COUCHDB_URL",
        "BTQ_COUCHDB_USER",
        "BTQ_COUCHDB_PASSWORD",
        "BTQ_MEDIA_STORE",
        "BTQ_R2_ENDPOINT_URL",
        "BTQ_R2_BUCKET",
        "BTQ_R2_REGION",
        "BTQ_R2_ACCESS_KEY_ID",
        "BTQ_R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    inst = instance_config.load_instance_config(config_path=EXAMPLE_CONFIG)

    assert inst.media_store == "local"
    assert "EXAMPLE" in inst.r2_endpoint_url
    assert "cloudflarestorage.com" in inst.r2_endpoint_url
    assert inst.r2_bucket == "btq-example"
    assert inst.r2_access_key_id == ""
    assert inst.r2_secret_access_key == ""
    # couchdb fallback from the synthetic example is localhost (no real creds).
    assert inst.couchdb_url == "http://127.0.0.1:5984"
    assert inst.couchdb_user == ""
    assert inst.couchdb_password == ""


def test_media_store_default_is_local_with_no_config_and_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BTQ_MEDIA_STORE", raising=False)
    # Resolve against a non-existent config path -> pure code default.
    inst = instance_config.load_instance_config(config_path=REPO_ROOT / "does-not-exist.json")
    assert inst.media_store == "local"
    assert instance_config.DEFAULT_MEDIA_STORE == "local"


# --------------------------------------------------------------------------- #
# INERT R2: the R2 params are carried but NO storage/serving code consumes them.
# --------------------------------------------------------------------------- #
def test_r2_params_are_inert_no_consumer_in_project_source() -> None:
    project_dir = REPO_ROOT / "project"
    offenders: list[str] = []
    for path in project_dir.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "tests" in parts:
            continue
        if path.name == "instance_config.py":
            continue  # the defining module legitimately mentions the fields
        text = path.read_text(encoding="utf-8")
        for token in ("r2_endpoint_url", "r2_bucket", "r2_access_key_id", "r2_secret_access_key"):
            if token in text:
                offenders.append(f"{path}: {token}")
    assert offenders == [], f"R2 params are consumed by storage/serving code: {offenders}"


def test_site_registry_resolution_order_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # FS resolution helper behavior is preserved: explicit env override wins.
    custom = REPO_ROOT / "some" / "custom" / "registry.json"
    monkeypatch.setenv("BTQ_SITE_REGISTRY_PATH", str(custom))
    assert site_registry_data._resolve_path() == custom


# --------------------------------------------------------------------------- #
# ENGINE-AGNOSTIC NOT MOVED: queue/whisper/ffmpeg stay on PipelineConfig and are
# absent from InstanceConfig.
# --------------------------------------------------------------------------- #
def test_engine_settings_on_pipeline_config_not_instance_config() -> None:
    pipeline_fields = {f.name for f in pipeline_config_module.PipelineConfig.__dataclass_fields__.values()}
    instance_fields = set(instance_config.InstanceConfig.__dataclass_fields__.keys())

    for engine_field in ("queue_dir", "whisper_model", "ffmpeg_path_prefix"):
        assert engine_field in pipeline_fields, f"{engine_field} must stay on PipelineConfig"
        assert engine_field not in instance_fields, f"{engine_field} must NOT move to InstanceConfig"

    # InstanceConfig must not have leaked any engine-agnostic surface.
    for forbidden in ("queue_dir", "whisper_model", "ffmpeg_path_prefix", "audio_inbox_dir", "logs_dir"):
        assert forbidden not in instance_fields


def test_instance_config_holds_only_instance_surface() -> None:
    instance_fields = set(instance_config.InstanceConfig.__dataclass_fields__.keys())
    expected = {
        "couchdb_url",
        "couchdb_user",
        "couchdb_password",
        "couchdb_sites_db",
        "couchdb_field_captures_db",
        "couchdb_photo_vision_db",
        "couchdb_queue_db",
        "couchdb_vault_db",
        "couchdb_personal_journal_db",
        "couchdb_voice_memos_db",
        "site_registry_path",
        "brand_keywords_path",
        "media_store",
        "r2_endpoint_url",
        "r2_bucket",
        "r2_region",
        "r2_access_key_id",
        "r2_secret_access_key",
        "r2_secret_prefix",
    }
    assert instance_fields == expected
