from __future__ import annotations

from ops_dashboard.sections.entity_edit import (
    apply_section_update,
    recompute_employee_derived,
    render_editable_section,
)


def test_apply_section_update_only_changes_allowed_keys():
    existing = {
        "_id": "e1",
        "_rev": "1-abc",
        "type": "employee",
        "email": "old@example.com",
        "phone": "555-0000",
        "first_name": "Alice",
        "last_name": "Smith",
    }
    form = {
        "email": "new@example.com",
        "phone": "555-9999",
        "first_name": "Hacker",
        "_id": "injected",
    }
    allowed_keys = frozenset({"email", "phone"})
    result = apply_section_update(existing, form, allowed_keys)

    assert result["email"] == "new@example.com"
    assert result["phone"] == "555-9999"
    assert result["first_name"] == "Alice"
    assert result["_id"] == "e1"


def test_apply_section_update_preserves_protected_fields():
    existing = {
        "_id": "e1",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "jordan",
        "vault_path": "/v/e1",
        "site_ids": ["s1"],
        "name": "Alice Smith",
    }
    form = {
        "_id": "evil",
        "_rev": "9-zzz",
        "type": "site",
        "operator": "hacker",
        "vault_path": "/evil",
        "site_ids": [],
        "name": "Bad Actor",
    }
    allowed_keys = frozenset({"_id", "_rev", "type", "operator", "vault_path", "site_ids", "name"})
    result = apply_section_update(existing, form, allowed_keys)

    assert result["_id"] == existing["_id"]
    assert result["_rev"] == existing["_rev"]
    assert result["type"] == existing["type"]
    assert result["operator"] == existing["operator"]
    assert result["vault_path"] == existing["vault_path"]
    assert result["site_ids"] == existing["site_ids"]
    assert result["name"] == existing["name"]


def test_apply_section_update_strips_whitespace():
    existing = {"_id": "e1", "_rev": "1-abc", "notes": "old"}
    form = {"notes": "  Hello World  "}
    allowed_keys = frozenset({"notes"})
    result = apply_section_update(existing, form, allowed_keys)

    assert result["notes"] == "Hello World"


def test_apply_section_update_removes_empty_string_value():
    existing = {"_id": "e1", "_rev": "1-abc", "email": "old@example.com"}
    form = {"email": ""}
    allowed_keys = frozenset({"email"})
    result = apply_section_update(existing, form, allowed_keys)

    assert "email" not in result
    assert result["_id"] == "e1"


def test_recompute_employee_derived_updates_name_and_site_ids():
    doc = {"first": "Alice", "last": "Smith", "preferred_name": "", "job": "7050"}
    result = recompute_employee_derived(doc)

    assert result is doc
    assert doc["name"] == "Alice Smith"
    assert "7050" in doc["site_ids"]


def test_render_editable_section_view_state():
    doc = {
        "_id": "s1",
        "_rev": "2-xyz",
        "address": "123 Main St",
        "phone": "555-1234",
        "notes": "",
    }
    html = render_editable_section(
        "Contact",
        doc,
        ("address", "phone", "notes"),
        edit_active=False,
        save_action="/ops/site/s1/save",
        entity_id="s1",
    )

    assert "<dl" in html and 'class="fields"' in html
    assert 'href="?edit=contact"' in html
    assert "<form" not in html
    assert "123 Main St" in html
    assert "notes" not in html


def test_render_editable_section_edit_state():
    doc = {"_id": "s1", "_rev": "2-xyz", "address": "123 Main St", "phone": ""}
    html = render_editable_section(
        "Contact",
        doc,
        ("address", "phone"),
        edit_active=True,
        save_action="/ops/site/s1/save",
        entity_id="s1",
    )

    assert '<form method="post"' in html
    assert 'action="/ops/site/s1/save"' in html
    assert 'name="_rev"' in html and 'value="2-xyz"' in html
    assert 'name="_entity_id"' in html and 'value="s1"' in html
    assert 'name="_section"' in html and 'value="contact"' in html
    assert 'name="address"' in html
    assert 'name="phone"' in html
    assert 'href="?"' in html
