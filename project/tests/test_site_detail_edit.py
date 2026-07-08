from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from ops_dashboard.sections import entity_edit, site_detail
from queue_spec import validate_job


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{location}">Return</a>'.encode(), {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


def _minimal_location(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "location_7050",
        "_rev": "1-abc",
        "type": "location",
        "operator": "op_greg",
    }
    doc.update(overrides)
    return doc


def _stub_expensive_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    # The redesigned render pulls related data + captures from CouchDB; neutralize
    # them so the genuine non-degraded page renders without a live backend.
    monkeypatch.setattr(
        site_detail,
        "_related_data",
        lambda site_id: {
            "notes": [],
            "employee_rows": [],
            "opportunity_rows": [],
            "visit_rows": [],
            "recent_visits": [],
        },
    )
    monkeypatch.setattr(site_detail, "_related_sections", lambda data: [])
    monkeypatch.setattr(site_detail, "_site_capture_records", lambda ctx, site_id: ([], False, 0))


def test_site_detail_header_says_admin_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: _minimal_location())
    _stub_expensive_sections(monkeypatch)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "7050")

    assert "Admin metadata" in html
    header_actions = html.split("</header>", 1)[0]
    assert ">Edit<" not in header_actions


def test_get_site_detail_edit_contact_renders_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(_rev="2-abc", customer_name="ACME"),
    )
    _stub_expensive_sections(monkeypatch)
    ctx = DummyContext(tmp_path, {"edit": ["contact"]})

    html = site_detail.render(ctx, "7050")

    assert "<form" in html
    assert 'name="customer_name"' in html
    assert 'value="contact"' in html
    assert "/sites/7050/save-section" in html


def test_get_site_detail_about_edit_renders_raw_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(content="Raw text\n```dataview\nLIST\n```"),
    )
    _stub_expensive_sections(monkeypatch)
    ctx = DummyContext(tmp_path, {"edit": ["about"]})

    html = site_detail.render(ctx, "7050")

    assert '<textarea name="content">' in html
    assert "dataview" in html


def test_site_detail_reference_links_render_controls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(
            urls=[
                {
                    "url": "https://example.com/sites/acme",
                    "label": "Official page",
                    "kind": "official_location_page",
                    "status": "verified",
                    "verification_note": "Operator checked.",
                },
                {
                    "url": "https://old.example.com/sites/acme",
                    "label": "Old page",
                    "kind": "official_location_page",
                    "status": "deprecated",
                },
            ]
        ),
    )
    _stub_expensive_sections(monkeypatch)
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "7050")

    assert "Reference Links" in html
    assert "https://example.com/sites/acme" in html
    assert "https://old.example.com/sites/acme" not in html
    assert 'action="/sites/7050/urls"' in html
    assert 'name="action" value="add"' in html
    assert 'name="action" value="edit"' in html
    assert 'name="action" value="remove"' in html


def test_site_detail_facility_hours_render_controls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(
            facility_hours={
                "status": "verified",
                "last_verified_at": "2026-06-26",
                "last_verified_by": "Greg",
                "source": "operator_verified",
                "note": "Operator checked.",
                "weekly": {
                    "mon": [{"open": "08:30", "close": "17:00"}],
                    "tue": [{"open": "08:30", "close": "17:00"}],
                    "wed": [{"open": "08:30", "close": "17:00"}],
                    "thu": [{"open": "08:30", "close": "17:00"}],
                    "fri": [{"open": "08:30", "close": "15:00"}],
                    "sat": [],
                    "sun": [],
                },
                "exceptions": [
                    {
                        "rule": "nth_weekday",
                        "weekday": "tue",
                        "ordinals": [2, 4],
                        "hours": [{"open": "10:00", "close": "19:00"}],
                        "note": "Second and fourth Tuesday",
                    }
                ],
            }
        ),
    )
    _stub_expensive_sections(monkeypatch)
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "7050")

    assert "Facility Hours" in html
    assert "08:30-17:00" in html
    assert "10:00-19:00" in html
    assert 'action="/sites/7050/facility-hours"' in html
    assert 'name="facility_hours_json"' in html
    assert 'name="action" value="clear"' in html


