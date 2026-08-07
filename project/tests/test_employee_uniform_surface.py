from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import queue_spec as qs
from ops_dashboard.sections import employee_detail as ed


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", b"", {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


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


def _install(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object]) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    monkeypatch.setattr(ed, "_load_location_name", lambda site: f"Site {site}")
    monkeypatch.setattr(ed, "_field_captures", lambda _person: [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _doc: [])
    monkeypatch.setattr(ed, "_availability_section", lambda _employee_id, _doc=None: "<section></section>")


def _render(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object], query=None) -> str:
    _install(monkeypatch, doc)
    return ed.render(SimpleNamespace(query=query or {}), "jordan")


def test_uniform_read_mode_shows_status_count_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(
        monkeypatch,
        _employee_doc(
            uniform_status="needs_shirts",
            uniform_shirt_count=1,
            uniform_shirt_size="2XL",
            uniform_updated_at="2026-08-05T16:00:00+00:00",
        ),
    )

    assert "<h3>Uniform</h3>" in body
    assert "Needs shirts" in body
    assert "B&amp;T T-shirts" in body
    assert "2XL" in body
    assert 'href="?edit=uniform"' in body


def test_uniform_zero_state_is_awaiting_response(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, _employee_doc())

    assert "Awaiting response" in body
    assert "Required size" in body


def test_uniform_edit_mode_posts_to_queue_route(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(
        monkeypatch,
        _employee_doc(uniform_status="adequate", uniform_shirt_count=3, uniform_shirt_size="L"),
        query={"edit": ["uniform"]},
    )

    assert 'action="/employees/jordan/uniform"' in body
    assert 'name="shirt_count" min="0" max="99" value="3"' in body
    assert 'name="shirt_size"' in body
    assert 'value="L"' in body
    assert "validated queue job" in body


def test_uniform_post_stages_valid_queue_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)

    status, _ctype, _body, headers = ed.handle_uniform_post(
        ctx,
        "jordan",
        b"status=needs_shirts&shirt_count=1&shirt_size=2XL",
    )

    assert status == 303
    assert headers["Location"] == "/employees/jordan?staged=uniform"
    assert len(enqueue_capture) == 1
    job = enqueue_capture[0]["job"]
    assert job["job_id"].startswith("set-employee-uniform-")
    assert job["job_type"] == "set_employee_uniform"
    assert job["payload"]["person"] == "jordan_001"
    assert job["payload"]["uniform"] == {
        "status": "needs_shirts",
        "shirt_count": 1,
        "shirt_size": "2XL",
    }
    assert qs.validate_job(job)
    # Unified transport: nothing lands in the runtime file queue.
    assert not (ctx.runtime_root / "queue").exists()


def test_uniform_post_rejects_missing_required_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)

    status, _ctype, _body, headers = ed.handle_uniform_post(
        ctx,
        "jordan",
        b"status=needs_shirts&shirt_count=0&shirt_size=",
    )

    assert status == 303
    assert "error=invalid_uniform" in headers["Location"]
    assert enqueue_capture == []


def test_save_section_cannot_smuggle_uniform_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(ed.sites, "request_json", fake_request_json)
    ctx = DummyContext(tmp_path)
    ed.handle_save_section(
        ctx,
        "jordan",
        b"_section=identity&first=Jordan&uniform_status=adequate&uniform_shirt_count=3",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "uniform_status" not in payload
    assert "uniform_shirt_count" not in payload
