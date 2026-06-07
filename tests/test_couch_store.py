from __future__ import annotations

import io
import json
from typing import Any
from urllib import error

import pytest

from btq_vault.couch_store import CouchDBEntityStore, CouchDBEntityStoreError


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        if self._payload is None:
            return b""
        return json.dumps(self._payload).encode("utf-8")


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://example.test", code, "error", {}, io.BytesIO(b""))


def install_urlopen(monkeypatch, events: list[Any], requests: list[dict[str, Any]]) -> None:
    def fake_urlopen(req, timeout: float):
        requests.append(
            {
                "method": req.get_method(),
                "url": req.full_url,
                "timeout": timeout,
                "body": json.loads(req.data.decode("utf-8")) if req.data else None,
            }
        )
        event = events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event

    monkeypatch.setattr("btq_vault.couch_store.request.urlopen", fake_urlopen)


def test_always_enabled(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_VAULT_COUCHDB_WRITE", raising=False)

    assert CouchDBEntityStore.enabled() is True


def test_upsert_creates_document(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [http_error(404), FakeResponse(201, {"ok": True})], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    store.upsert({"_id": "x", "type": "visit"})

    assert [request["method"] for request in requests] == ["GET", "PUT"]
    assert requests[1]["body"] == {"_id": "x", "type": "visit"}


def test_upsert_updates_existing_document(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [FakeResponse(200, {"_id": "x", "_rev": "1-abc"}), FakeResponse(201, {"ok": True})], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    store.upsert({"_id": "x", "type": "visit"})

    assert requests[1]["body"] == {"_id": "x", "_rev": "1-abc", "type": "visit"}


def test_upsert_retries_on_409(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(
        monkeypatch,
        [
            FakeResponse(200, {"_id": "x", "_rev": "1-abc"}),
            http_error(409),
            FakeResponse(200, {"_id": "x", "_rev": "2-def"}),
            FakeResponse(201, {"ok": True}),
        ],
        requests,
    )
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    store.upsert({"_id": "x", "type": "visit"})

    assert [request["method"] for request in requests] == ["GET", "PUT", "GET", "PUT"]
    assert requests[3]["body"]["_rev"] == "2-def"


def test_get_required_raises_when_absent(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [http_error(404)], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    with pytest.raises(CouchDBEntityStoreError, match="employee_missing"):
        store.get_required("employee_missing")

    assert [request["method"] for request in requests] == ["GET"]


def test_get_optional_returns_none_when_absent(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [http_error(404)], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    assert store.get_optional("employee_missing") is None
    assert [request["method"] for request in requests] == ["GET"]


def test_put_with_rev_sets_rev_and_returns_new_rev(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [FakeResponse(201, {"ok": True, "id": "employee_prs_1", "rev": "2-new"})], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    result = store.put_with_rev(
        {"_id": "employee_prs_1", "_rev": "stale", "type": "employee"},
        expected_rev="1-old",
    )

    assert requests[0]["method"] == "PUT"
    assert requests[0]["body"] == {"_id": "employee_prs_1", "_rev": "1-old", "type": "employee"}
    assert result == {"_id": "employee_prs_1", "_rev": "2-new", "type": "employee"}


def test_patch_status_updates_field(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(
        monkeypatch,
        [FakeResponse(200, {"_id": "supply_need_sup_1", "_rev": "1-abc", "status": "open"}), FakeResponse(201, {"ok": True})],
        requests,
    )
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    store.patch_status("supply_need_sup_1", "ordered")

    assert requests[1]["method"] == "PUT"
    assert requests[1]["body"]["status"] == "ordered"


def test_patch_status_noop_on_missing_doc(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [http_error(404)], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    store.patch_status("supply_need_sup_missing", "ordered")

    assert [request["method"] for request in requests] == ["GET"]


def test_patch_status_can_require_existing_doc(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []
    install_urlopen(monkeypatch, [http_error(404)], requests)
    store = CouchDBEntityStore("http://couchdb.test", {}, "btq_vault")

    with pytest.raises(CouchDBEntityStoreError, match="supply_need_sup_missing"):
        store.patch_status("supply_need_sup_missing", "ordered", require_existing=True)

    assert [request["method"] for request in requests] == ["GET"]
