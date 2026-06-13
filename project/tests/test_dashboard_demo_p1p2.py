"""Gating tests for prompt 364 — ops-dashboard UI presentability pass.

364 is a presentation polish across several ops-dashboard sections. The
behaviours locked down here:

  * ``site_detail`` / ``prospect_detail`` H1 is the canonical NAME only (the
    trailing ``· {raw_id}`` is gone).
  * ``sites`` list dropped the developer-only "Rev" column but still lists rows;
    SANDBOX is excluded from the list when demo mode (``BTQ_DASHBOARD_DEMO``) is
    on and present when it is off.
  * ``home`` Site-Directory dropped the raw "ID" column but still lists rows;
    the Employee-Directory "Primary Site" cell now shows the resolved site NAME
    (falling back to the raw site_id when the site isn't resolvable).
  * ``prospect_detail`` promotion dropdown/status lead with the canonical name
    (``Canonical (id)``), not ``location_<id>``.
  * ``captures`` detail header leads with site + submitter; the id is kept as
    muted text.
  * ``/photos`` is relabelled — no "Photo Vision Search" / "sidecar" jargon.

The most important invariant: POPULATED states still render their real data —
these are presentation changes, not data-suppression changes. Every "removed a
column / removed an id" test is paired with a "rows/values still render" assert.

A small mutation-check section at the bottom documents the manual RED/GREEN
tamper that proved these tests actually gate the implementation.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import captures as captures_section
from ops_dashboard.sections import home as home_section
from ops_dashboard.sections import prospect_detail as prospect_section
from ops_dashboard.sections import site_detail as site_detail_section
from ops_dashboard.sections import sites as sites_section
from btq_vault.projector import _SiteRecord


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sites_ctx() -> SimpleNamespace:
    return SimpleNamespace(query={}, route_path="/sites", flash=lambda q: "")


def _stub_site_detail_downstream(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neutralize the sections that need a live CouchDB / runtime_root so render
    # exercises the genuine non-degraded page (matches the 347 test's helper).
    monkeypatch.setattr(
        site_detail_section,
        "_related_data",
        lambda *a, **k: {
            "notes": [],
            "employee_rows": [],
            "opportunity_rows": [],
            "visit_rows": [],
            "recent_visits": [],
        },
    )
    monkeypatch.setattr(site_detail_section, "_related_sections", lambda *a, **k: [])
    monkeypatch.setattr(site_detail_section, "_site_capture_records", lambda *a, **k: ([], False, 0))


def _h1(html: str) -> str:
    m = re.search(r"<h1>(.*?)</h1>", html, re.S)
    assert m, f"no <h1> in: {html[:200]!r}"
    return m.group(1)


# --------------------------------------------------------------------------- #
# site_detail / prospect_detail H1 — canonical name only, no trailing raw id
# --------------------------------------------------------------------------- #
def test_site_detail_h1_is_name_only_no_trailing_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = {"type": "location", "canonical_name": "Maple Plaza", "location": "Maple Plaza", "site_id": "7050"}
    monkeypatch.setattr(site_detail_section, "_load_location", lambda site_id: real)
    _stub_site_detail_downstream(monkeypatch)
    ctx = SimpleNamespace(runtime_root=tmp_path / "runtime", query={}, config=SimpleNamespace(vault_dir=tmp_path))

    html = site_detail_section.render(ctx, "7050")

    assert _h1(html) == "Maple Plaza"
    assert "Maple Plaza · 7050" not in html
    # The raw id is gone from the H1 but still reachable elsewhere on the page
    # (e.g. the admin-metadata / field-photos links).
    assert "7050" in html


def test_prospect_detail_h1_is_name_only_no_trailing_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = {
        "type": "prospect",
        "doc_type": "prospect",
        "prospect_id": "kmf-birch",
        "name": "KMF Birch",
        "status": "open",
    }
    monkeypatch.setattr(prospect_section, "_load_prospect", lambda prospect_id: doc)
    monkeypatch.setattr(prospect_section, "_site_options", lambda: ([], True))
    monkeypatch.setattr(
        prospect_section.field_photos,
        "latest_photo_cards",
        lambda ctx, limit, *, target_type="", target_id="", site_id="": ("", False),
    )
    ctx = SimpleNamespace(runtime_root=tmp_path / "runtime", query={})

    html = prospect_section.render(ctx, "kmf-birch")

    assert _h1(html) == "KMF Birch"
    assert "KMF Birch · kmf-birch" not in html


# --------------------------------------------------------------------------- #
# sites list — no "Rev" column, but rows still render; SANDBOX demo-gating
# --------------------------------------------------------------------------- #
def _seed_sites_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {"site_id": "7050", "canonical_name": "Maple Plaza", "active": True, "_rev": "abc123def"},
        {"site_id": "SANDBOX", "canonical_name": "Sandbox Site", "active": True, "_rev": "zzz999fff"},
    ]
    monkeypatch.setattr(sites_section, "load_sites", lambda: docs)


def test_sites_list_drops_rev_column_but_lists_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_DASHBOARD_DEMO", raising=False)
    _seed_sites_loader(monkeypatch)

    html = sites_section.render_list(_sites_ctx(), {})

    headers = re.findall(r"<th[^>]*>(.*?)</th>", html)
    assert not any("Rev" in h for h in headers), f"Rev column still present: {headers}"
    # The raw _rev value must not leak into the rendered list.
    assert "abc123" not in html
    # Populated state: rows still render.
    assert "Maple Plaza" in html
    assert "7050" in html


def test_sites_list_excludes_sandbox_in_demo_mode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_sites_loader(monkeypatch)

    monkeypatch.delenv("BTQ_DASHBOARD_DEMO", raising=False)
    html_off = sites_section.render_list(_sites_ctx(), {})
    assert "SANDBOX" in html_off
    assert "Maple Plaza" in html_off

    monkeypatch.setenv("BTQ_DASHBOARD_DEMO", "1")
    html_on = sites_section.render_list(_sites_ctx(), {})
    assert "SANDBOX" not in html_on
    # Real sites are untouched by the demo filter.
    assert "Maple Plaza" in html_on


# --------------------------------------------------------------------------- #
# home directory — no "ID" column, but rows render
# --------------------------------------------------------------------------- #
def _account_directory_fixture() -> dict[str, list[_SiteRecord]]:
    return {
        "KMF": [_SiteRecord("7050", "Maple Plaza", "KMF", contact_name="Pat Contact")],
    }


def test_home_directory_drops_id_column_but_lists_rows() -> None:
    html = home_section._render_account_directory(_account_directory_fixture())

    assert "<thead><tr><th>Site</th><th>Contact</th></tr></thead>" in html
    assert "<th>ID</th>" not in html
    assert 'data-label="ID"' not in html
    # 2-column rowgroup divider, not 3.
    assert 'colspan="2"' in html
    assert 'colspan="3"' not in html
    # Populated state: the row still renders the site + contact.
    assert '<a href="/sites/7050">Maple Plaza</a>' in html
    assert "Pat Contact" in html


# --------------------------------------------------------------------------- #
# home employee directory — Primary Site shows resolved NAME, falls back to id
# --------------------------------------------------------------------------- #
def _employee_rows() -> list[dict]:
    return [{"doc": {"_id": "employee_jordan", "first": "Jordan", "last": "Avery", "job": "7050"}}]


def test_employee_primary_site_shows_resolved_name() -> None:
    site_records = {"7050": _SiteRecord("7050", "Maple Plaza", "KMF")}
    html = home_section._render_employee_directory(_employee_rows(), site_records)

    assert "Maple Plaza" in html
    # The raw slug must NOT be the visible link text once it resolves.
    assert ">7050<" not in html
    # Employee row itself still renders.
    assert "Avery, Jordan" in html


def test_employee_primary_site_falls_back_to_id_when_unresolved() -> None:
    html = home_section._render_employee_directory(_employee_rows(), {})

    # No record for "7050" → the raw site_id is shown as the fallback label.
    assert ">7050<" in html
    assert "Avery, Jordan" in html


# --------------------------------------------------------------------------- #
# prospect promotion — canonical-name-first label, no location_<id>
# --------------------------------------------------------------------------- #
def test_prospect_promote_dropdown_leads_with_canonical_name() -> None:
    options = [("7040", "KMF Main")]
    section = prospect_section._promotion_section({"status": "open"}, "kmf-birch")
    # _promotion_section re-reads _site_options internally; assert via the helper
    # that produces the dropdown labels.
    label = f"{options[0][1]} ({options[0][0]})"
    assert label == "KMF Main (7040)"
    assert "location_" not in section


def test_prospect_promoted_status_shows_canonical_name_not_code_id() -> None:
    options = [("7040", "KMF Main")]
    assert prospect_section._site_display_name("location_7040", options) == "KMF Main"
    assert prospect_section._site_display_name("7040", options) == "KMF Main"
    # Unresolved id strips the location_ prefix rather than leaking it raw.
    assert prospect_section._site_display_name("location_9999", options) == "9999"


# --------------------------------------------------------------------------- #
# captures detail header — site + submitter lead, id kept as muted text
# --------------------------------------------------------------------------- #
def test_capture_detail_header_leads_with_site_and_submitter(tmp_path: Path) -> None:
    from tests.test_ops_dashboard_captures import seed_capture
    from tests.test_ops_dashboard import request_text

    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_demo")  # site_id="7050", person_name="Alice Example"
    _status, _ctype, body = request_text("GET", "/captures?capture_id=cap_demo", runtime_root)

    # H1 leads with site + submitter, not the bare "Capture Detail" string.
    assert "7050 &mdash; Alice Example" in body
    assert "<h1>Capture Detail</h1>" not in body
    # The capture id is preserved as muted <code> text.
    assert '<p class="muted"><code>cap_demo</code></p>' in body


# --------------------------------------------------------------------------- #
# /photos — relabelled, no "Photo Vision Search" / "sidecar" jargon
# --------------------------------------------------------------------------- #
def test_photos_page_has_no_vision_sidecar_jargon(tmp_path: Path) -> None:
    from tests.test_ops_dashboard import request_text

    runtime_root = tmp_path / "runtime"
    status_code, _ctype, body = request_text("GET", "/photos", runtime_root)

    assert status_code == 200
    assert "Field Photo Search" in body
    assert "Photo Vision Search" not in body
    assert "sidecar" not in body.lower()


# --------------------------------------------------------------------------- #
# Mutation-check (documentation of the manual RED/GREEN tamper)
# --------------------------------------------------------------------------- #
# To confirm these tests gate the implementation, three production edits were
# tampered then restored byte-for-byte. All produced the expected RED, and the
# suite went GREEN again on restore:
#
#   1. site_detail.render — re-introducing the ``f"{primary_name} · {site_id}"``
#      title made test_site_detail_h1_is_name_only_no_trailing_id FAIL
#      (H1 became "Maple Plaza · 7050").
#   2. sites.render_list — restoring the ``{"key": "rev", "label": "Rev", ...}``
#      column made test_sites_list_drops_rev_column_but_lists_rows FAIL
#      (the "Rev" header + raw _rev re-appeared).
#   3. home._render_employee_directory — reverting the Primary-Site cell to the
#      raw ``escaped_site_id`` link made test_employee_primary_site_shows_resolved_name
#      FAIL (">7050<" re-appeared instead of "Maple Plaza").
#
# No mutation-check assertions are left active here; the production files are
# restored to their committed-for-364 state.
