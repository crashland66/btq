"""Gating tests for prompt 297: the shared record-layout helpers in
``ops_dashboard.common`` (``has_value``, ``field_value``, ``humanize``,
``field_rows``, ``record_section``, ``other_section``) and the equivalence of
the ``site_detail`` / ``prospect_detail`` field-group rendering after the
refactor onto those helpers.

Authored independently of the implementation (verifier-owned). These assert the
documented markup contract exactly so the helpers cannot drift back into a
copy-paste duplicate or silently change the rendered HTML.
"""

from __future__ import annotations

from ops_dashboard import common


# ---------------------------------------------------------------------------
# has_value
# ---------------------------------------------------------------------------


def test_has_value_rejects_empty_and_sentinel_values() -> None:
    for falsey in (None, "", "   ", "[]", "{}", "None", "null", [], {}, (), set()):
        assert common.has_value(falsey) is False, falsey


def test_has_value_accepts_real_values_including_zero() -> None:
    for truthy in ("x", "0", 0, ["a"], {"k": "v"}, ("a",), "  hi  "):
        assert common.has_value(truthy) is True, truthy


# ---------------------------------------------------------------------------
# field_value
# ---------------------------------------------------------------------------


def test_field_value_joins_lists_dropping_blank_items_and_escapes() -> None:
    assert common.field_value(["a", "", "b & c"]) == "a, b &amp; c"


def test_field_value_strips_single_wrapping_quote_pair() -> None:
    assert common.field_value('"wrapped"') == "wrapped"
    assert common.field_value("'wrapped'") == "wrapped"


def test_field_value_leaves_embedded_quotes_untouched() -> None:
    # Not a wrapping pair -> no strip; html-escaped.
    assert common.field_value('he said "hi"') == "he said &quot;hi&quot;"


# ---------------------------------------------------------------------------
# humanize
# ---------------------------------------------------------------------------


def test_humanize_title_cases_snake_case_keys() -> None:
    assert common.humanize("customer_name") == "Customer Name"
    assert common.humanize("billing_monthly") == "Billing Monthly"


# ---------------------------------------------------------------------------
# field_rows
# ---------------------------------------------------------------------------


def test_field_rows_emits_documented_markup_only_for_present_keys() -> None:
    doc = {"customer_name": "Jane", "customer_email": "", "missing": None}
    rows = common.field_rows(doc, ("customer_name", "customer_email", "missing", "absent"))
    assert rows == (
        '<div class="field-row"><dt>Customer Name</dt><dd>Jane</dd></div>'
    )


def test_field_rows_respects_custom_value_formatter() -> None:
    # prospect_detail passes a non-quote-stripping formatter.
    doc = {"account": '"Quoted"'}
    rows = common.field_rows(
        doc, ("account",), value_formatter=lambda v: __import__("html").escape(str(v))
    )
    assert rows == '<div class="field-row"><dt>Account</dt><dd>&quot;Quoted&quot;</dd></div>'


# ---------------------------------------------------------------------------
# record_section
# ---------------------------------------------------------------------------


def test_record_section_documented_markup() -> None:
    doc = {"customer_name": "Jane"}
    out = common.record_section("Contact", doc, ("customer_name",))
    assert out == (
        '<section><h3>Contact</h3>'
        '<dl class="fields">'
        '<div class="field-row"><dt>Customer Name</dt><dd>Jane</dd></div>'
        "</dl></section>"
    )


def test_record_section_injects_actions_html_between_heading_and_dl() -> None:
    doc = {"customer_name": "Jane"}
    out = common.record_section(
        "Contact", doc, ("customer_name",), actions_html="<p class=\"actions\">EDIT</p>"
    )
    assert out == (
        '<section><h3>Contact</h3>'
        '<p class="actions">EDIT</p>'
        '<dl class="fields">'
        '<div class="field-row"><dt>Customer Name</dt><dd>Jane</dd></div>'
        "</dl></section>"
    )


def test_record_section_returns_empty_string_when_no_rows() -> None:
    doc = {"customer_name": "", "other": None}
    assert common.record_section("Contact", doc, ("customer_name", "other")) == ""


def test_record_section_uses_h3_not_h2() -> None:
    out = common.record_section("Contact", {"customer_name": "Jane"}, ("customer_name",))
    assert "<h3>Contact</h3>" in out
    assert "<h2>" not in out


# ---------------------------------------------------------------------------
# other_section
# ---------------------------------------------------------------------------


def test_other_section_excludes_ordered_and_suppressed_keys_and_sorts() -> None:
    doc = {
        "zeta": "z",
        "alpha": "a",
        "ordered_key": "should-be-skipped",
        "_rev": "1-x",  # suppressed
        "empty": "",  # omitted by has_value
    }
    out = common.other_section(doc, {"ordered_key"}, {"_rev"})
    assert out == (
        '<section><h3>Other</h3>'
        '<dl class="fields">'
        '<div class="field-row"><dt>Alpha</dt><dd>a</dd></div>'
        '<div class="field-row"><dt>Zeta</dt><dd>z</dd></div>'
        "</dl></section>"
    )


def test_other_section_empty_when_nothing_remains() -> None:
    doc = {"ordered_key": "x", "_rev": "1-x"}
    assert common.other_section(doc, {"ordered_key"}, {"_rev"}) == ""


# ---------------------------------------------------------------------------
# Equivalence golden: site_detail / prospect_detail field-group rendering
# ---------------------------------------------------------------------------


def test_site_detail_summary_section_golden_markup() -> None:
    from ops_dashboard.sections import site_detail

    doc = {
        "account": '"Quoted Account"',  # site strips the wrapping quotes
        "customer_name": "Jane Doe",
        "service_days": ["Mon", "Tue"],
        "zzz_other": "extra",
    }
    out = site_detail._quick_facts_section(dict(doc), "S-1", "")

    # Identity strips wrapping quotes via field_value.
    assert '<div class="field-row"><dt>Account</dt><dd>Quoted Account</dd></div>' in out
    # Contact group carries the Edit action and an <h3>.
    assert '<section><h3>Contact</h3><p class="actions">' in out
    assert '<div class="field-row"><dt>Customer Name</dt><dd>Jane Doe</dd></div>' in out
    # List values are joined.
    assert '<div class="field-row"><dt>Service Days</dt><dd>Mon, Tue</dd></div>' in out
    # The redesign drops the "Other" catch-all: unmapped keys are no longer
    # surfaced in the quick-facts grid.
    assert '<section><h3>Other</h3>' not in out
    assert "Zzz Other" not in out
    # Field-group panels use <h3> headers (the outer "Quick facts" wrapper is
    # <h2> — so only assert no stray <h2> inside the panels).
    assert "<h2>Contact</h2>" not in out
    assert "<h2>Identity</h2>" not in out


def test_prospect_detail_summary_section_preserves_non_quote_stripping() -> None:
    from ops_dashboard.sections import prospect_detail

    doc = {
        "account": '"Quoted Account"',  # prospect does NOT strip wrapping quotes
        "name": "Prospect Name",
        "zzz_other": "extra",
    }
    out = prospect_detail._summary_section(dict(doc))

    # prospect keeps the wrapping quotes (escaped) — distinct from site_detail.
    assert (
        '<div class="field-row"><dt>Account</dt><dd>&quot;Quoted Account&quot;</dd></div>'
        in out
    )
    assert '<div class="field-row"><dt>Name</dt><dd>Prospect Name</dd></div>' in out
    assert '<section><h3>Other</h3>' in out
    assert "<h2>Identity</h2>" not in out
    assert "<h2>Other</h2>" not in out
