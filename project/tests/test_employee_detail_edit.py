from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employee_detail, entity_edit


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{location}">Return</a>'.encode(), {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


def _employee_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_x",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "first": "Old",
        "last": "Name",
        "name": "Old Name",
        "site_ids": [],
    }
    doc.update(overrides)
    return doc


def _capture_put(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(employee_detail.sites, "request_json", fake_request_json)
    return captured


def test_post_save_employee_section_identity_recomputes_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    employee_detail.handle_save_section(
        ctx,
        "x",
        b"_section=identity&first=Alice&last=Smith&_entity_id=x",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["name"] == "Alice Smith"


def test_post_save_employee_section_assignment_writes_job_without_recomputing_site_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # RECONCILED for prompt 542 (deliberate assignment editor): this test
    # previously pinned the OLD behavior where any assignment-section save
    # ran recompute_employee_derived, so a bare `job` edit silently derived
    # site_ids from job/additional_jobs/sites. Under the new contract, job
    # and role are the only direct assignment fields; membership (site_ids)
    # is exclusively queue-owned via assign_employee_site jobs, and the
    # assignment save branch deliberately skips recompute_employee_derived
    # so site_ids stays untouched (_PROTECTED) on a legacy-only job/role
    # edit with no assigned_sites/_assigned_baseline present.
    monkeypatch.setattr(
        employee_detail,
        "_load_vault_doc",
        lambda _doc_id: _employee_doc(job="", site_ids=[]),
    )
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    employee_detail.handle_save_section(
        ctx,
        "x",
        b"_section=assignment&job=7040&_entity_id=x",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["job"] == "7040"
    assert payload["site_ids"] == []


def test_post_save_employee_section_only_updates_allowed_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    employee_detail.handle_save_section(
        ctx,
        "x",
        b"_section=contact&phone=555-1234&type=location&operator=&name=Bad&_entity_id=x",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["phone"] == "555-1234"
    assert payload["type"] == "employee"
    assert payload["operator"] == "op_greg"
    assert payload["name"] == "Old Name"


def test_post_save_employee_section_preserves_type_and_operator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    employee_detail.handle_save_section(
        ctx,
        "x",
        b"_section=about&content=Updated&_entity_id=x",
    )

    updated_doc = captured["payload"]
    assert isinstance(updated_doc, dict)
    assert updated_doc["type"] == "employee"
    assert updated_doc["operator"]


def test_post_save_employee_section_409_rerenders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ConflictError(Exception):
        code = 409

    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    monkeypatch.setattr(
        employee_detail.sites,
        "request_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConflictError()),
    )
    monkeypatch.setattr(employee_detail, "render", lambda ctx, employee_id: "<html>rerendered</html>")
    ctx = DummyContext(tmp_path)

    status, content_type, body, _headers = employee_detail.handle_save_section(
        ctx,
        "x",
        b"_section=contact&phone=555-1234&_entity_id=x",
    )

    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert b"rerendered" in body


def test_post_save_employee_section_write_doc_satisfies_validator_contract() -> None:
    existing = {
        "_id": "employee_x",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "first": "Alice",
        "last": "Smith",
        "name": "Alice Smith",
        "site_ids": [],
    }
    form_flat = {"first": "Alice", "last": "Jones", "_section": "identity", "_entity_id": "x"}
    updated = entity_edit.apply_section_update(
        existing,
        form_flat,
        frozenset({"first", "last", "preferred_name", "person_id", "status"}),
    )
    entity_edit.recompute_employee_derived(updated)

    assert updated["type"] == "employee"
    assert updated["operator"] == "op_greg"
    assert updated["operator"]
    assert updated["name"] == "Alice Jones"
