from __future__ import annotations

import json
import types
import urllib.request

import pytest

import site_supplies
from queue_processor.handlers.supplies_equipment import (
    _build_equipment_request_entity_doc,
    _build_supply_need_entity_doc,
)


class _FindResponse:
    """Mimics the urlopen context-manager returned for a CouchDB ``_find`` POST."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> "_FindResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _supply_doc(supply_id: str, site_id: str, **extra: object) -> dict:
    doc = {
        "_id": f"supply_need_{supply_id}",
        "type": "supply_need",
        "supply_id": supply_id,
        "site_id": site_id,
        "item_name": f"item-{supply_id}",
    }
    doc.update(extra)
    return doc


# The three docs that exercise the bug: one with NO archived field (the
# regression case dropped pre-fix by the buggy ``$ne`` Mango selector), one
# explicitly archived:false, one archived:true.
_DOC_MISSING = _supply_doc("missing", "S1")  # no archived key at all
_DOC_FALSE = _supply_doc("falsey", "S1", archived=False)
_DOC_TRUE = _supply_doc("truey", "S1", archived=True)
_DOC_OTHER_SITE = _supply_doc("othersite", "S2", archived=False)


def _install_mock(monkeypatch: pytest.MonkeyPatch, docs: list[dict]) -> None:
    """Force the CouchDB path and stub urlopen to return ``docs`` for ``_find``.

    The real CouchDB ``_find`` selector pre-filtering is intentionally NOT
    emulated here: the implementation receives every doc and is responsible for
    archived/site/status filtering downstream in Python. That is exactly the
    behavior under test.
    """
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.delenv("BTQ_COUCHDB_USER", raising=False)
    monkeypatch.delenv("BTQ_COUCHDB_PASSWORD", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FindResponse({"docs": [dict(d) for d in docs]}),
    )


def _ids(result: dict) -> set[str]:
    return {s.supply_id for s in result["supplies"]}


def test_default_view_shows_missing_archived_and_hides_archived_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE])

    result = site_supplies.discover_site_supplies(site_id=None)
    ids = _ids(result)

    # Load-bearing regression-and-fix assertion: the doc with NO archived field
    # is visible (pre-fix it was dropped by archived:{"$ne": True}), the
    # explicit archived:false is visible, and genuinely-archived stays hidden.
    assert "missing" in ids
    assert "falsey" in ids
    assert "truey" not in ids
    assert ids == {"missing", "falsey"}


def test_include_archived_returns_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE])

    result = site_supplies.discover_site_supplies(
        site_id=None, include_archived=True
    )

    assert _ids(result) == {"missing", "falsey", "truey"}


def test_archived_only_returns_only_archived_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE])

    result = site_supplies.discover_site_supplies(
        site_id=None, archived_only=True
    )

    assert _ids(result) == {"truey"}


def test_site_id_filter_excludes_other_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(
        monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE, _DOC_OTHER_SITE]
    )

    result = site_supplies.discover_site_supplies(site_id="S1")
    ids = _ids(result)

    assert "othersite" not in ids
    assert ids == {"missing", "falsey"}


def _site_ctx() -> types.SimpleNamespace:
    # SiteContext only exposes site_id / name / account; the builders read the
    # first two. A namespace stand-in keeps the input minimal.
    return types.SimpleNamespace(site_id="S1", name="Site One", account=None)


def _job() -> types.SimpleNamespace:
    return types.SimpleNamespace(job_id="job-123")


def test_supply_need_entity_doc_includes_archived_false() -> None:
    doc = _build_supply_need_entity_doc(
        payload={"item_name": "mop"},
        job=_job(),
        site_ctx=_site_ctx(),
        supply_id="sup-1",
        created_at="2026-06-11T00:00:00+00:00",
    )

    assert doc["archived"] is False
    assert doc["type"] == "supply_need"


def test_equipment_request_entity_doc_includes_archived_false() -> None:
    doc = _build_equipment_request_entity_doc(
        payload={"item_name": "buffer"},
        job=_job(),
        site_ctx=_site_ctx(),
        equipment_id="eq-1",
        created_at="2026-06-11T00:00:00+00:00",
    )

    assert doc["archived"] is False
    assert doc["type"] == "equipment_request"
