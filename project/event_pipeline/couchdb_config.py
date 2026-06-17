from __future__ import annotations

import base64
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from instance_config import (
    DEFAULT_COUCHDB_URL,
    DEFAULT_FIELD_CAPTURES_DB,
    DEFAULT_PEOPLE_DB,
    DEFAULT_PERSONAL_JOURNAL_DB,
    DEFAULT_PHOTO_VISION_DB,
    DEFAULT_QUEUE_DB,
    DEFAULT_SITES_DB,
    DEFAULT_VAULT_DB,
    DEFAULT_VOICE_MEMOS_DB,
    load_instance_config,
)


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_LISTENER_TIMEOUT_SECONDS = 60.0
DEFAULT_HEARTBEAT_MS = 10000


class CouchDBConfigError(Exception):
    pass


@dataclass(frozen=True)
class CouchDBConfig:
    base_url: str
    username: str
    password: str
    timeout: float
    heartbeat_ms: int

    def auth_header(self) -> Mapping[str, str]:
        if not self.username and not self.password:
            return {}
        credentials = f"{self.username}:{self.password}".encode("utf-8")
        return {"Authorization": f"Basic {base64.b64encode(credentials).decode('ascii')}"}

    def assert_can_authenticate(self) -> None:
        headers = self.auth_header()
        if not headers:
            return
        url = f"{self.base_url}/_session"
        req = urllib.request.Request(url, headers={**headers, "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if 200 <= resp.status < 300:
                    return
                raise CouchDBConfigError(
                    f"CouchDB credentials rejected at startup: HTTP {resp.status} ({url})"
                )
        except urllib.error.HTTPError as exc:
            raise CouchDBConfigError(
                f"CouchDB credentials rejected at startup: HTTP {exc.code} ({url})"
            ) from exc
        except urllib.error.URLError as exc:
            raise CouchDBConfigError(
                f"CouchDB unreachable at startup ({url}): {exc.reason}"
            ) from exc


def base_url(override: str | None = None) -> str:
    raw = override if override is not None else load_instance_config().couchdb_url
    return raw.rstrip("/")


def username(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_user


def password(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_password


def timeout(override: float | None = None, *, default: float = DEFAULT_TIMEOUT_SECONDS) -> float:
    if override is not None:
        return _validate_positive_float("override timeout", override)
    raw = os.environ.get("BTQ_COUCHDB_TIMEOUT", "")
    if not raw:
        return default
    return _validate_positive_float("BTQ_COUCHDB_TIMEOUT", raw)


def heartbeat_ms(override: int | None = None, *, default: int = DEFAULT_HEARTBEAT_MS) -> int:
    if override is not None:
        return _validate_positive_int("override heartbeat_ms", override)
    raw = os.environ.get("BTQ_COUCHDB_HEARTBEAT_MS", "")
    if not raw:
        return default
    return _validate_positive_int("BTQ_COUCHDB_HEARTBEAT_MS", raw)


def sites_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_sites_db


def field_captures_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_field_captures_db


def people_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_people_db


def photo_vision_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_photo_vision_db


def queue_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_queue_db


def vault_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_vault_db


def personal_journal_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_personal_journal_db


def voice_memos_database(override: str | None = None) -> str:
    return override if override is not None else load_instance_config().couchdb_voice_memos_db


def from_env(
    *,
    base_url_override: str | None = None,
    username_override: str | None = None,
    password_override: str | None = None,
    timeout_override: float | None = None,
    heartbeat_ms_override: int | None = None,
    timeout_default: float = DEFAULT_TIMEOUT_SECONDS,
) -> CouchDBConfig:
    u = username(username_override)
    p = password(password_override)
    if bool(u) != bool(p):
        raise CouchDBConfigError(
            "CouchDB credentials are asymmetric: "
            f"BTQ_COUCHDB_USER is {'set' if u else 'empty'} but "
            f"BTQ_COUCHDB_PASSWORD is {'set' if p else 'empty'}. "
            "Set both or neither."
        )
    return CouchDBConfig(
        base_url=base_url(base_url_override),
        username=u,
        password=p,
        timeout=timeout(timeout_override, default=timeout_default),
        heartbeat_ms=heartbeat_ms(heartbeat_ms_override),
    )


def _validate_positive_float(name: str, value: object) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CouchDBConfigError(f"Invalid {name}: {value!r}") from exc
    if result <= 0:
        raise CouchDBConfigError(f"Invalid {name}: {value!r}")
    return result


def _validate_positive_int(name: str, value: object) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CouchDBConfigError(f"Invalid {name}: {value!r}") from exc
    if result <= 0:
        raise CouchDBConfigError(f"Invalid {name}: {value!r}")
    return result
