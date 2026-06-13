"""The SANDBOX canonical fixture — the one fake site + employee for tests + the demo."""

from __future__ import annotations

from ops_dashboard.sections.home import _render_employee_directory
from test_helpers.canonical_fixtures import (
    SANDBOX_PERSON_ID,
    SANDBOX_PERSON_NAME,
    SANDBOX_SITE_ID,
    SANDBOX_SITE_NAME,
    sandbox_employee_doc,
    sandbox_employee_view_row,
    sandbox_site_doc,
    sandbox_site_registry_entry,
)


def test_sandbox_employee_doc_shape() -> None:
    doc = sandbox_employee_doc()
    assert doc["_id"] == "employee_sandbox-user"
    assert doc["type"] == "employee"
    assert doc["person_id"] == SANDBOX_PERSON_ID == "sandbox-user"
    assert doc["name"] == SANDBOX_PERSON_NAME == "Sandy Sandbox"
    assert doc["status"] == "active"
    assert doc["job"] == SANDBOX_SITE_ID == "SANDBOX"
    assert doc["site_ids"] == ["SANDBOX"]


def test_sandbox_site_doc_shape() -> None:
    doc = sandbox_site_doc()
    assert doc["_id"] == "SANDBOX"
    assert doc["site_id"] == SANDBOX_SITE_ID
    assert doc["type"] == "site"
    assert doc["location"] == SANDBOX_SITE_NAME
    # active:True (boolean) is required for the site to resolve/submit (by_site_id view);
    # account is what the fc app shows as the site label.
    assert doc["active"] is True
    assert doc["account"] == SANDBOX_SITE_NAME


def test_sandbox_site_registry_entry_shape() -> None:
    entry = sandbox_site_registry_entry()
    assert entry["site_id"] == "SANDBOX"
    assert entry["canonical"] == SANDBOX_SITE_NAME
    assert "sandbox" in entry["aliases"]


def test_sandbox_employee_renders_in_active_directory() -> None:
    out = _render_employee_directory([sandbox_employee_view_row()], {})
    assert "Sandbox, Sandy" in out


def test_sandbox_employee_inactive_override_is_excluded() -> None:
    out = _render_employee_directory([sandbox_employee_view_row(status="inactive")], {})
    assert "Sandbox, Sandy" not in out


def test_overrides_apply() -> None:
    assert sandbox_employee_doc(name="Override Name")["name"] == "Override Name"
    assert sandbox_site_doc(status="inactive")["status"] == "inactive"
