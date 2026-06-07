from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any
from urllib import error

import pytest

from btq_vault import backup_worker
from btq_vault.backup_worker import BackupError, DEFAULT_RETAIN_COUNT, check_freshness, main, write_backup


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_write_backup_creates_timestamped_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "btq_vault.backup_worker.request.urlopen",
        lambda req, timeout: FakeResponse({"rows": [{"doc": {"_id": "x", "type": "location"}}]}),
    )

    path = write_backup(tmp_path, "http://couchdb.test", {}, "btq_vault")

    assert path.name.startswith("btq_vault_")
    backups = list(tmp_path.glob("btq_vault_*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["rows"][0]["doc"]["_id"] == "x"


def test_write_backup_retains_last_n(tmp_path, monkeypatch) -> None:
    for index in range(DEFAULT_RETAIN_COUNT + 2):
        (tmp_path / f"btq_vault_20260524T1200{index:02d}Z.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "btq_vault.backup_worker.request.urlopen",
        lambda req, timeout: FakeResponse({"rows": [{"doc": {"_id": "x", "type": "location"}}]}),
    )

    write_backup(tmp_path, "http://couchdb.test", {}, "btq_vault")

    assert len(list(tmp_path.glob("btq_vault_*.json"))) == DEFAULT_RETAIN_COUNT


def test_write_backup_raises_on_http_error(monkeypatch, tmp_path) -> None:
    def fake_urlopen(req, timeout):
        raise error.HTTPError("http://couchdb.test", 500, "error", {}, io.BytesIO(b""))

    monkeypatch.setattr("btq_vault.backup_worker.request.urlopen", fake_urlopen)

    with pytest.raises(BackupError):
        write_backup(tmp_path, "http://couchdb.test", {}, "btq_vault")


def test_check_freshness_fresh_when_recent_backup(tmp_path, monkeypatch) -> None:
    (tmp_path / "btq_vault_20260524T120000Z.json").write_text("{}\n", encoding="utf-8")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 24, 13, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(backup_worker, "datetime", FrozenDateTime)

    assert check_freshness(tmp_path)["fresh"] is True


def test_main_requires_backup_dir(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_VAULT_BACKUP_DIR", raising=False)

    assert main() != 0
