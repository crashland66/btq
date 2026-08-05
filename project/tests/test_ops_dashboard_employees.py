from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employees


def _employee_doc(employee_id: str, **overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": f"employee_{employee_id}",
        "type": "employee",
        "first": employee_id.title(),
        "last": "Worker",
        "name": f"{employee_id.title()} Worker",
        "status": "active",
        "job": "",
        "phone": "",
        "email": "",
    }
    doc.update(overrides)
    return doc


def _ctx(query: dict[str, list[str]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(query=query or {})


def test_employees_render_lists_all_employees(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employees,
        "load_employees",
        lambda: [
            _employee_doc("alice", first="Alice", last="Able"),
            _employee_doc("bob", first="Bob", last="Baker"),
        ],
    )

    body = employees.render(_ctx())

    assert "Able, Alice" in body
    assert "Baker, Bob" in body
    assert 'href="/employees/alice"' in body
    assert 'href="/employees/bob"' in body
    assert 'href="/vault/entity/employee/employee_alice.html"' not in body


def test_employees_render_links_primary_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employees, "load_employees", lambda: [_employee_doc("alice", job="7050")])

    body = employees.render(_ctx())

    assert 'href="/sites/7050"' in body


def test_employees_render_status_filter_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employees,
        "load_employees",
        lambda: [
            _employee_doc("alice", first="Alice", last="Able", status="active"),
            _employee_doc("bob", first="Bob", last="Baker", status="inactive"),
        ],
    )

    body = employees.render(_ctx({"status": ["active"]}))

    assert "Able, Alice" in body
    assert "Baker, Bob" not in body


def test_employees_render_active_roster_communication_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        _employee_doc("alice", first="Alice", last="Able", job="705", phone="8145550101", email="ALICE@example.com"),
        _employee_doc("bob", first="Bob", last="Baker", job="1337", phone="814-555-0102", email="bob@example.com"),
        _employee_doc("inactive", status="inactive", job="705", phone="8145550199", email="inactive@example.com"),
        _employee_doc("sandbox-user", job="SANDBOX", phone="8145550188", email="sandbox@example.com"),
        _employee_doc("stoltz_gregory", first="Gregory", last="Stoltz", job="699", phone="8145550177", email="greg@example.com"),
        _employee_doc("other-operator", operator="op_someone_else", job="699", phone="8145550166", email="other@example.com"),
    ]
    monkeypatch.setattr(employees, "load_employees", lambda: docs)

    body = employees.render(_ctx())
    panel = employees._communication_panel(docs)

    assert "Active employee communications" in body
    assert "Canonical roster: 2 active assigned employees · 2 emails · 2 phone numbers." in body
    assert "Copy BCC emails" in body
    assert "Draft BCC email" in body
    assert "Copy phone list" in body
    assert "alice@example.com" in panel
    assert "bob@example.com" in panel
    assert "inactive@example.com" not in panel
    assert "sandbox@example.com" not in panel
    assert "greg@example.com" not in panel
    assert "other@example.com" not in panel


def test_employees_communication_panel_surfaces_missing_contact_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employees,
        "load_employees",
        lambda: [
            _employee_doc("alice", first="Alice", last="Able", job="705", phone="", email=""),
        ],
    )

    body = employees.render(_ctx())

    assert "Canonical roster: 1 active assigned employee · 0 emails · 0 phone numbers." in body
    assert "Missing email: Able, Alice." in body
    assert "Missing phone: Able, Alice." in body


def test_employees_render_name_contains_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        employees,
        "load_employees",
        lambda: [
            _employee_doc("alice", first="Alice", last="Able"),
            _employee_doc("bob", first="Bob", last="Baker"),
        ],
    )

    body = employees.render(_ctx({"name_contains": ["bak"]}))

    assert "Baker, Bob" in body
    assert "Able, Alice" not in body


def test_employees_render_empty_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employees, "load_employees", lambda: [])

    body = employees.render(_ctx())

    assert "No employee records." in body
    assert 'class="error"' not in body


def test_employees_render_degraded_on_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> list[dict[str, object]]:
        raise RuntimeError("couch unavailable")

    monkeypatch.setattr(employees, "load_employees", fail)

    body = employees.render(_ctx())

    assert "<!doctype html>" in body
    assert "<h1>Employees</h1>" in body
    assert 'class="error"' in body
    assert "couch unavailable" in body