def test_site_detail_contacts_render_authoring_controls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(
            account="Acme",
            customer_name="Legacy Person",
            site_contacts=[
                {
                    "id": "cnt_site_7050_alex_site_contact",
                    "name": "Alex Site",
                    "role": "site_contact",
                    "scope": "site",
                    "source": "operator_verified",
                    "phone": "555-0101",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        site_detail,
        "_load_account_doc_for_location",
        lambda doc: {
            "_id": "account_acme",
            "type": "account",
            "account_contacts": [
                {
                    "id": "cnt_account_acme_jordan_account_escalation",
                    "name": "Jordan Escalation",
                    "role": "account_escalation",
                    "scope": "account",
                    "source": "operator_verified",
                    "email": "jordan@example.com",
                }
            ],
        },
    )
    _stub_expensive_sections(monkeypatch)
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "7050")

    assert "Contacts" in html
    assert "Alex Site" in html
    assert "Jordan Escalation" in html
    assert 'action="/sites/7050/contacts"' in html
    assert 'action="/sites/7050/account-contacts"' in html
    assert 'name="role"' in html
    assert "Account Escalation" in html
    assert 'name="action" value="add"' in html
    assert 'name="action" value="edit"' in html
    assert 'name="action" value="remove"' in html


def test_contact_panel_legacy_fallback_uses_displayed_structured_contacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: _minimal_location(
            account="Acme",
            customer_name="Jordan Legacy",
            customer_email="jordan@example.com",
        ),
    )
    monkeypatch.setattr(
        site_detail,
        "_load_account_doc_for_location",
        lambda doc: {
            "_id": "account_acme",
            "type": "account",
            "account_contacts": [
                {
                    "id": "cnt_account_acme_jordan_billing",
                    "name": "Jordan Legacy",
                    "role": "billing",
                    "scope": "account",
                    "source": "operator_verified",
                    "email": "jordan@example.com",
                }
            ],
        },
    )
    _stub_expensive_sections(monkeypatch)
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    ctx = DummyContext(tmp_path, {})

    html = site_detail.render(ctx, "7050")

    assert "Jordan Legacy" in html
    assert "Contact</h3>" in html or "Contact</h2>" in html
    assert "No account escalation contact recorded" not in html


def test_facility_hours_form_json_preserves_literal_empty_array_note_text() -> None:
    note = "Operator note with literal [] bytes."
    rendered = site_detail._facility_hours_json_for_form(
        {
            "status": "verified",
            "last_verified_at": "2026-06-26",
            "last_verified_by": "Greg",
            "source": "operator_verified",
            "note": note,
            "weekly": {
                "mon": [{"open": "08:30", "close": "17:00"}],
                "tue": [{"open": "08:30", "close": "17:00"}],
                "wed": [{"open": "08:30", "close": "17:00"}],
                "thu": [{"open": "08:30", "close": "17:00"}],
                "fri": [{"open": "08:30", "close": "15:00"}],
                "sat": [],
                "sun": [],
            },
            "exceptions": [
                {
                    "rule": "date",
                    "date": "2026-12-25",
                    "hours": [],
                    "note": note,
                }
            ],
        }
    )

    round_tripped = json.loads(rendered)
    assert round_tripped["note"] == note
    assert round_tripped["exceptions"][0]["note"] == note


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            {
                "action": "add",
                "url": "https://example.com/sites/acme",
                "label": "Official page",
                "kind": "official_location_page",
                "status": "reference",
                "actor": "Greg",
            },
            {
                "action": "add",
                "url": "https://example.com/sites/acme",
                "kind": "official_location_page",
            },
        ),
        (
            {
                "action": "edit",
                "url": "https://example.com/sites/acme",
                "new_url": "https://example.com/sites/acme-updated",
                "label": "Updated page",
                "kind": "official_location_page",
                "status": "verified",
                "last_verified_at": "2026-06-26",
                "last_verified_by": "Greg",
                "actor": "Greg",
            },
            {
                "action": "edit",
                "url": "https://example.com/sites/acme",
                "new_url": "https://example.com/sites/acme-updated",
                "status": "verified",
            },
        ),
        (
            {
                "action": "remove",
                "url": "https://example.com/sites/acme",
                "confirm": "1",
                "actor": "Greg",
            },
            {
                "action": "remove",
                "url": "https://example.com/sites/acme",
            },
        ),
    ],
)
def test_site_url_post_enqueues_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: dict[str, str],
    expected: dict[str, str],
) -> None:
    monkeypatch.setattr(site_detail.sites, "request_json", lambda *args, **kwargs: pytest.fail("must not direct-write CouchDB"))
    ctx = DummyContext(tmp_path)

    status, _content_type, _response_body, headers = site_detail.handle_site_url_post(
        ctx,
        "7050",
        urlencode(body).encode(),
    )

    assert status == 303
    assert headers["Location"] == "/sites/7050?message=url_queued"
    queue_files = list((ctx.runtime_root / "queue").glob("set-site-url-*.json"))
    assert len(queue_files) == 1
    job = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert job["job_type"] == "set_site_url"
    payload = job["payload"]
    assert payload["site_id"] == "7050"
    for key, value in expected.items():
        assert payload[key] == value
    assert payload["source"] == "ops_dashboard_site_detail"


