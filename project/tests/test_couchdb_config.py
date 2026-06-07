from __future__ import annotations

import base64
import urllib.error

import pytest

from event_pipeline import couchdb_config


@pytest.fixture(autouse=True)
def clear_couchdb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BTQ_COUCHDB_URL",
        "BTQ_COUCHDB_USER",
        "BTQ_COUCHDB_PASSWORD",
        "BTQ_COUCHDB_TIMEOUT",
        "BTQ_COUCHDB_HEARTBEAT_MS",
        "BTQ_COUCHDB_SITES_DB",
        "BTQ_COUCHDB_VAULT_DB",
    ):
        monkeypatch.delenv(name, raising=False)


def test_base_url_strips_trailing_slash() -> None:
    assert couchdb_config.base_url("http://couchdb.test/") == "http://couchdb.test"


def test_timeout_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_TIMEOUT", "12.5")

    assert couchdb_config.timeout() == 12.5


def test_timeout_raises_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_TIMEOUT", "nope")

    with pytest.raises(couchdb_config.CouchDBConfigError):
        couchdb_config.timeout()


def test_timeout_uses_default_when_unset() -> None:
    assert couchdb_config.timeout(default=22.0) == 22.0


def test_heartbeat_ms_uses_env_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_HEARTBEAT_MS", "25000")

    assert couchdb_config.heartbeat_ms() == 25000


def test_auth_header_returns_empty_when_no_credentials() -> None:
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="",
        password="",
        timeout=10.0,
        heartbeat_ms=10000,
    )

    assert config.auth_header() == {}


def test_auth_header_uses_basic_auth_when_set() -> None:
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="jordan",
        password="secret",
        timeout=10.0,
        heartbeat_ms=10000,
    )
    expected = base64.b64encode(b"jordan:secret").decode("ascii")

    assert config.auth_header() == {"Authorization": f"Basic {expected}"}


def test_from_env_raises_when_user_empty_password_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_USER", "")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "secret")

    with pytest.raises(couchdb_config.CouchDBConfigError, match="BTQ_COUCHDB_USER is empty"):
        couchdb_config.from_env()


def test_from_env_raises_when_user_set_password_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_USER", "jordan")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "")

    with pytest.raises(couchdb_config.CouchDBConfigError, match="BTQ_COUCHDB_PASSWORD is empty"):
        couchdb_config.from_env()


def test_from_env_allows_both_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_USER", "")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "")

    config = couchdb_config.from_env()

    assert config.username == ""
    assert config.password == ""


def test_from_env_allows_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_USER", "jordan")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "secret")

    config = couchdb_config.from_env()

    assert config.username == "jordan"
    assert config.password == "secret"


def test_assert_can_authenticate_skips_when_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("urlopen should not be called")

    monkeypatch.setattr(couchdb_config.urllib.request, "urlopen", raise_if_called)
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="",
        password="",
        timeout=10.0,
        heartbeat_ms=10000,
    )

    config.assert_can_authenticate()


def test_assert_can_authenticate_passes_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(couchdb_config.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="jordan",
        password="secret",
        timeout=10.0,
        heartbeat_ms=10000,
    )

    assert config.assert_can_authenticate() is None


def test_assert_can_authenticate_raises_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_401(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.HTTPError("http://couchdb.test/_session", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(couchdb_config.urllib.request, "urlopen", raise_401)
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="jordan",
        password="secret",
        timeout=10.0,
        heartbeat_ms=10000,
    )

    with pytest.raises(couchdb_config.CouchDBConfigError, match="HTTP 401"):
        config.assert_can_authenticate()


def test_assert_can_authenticate_raises_on_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_url_error(*_args: object, **_kwargs: object) -> object:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(couchdb_config.urllib.request, "urlopen", raise_url_error)
    config = couchdb_config.CouchDBConfig(
        base_url="http://couchdb.test",
        username="jordan",
        password="secret",
        timeout=10.0,
        heartbeat_ms=10000,
    )

    with pytest.raises(couchdb_config.CouchDBConfigError, match="unreachable"):
        config.assert_can_authenticate()


def test_from_env_composes_full_config_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test/")
    monkeypatch.setenv("BTQ_COUCHDB_USER", "jordan")
    monkeypatch.setenv("BTQ_COUCHDB_PASSWORD", "secret")
    monkeypatch.setenv("BTQ_COUCHDB_TIMEOUT", "42")
    monkeypatch.setenv("BTQ_COUCHDB_HEARTBEAT_MS", "15000")

    config = couchdb_config.from_env()

    assert config.base_url == "http://couchdb.test"
    assert config.username == "jordan"
    assert config.password == "secret"
    assert config.timeout == 42.0
    assert config.heartbeat_ms == 15000


def test_vault_database_default() -> None:
    assert couchdb_config.vault_database() == "btq_vault"


def test_vault_database_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_VAULT_DB", "custom_vault")

    assert couchdb_config.vault_database() == "custom_vault"
