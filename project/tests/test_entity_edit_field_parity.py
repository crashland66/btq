"""Gating tests for prompt 300: employee detail field-group parity with site detail.

These tests are authored by the independent verifier (NOT the executor). They pin:
  * entity_edit display branch renders shared field-row markup with Title-Cased labels
    and the site-style ``<h3>{Title}</h3><p class="actions">...Edit...</p>`` header.
  * empties are skipped in the display branch.
  * the edit-form branch humanizes visible <label> text but keeps RAW keys in name=.
  * employee About markdown headings are demoted (no stray <h1>/<h2>) while the shared
    render_markdown is unchanged.
  * site detail non-edit render is byte-identical (golden) to the pre-300 behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ops_dashboard.common import field_rows
from ops_dashboard.sections import employee_detail, entity_edit, site_detail


# --------------------------------------------------------------------------- #
# 1. Display branch: field-row wrappers + Title-Cased labels + site header     #
# --------------------------------------------------------------------------- #

def _contact_args(edit_active: bool) -> dict[str, object]:
    return dict(
        title="Contact",
        doc={"phone": "7245550100", "email": "x@y.com", "_rev": "r"},
        keys=("phone", "email"),
        edit_active=edit_active,
        save_action="/employees/e1/save-section",
        entity_id="e1",
    )


def test_display_branch_uses_field_row_with_title_cased_labels() -> None:
    out = entity_edit.render_editable_section(**_contact_args(edit_active=False))

    # field-row wrapped, Title-Cased label (NOT raw lowercase `phone`)
    assert '<div class="field-row"><dt>Phone</dt><dd>7245550100</dd></div>' in out
    assert "<dt>Email</dt>" in out
    # the raw lowercase bare-key markup must be gone
    assert "<dt>phone</dt>" not in out
    assert "<dt>email</dt>" not in out


def test_display_branch_header_matches_site_style() -> None:
    out = entity_edit.render_editable_section(**_contact_args(edit_active=False))
    assert (
        '<h3>Contact</h3><p class="actions">'
        '<a class="button" href="?edit=contact">Edit</a></p>'
    ) in out


def test_display_branch_rows_byte_equal_site_field_rows() -> None:
    doc = {"phone": "7245550100", "email": "x@y.com", "_rev": "r"}
    keys = ("phone", "email")
    out = entity_edit.render_editable_section(
        title="Contact", doc=doc, keys=keys, edit_active=False,
        save_action="/x", entity_id="e1",
    )
    # The exact row markup site detail produces must appear verbatim.
    assert field_rows(doc, keys) in out


def test_display_branch_skips_empty_values() -> None:
    doc = {"phone": "7245550100", "email": "", "fax": "   ", "_rev": "r"}
    out = entity_edit.render_editable_section(
        title="Contact", doc=doc, keys=("phone", "email", "fax"),
        edit_active=False, save_action="/x", entity_id="e1",
    )
    assert "<dt>Phone</dt>" in out
    # empty string and whitespace-only values are omitted entirely
    assert "<dt>Email</dt>" not in out
    assert "<dt>Fax</dt>" not in out


# --------------------------------------------------------------------------- #
# 2. Edit-form branch: humanized label text, RAW keys in name=                 #
# --------------------------------------------------------------------------- #

def test_edit_form_keeps_raw_name_attribute() -> None:
    out = entity_edit.render_editable_section(**_contact_args(edit_active=True))
    # Mutation target: if someone humanizes name=, the POST handler breaks.
    assert '<input type="text" name="phone"' in out
    assert '<input type="text" name="email"' in out
    assert 'name="Phone"' not in out
    assert 'name="Email"' not in out


def test_edit_form_label_text_is_humanized() -> None:
    out = entity_edit.render_editable_section(**_contact_args(edit_active=True))
    assert '<span class="field-row-label">Phone</span>' in out
    assert '<span class="field-row-label">Email</span>' in out


def test_edit_form_hidden_fields_save_action_and_submit_intact() -> None:
    out = entity_edit.render_editable_section(**_contact_args(edit_active=True))
    assert '<input type="hidden" name="_rev" value="r">' in out
    assert '<input type="hidden" name="_entity_id" value="e1">' in out
    assert '<input type="hidden" name="_section" value="contact">' in out
    assert 'action="/employees/e1/save-section"' in out
    assert '<button type="submit">Save</button>' in out


# --------------------------------------------------------------------------- #
# 3. Employee About markdown demotion (shared render_markdown unchanged)       #
# --------------------------------------------------------------------------- #

def _employee_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "employee_wade",
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "name": "Henry Wade",
        "first": "Henry",
        "last": "Wade",
        "status": "active",
        "site_ids": ["7090"],
        "content": "# Wade, Henry\n\n## Availability\n\n### Notes\n\nbody",
    }
    doc.update(overrides)
    return doc


def test_employee_about_headings_demoted_no_h1_h2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(employee_detail, "_load_vault_doc", lambda _doc_id: _employee_doc())
    out = employee_detail.render(SimpleNamespace(), "wade")

    assert "<h3>About</h3>" in out
    # The About content must not inject a second page <h1> or oversized <h2>.
    lower = out.lower()
    assert "<h1>wade, henry</h1>" not in lower
    assert "<h2>availability</h2>" not in lower
    # Demoted levels present instead.
    assert "<h4>Wade, Henry</h4>" in out
    assert "<h5>Availability</h5>" in out
    assert "<h6>Notes</h6>" in out


def test_shared_render_markdown_still_emits_h1() -> None:
    # Pin that the shared helper is NOT changed: vault pages still get h1/h2.
    rendered = employee_detail.render_markdown("# Title\n\n## Sub")
    assert "<h1>Title</h1>" in rendered
    assert "<h2>Sub</h2>" in rendered


def test_demote_helper_clamps_at_h6() -> None:
    src = "<h1>a</h1><h2>b</h2><h3>c</h3><h4>d</h4>"
    out = employee_detail._demote_about_headings(src)
    assert out == "<h4>a</h4><h5>b</h5><h6>c</h6><h6>d</h6>"


# --------------------------------------------------------------------------- #
# 4. Site detail non-edit render unchanged (golden)                           #
# --------------------------------------------------------------------------- #

def _golden_site_doc() -> dict[str, object]:
    return {
        "account": "Roy Carson Inc",
        "job": "7090",
        "location": "Main Plant",
        "address": ["123 Main St", "Suite 4"],
        "facility_type": "office",
        "status": "active",
        "customer_name": "Roy Carson",
        "customer_phone": "7245550100",
        "service_days": ["Mon", "Wed", "Fri"],
        "hours_per_week": "20",
        "billing_monthly": "1500",
        "rate": '"25.00"',
        "background_check_fee": "",
        "extra_unmapped_key": "shows up in Other",
    }


# Golden string captured from the pre-300 base commit (b62843f) render of
# site_detail._summary_section(doc, "7090", "") on the doc above. Proven
# byte-identical across base..HEAD by the verifier harness.
_SITE_GOLDEN = (
    '<section><h2>Summary</h2>'
    '<section><h3>Identity</h3><dl class="fields">'
    '<div class="field-row"><dt>Account</dt><dd>Roy Carson Inc</dd></div>'
    '<div class="field-row"><dt>Job</dt><dd>7090</dd></div>'
    '<div class="field-row"><dt>Location</dt><dd>Main Plant</dd></div>'
    '<div class="field-row"><dt>Address</dt><dd>123 Main St, Suite 4</dd></div>'
    '<div class="field-row"><dt>Facility Type</dt><dd>office</dd></div>'
    '<div class="field-row"><dt>Status</dt><dd>active</dd></div></dl></section>'
    '<section><h3>Contact</h3>'
    '<p class="actions"><a class="button" href="?edit=contact">Edit</a></p>'
    '<dl class="fields">'
    '<div class="field-row"><dt>Customer Name</dt><dd>Roy Carson</dd></div>'
    '<div class="field-row"><dt>Customer Phone</dt><dd>7245550100</dd></div>'
    '</dl></section>'
    '<section><h3>Schedule</h3>'
    '<p class="actions"><a class="button" href="?edit=schedule">Edit</a></p>'
    '<dl class="fields">'
    '<div class="field-row"><dt>Service Days</dt><dd>Mon, Wed, Fri</dd></div>'
    '<div class="field-row"><dt>Hours Per Week</dt><dd>20</dd></div>'
    '</dl></section>'
    '<section><h3>Billing &amp; Wages</h3>'
    '<p class="actions"><a class="button" href="?edit=billing_wages">Edit</a></p>'
    '<dl class="fields">'
    '<div class="field-row"><dt>Billing Monthly</dt><dd>1500</dd></div>'
    '<div class="field-row"><dt>Rate</dt><dd>25.00</dd></div>'
    '</dl></section>'
)


def test_site_detail_non_edit_render_unchanged_golden() -> None:
    out = site_detail._summary_section(_golden_site_doc(), "7090", "")
    # Pin the exact display markup so any regression to the site path is caught.
    # (Other/provenance sections appended after the groups are allowed to vary;
    # assert the field-group prefix is byte-stable.)
    assert out.startswith(_SITE_GOLDEN)
