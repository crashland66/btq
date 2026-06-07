from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employee_detail, home


def _employee_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_jordan",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "name": "Jordan Avery",
        "person_id": "jordan_001",
        "first": "Jordan",
        "last": "Avery",
        "status": "active",
        "job": "7050",
        "site_ids": ["7050"],
    }
    doc.update(overrides)
    return doc


def test_get_employee_detail_renders_identity_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    ctx = SimpleNamespace()

    html = employee_detail.render(ctx, "jordan")

    assert "Jordan Avery" in html
    assert "jordan_001" in html
    assert "<h3>Identity</h3>" in html
    assert "<h3>Assignment</h3>" in html


def test_employee_detail_vault_button_uses_full_doc_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Vault page button must point at the static entity page, which is
    keyed by the full CouchDB _id (employee_<id>), not the bare route id."""
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())

    html = employee_detail.render(SimpleNamespace(query={}), "jordan")

    assert 'href="/vault/entity/employee/employee_jordan.html"' in html
    assert 'href="/vault/entity/employee/jordan.html"' not in html


def test_employee_detail_header_has_all_employees_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())

    html = employee_detail.render(SimpleNamespace(query={}), "jordan")

    assert 'href="/employees"' in html


def test_employee_detail_summary_section_wraps_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employee_detail,
        "_load_vault_doc",
        lambda _doc_id: _employee_doc(content="Some notes"),
    )

    html = employee_detail.render(SimpleNamespace(query={}), "jordan")

    assert html.count("<h2>Summary</h2>") == 1
    summary_html = html.split("<h2>Summary</h2>", 1)[1].split("<h3>About</h3>", 1)[0]
    assert "<h3>Identity</h3>" in summary_html
    assert "<h3>Assignment</h3>" in summary_html
    assert "<h3>Contact</h3>" in summary_html


def test_get_employee_detail_renders_about_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employee_detail,
        "_load_vault_doc",
        lambda _doc_id: _employee_doc(content="Some notes"),
    )
    ctx = SimpleNamespace()

    html = employee_detail.render(ctx, "jordan")

    assert "<h3>About</h3>" in html
    assert "Some notes" in html


def test_get_employee_detail_renders_assigned_sites_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employee_detail,
        "_load_vault_doc",
        lambda _doc_id: _employee_doc(site_ids=["7050", "1338"]),
    )
    ctx = SimpleNamespace()

    html = employee_detail.render(ctx, "jordan")

    assert "/sites/7050" in html
    assert "/sites/1338" in html


def test_get_employee_detail_not_found_for_wrong_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: {"type": "location"})
    ctx = SimpleNamespace()

    html = employee_detail.render(ctx, "jordan")

    assert "Employee not found" in html
    assert "<h3>Identity</h3>" not in html
    assert "<h3>Assignment</h3>" not in html


def test_get_employee_detail_not_found_missing_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: None)
    ctx = SimpleNamespace()

    html = employee_detail.render(ctx, "jordan")

    assert "Employee not found" in html


def test_home_employee_name_links_to_employee_detail() -> None:
    employee_id = "employee_jordan"

    html = home._render_employee_directory(
        [
            {
                "doc": {
                    "_id": employee_id,
                    "first": "Jordan",
                    "last": "Avery",
                    "phone": "8145550100",
                    "email": "jordan@example.com",
                    "job": "7050",
                }
            }
        ]
    )

    # The home link must carry the BARE id (employee_detail re-prepends
    # "employee_" on load); linking the full doc _id would double the prefix.
    assert 'href="/employees/jordan"' in html
    assert f'href="/employees/{employee_id}"' not in html
    assert f'href="/vault/entity/employee/{employee_id}.html"' not in html


def test_home_employee_link_round_trips_to_same_doc_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a home row's link target must resolve back to the original
    CouchDB _id when employee_detail loads it. Guards the double-prefix bug."""
    import re

    doc_id = "employee_per_test"
    html = home._render_employee_directory(
        [{"doc": {"_id": doc_id, "first": "Per", "last": "Test", "job": "7050"}}]
    )
    match = re.search(r'/employees/([^"]+)"', html)
    assert match, "no employee detail link rendered"
    link_target = match.group(1)
    assert link_target == "per_test"

    loaded: list[str] = []
    monkeypatch.setattr(
        employee_detail, "_load_vault_doc", lambda d: loaded.append(d) or None
    )
    employee_detail.render(SimpleNamespace(query={}), link_target)
    assert loaded == [doc_id]