def test_site_hours_post_enqueues_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(site_detail.sites, "request_json", lambda *args, **kwargs: pytest.fail("must not direct-write CouchDB"))
    ctx = DummyContext(tmp_path)
    body = {
        "action": "set",
        "actor": "Greg",
        "facility_hours_json": json.dumps(
            {
                "status": "verified",
                "last_verified_at": "2026-06-26",
                "last_verified_by": "Greg",
                "source": "operator_verified",
                "note": "Operator checked.",
                "weekly": {
                    "mon": [{"open": "08:30", "close": "17:00"}],
                    "tue": [{"open": "08:30", "close": "17:00"}],
                    "wed": [{"open": "08:30", "close": "17:00"}],
                    "thu": [{"open": "08:30", "close": "17:00"}],
                    "fri": [{"open": "08:30", "close": "15:00"}],
                    "sat": [],
                    "sun": [],
                },
                "exceptions": [],
            }
        ),
    }

    status, _content_type, _response_body, headers = site_detail.handle_site_hours_post(
        ctx,
        "7050",
        urlencode(body).encode(),
    )

    assert status == 303
    assert headers["Location"] == "/sites/7050?message=facility_hours_queued"
    queue_files = list((ctx.runtime_root / "queue").glob("set-site-hours-*.json"))
    assert len(queue_files) == 1
    job = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert job["job_type"] == "set_site_hours"
    payload = job["payload"]
    assert payload["site_id"] == "7050"
    assert payload["action"] == "set"
    assert payload["facility_hours"]["status"] == "verified"
    assert payload["facility_hours"]["weekly"]["fri"] == [{"open": "08:30", "close": "15:00"}]
    assert payload["source"] == "ops_dashboard_site_detail"


@pytest.mark.parametrize(
    ("target_type", "body", "expected_target", "expected_action", "expected_id"),
    [
        (
            "site",
            {
                "action": "add",
                "name": "Alex Site",
                "title": "Facility Manager",
                "phone": "555-0101",
                "email": "alex@example.com",
                "role": "site_contact",
                "source": "operator_verified",
                "source_date": "2026-07-07",
                "notes": "Synthetic fixture.",
                "actor": "Greg",
            },
            {"type": "site", "id": "7050"},
            "upsert",
            "cnt_site_7050_alex_site_site_contact",
        ),
        (
            "site",
            {
                "action": "edit",
                "contact_id": "cnt_existing",
                "name": "Alex Updated",
                "phone": "555-0102",
                "role": "site_contact",
                "source": "operator_verified",
                "actor": "Greg",
            },
            {"type": "site", "id": "7050"},
            "upsert",
            "cnt_existing",
        ),
        (
            "site",
            {
                "action": "remove",
                "contact_id": "cnt_existing",
                "confirm": "1",
                "actor": "Greg",
            },
            {"type": "site", "id": "7050"},
            "remove",
            "cnt_existing",
        ),
        (
            "account",
            {
                "action": "add",
                "name": "Jordan Account",
                "email": "jordan@example.com",
                "role": "account_escalation",
                "source": "operator_verified",
                "actor": "Greg",
            },
            {"type": "account", "id": "account_acme"},
            "upsert",
            "cnt_account_acme_jordan_account_account_escalation",
        ),
        (
            "account",
            {
                "action": "edit",
                "contact_id": "cnt_account_existing",
                "name": "Jordan Updated",
                "email": "jordan.updated@example.com",
                "role": "billing",
                "source": "operator_verified",
                "actor": "Greg",
            },
            {"type": "account", "id": "account_acme"},
            "upsert",
            "cnt_account_existing",
        ),
        (
            "account",
            {
                "action": "remove",
                "contact_id": "cnt_account_existing",
                "confirm": "1",
                "actor": "Greg",
            },
            {"type": "account", "id": "account_acme"},
            "remove",
            "cnt_account_existing",
        ),
    ],
)
def test_contacts_post_enqueues_set_contact_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_type: str,
    body: dict[str, str],
    expected_target: dict[str, str],
    expected_action: str,
    expected_id: str,
) -> None:
    monkeypatch.setattr(site_detail.sites, "request_json", lambda *args, **kwargs: pytest.fail("must not direct-write CouchDB"))
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: _minimal_location(account="Acme"))
    monkeypatch.setattr(site_detail, "_load_account_doc_for_location", lambda doc: {"_id": "account_acme", "type": "account"})
    ctx = DummyContext(tmp_path)

    status, _content_type, _response_body, headers = site_detail.handle_contacts_post(
        ctx,
        "7050",
        urlencode(body).encode(),
        target_type=target_type,
    )

    assert status == 303
    assert headers["Location"] == "/sites/7050?message=contact_queued"
    queue_files = list((ctx.runtime_root / "queue").glob("set-contact-*.json"))
    assert len(queue_files) == 1
    job = json.loads(queue_files[0].read_text(encoding="utf-8"))
    assert job["job_type"] == "set_contact"
    assert validate_job(job) is True
    payload = job["payload"]
    assert payload["action"] == expected_action
    assert payload["target"] == expected_target
    assert payload["actor"] == "Greg"
    assert payload["contact"]["id"] == expected_id
    if expected_action == "upsert":
        assert payload["contact"]["scope"] == expected_target["type"]
        assert payload["source"] == "ops_dashboard_site_detail"
    else:
        assert payload["contact"] == {"id": expected_id}


