"""Independent verification of prompt 363 P0 presentability changes (VP demo).

These are BEHAVIORAL gating tests authored by a verifier who did NOT write the
implementation. They drive the server-side render functions in-process (browser
behavior is not pytest-executable; where a marker is purely CSS/JS the test says
so and asserts the structural HTML marker that IS server-rendered).

The MOST important invariant under test: populated states render exactly as
today, and the demo flag (env BTQ_DASHBOARD_DEMO) is OFF by default.

Run ONLY this file:
    project/.venv/bin/python -m pytest project/tests/test_dashboard_demo_p0.py
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard import layout
from ops_dashboard.layout import html_page, nav_html
from ops_dashboard.sections import captures as captures_section
from ops_dashboard.sections import swipe as swipe_section


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def swipe_body(tmp_path: Path, *, payload: dict | None = None) -> str:
    ctx = SimpleNamespace(runtime_root=tmp_path, config=SimpleNamespace(vault_dir=tmp_path / "vault"))
    return swipe_section.render_body(ctx, payload=payload)


@pytest.fixture(autouse=True)
def _demo_off_by_default(monkeypatch):
    """Hard-guarantee the env is UNSET unless a test sets it, so the autouse
    default mirrors a clean operator environment (flag off)."""
    monkeypatch.delenv("BTQ_DASHBOARD_DEMO", raising=False)
    yield


# ===========================================================================
# Contract 1 — Demo flag (env BTQ_DASHBOARD_DEMO)
# ===========================================================================

def test_demo_unset_keeps_admin_nav_and_version_footer(monkeypatch):
    """Default (env unset): today's behavior unchanged — Admin nav entry present,
    version/SHA footer present."""
    monkeypatch.delenv("BTQ_DASHBOARD_DEMO", raising=False)

    nav = nav_html("home")
    assert 'href="/admin"' in nav, "Admin nav entry must be present when demo is off"
    assert ">Admin<" in nav

    page = html_page("T", "<p>body</p>", active_section="home")
    assert layout._VERSION in page, "version string must be present when demo is off"
    # The version footer is a labeled <span>; confirm it actually rendered.
    assert "font-size:0.75em" in page


def test_demo_set_drops_admin_nav_and_version_footer(monkeypatch):
    """Demo ON ('1'): Admin nav entry removed AND version string omitted."""
    monkeypatch.setenv("BTQ_DASHBOARD_DEMO", "1")

    nav = nav_html("home")
    assert "/admin" not in nav, "Admin nav entry must be dropped in demo mode"
    assert ">Admin<" not in nav

    page = html_page("T", "<p>body</p>", active_section="home")
    assert layout._VERSION not in page, "version string must be omitted in demo mode"
    # The version footer markup is gone entirely.
    assert "font-size:0.75em" not in page


def test_demo_set_keeps_all_other_nav_items(monkeypatch):
    """Only Admin is dropped — every other operator nav entry survives demo mode."""
    monkeypatch.setenv("BTQ_DASHBOARD_DEMO", "1")
    nav = nav_html("home")
    for href in ("/", "/swipe", "/candidates", "/vault/types/index.html", "/records", "/field-photos", "/help"):
        assert f'href="{href}"' in nav, f"non-admin nav entry {href} must remain in demo mode"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "  TRUE ", "yes", "on", "On", "YeS"])
def test_demo_truthy_values_treated_as_on(monkeypatch, value):
    """Probe beyond spec: whitespace + case variants of truthy strings turn demo ON."""
    monkeypatch.setenv("BTQ_DASHBOARD_DEMO", value)
    assert layout._demo_mode() is True, f"{value!r} should be ON"
    assert "/admin" not in nav_html("home")


@pytest.mark.parametrize("value", ["0", "", "off", "OFF", "no", "false", "  ", "disabled", "2", "anythingelse"])
def test_demo_falsey_or_unknown_values_treated_as_off(monkeypatch, value):
    """0/empty/off/unknown -> OFF (today's behavior). Admin + version both present."""
    monkeypatch.setenv("BTQ_DASHBOARD_DEMO", value)
    assert layout._demo_mode() is False, f"{value!r} should be OFF"
    nav = nav_html("home")
    assert 'href="/admin"' in nav
    assert layout._VERSION in html_page("T", "<p>b</p>", active_section="home")


def test_demo_off_on_admin_section_still_marks_active(monkeypatch):
    """Sanity: with demo off, an admin-subsection page still resolves Admin active
    (unchanged behavior — the nav item exists and is current)."""
    monkeypatch.delenv("BTQ_DASHBOARD_DEMO", raising=False)
    nav = nav_html("health")  # health is in ADMIN_SECTIONS
    assert 'href="/admin"' in nav and 'aria-current="page"' in nav


# ===========================================================================
# Contract 2 — Copy
# ===========================================================================

def test_captures_list_has_no_dev_note_placeholder(tmp_path: Path):
    ctx = SimpleNamespace(runtime_root=tmp_path / "runtime", query={}, config=SimpleNamespace(vault_dir=tmp_path / "vault"))
    body = captures_section.render_list(ctx)
    assert "Coming in prompt" not in body
    assert "remain separate" not in body
    # The softened subtitle is what renders now.
    assert "Read-only browse of recent field captures." in body


def test_sites_list_copy_has_no_couchdb_jargon_or_duplicate_sentence(tmp_path: Path, monkeypatch):
    """sites render contains no 'CouchDB' substring and no duplicated
    'Edit ... site documents.' sentence. Drive render_list with load_sites failing
    closed (no live CouchDB in tests) so we exercise the operator-visible copy."""
    monkeypatch.setattr(captures_section, "render_list", captures_section.render_list, raising=False)
    from ops_dashboard.sections import sites as sites_section

    # load_sites would hit CouchDB; force the error path so the page still renders
    # its header/filter copy (the part under test) without a live DB.
    monkeypatch.setattr(sites_section, "load_sites", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
    ctx = SimpleNamespace(query={}, route_path="/sites", flash=lambda q: "")
    body = sites_section.render_list(ctx, {})

    assert "CouchDB" not in body, "operator copy must not mention CouchDB"
    # The old copy had the sentence twice ("Edit one CouchDB site document at a
    # time. Edit CouchDB site documents."). Even with jargon gone, guard against a
    # duplicated 'site document' sentence.
    assert body.count("site documents.") == 0
    assert "Edit one site at a time." in body


def test_sites_edit_form_copy_has_no_couchdb_jargon(tmp_path: Path):
    from ops_dashboard.sections import sites as sites_section

    ctx = SimpleNamespace(query={}, flash=lambda q: "")
    # render_form with is_new=True avoids the visits panel (no CouchDB call).
    body = sites_section.render_form(ctx, {}, is_new=True, query={})
    assert "CouchDB" not in body
    assert "Single-document CouchDB write" not in body
    assert "Saves one site. Sites aren't deleted here." in body


def test_home_directory_unavailable_message_softened(tmp_path: Path, monkeypatch):
    """The ProjectorError fallback path renders the softened message — no
    'CouchDB connection failed'."""
    from ops_dashboard.sections import home as home_section
    from btq_vault.projector import ProjectorError

    runtime_root = tmp_path / "runtime"

    # Force the directory build to raise ProjectorError so we hit the fallback
    # branch deterministically, and stub the console + voice card so the render
    # doesn't depend on a live CouchDB.
    monkeypatch.setattr(home_section._console_mod, "render_console", lambda ctx: "<div>console</div>")
    monkeypatch.setattr(home_section, "_render_voice_card", lambda *a, **k: "<section>voice</section>")

    def _boom(*a, **k):
        raise ProjectorError("forced")

    monkeypatch.setattr(home_section, "query_view", _boom)
    monkeypatch.setattr(home_section.couchdb_config, "from_env", lambda: SimpleNamespace(base_url="http://x", auth_header=lambda: {}))

    ctx = SimpleNamespace(runtime_root=runtime_root)
    body = home_section.render(ctx)

    assert "Directory temporarily unavailable." in body
    assert "CouchDB connection failed" not in body
    assert "CouchDB" not in body


# ===========================================================================
# Contract 3 — Captures detail / photo card
# ===========================================================================

def _missing_photo_record() -> dict:
    """The exact record shape produced by _missing_sidecar_record for a photo
    asset with no vision sidecar yet."""
    return {
        "status": "missing",
        "capture_id": "cap-x",
        "photo_asset_id": "fcp_rawassetid_1234567890",
        "photo_id": "photo-1",
        "site_id": "7050",
        "submitted_area": "Restrooms",
        "submitted_phase": "reset",
        "captured_at": "2026-06-10T12:00:00Z",
        "submitter_name": "Alice Example",
        "submitter_id": "per_alice",
        "source_image_path": "/x/photo.jpg",
        "image_media_url": "/media/2026-06-10/cap-x/photo.jpg",
        "area_guess": "",
        "description": "No vision description yet.",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "confidence": "",
        "model_name": "",
        "model_provider": "",
        "generated_at": "",
        "warnings": [],
        "error": {},
    }


def test_unprocessed_photo_card_shows_awaiting_analysis_not_raw_placeholder():
    card = captures_section.render_photo_card(_missing_photo_record())

    # Status + description remapped to the friendly label.
    assert "Awaiting analysis." in card
    assert "No vision description yet." not in card
    assert ">missing<" not in card  # the raw status string must not surface as a value/title


def test_unprocessed_photo_card_title_is_never_missing_or_raw_asset_id():
    card = captures_section.render_photo_card(_missing_photo_record())
    title = re.search(r"<h3>(.*?)</h3>", card, re.S).group(1).strip()
    assert title == "Photo", f"title fell back to {title!r}; expected 'Photo'"
    assert title != "missing"
    assert "fcp_rawassetid" not in title


def test_unprocessed_photo_card_suppresses_empty_labeled_rows():
    card = captures_section.render_photo_card(_missing_photo_record())
    # Empty-valued fields must not produce a labeled row with a blank value.
    for empty_field in ("confidence", "model_name", "area_guess"):
        assert f"<th>{empty_field}</th><td></td>" not in card, f"{empty_field} rendered an empty row"
    # And present fields still render.
    assert "<th>submitter</th>" in card
    assert "Alice Example" in card
    assert "<th>captured_at</th>" in card


def test_fully_populated_photo_card_renders_all_real_fields():
    """The invariant that matters: a populated capture still shows ALL its real
    fields with their real values."""
    record = {
        "status": "completed",
        "photo_asset_id": "fcp_done_1",
        "submitter_name": "Sandy Field",
        "submitter_id": "per_sandy",
        "submitted_area": "Lobby",
        "submitted_phase": "reset",
        "captured_at": "2026-06-10T08:30:00Z",
        "image_media_url": "/media/2026-06-10/cap/photo.jpg",
        "site_context_summary": "Summit Wire; industrial wire facility",
        "area_guess": "lobby",
        "description": "A clean lobby with a polished floor and a front desk.",
        "visible_objects": ["front desk", "polished floor"],
        "possible_conditions": ["recently mopped"],
        "possible_issues": ["scuff near entrance"],
        "warnings": ["Vision output is advisory interpretation only."],
        "model_name": "ollama:qwen2.5vl:7b",
        "confidence": 0.82,
        "error": {},
    }
    card = captures_section.render_photo_card(record)

    # Title comes from the real area_guess, not "Photo".
    assert "<h3>lobby</h3>" in card
    # Real values present and NOT remapped.
    assert "Awaiting analysis." not in card
    assert "A clean lobby with a polished floor and a front desk." in card
    assert "fcp_done_1" in card
    assert "Sandy Field" in card
    assert "per_sandy" in card
    assert "Lobby" in card
    assert "reset" in card
    assert "2026-06-10T08:30:00Z" in card
    assert "Summit Wire; industrial wire facility" in card
    assert "front desk" in card and "polished floor" in card
    assert "recently mopped" in card
    assert "scuff near entrance" in card
    assert "ollama:qwen2.5vl:7b" in card
    assert "0.82" in card
    # vision_status for a completed record is its real status (not remapped).
    assert "<th>vision_status</th><td>completed</td>" in card


def test_partial_sidecar_present_fields_show_empty_fields_suppressed():
    """Beyond-spec: a partial record (some fields, some empty) — present fields
    render, empty ones are suppressed."""
    record = {
        "status": "completed",
        "photo_asset_id": "fcp_partial",
        "submitter_name": "Jordan",
        "submitter_id": "",          # empty -> suppressed
        "submitted_area": "Kitchen",
        "submitted_phase": "",       # empty -> suppressed
        "captured_at": "2026-06-10T09:00:00Z",
        "image_media_url": "",       # empty -> suppressed
        "area_guess": "kitchen",
        "description": "Partial description present.",
        "visible_objects": [],       # empty join -> suppressed
        "possible_conditions": ["wet floor"],
        "possible_issues": [],
        "warnings": [],
        "model_name": "",            # empty -> suppressed
        "confidence": "",            # empty -> suppressed
        "error": {},
    }
    card = captures_section.render_photo_card(record)

    # Present fields render.
    assert "<th>submitter</th>" in card and "Jordan" in card
    assert "<th>submitted_area</th>" in card and "Kitchen" in card
    assert "Partial description present." in card
    assert "wet floor" in card
    # Empty fields suppressed (no blank labeled rows).
    for empty_field in ("submitter_id", "submitted_phase", "image_media_url", "visible_objects", "model_name", "confidence", "possible_issues"):
        assert f"<th>{empty_field}</th><td></td>" not in card


def test_capture_detail_record_section_suppresses_empty_rows(tmp_path: Path):
    """End-to-end-ish: a missing-sidecar photo flows through render_detail and the
    page shows 'Awaiting analysis.' but never the raw placeholder."""
    from tests.test_ops_dashboard_captures import seed_capture
    from tests.test_ops_dashboard import request_text

    runtime_root = tmp_path / "runtime"
    seed_capture(runtime_root, "cap_unprocessed")
    _status, _ctype, body = request_text("GET", "/captures?capture_id=cap_unprocessed", runtime_root)

    assert "Awaiting analysis." in body
    assert "No vision description yet." not in body
    # No labeled empty rows for the always-blank vision fields on an unprocessed photo.
    assert "<th>confidence</th><td></td>" not in body
    assert "<th>model_name</th><td></td>" not in body


# ===========================================================================
# Contract 4 — Swipe
# ===========================================================================

def test_swipe_header_shows_pending_count_and_no_rejected_chip(tmp_path: Path):
    body = swipe_body(tmp_path, payload={"cards": [], "counts": {"pending_approval": 7, "rejected": 4}})
    assert '<a class="swipe-queue" data-queue="approval" href="/field-capture/review?status=pending_approval"' in body
    assert "<strong>7</strong> needs approval</a>" in body
    # The rejected chip is gone (no chip, no link to ?status=rejected).
    assert 'data-queue="rejected"' not in body
    assert "?status=rejected" not in body
    assert "rejected</a>" not in body


def test_swipe_has_no_defer_button(tmp_path: Path):
    body = swipe_body(tmp_path, payload={"cards": [], "counts": {}})
    assert "Defer" not in body
    assert 'class="swipe-btn defer"' not in body
    assert "Defer is not a draft review action" not in body
    # The other action buttons remain wired.
    assert 'data-act="reject"' in body
    assert 'data-act="skip"' in body
    assert 'data-act="approve"' in body


def test_swipe_empty_state_is_compact_one_liner(tmp_path: Path):
    body = swipe_body(tmp_path, payload={"cards": [], "counts": {}})
    # Compact muted one-liner present.
    assert '<p id="swipe-empty" class="muted" hidden>' in body
    assert "Nothing waiting for approval" in body
    # The old multi-line ✓ block is gone.
    assert "swipe-empty-mark" not in body
    assert 'class="swipe-empty"' not in body


def test_swipe_populated_pending_count_still_links_and_stack_wired(tmp_path: Path):
    """Populated invariant: pending count still links, and the card-stack/script
    wiring (bootstrap JSON + init) is intact."""
    body = swipe_body(tmp_path, payload={"cards": [], "counts": {"pending_approval": 3}})
    assert 'href="/field-capture/review?status=pending_approval"' in body
    assert "<strong>3</strong> needs approval" in body
    # Card-stack mount + bootstrap + init call all present.
    assert 'id="swipe-card-mount"' in body
    assert 'id="swipe-bootstrap" type="application/json"' in body
    assert "window.__btqSwipeInit && window.__btqSwipeInit();" in body
    # The card render path / decide loop is intact.
    assert "function decide(card, action, btn)" in body
    assert "/field-capture/review/approve" in body
    assert "/field-capture/review/reject" in body


def test_swipe_populated_sandbox_card_path_unaffected(tmp_path: Path, couchdb_job_draft_review):
    """Beyond-spec: the populated/SANDBOX card path is unaffected by the P0 chrome
    changes — a seeded pending draft surfaces through the real read path and the
    bootstrap carries it, with the action buttons intact (and no Defer)."""
    import json

    draft = {
        "draft_id": "ac_demo",
        "job_type": "append_to_note",
        "payload": {"path": "Accounts/7050.md", "content": "note", "destination": "site_note"},
        "message": "Bruce no-showed again.",
        "site_id": "7050",
        "submitter_name": "Greg",
        "source_capture_id": "cap-ac_demo",
        "confidence": "high",
        "created_at": "2026-06-11T00:00:00Z",
    }
    couchdb_job_draft_review.seed_draft(draft)

    payload = swipe_section.swipe_payload(tmp_path)
    assert payload["counts"]["pending_approval"] == 1
    body = swipe_body(tmp_path, payload=payload)

    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap not found"
    data = json.loads(m.group(1))
    assert any(c["draft_id"] == "ac_demo" for c in data["cards"])
    # Action buttons still present in the script; Defer still absent.
    assert 'data-act="approve"' in body
    assert "Defer" not in body
