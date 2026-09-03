"""Gating tests for inline quick-facts editing on the employee detail page.

The contract:

  * Each quick-facts group (Identity / Assignment) carries an edit glyph in
    its header linking to ?edit=<slug> — no more edit links buried in the
    Admin links block at the bottom of the page.
  * ?edit=identity / ?edit=assignment re-renders THAT group inside the
    quick-facts box as an inline form (same box, fields become controls) —
    the separate "Edit employee" section is gone.
  * `status` is a <select> constrained to queue_spec.ENTITY_STATUSES, with a
    nonstandard stored value kept selectable so a save never rewrites it.
  * Saves execute the new path: a select-provided status persists, an invalid
    status is rejected with 400, and comma-separated multi-value assignment
    fields split back into lists.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import employee_detail as ed


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
        "role": "Cleaner",
        "job": "7050",
        "site_ids": ["7050"],
        "phone": "555-1212",
        "email": "j@example.com",
    }
    doc.update(overrides)
    return doc


def _install(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object]) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: doc)
    monkeypatch.setattr(ed, "_load_location_name", lambda s: f"Site {s}")
    monkeypatch.setattr(ed, "_field_captures", lambda _p: [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _d: [])
    monkeypatch.setattr(ed, "_availability_section", lambda _eid, _doc=None: "<section></section>")


def _render(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object] | None = None, *, edit: str = "") -> str:
    _install(monkeypatch, doc if doc is not None else _employee_doc())
    query = {"edit": [edit]} if edit else {}
    return ed.render(SimpleNamespace(query=query), "jordan")


def _quick_facts_region(body: str) -> str:
    start = body.index('class="quick-facts"')
    return body[start:body.index("Recent activity", start)]


def _capture_put(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(ed.sites, "request_json", fake_request_json)
    return captured


# ---------------------------------------------------------------------------
# Read mode: edit glyphs live in the quick-facts box, not in Admin links
# ---------------------------------------------------------------------------

def test_quick_facts_groups_have_edit_glyphs(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _quick_facts_region(_render(monkeypatch))
    assert '<a class="icon-btn" href="?edit=identity"' in facts
    assert '<a class="icon-btn" href="?edit=assignment"' in facts
    assert "&#9998;" in facts


def test_admin_links_no_longer_carry_edit_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch)
    assert ">Edit identity</a>" not in body
    assert ">Edit assignment</a>" not in body
    # No vault-projection links anywhere (retired 2026-08-07).
    assert "/vault/" not in body


def test_sparse_doc_still_offers_edit_glyphs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A person with no facts must still be editable — the glyphs are the only
    # entry point now, so empty groups render a header + zero-state, not "".
    sparse = {"_id": "employee_jordan", "_rev": "1-x", "type": "employee", "operator": "op", "first": "Jordan", "last": "Avery"}
    facts = _quick_facts_region(_render(monkeypatch, sparse))
    assert 'href="?edit=assignment"' in facts
    assert '<h3>Assignment</h3>' in facts


# ---------------------------------------------------------------------------
# Edit mode: the group becomes an inline form inside the quick-facts box
# ---------------------------------------------------------------------------

def test_edit_identity_renders_inline_form_in_quick_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _render(monkeypatch, edit="identity")
    facts = _quick_facts_region(body)
    # The form is INSIDE the quick-facts box.
    assert 'action="/employees/jordan/save-section"' in facts
    assert '<input type="hidden" name="_section" value="identity">' in facts
    assert '<input type="text" name="first" value="Jordan">' in facts
    assert '<input type="text" name="last" value="Avery">' in facts
    assert '<input type="text" name="phone" value="555-1212">' in facts
    # The separate bottom form is gone.
    assert "<h2>Edit employee</h2>" not in body
    # The group being edited hides its own glyph; the other group keeps its.
    assert 'href="?edit=identity"' not in facts
    assert 'href="?edit=assignment"' in facts


def test_edit_assignment_renders_inline_form_with_list_values_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _employee_doc(additional_jobs=["7040", "1338"], sites=["7050"])
    facts = _quick_facts_region(_render(monkeypatch, doc, edit="assignment"))
    assert '<input type="hidden" name="_section" value="assignment">' in facts
    assert '<input type="text" name="job" value="7050">' in facts
    # Lists display comma-joined, never as a Python repr.
    assert '<input type="text" name="additional_jobs" value="7040, 1338">' in facts
    assert "[&#x27;" not in facts
    # Identity stays read-only with its glyph.
    assert 'href="?edit=identity"' in facts


# ---------------------------------------------------------------------------
# Status is a choice, not free text
# ---------------------------------------------------------------------------

def test_status_renders_as_select_with_entity_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _quick_facts_region(_render(monkeypatch, edit="identity"))
    assert '<select name="status">' in facts
    assert '<option value="active" selected>active</option>' in facts
    assert '<option value="inactive">inactive</option>' in facts
    assert '<input type="text" name="status"' not in facts


def test_nonstandard_stored_status_stays_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _quick_facts_region(_render(monkeypatch, _employee_doc(status="resigned"), edit="identity"))
    assert '<option value="resigned" selected>resigned</option>' in facts
    assert '<option value="active">active</option>' in facts


# ---------------------------------------------------------------------------
# Save path: select value persists, invalid status rejected, lists split
# ---------------------------------------------------------------------------

def test_save_identity_with_select_status_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    ed.handle_save_section(
        ctx,
        "jordan",
        b"_section=identity&first=Jordan&last=Avery&status=inactive&_entity_id=jordan",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "inactive"


def test_save_identity_rejects_invalid_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    result = ed.handle_save_section(
        ctx,
        "jordan",
        b"_section=identity&first=Jordan&status=vaporized&_entity_id=jordan",
    )

    assert result[0] == 400
    assert "payload" not in captured  # nothing was written


def test_save_identity_allows_existing_nonstandard_status_roundtrip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An identity edit that re-submits the doc's own nonstandard status must
    # not be rejected — otherwise unrelated fixes (phone, name) become
    # unsaveable for that person.
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc(status="resigned"))
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    ed.handle_save_section(
        ctx,
        "jordan",
        b"_section=identity&first=Jordan&last=Avery&status=resigned&phone=555-9999&_entity_id=jordan",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["status"] == "resigned"
    assert payload["phone"] == "555-9999"


def test_save_assignment_splits_comma_lists_and_preserves_canonical_site_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # RECONCILED for prompt 541 (canonical-first employee_assignments rule):
    # this test previously pinned the OLD always-overwrite-from-legacy
    # behavior of recompute_employee_derived, asserting site_ids became the
    # flat union of job+additional_jobs even though the fixture doc already
    # carries a non-empty canonical site_ids=["7050"]. That was the exact bug
    # 541 fixes (canonical site_ids silently dropped on every section save).
    # Under the new contract, a non-empty stored site_ids is preserved
    # verbatim regardless of legacy job/additional_jobs edits — comma-split
    # parsing of additional_jobs itself is unaffected and still covered here.
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: _employee_doc())
    captured = _capture_put(monkeypatch)
    ctx = DummyContext(tmp_path)

    ed.handle_save_section(
        ctx,
        "jordan",
        b"_section=assignment&job=7050&additional_jobs=7040,%201338&_entity_id=jordan",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["additional_jobs"] == ["7040", "1338"]
    assert payload["site_ids"] == ["7050"]
