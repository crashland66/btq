"""Independent behavioral verification of the read-only ops-dashboard /records view.

The author did NOT write the implementation under test
(``ops_dashboard.sections.records``). These tests gate the contract:

  1. list: date-descending, Date links to /records/<id>, Type="Shift Report", Prepared by
  2. journal exclusion (selector is type=shift_report, operator=op_greg)
  3. detail: render_markdown(content) + header + back link; not-found for bad id/type/operator
  4. degradation: _find raising -> empty state, no 500
  5. escaping of prepared_by / date
  6. nav entry

Plus beyond-spec probes and a few in-test mutation hooks documented in the report.

We stub ``records._find`` (the single CouchDB seam used by both the list path,
via ``_shift_report_docs``, and the detail path, via ``_load_record``). The stub
dispatches on the ``selector`` so one fake serves both, and records every selector
it is asked for so we can assert on the query contract.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.app import route_response
from ops_dashboard.sections import records


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query={})


def shift_report(
    doc_id: str,
    date: str,
    *,
    prepared_by: str = "Greg Stoltz",
    operator: str = "op_greg",
    doc_type: str = "shift_report",
    content: str = "# Shift\n\nAll quiet.",
) -> dict[str, object]:
    return {
        "_id": doc_id,
        "type": doc_type,
        "operator": operator,
        "date": date,
        "prepared_by": prepared_by,
        "content": content,
    }


def install_find(
    monkeypatch: pytest.MonkeyPatch,
    docs: list[dict[str, object]],
    *,
    raises: bool = False,
) -> list[dict[str, object]]:
    """Stub records._find. Returns the list of selectors it was asked for.

    Dispatch mirrors the real CouchDB: a selector with ``_id`` returns that one
    doc (list-of-one or empty); the list selector returns every doc that matches
    its type/operator constraints. ``fields`` projection is intentionally NOT
    applied (the section must not depend on it for correctness).
    """
    selectors: list[dict[str, object]] = []

    def fake_find(payload: dict[str, object]) -> list[dict[str, object]]:
        if raises:
            raise RuntimeError("couch is down")
        selector = dict(payload.get("selector") or {})
        selectors.append(selector)
        if "_id" in selector:
            return [d for d in docs if d.get("_id") == selector["_id"]][:1]
        # list query: honor whatever constraints the section put in the selector
        wanted_type = selector.get("type")
        wanted_op = selector.get("operator")
        out = []
        for d in docs:
            if wanted_type is not None and d.get("type") != wanted_type:
                continue
            if wanted_op is not None and d.get("operator") != wanted_op:
                continue
            out.append(d)
        return out

    monkeypatch.setattr(records, "_find", fake_find)
    return selectors


# ---------------------------------------------------------------------------
# 1. List ordering / links / columns
# ---------------------------------------------------------------------------

def test_list_orders_dates_descending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [
            shift_report("sr_a", "2026-06-10"),
            shift_report("sr_c", "2026-06-12"),
            shift_report("sr_b", "2026-06-11"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    i12 = html.index("2026-06-12")
    i11 = html.index("2026-06-11")
    i10 = html.index("2026-06-10")
    assert i12 < i11 < i10, "rows must be date-descending"


def test_list_date_links_to_record_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_42", "2026-06-12")])

    html = records.render(DummyContext(tmp_path))

    assert 'href="/records/sr_42"' in html
    assert ">2026-06-12<" in html  # the date text is the link label


def test_list_shows_type_and_prepared_by(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_1", "2026-06-12", prepared_by="Greg Stoltz")])

    html = records.render(DummyContext(tmp_path))

    assert "Shift Report" in html
    assert "Greg Stoltz" in html
    assert 'active_section' not in html  # sanity: rendered, not a repr


def test_list_route_in_process_returns_200(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_1", "2026-06-12")])

    status, content_type, body = route_response("GET", "/records", tmp_path / "runtime")

    assert int(status) == 200
    assert "text/html" in content_type
    assert b"sr_1" in body
    assert b"Records" in body


# ---------------------------------------------------------------------------
# 2. Journal exclusion -- selector contract
# ---------------------------------------------------------------------------

def test_list_selector_pins_type_and_operator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    selectors = install_find(monkeypatch, [shift_report("sr_1", "2026-06-12")])

    records.render(DummyContext(tmp_path))

    list_selectors = [s for s in selectors if "_id" not in s]
    assert list_selectors, "the list path must issue a _find query"
    # 365: the combined list now sources both operational record types; each query
    # still pins operator=op_greg and an operational type (journal stays excluded).
    queried_types = set()
    for sel in list_selectors:
        assert sel.get("operator") == "op_greg"
        assert sel.get("type") in {"shift_report", "day_record"}
        queried_types.add(sel.get("type"))
    assert queried_types == {"shift_report", "day_record"}


def test_journal_docs_never_appear_in_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A journal doc with the right operator but wrong type must be excluded by
    # the selector. Our stub honors the selector, so if the section ever asked
    # for type=journal this would leak.
    install_find(
        monkeypatch,
        [
            shift_report("sr_1", "2026-06-12"),
            {
                "_id": "journal_2026-06-12",
                "type": "journal",
                "operator": "op_greg",
                "date": "2026-06-12",
                "prepared_by": "Greg Stoltz",
                "content": "secret journal body",
            },
        ],
    )

    html = records.render(DummyContext(tmp_path))

    assert "journal_2026-06-12" not in html
    assert "secret journal body" not in html
    assert "sr_1" in html


# ---------------------------------------------------------------------------
# 3. Detail
# ---------------------------------------------------------------------------

def test_detail_renders_markdown_and_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [shift_report("sr_1", "2026-06-12", prepared_by="Greg Stoltz", content="# Heading\n\nBody text.")],
    )

    html = records.render_detail(DummyContext(tmp_path), "sr_1")

    assert '<div class="md">' in html  # render_markdown wrapper
    assert "Body text." in html
    assert "2026-06-12" in html
    assert "Shift Report" in html
    assert "Greg Stoltz" in html
    assert 'href="/records"' in html  # back link to list
    assert "Record not found" not in html


def test_detail_route_in_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_1", "2026-06-12", content="Hello body.")])

    status, content_type, body = route_response("GET", "/records/sr_1", tmp_path / "runtime")

    assert int(status) == 200
    assert b'<div class="md">' in body
    assert b"Hello body." in body


def test_detail_unknown_id_is_not_found_not_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_1", "2026-06-12")])

    html = records.render_detail(DummyContext(tmp_path), "sr_does_not_exist")

    assert "Record not found" in html
    assert '<div class="md">' not in html


def test_detail_wrong_type_is_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A doc that exists by _id but is a journal must NOT render as a record.
    install_find(
        monkeypatch,
        [
            {
                "_id": "journal_x",
                "type": "journal",
                "operator": "op_greg",
                "date": "2026-06-12",
                "content": "journal body should not show",
            }
        ],
    )

    html = records.render_detail(DummyContext(tmp_path), "journal_x")

    assert "Record not found" in html
    assert "journal body should not show" not in html


def test_detail_wrong_operator_is_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [shift_report("sr_other", "2026-06-12", operator="op_someone_else", content="not greg")],
    )

    html = records.render_detail(DummyContext(tmp_path), "sr_other")

    assert "Record not found" in html
    assert "not greg" not in html


# ---------------------------------------------------------------------------
# 4. Degradation
# ---------------------------------------------------------------------------

def test_list_degrades_to_empty_state_when_find_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [], raises=True)

    html = records.render(DummyContext(tmp_path))

    assert "No records yet." in html  # empty_text from render()
    # page still wrapped / no exception bubbled


def test_list_route_degrades_returns_200(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [], raises=True)

    status, _ct, body = route_response("GET", "/records", tmp_path / "runtime")

    assert int(status) == 200
    assert b"No records yet." in body


def test_detail_degrades_to_not_found_when_find_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [], raises=True)

    html = records.render_detail(DummyContext(tmp_path), "sr_1")

    assert "Record not found" in html


# ---------------------------------------------------------------------------
# 5. Escaping
# ---------------------------------------------------------------------------

def test_list_escapes_prepared_by_html(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [shift_report("sr_1", "2026-06-12", prepared_by="<script>alert(1)</script>")],
    )

    html = records.render(DummyContext(tmp_path))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_detail_escapes_prepared_by_and_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [
            shift_report(
                "sr_1",
                "2026<b>06</b>12",
                prepared_by="<img src=x onerror=alert(1)>",
                content="safe body",
            )
        ],
    )

    html = records.render_detail(DummyContext(tmp_path), "sr_1")

    assert "<img src=x onerror=alert(1)>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<b>06</b>" not in html  # date in header must be escaped
    assert "safe body" in html  # content (trusted markdown) still rendered


def test_list_escapes_date_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [shift_report("sr_1", "<b>2026</b>")])

    html = records.render(DummyContext(tmp_path))

    assert "<b>2026</b>" not in html
    assert "&lt;b&gt;2026&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# 6. Nav
# ---------------------------------------------------------------------------

def test_nav_items_includes_records() -> None:
    from ops_dashboard.layout import NAV_ITEMS

    entries = [item for item in NAV_ITEMS if item[0] == "records"]
    assert entries, "NAV_ITEMS must contain a 'records' entry"
    section, label, href = entries[0][0], entries[0][1], entries[0][2]
    assert section == "records"
    assert label == "Records"
    assert href == "/records"


# ---------------------------------------------------------------------------
# Beyond-spec probes
# ---------------------------------------------------------------------------

def test_empty_list_shows_graceful_empty_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(monkeypatch, [])

    html = records.render(DummyContext(tmp_path))

    assert "No records yet." in html


def test_route_record_id_with_slash_is_not_dispatched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The app.py prefix guard refuses ids containing a slash. It should NOT reach
    # render_detail; it falls through to the global 404.
    called = {"detail": False}

    def boom(_ctx: object, _rid: str) -> str:
        called["detail"] = True
        return "SHOULD NOT RENDER"

    monkeypatch.setattr(records, "render_detail", boom)

    status, _ct, body = route_response("GET", "/records/foo/bar", tmp_path / "runtime")

    assert called["detail"] is False
    assert int(status) == 404
    assert b"SHOULD NOT RENDER" not in body


def test_route_empty_record_id_not_dispatched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = {"detail": False}

    def boom(_ctx: object, _rid: str) -> str:
        called["detail"] = True
        return "SHOULD NOT RENDER"

    monkeypatch.setattr(records, "render_detail", boom)

    # "/records/" -> after prefix strip + rstrip("/") the id is empty -> falls
    # through. (It is also not in SECTION_ROUTES.)
    status, _ct, _body = route_response("GET", "/records/", tmp_path / "runtime")

    assert called["detail"] is False
    assert int(status) == 404


def test_list_tolerates_missing_prepared_by_and_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Docs missing prepared_by / date must not crash the list render.
    install_find(
        monkeypatch,
        [
            {"_id": "sr_nofields", "type": "shift_report", "operator": "op_greg", "content": "x"},
            shift_report("sr_full", "2026-06-12"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    # No crash; the no-fields row still links by id (date label falls back to id).
    assert 'href="/records/sr_nofields"' in html
    assert "sr_full" in html
    assert "None" not in html  # empty fields must not stringify to "None"


def test_detail_tolerates_missing_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [{"_id": "sr_min", "type": "shift_report", "operator": "op_greg"}],
    )

    html = records.render_detail(DummyContext(tmp_path), "sr_min")

    assert "Record not found" not in html
    assert "Shift Report" in html  # title falls back to type label when no date
    assert "None" not in html


def test_doc_missing_id_is_skipped_in_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_find(
        monkeypatch,
        [
            {"type": "shift_report", "operator": "op_greg", "date": "2026-06-12", "prepared_by": "Ghost"},
            shift_report("sr_real", "2026-06-11"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    # The id-less doc cannot produce a usable link; it must be dropped.
    assert "Ghost" not in html
    assert "sr_real" in html
