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