def test_contacts_post_invalid_input_rerenders_without_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: _minimal_location(account="Acme"))
    monkeypatch.setattr(site_detail, "_load_account_doc_for_location", lambda doc: {"_id": "account_acme", "type": "account"})
    _stub_expensive_sections(monkeypatch)
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    ctx = DummyContext(tmp_path)
    body = {
        "action": "add",
        "name": "Broken Email",
        "email": "broken.example.com",
        "role": "site_contact",
        "source": "operator_verified",
        "actor": "Greg",
    }

    status, _content_type, response_body, _headers = site_detail.handle_contacts_post(
        ctx,
        "7050",
        urlencode(body).encode(),
        target_type="site",
    )

    assert status == 200
    assert b"Contact update was not queued" in response_body
    assert list((ctx.runtime_root / "queue").glob("set-contact-*.json")) == []


def test_post_save_section_updates_only_contact_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        site_detail,
        "_load_location",
        lambda site_id: {
            "_id": "location_7050",
            "_rev": "3-xyz",
            "type": "location",
            "operator": "op_greg",
            "billing_monthly": "100",
            "customer_name": "Old Name",
        },
    )
    captured: dict[str, object] = {}

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(site_detail.sites, "request_json", fake_request_json)
    ctx = DummyContext(tmp_path)

    site_detail.handle_save_section(
        ctx,
        "7050",
        b"_section=contact&customer_name=New+Name&_entity_id=1337",
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["customer_name"] == "New Name"
    assert payload["billing_monthly"] == "100"
    assert payload["type"] == "location"
    assert payload["operator"] == "op_greg"


def test_post_save_section_409_rerenders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class ConflictError(Exception):
        code = 409

    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: _minimal_location(customer_name="Old Name"))
    monkeypatch.setattr(site_detail.sites, "request_json", lambda *args, **kwargs: (_ for _ in ()).throw(ConflictError()))
    monkeypatch.setattr(site_detail, "render", lambda ctx, site_id: "<html>rerendered</html>")
    ctx = DummyContext(tmp_path)

    status, _content_type, body, _headers = site_detail.handle_save_section(
        ctx,
        "7050",
        b"_section=contact&customer_name=New+Name&_entity_id=1337",
    )

    assert status == 200
    assert b"rerendered" in body


def test_post_save_section_write_doc_has_valid_vault_fields() -> None:
    existing = _minimal_location(
        _rev="3-xyz",
        customer_name="Old Name",
        billing_monthly="100",
    )
    form = {"customer_name": "New Name"}
    updated_doc = entity_edit.apply_section_update(existing, form, frozenset({"customer_name"}))

    assert updated_doc["type"] == "location"
    assert updated_doc["operator"]
