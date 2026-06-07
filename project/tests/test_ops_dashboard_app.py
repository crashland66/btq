from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from ops_dashboard.app import route_response
from ops_dashboard.sections import employee_detail, employees


def test_employees_route_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(employees, "render", lambda _ctx: "<html>employees section</html>")

    status, content_type, body = route_response("GET", "/employees", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert b"employees section" in body


def test_employees_detail_route_still_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rendered_ids: list[str] = []

    def render_detail(_ctx: object, employee_id: str) -> str:
        rendered_ids.append(employee_id)
        return "<html>employee detail</html>"

    monkeypatch.setattr(employee_detail, "render", render_detail)

    status, content_type, body = route_response("GET", "/employees/jordan", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert b"employee detail" in body
    assert rendered_ids == ["jordan"]
