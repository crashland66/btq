"""Independent behavioral verification for prompt 365.

The ``/records`` ops-dashboard view now sources ``day_record`` docs in addition
to ``shift_report`` (uncommitted change in
``ops_dashboard.sections.records``). The verifier did NOT write the impl.

Contract gated here:
  1. list merges shift_report + day_record into ONE date-descending table; Type
     column reads "Shift Report" / "Day Record"; each date links to /records/<id>
  2. day_record detail renders content via render_markdown, a Date + Type header,
     a back link, and NO "Prepared by" row (day records have none)
  3. shift_report detail still renders its Prepared-by row + content unchanged
  4. list queries pin operator=op_greg and only types {shift_report, day_record};
     a journal doc never appears; a non-op_greg id is not-found
  5. _find error degrades to empty list / not-found (no 500/exception)

Plus beyond-spec probes (only-day-records; wrong-operator day_record not found;
mixed-date cross-type sort; markdown special chars verbatim).

Harness is modeled on ``test_records_view.py``: we stub ``records._find`` (the
single CouchDB seam) with a dispatcher that honors the selector, and record
every selector so we can assert the query contract. A ``day_record(...)`` helper
is added analogously to ``shift_report(...)`` but with NO ``prepared_by``.
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


def day_record(
    doc_id: str,
    date: str,
    *,
    operator: str = "op_greg",
    doc_type: str = "day_record",
    content: str = "# Day\n\nDetailed log.",
) -> dict[str, object]:
    """Analogous to ``shift_report`` but day records carry NO ``prepared_by``."""
    return {
        "_id": doc_id,
        "type": doc_type,
        "operator": operator,
        "date": date,
        "content": content,
    }


def install_find(
    monkeypatch: pytest.MonkeyPatch,
    docs: list[dict[str, object]],
    *,
    raises: bool = False,
) -> list[dict[str, object]]:
    """Stub records._find. Returns the list of selectors it was asked for.

    Dispatch mirrors real CouchDB: a selector with ``_id`` returns that one doc
    (list-of-one or empty); the list selector returns every doc that matches its
    type/operator constraints. ``fields`` projection is intentionally NOT applied.
    """
    selectors: list[dict[str, object]] = []

    def fake_find(payload: dict[str, object]) -> list[dict[str, object]]:
        if raises:
            raise RuntimeError("couch is down")
        selector = dict(payload.get("selector") or {})
        selectors.append(selector)
        if "_id" in selector:
            return [d for d in docs if d.get("_id") == selector["_id"]][:1]
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
# 1. Combined list: both types, one table, date-descending, Type labels, links
# ---------------------------------------------------------------------------

def test_list_includes_both_shift_report_and_day_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [
            shift_report("sr_1", "2026-06-12"),
            day_record("dr_1", "2026-06-11"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    # Both docs present in the single table.
    assert 'href="/records/sr_1"' in html
    assert 'href="/records/dr_1"' in html
    # Type labels for each row.
    assert "Shift Report" in html
    assert "Day Record" in html


def test_list_type_column_labels_are_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_1", "2026-06-11")])

    html = records.render(DummyContext(tmp_path))

    assert "Day Record" in html
    # A day record must not be mislabeled as a shift report.
    assert "Shift Report" not in html


def test_list_dates_descending_across_both_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Interleave types so a per-source (non-merged) sort would get this wrong:
    # sources arrive shift_reports-then-day_records, but the true date order
    # alternates between the two types.
    install_find(
        monkeypatch,
        [
            shift_report("sr_old", "2026-06-09"),
            shift_report("sr_new", "2026-06-13"),
            day_record("dr_mid_hi", "2026-06-12"),
            day_record("dr_mid_lo", "2026-06-10"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    order = [
        html.index("2026-06-13"),
        html.index("2026-06-12"),
        html.index("2026-06-10"),
        html.index("2026-06-09"),
    ]
    assert order == sorted(order), "merged rows must be date-descending across both types"


def test_list_day_record_date_links_to_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_42", "2026-06-12")])

    html = records.render(DummyContext(tmp_path))

    assert 'href="/records/dr_42"' in html
    assert ">2026-06-12<" in html


def test_list_route_in_process_returns_200_with_day_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_1", "2026-06-12")])

    status, content_type, body = route_response("GET", "/records", tmp_path / "runtime")

    assert int(status) == 200
    assert "text/html" in content_type
    assert b"dr_1" in body
    assert b"Day Record" in body


# ---------------------------------------------------------------------------
# 2. Day-record detail: markdown content, header, back link, NO Prepared-by row
# ---------------------------------------------------------------------------

def test_day_record_detail_renders_markdown_and_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [day_record("dr_1", "2026-06-12", content="# Heading\n\nDay body text.")],
    )

    html = records.render_detail(DummyContext(tmp_path), "dr_1")

    assert '<div class="md">' in html  # render_markdown wrapper
    assert "<h1>Heading</h1>" in html  # markdown actually rendered
    assert "Day body text." in html
    assert "2026-06-12" in html  # Date in header
    assert "Day Record" in html  # Type label
    assert 'href="/records"' in html  # back link
    assert "Record not found" not in html


def test_day_record_detail_omits_prepared_by_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_1", "2026-06-12")])

    html = records.render_detail(DummyContext(tmp_path), "dr_1")

    # The detail uses a <dt>label</dt> structure; the Prepared-by row must be
    # absent entirely (not present-but-empty) for day records.
    assert "<dt>Prepared by</dt>" not in html
    assert "Prepared by" not in html
    # Date + Type rows are still present.
    assert "<dt>Date</dt>" in html
    assert "<dt>Type</dt>" in html


def test_day_record_detail_route_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_1", "2026-06-12", content="Hello day body.")])

    status, content_type, body = route_response("GET", "/records/dr_1", tmp_path / "runtime")

    assert int(status) == 200
    assert b'<div class="md">' in body
    assert b"Hello day body." in body
    assert b"Prepared by" not in body


# ---------------------------------------------------------------------------
# 3. Shift-report detail UNCHANGED (Prepared-by row + content still render)
# ---------------------------------------------------------------------------

def test_shift_report_detail_still_renders_prepared_by(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [shift_report("sr_1", "2026-06-12", prepared_by="Greg Stoltz", content="# H\n\nReport body.")],
    )

    html = records.render_detail(DummyContext(tmp_path), "sr_1")

    assert "<dt>Prepared by</dt>" in html
    assert "Greg Stoltz" in html
    assert '<div class="md">' in html
    assert "Report body." in html
    assert "Shift Report" in html
    assert 'href="/records"' in html
    assert "Record not found" not in html


# ---------------------------------------------------------------------------
# 4. Selector contract / journal exclusion / wrong operator
# ---------------------------------------------------------------------------

def test_list_selectors_pin_operator_and_only_record_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selectors = install_find(
        monkeypatch,
        [shift_report("sr_1", "2026-06-12"), day_record("dr_1", "2026-06-11")],
    )

    records.render(DummyContext(tmp_path))

    list_selectors = [s for s in selectors if "_id" not in s]
    assert list_selectors, "the list path must issue _find queries"
    queried_types = set()
    for sel in list_selectors:
        assert sel.get("operator") == "op_greg", f"operator not pinned in {sel}"
        assert sel.get("type") in {"shift_report", "day_record"}, f"unexpected type {sel}"
        queried_types.add(sel.get("type"))
    # Both operational types are queried; nothing else.
    assert queried_types == {"shift_report", "day_record"}


def test_journal_doc_never_appears_in_combined_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [
            day_record("dr_1", "2026-06-12"),
            {
                "_id": "journal_2026-06-12",
                "type": "journal",
                "operator": "op_greg",
                "date": "2026-06-12",
                "content": "secret journal body",
            },
        ],
    )

    html = records.render(DummyContext(tmp_path))

    assert "journal_2026-06-12" not in html
    assert "secret journal body" not in html
    assert "dr_1" in html


def test_day_record_detail_wrong_operator_is_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [day_record("dr_other", "2026-06-12", operator="op_someone_else", content="not greg day")],
    )

    html = records.render_detail(DummyContext(tmp_path), "dr_other")

    assert "Record not found" in html
    assert "not greg day" not in html


def test_day_record_detail_unknown_id_is_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [day_record("dr_1", "2026-06-12")])

    html = records.render_detail(DummyContext(tmp_path), "dr_missing")

    assert "Record not found" in html
    assert '<div class="md">' not in html


# ---------------------------------------------------------------------------
# 5. Degradation
# ---------------------------------------------------------------------------

def test_list_degrades_to_empty_when_find_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [], raises=True)

    html = records.render(DummyContext(tmp_path))

    assert "No records yet." in html


def test_day_record_detail_degrades_to_not_found_when_find_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(monkeypatch, [], raises=True)

    html = records.render_detail(DummyContext(tmp_path), "dr_1")

    assert "Record not found" in html


# ---------------------------------------------------------------------------
# Beyond-spec probes
# ---------------------------------------------------------------------------

def test_list_only_day_records_still_renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No shift reports at all; the day-record source alone must populate the list.
    install_find(
        monkeypatch,
        [
            day_record("dr_a", "2026-06-12"),
            day_record("dr_b", "2026-06-11"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    assert "No records yet." not in html
    assert 'href="/records/dr_a"' in html
    assert 'href="/records/dr_b"' in html
    assert "Day Record" in html
    # Ordering still descending.
    assert html.index("2026-06-12") < html.index("2026-06-11")


def test_day_record_detail_markdown_special_chars_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Markdown-meaningful + ampersand content must render through render_markdown
    # without being mangled: list items, emphasis, and an escaped ampersand.
    content = "- item one\n- a & b\n\n*stressed*"
    install_find(monkeypatch, [day_record("dr_md", "2026-06-12", content=content)])

    html = records.render_detail(DummyContext(tmp_path), "dr_md")

    assert "<li>item one</li>" in html
    assert "a &amp; b" in html  # ampersand escaped by the markdown renderer
    assert "<em>stressed</em>" in html
    assert "Record not found" not in html


def test_day_record_detail_missing_date_falls_back_to_type_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_find(
        monkeypatch,
        [{"_id": "dr_nodate", "type": "day_record", "operator": "op_greg", "content": "body"}],
    )

    html = records.render_detail(DummyContext(tmp_path), "dr_nodate")

    assert "Record not found" not in html
    assert "Day Record" in html  # title falls back to type label
    assert "None" not in html
    assert "Prepared by" not in html


def test_mixed_same_date_both_types_both_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Same date on both a shift report and a day record: both rows must survive
    # (no dedup-by-date collapsing).
    install_find(
        monkeypatch,
        [
            shift_report("sr_same", "2026-06-12"),
            day_record("dr_same", "2026-06-12"),
        ],
    )

    html = records.render(DummyContext(tmp_path))

    assert 'href="/records/sr_same"' in html
    assert 'href="/records/dr_same"' in html
