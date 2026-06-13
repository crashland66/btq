from __future__ import annotations

import json
import urllib.request

import pytest

import site_equipment


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


def _equipment_doc(equipment_id: str, site_id: str, **extra: object) -> dict:
    doc = {
        "_id": f"equipment_request_{equipment_id}",
        "type": "equipment_request",
        "equipment_id": equipment_id,
        "site_id": site_id,
        "equipment_name": f"equip-{equipment_id}",
    }
    doc.update(extra)
    return doc


# The three docs that exercise the bug: one with NO archived field (the
# regression case dropped pre-fix by the buggy ``$ne`` Mango selector), one
# explicitly archived:false, one archived:true.
_DOC_MISSING = _equipment_doc("missing", "S1")  # no archived key at all
_DOC_FALSE = _equipment_doc("falsey", "S1", archived=False)
_DOC_TRUE = _equipment_doc("truey", "S1", archived=True)
_DOC_OTHER_SITE = _equipment_doc("othersite", "S2", archived=False)


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
    return {e.equipment_id for e in result["equipment"]}


def test_default_view_shows_missing_archived_and_hides_archived_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE])

    result = site_equipment.discover_site_equipment(site_id=None)
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

    result = site_equipment.discover_site_equipment(
        site_id=None, include_archived=True
    )

    assert _ids(result) == {"missing", "falsey", "truey"}


def test_archived_only_returns_only_archived_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE])

    result = site_equipment.discover_site_equipment(
        site_id=None, archived_only=True
    )

    assert _ids(result) == {"truey"}


def test_site_id_filter_excludes_other_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock(
        monkeypatch, [_DOC_MISSING, _DOC_FALSE, _DOC_TRUE, _DOC_OTHER_SITE]
    )

    result = site_equipment.discover_site_equipment(site_id="S1")
    ids = _ids(result)

    assert "othersite" not in ids
    assert ids == {"missing", "falsey"}
