from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

import pytest

from ops_dashboard.app import route_response
from ops_dashboard.sections import employee_detail, employees, site_detail


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


def test_site_contact_post_routes_registered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_handle(ctx: object, site_id: str, body: bytes, *, target_type: str = "site"):
        calls.append((site_id, target_type))
        return 303, "text/html; charset=utf-8", b"", {"Location": "/sites/7050"}

    monkeypatch.setattr(site_detail, "handle_contacts_post", fake_handle)

    status, _content_type, _body = route_response("POST", "/sites/7050/contacts", tmp_path / "runtime", b"action=add")
    assert status == HTTPStatus.SEE_OTHER
    status, _content_type, _body = route_response("POST", "/sites/7050/account-contacts", tmp_path / "runtime", b"action=add")

    assert status == HTTPStatus.SEE_OTHER
    assert calls == [("7050", "site"), ("7050", "account")]
