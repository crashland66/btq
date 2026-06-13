"""Gating tests for the redesigned /sites/<id> detail page.

These pin the redesign's INVARIANTS so a regression to the page structure fails
loudly:

  * Don't lose information — the About markdown and every projector section
    (notes/employees/opportunities/visits) still render, but now INSIDE a
    collapsed <details> (with a count in the summary). Collapsed != removed: the
    full content must still be present in the HTML. Capture provenance likewise.
  * Captures link out — the field-captures section is a gallery of cards that
    link to the capture detail; it does NOT render the full capture body inline.
  * Degrade gracefully — a sparse site renders the shell (metrics show "—"/0,
    empty sections empty-stated) with no broken box and no 500.
  * Edit flows intact — ?edit=contact and ?edit=about still render their forms.
  * Header — site name is the H1 (no raw id in the H1); a muted subline carries
    account · site_id · status.

Render reaches into CouchDB (related data) and the runtime root / field-photo
store (captures); both are monkeypatched here so we exercise the genuine
non-degraded page without a live backend.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard.sections import site_detail


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _rich_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "location_9001",
        "_rev": "5-z",
        "type": "location",
        "operator": "op_greg",
        "account": "Acme Corp",
        "location": "Acme HQ",
        "site_id": "9001",
        "status": "active",
        "customer_name": "Jane Customer",
        "service_days": ["Mon", "Tue"],
        "billing_monthly": "2000",
        "supply_budget_type": "fixed",
        "content": (
            "## Site notes\n- access via rear door\n"
            "## Access Constraints\nKey from front desk"
        ),
        "btq_job_ids": ["j1", "j2"],
    }
    doc.update(overrides)
    return doc


def _sparse_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "location_5",
        "_rev": "1-a",
        "type": "location",
        "operator": "op_greg",
        "site_id": "5",
    }
    doc.update(overrides)
    return doc


def _rich_related(_site_id: str) -> dict[str, object]:
    return {
        "notes": [{"content": "PURPLE_NOTE_BODY operational note", "created_at": "2026-06-01"}],
        "employee_rows": [{"doc": {"name": "Bob Worker", "site_id": "9001"}}],
        "opportunity_rows": [{"doc": {"site_id": "9001", "status": "open"}}],
        "visit_rows": [{"doc": {"site_id": "9001", "date": "2026-06-09"}}],
        "recent_visits": [{"doc": {"site_id": "9001", "date": "2026-06-09"}}],
    }


def _empty_related(_site_id: str) -> dict[str, object]:
    return {
        "notes": [],
        "employee_rows": [],
        "opportunity_rows": [],
        "visit_rows": [],
        "recent_visits": [],
    }


def _capture() -> dict[str, object]:
    return {
        "capture_id": "cap-9",
        "area_guess": "Lobby",
        "captured_at": "2026-06-10T10:00:00",
        "status": "processed",
        "image_media_url": "https://media.example/x.jpg",
        # A long description that, if rendered inline, would prove the gallery is
        # NOT linking out. The redesign must NOT surface this on the site page.
        "description": "INLINE_CAPTURE_BODY_SENTINEL full vision narrative goes here",
    }


def _render(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    doc: dict[str, object],
    *,
    related,
    captures: tuple[list[dict[str, object]], bool, int],
    query: dict[str, list[str]] | None = None,
) -> str:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: doc)
    monkeypatch.setattr(site_detail, "_related_data", related)
    monkeypatch.setattr(site_detail, "_site_capture_records", lambda ctx, site_id: captures)
    monkeypatch.setattr(site_detail, "submitters_by_capture", lambda runtime_root: {})
    ctx = SimpleNamespace(runtime_root=tmp_path / "runtime", query=query or {})
    return site_detail.render(ctx, doc.get("site_id", "9001"))


def _h1(html: str) -> str:
    m = re.search(r"<h1>(.*?)</h1>", html, re.S)
    assert m, f"no <h1> in: {html[:200]!r}"
    return m.group(1)


def _summaries(html: str) -> list[str]:
    return [re.sub(r"<.*?>", "", s) for s in re.findall(r"<summary>(.*?)</summary>", html, re.S)]


# --------------------------------------------------------------------------- #
# Header + subline
# --------------------------------------------------------------------------- #
def test_header_h1_is_name_only_subline_carries_account_id_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _rich_doc(),
        related=_rich_related,
        captures=([_capture()], False, 3),
    )

    # H1 is the canonical name only — no raw id in the H1.
    assert _h1(html) == "Acme Corp"
    assert "9001" not in _h1(html)
    # A muted subline carries account · site_id · status.
    m = re.search(r'class="subline">(.*?)</p>', html)
    assert m, "no .subline in header"
    subline = m.group(1)
    assert "Acme Corp" in subline
    assert "9001" in subline
    assert "active" in subline
    assert "&middot;" in subline


# --------------------------------------------------------------------------- #
# Metric cards
# --------------------------------------------------------------------------- #
def test_four_metric_cards_show_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _rich_doc(),
        related=_rich_related,
        captures=([_capture()], False, 3),
    )

    cards = re.findall(
        r'<div class="metric-card"><strong>(.*?)</strong><span>(.*?)</span>', html
    )
    assert len(cards) == 4
    by_label = {label: value for value, label in cards}
    assert by_label["Open opportunities"] == "1"
    assert by_label["Field captures"] == "3"
    # One "## Access Constraints" heading in the content → one access flag.
    assert by_label["Access flags"] == "1"


def test_metric_cards_dash_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _sparse_doc(),
        related=_empty_related,
        captures=([], False, 0),
    )

    cards = re.findall(
        r'<div class="metric-card"><strong>(.*?)</strong><span>(.*?)</span>', html
    )
    assert len(cards) == 4
    by_label = {label: value for value, label in cards}
    assert by_label["Open opportunities"] == "0"
    assert by_label["Field captures"] == "0"
    # No parseable visit → "Last visit" degrades to an em-dash, not a crash.
    assert by_label["Last visit"] == "&mdash;"


# --------------------------------------------------------------------------- #
# Don't lose information — About + projector sections live inside <details>
# --------------------------------------------------------------------------- #
def test_about_and_projector_sections_present_inside_collapsed_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _rich_doc(),
        related=_rich_related,
        captures=([_capture()], False, 3),
    )

    # Each section is a <details> with a labelled summary.
    summaries = _summaries(html)
    assert any("About" in s for s in summaries)
    assert any("Notes" in s for s in summaries)
    assert any("Employees Assigned" in s for s in summaries)
    assert any("Open Opportunities" in s for s in summaries)
    assert any("Recent Visits" in s for s in summaries)

    # The projector summaries carry a count (e.g. "Notes" + "1 record").
    assert any("Notes" in s and "record" in s for s in summaries)

    # Collapsed != removed: the FULL content is still present in the HTML.
    assert "access via rear door" in html  # About markdown body
    assert "PURPLE_NOTE_BODY" in html  # note body inside the Notes details
    assert "Bob Worker" in html  # employee inside the Employees details


def test_capture_provenance_still_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _rich_doc(),
        related=_rich_related,
        captures=([_capture()], False, 3),
    )

    # Provenance lives inside a <details> but is still fully reachable.
    assert "Capture provenance" in html
    assert "btq_job_ids: j1, j2" in html


# --------------------------------------------------------------------------- #
# Captures link out — gallery, no inline body
# --------------------------------------------------------------------------- #
def test_captures_gallery_links_out_and_has_no_inline_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _rich_doc(),
        related=_rich_related,
        captures=([_capture()], False, 3),
    )

    assert '<section class="site-captures">' in html
    # Each card links to the capture detail; the section links to /field-photos.
    assert 'href="/captures?capture_id=cap-9"' in html
    assert "/field-photos?site_id=9001" in html
    assert "site-gallery-card" in html
    # The full capture body/description must NOT be rendered inline on the site
    # page — that lives on the capture detail the card links to.
    assert "INLINE_CAPTURE_BODY_SENTINEL" not in html


# --------------------------------------------------------------------------- #
# Degrade gracefully — sparse site renders the shell, no 500
# --------------------------------------------------------------------------- #
def test_sparse_site_renders_shell_without_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _sparse_doc(),
        related=_empty_related,
        captures=([], False, 0),
    )

    # Not the _degraded error box, not the not_found page.
    assert 'class="error"' not in html
    assert "Site not found" not in html
    # The shell renders: header, metric grid, and an empty-stated captures panel.
    assert '<section class="metric-grid"' in html
    assert "No field captures yet" in html
    assert "Field captures &middot; 0" in html


# --------------------------------------------------------------------------- #
# Edit flows intact
# --------------------------------------------------------------------------- #
def test_edit_contact_renders_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _sparse_doc(customer_name="ACME"),
        related=_empty_related,
        captures=([], False, 0),
        query={"edit": ["contact"]},
    )

    assert "<form" in html
    assert 'name="customer_name"' in html
    assert "/sites/5/save-section" in html


def test_edit_about_renders_textarea(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = _render(
        monkeypatch,
        tmp_path,
        _sparse_doc(content="Raw about text"),
        related=_empty_related,
        captures=([], False, 0),
        query={"edit": ["about"]},
    )

    assert '<textarea name="content">' in html
    assert "Raw about text" in html
    assert "/sites/5/save-section" in html
