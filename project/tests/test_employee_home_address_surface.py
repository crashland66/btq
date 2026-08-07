"""Gating tests for the home-address operator surface (Phase 2).

Contract (design spec 2026-07-28, employee-home-address-coverage-opportunities):

  * The employee detail page shows the structured home address to the operator,
    labeled as sensitive, with the ✎ inline-edit pattern from the quick facts.
  * Edit/clear STAGE the narrow set_employee_home_address queue job — the
    surface never writes the address to CouchDB directly, and save-section
    cannot be used to smuggle it in.
  * Audit entries and redirects never contain the address itself.
  * Fixtures use obviously fictional addresses only.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import queue_spec as qs
from ops_dashboard.sections import employee_detail as ed


FICTIONAL_ADDRESS = {
    "line1": "123 Example Street",
    "line2": "Apt 9",
    "city": "Exampletown",
    "state": "PA",
    "postal_code": "15900",
}


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
    monkeypatch.setattr(ed, "_load_location_name", lambda s: f"Site {s}")
    monkeypatch.setattr(ed, "_field_captures", lambda _p: [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _d: [])
    monkeypatch.setattr(ed, "_availability_section", lambda _eid, _doc=None: "<section></section>")


def _render(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object], query: dict[str, list[str]] | None = None) -> str:
    _install(monkeypatch, doc)
    return ed.render(SimpleNamespace(query=query or {}), "jordan")


# ---------------------------------------------------------------------------
# Read mode: sensitive-labeled section with the inline-edit glyph
# ---------------------------------------------------------------------------

def test_address_renders_for_operator_with_sensitive_label(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, _employee_doc(home_address=dict(FICTIONAL_ADDRESS)))
    section = body[body.index("<h3>Home address</h3>"):]
    section = section[:section.index("</section>")]
    assert "123 Example Street" in section
    assert "Apt 9" in section
    assert "Exampletown, PA 15900" in section
    assert '<span class="pill warning">Sensitive</span>' in section
    assert 'href="?edit=home_address"' in section
    assert "Operator-only" in section


def test_missing_address_shows_zero_state_with_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, _employee_doc())
    section = body[body.index("<h3>Home address</h3>"):]
    section = section[:section.index("</section>")]
    assert "No home address recorded." in section
    assert 'href="?edit=home_address"' in section


# ---------------------------------------------------------------------------
# Edit mode: inline form staging through the queue route
# ---------------------------------------------------------------------------

def test_edit_mode_renders_prefilled_form_posting_to_queue_route(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(
        monkeypatch,
        _employee_doc(home_address=dict(FICTIONAL_ADDRESS)),
        query={"edit": ["home_address"]},
    )
    assert 'action="/employees/jordan/home-address"' in body
    assert 'name="line1" value="123 Example Street"' in body
    assert 'name="postal_code" value="15900"' in body
    assert '<input type="hidden" name="_action" value="clear">' in body  # clear workflow present
    assert "validated queue job" in body
    # The edit glyph hides while editing.
    section = body[body.index("<h3>Home address</h3>"):]
    section = section[:section.index("</section>")]
    assert 'href="?edit=home_address"' not in section


def test_notices_render_for_staged_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    staged = _render(monkeypatch, _employee_doc(), query={"staged": ["home_address"]})
    assert "Change staged" in staged
    invalid = _render(monkeypatch, _employee_doc(), query={"edit": ["home_address"], "error": ["invalid_address"]})
    assert "address was rejected" in invalid


# ---------------------------------------------------------------------------
# POST: stages a validated queue job; never a direct write
# ---------------------------------------------------------------------------

def test_post_set_stages_valid_queue_job_keyed_to_person_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)

    status, _ctype, _body, headers = ed.handle_home_address_post(
        ctx,
        "jordan",
        b"_action=set&line1=123+Example+Street&line2=Apt+9&city=Exampletown&state=PA&postal_code=15900&country=",
    )

    assert status == 303
    assert headers["Location"] == "/employees/jordan?staged=home_address"
    assert len(enqueue_capture) == 1
    job = enqueue_capture[0]["job"]
    assert job["job_id"].startswith("set-employee-home-address-")
    assert job["job_type"] == "set_employee_home_address"
    assert qs.validate_job(job)
    assert job["payload"]["person"] == "jordan_001"
    assert job["payload"]["home_address"] == FICTIONAL_ADDRESS  # empty country omitted
    assert job["payload"]["source"] == "ops_dashboard"
    # Unified transport: nothing lands in the runtime file queue.
    assert not (ctx.runtime_root / "queue").exists()


def test_post_clear_stages_clear_job_without_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc(home_address=dict(FICTIONAL_ADDRESS)))
    ctx = DummyContext(tmp_path)

    ed.handle_home_address_post(ctx, "jordan", b"_action=clear")

    assert len(enqueue_capture) == 1
    job = enqueue_capture[0]["job"]
    assert job["job_id"].startswith("set-employee-home-address-")
    assert job["payload"]["action"] == "clear"
    assert "home_address" not in job["payload"]
    assert qs.validate_job(job)
    assert not (ctx.runtime_root / "queue").exists()


def test_post_invalid_address_stages_nothing_and_redirects_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)

    status, _ctype, _body, headers = ed.handle_home_address_post(
        ctx,
        "jordan",
        b"_action=set&line1=123+Example+Street&city=&state=PA&postal_code=15900",
    )

    assert status == 303
    assert "error=invalid_address" in headers["Location"]
    assert enqueue_capture == []


def test_post_audit_entries_never_contain_the_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)

    ed.handle_home_address_post(
        ctx,
        "jordan",
        b"_action=set&line1=123+Example+Street&city=Exampletown&state=PA&postal_code=15900",
    )

    audit_text = json.dumps(ctx.audit_entries)
    assert "123 Example Street" not in audit_text
    assert "Exampletown" not in audit_text
    assert "staged" in audit_text


def test_post_unwritable_queue_redirects_instead_of_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, _employee_doc())
    ctx = DummyContext(tmp_path)
    # The unified CouchDB transport being down surfaces as an enqueue error.
    from event_pipeline import btq_client

    def broken_enqueue(job: dict, created_by: str = "greg", **_kwargs: object) -> dict:
        raise btq_client.BTQClientError("CouchDB enqueue unavailable")

    monkeypatch.setattr(btq_client, "enqueue", broken_enqueue)

    status, _ctype, _body, headers = ed.handle_home_address_post(
        ctx,
        "jordan",
        b"_action=set&line1=123+Example+Street&city=Exampletown&state=PA&postal_code=15900",
    )

    assert status == 303
    assert "error=queue_unavailable" in headers["Location"]
    audit_text = json.dumps(ctx.audit_entries)
    assert "123 Example Street" not in audit_text


def test_post_unknown_employee_redirects_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enqueue_capture: list[dict]
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: None)
    ctx = DummyContext(tmp_path)

    status, _ctype, _body, headers = ed.handle_home_address_post(ctx, "ghost", b"_action=set&line1=X")

    assert status == 303
    assert "error=not_found" in headers["Location"]
    assert enqueue_capture == []


# ---------------------------------------------------------------------------
# The direct-write path cannot carry the address
# ---------------------------------------------------------------------------

def test_save_section_cannot_smuggle_home_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
        b"_section=identity&first=Jordan&home_address=999+Sneaky+Lane&_entity_id=jordan",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "home_address" not in payload
