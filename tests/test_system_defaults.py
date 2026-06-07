from __future__ import annotations

import json
from urllib import error

import pytest

from event_pipeline.couchdb import push_design_doc, system_defaults


class FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def http_error(code: int) -> error.HTTPError:
    return error.HTTPError("http://couchdb.test", code, "err", {}, None)


@pytest.fixture(autouse=True)
def couchdb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")


def test_load_returns_skeleton_when_doc_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: object, timeout: float = 10.0) -> FakeResponse:
        raise http_error(404)

    monkeypatch.setattr(system_defaults.request, "urlopen", fake_urlopen)

    doc = system_defaults.load_system_defaults()

    assert doc["_id"] == "system_defaults"
    assert "_rev" not in doc


def test_load_returns_existing_doc_with_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_defaults.request, "urlopen", lambda _req, timeout=10.0: FakeResponse({"_id": "system_defaults", "_rev": "1-a"}))

    assert system_defaults.load_system_defaults()["_rev"] == "1-a"


def test_save_creates_doc_when_rev_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def fake_urlopen(req: object, timeout: float = 10.0) -> FakeResponse:
        seen.append(json.loads(getattr(req, "data").decode("utf-8")))
        return FakeResponse({"ok": True, "rev": "1-new"}, 201)

    monkeypatch.setattr(system_defaults.request, "urlopen", fake_urlopen)

    saved = system_defaults.save_system_defaults(system_defaults.default_skeleton(), None)

    assert seen[0]["_id"] == "system_defaults"
    assert "_rev" not in seen[0]
    assert saved["_rev"] == "1-new"


def test_save_updates_doc_with_correct_rev(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def fake_urlopen(req: object, timeout: float = 10.0) -> FakeResponse:
        seen.append(json.loads(getattr(req, "data").decode("utf-8")))
        return FakeResponse({"ok": True, "rev": "2-new"})

    monkeypatch.setattr(system_defaults.request, "urlopen", fake_urlopen)

    system_defaults.save_system_defaults(system_defaults.default_skeleton(), "1-old")

    assert seen[0]["_rev"] == "1-old"


def test_save_raises_conflict_with_current_doc_on_409(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}

    def fake_urlopen(_req: object, timeout: float = 10.0) -> FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            raise http_error(409)
        return FakeResponse({"_id": "system_defaults", "_rev": "server"})

    monkeypatch.setattr(system_defaults.request, "urlopen", fake_urlopen)

    with pytest.raises(system_defaults.SystemDefaultsConflictError) as exc:
        system_defaults.save_system_defaults(system_defaults.default_skeleton(), "stale")

    assert exc.value.current_doc["_rev"] == "server"


def test_save_passes_through_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_defaults.request, "urlopen", lambda _req, timeout=10.0: (_ for _ in ()).throw(http_error(500)))

    with pytest.raises(error.HTTPError):
        system_defaults.save_system_defaults(system_defaults.default_skeleton(), None)


def test_load_raises_when_couchdb_url_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    with pytest.raises(Exception, match="BTQ_COUCHDB_URL"):
        system_defaults.load_system_defaults()


def test_seed_system_defaults_if_absent_creates_only_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    saves: list[tuple[dict[str, object], str | None]] = []
    monkeypatch.setattr(push_design_doc.system_defaults, "load_system_defaults", lambda: system_defaults.default_skeleton())
    monkeypatch.setattr(push_design_doc.system_defaults, "save_system_defaults", lambda doc, rev: saves.append((doc, rev)) or {"_rev": "1-a"})

    assert push_design_doc.seed_system_defaults_if_absent() == "created"
    assert len(saves) == 1


def test_seed_system_defaults_if_absent_does_not_overwrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_design_doc.system_defaults, "load_system_defaults", lambda: {"_id": "system_defaults", "_rev": "1-a"})
    monkeypatch.setattr(push_design_doc.system_defaults, "save_system_defaults", lambda _doc, _rev: pytest.fail("should not save"))

    assert push_design_doc.seed_system_defaults_if_absent() == "already exists"
