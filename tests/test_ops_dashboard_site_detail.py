from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard import common
from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import site_detail, sites


def location_doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": "location_7040",
        "type": "location",
        "account": "KMF Industries",
        "location": "KMF Main",
        "site_id": "7040",
    }
    doc.update(overrides)
    return doc


def _empty_related() -> dict[str, object]:
    return {
        "notes": [],
        "employee_rows": [],
        "opportunity_rows": [],
        "visit_rows": [],
        "recent_visits": [],
    }


def render_with_doc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    doc: dict[str, object],
    *,
    account_doc: dict[str, object] | None = None,
) -> str:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: doc)
    monkeypatch.setattr(site_detail, "_load_account_doc_for_location", lambda loaded_doc: account_doc)
    # The redesigned render pulls related data (notes/employees/opportunities/
    # visits) and field captures from CouchDB; neutralize those so we exercise
    # the genuine non-degraded page without a live backend.
    monkeypatch.setattr(site_detail, "_related_data", lambda site_id: _empty_related())
    monkeypatch.setattr(site_detail, "_related_sections", lambda data: [])
    monkeypatch.setattr(site_detail, "_site_capture_records", lambda ctx, site_id: ([], False, 0))
    monkeypatch.setattr(site_detail, "_site_capture_processing_counts", lambda site_id: None)
    return site_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "7040")


def site_contact(**overrides: object) -> dict[str, object]:
    contact: dict[str, object] = {
        "id": "contact_jackie",
        "name": "Jackie Synthetic",
        "title": "Site Lead",
        "phone": "724-699-5846",
        "email": "jackie@example.com",
        "role": "site_contact",
        "scope": "site",
        "source": "synthetic_fixture",
        "source_date": "2026-07-07",
        "notes": "",
    }
    contact.update(overrides)
    return contact


def account_contact(**overrides: object) -> dict[str, object]:
    contact: dict[str, object] = {
        "id": "contact_jeremy",
        "name": "Jeremy Fabian",
        "title": "Operations Manager",
        "phone": "(724) 977-5591",
        "email": "jeremy@example.com",
        "role": "account_escalation",
        "scope": "account",
        "source": "synthetic_fixture",
        "source_date": "2026-07-07",
        "notes": "",
    }
    contact.update(overrides)
    return contact


def test_site_detail_strip_dataview_drops_three_known_blocks() -> None:
    body = """# KMF

### Employees Assigned

```dataview
TABLE status
```

### Open Issues
```dataviewjs
dv.table([])
```

## Operational Notes
- keep this

### Recent Visits
```Dataview
LIST FROM #visits
```
"""

    stripped = site_detail._strip_dataview_blocks(body)

    assert "Employees Assigned" not in stripped
    assert "Open Issues" not in stripped
    assert "Recent Visits" not in stripped
    assert "dataview" not in stripped.lower()
    assert "## Operational Notes\n- keep this" in stripped


def test_site_detail_strip_dataview_leaves_other_code_fences() -> None:
    body = "## Notes\n\n```bash\necho ok\n```\n"

    assert site_detail._strip_dataview_blocks(body) == body


def test_site_detail_strip_dataview_no_strip_when_heading_unrelated() -> None:
    body = "### Random Heading\n```dataview\nTABLE file.name\n```\nAfter\n"

    stripped = site_detail._strip_dataview_blocks(body)

    assert "### Random Heading" in stripped
    assert "dataview" not in stripped
    assert "After" in stripped


def test_site_detail_render_renders_grouped_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        location_doc(
            customer_name="Jane Customer",
            service_days="Mon-Fri",
            billing_monthly="$1000",
            supply_budget_type="fixed",
        ),
    )

    headings = [
        "<h3>Identity</h3>",
        "<h3>Contact</h3>",
        "<h3>Schedule</h3>",
        "<h3>Billing &amp; Wages</h3>",
        "<h3>Supply Budget</h3>",
    ]
    assert all(heading in html for heading in headings)
    assert [html.index(heading) for heading in headings] == sorted(html.index(heading) for heading in headings)


def test_site_detail_structured_contacts_panel_shows_site_escalation_and_access_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        location_doc(
            site_contacts=[site_contact()],
            access_note="Use the rear staff entrance after 7 PM.",
            customer_name="Legacy Person",
            customer_phone="555-0111",
        ),
        account_doc={
            "_id": "account_kmf_industries",
            "type": "account",
            "account_contacts": [account_contact()],
        },
    )

    assert '<section class="contacts-panel">' in html
    assert "<h3>Site contact</h3>" in html
    assert "Jackie Synthetic" in html
    assert 'href="tel:7246995846"' in html
    assert "<h3>Account escalation</h3>" in html
    assert "Jeremy Fabian" in html
    assert 'href="tel:7249775591"' in html
    assert "<h3>Access note</h3>" in html
    assert "Use the rear staff entrance after 7 PM." in html
    assert "<h3>Legacy primary contact</h3>" in html
    assert "Legacy Person" in html


def test_site_detail_no_structured_contacts_keeps_legacy_contact_section_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    doc = location_doc(customer_name="Jane Customer", customer_phone="555-1234")
    html = render_with_doc(monkeypatch, tmp_path, doc)

    expected = (
        '<section><h3>Contact</h3><p class="actions"><a class="button" href="?edit=contact">Edit</a></p>'
        '<dl class="fields summary-fields">'
        '<div class="field-row"><dt>Customer Name</dt><dd>Jane Customer</dd></div>'
        '<div class="field-row"><dt>Customer Phone</dt><dd>555-1234</dd></div>'
        "</dl></section>"
    )
    assert expected in html
    assert '<section class="contacts-panel">' not in html


def test_site_detail_phn_592_shape_shows_jackie_and_jeremy_without_legacy_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        location_doc(
            account="PHN",
            site_id="592",
            site_contacts=[site_contact(name="Jackie", phone="724-699-5846")],
            customer_name="Jeremy Fabian",
            customer_phone="724-977-5591",
        ),
        account_doc={
            "_id": "account_phn",
            "type": "account",
            "account_contacts": [account_contact(name="Jeremy Fabian", phone="(724) 977-5591")],
        },
    )

    assert "Jackie" in html
    assert "Jeremy Fabian" in html
    assert "<h3>Account escalation</h3>" in html
    assert "Legacy primary contact" not in html
    assert html.index("Jackie") < html.index("Jeremy Fabian")


def test_site_detail_render_omits_empty_groups(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc())

    assert "<h3>Identity</h3>" in html
    assert "<h3>Contact</h3>" not in html


def test_site_detail_summary_omits_empty_list_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        location_doc(customer_name=[], customer_phone=""),
    )

    assert "customer_name" not in html
    assert "customer_phone" not in html
    assert "[]" not in html


def test_site_detail_summary_omits_string_brackets_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(floorwork="[]"))

    assert "floorwork" not in html
    assert "<dt>Floorwork</dt>" not in html
    assert "[]" not in html


def test_site_detail_summary_humanizes_field_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(customer_phone="555-1234"))

    assert "<dt>Customer Phone</dt>" in html
    assert "<dt>customer_phone</dt>" not in html


def test_site_detail_summary_field_row_wrapper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(customer_phone="555-1234"))

    assert '<div class="field-row"><dt>Customer Phone</dt><dd>555-1234</dd></div>' in html


def test_site_detail_summary_formats_list_value_as_comma_join(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(
        monkeypatch,
        tmp_path,
        location_doc(service_days=["Mon", "Wed", "Fri"]),
    )

    assert "Mon, Wed, Fri" in html
    assert "['Mon', 'Wed', 'Fri']" not in html


# The redesign moved value formatting into the shared common.field_value helper
# (record_section uses it). The quote-stripping / escaping / list-join behavior
# is unchanged — these tests now pin it at its new home.
def test_format_value_strips_double_quotes() -> None:
    assert common.field_value('"19:01"') == "19:01"


def test_format_value_strips_single_quotes() -> None:
    assert common.field_value("'hello'") == "hello"


def test_format_value_leaves_embedded_quotes_alone() -> None:
    assert common.field_value('say "hi" please') == "say &quot;hi&quot; please"


def test_format_value_leaves_mismatched_bookends_alone() -> None:
    assert common.field_value('"abc\'') == "&quot;abc&#x27;"


def test_format_value_leaves_short_strings_alone() -> None:
    assert common.field_value('"') == "&quot;"


def test_format_value_list_branch_unchanged() -> None:
    assert common.field_value(["a", "b"]) == "a, b"


def test_site_detail_render_includes_capture_provenance_when_lists_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(btq_job_ids=["a", "b"]))

    assert "<details><summary>Capture provenance</summary>" in html
    assert "btq_job_ids: a, b" in html


def test_site_detail_render_includes_about_section_when_content_nonempty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(content="## Operational Notes\n- foo"))

    # The redesign moved About into a collapsed <details> (collapsed != removed):
    # the full markdown content is still present in the HTML.
    assert '<details class="detail-block">' in html
    assert "<h2>About &amp; operational notes</h2>" in html
    assert "<h2>Operational Notes</h2>" in html
    assert "<li>foo</li>" in html


def test_site_detail_render_renders_captures_gallery_linking_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The redesign replaced the inline photos panel + filter form with a field-
    # captures gallery: each card links OUT to the capture detail and the section
    # links to /field-photos for the full set (no inline capture body).
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: location_doc())
    monkeypatch.setattr(site_detail, "_related_data", lambda site_id: _empty_related())
    monkeypatch.setattr(site_detail, "_related_sections", lambda data: [])
    monkeypatch.setattr(site_detail, "submitters_by_capture", lambda runtime_root: {})
    capture = {
        "capture_id": "cap-123",
        "area_guess": "Lobby",
        "captured_at": "2026-06-10T10:00:00",
        "status": "processed",
        "image_media_url": "https://media.example/x.jpg",
    }
    monkeypatch.setattr(site_detail, "_site_capture_records", lambda ctx, site_id: ([capture], False, 1))

    html = site_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime", query={}), "7040")

    assert '<section class="site-captures">' in html
    assert "Field captures &middot; 1" in html
    # The captures section links out to the field-photos page and each card links
    # to the capture detail — provenance lives there, not inline.
    assert "/field-photos?site_id=7040" in html
    assert 'href="/captures?capture_id=cap-123"' in html
    assert "site-gallery-card" in html


def test_load_location_returns_none_on_404_or_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(site_detail.urlrequest, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("missing")))

    assert site_detail._load_location("nonexistent") is None


def test_site_detail_route_dispatches_to_site_detail_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(site_detail, "render", lambda ctx, site_id: f"<html>detail {site_id}</html>")

    status, _content_type, body, _headers = route_response_with_headers("GET", "/sites/7040", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert "detail 7040" in body.decode("utf-8")


def test_sites_admin_route_still_handles_exact_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sites, "render", lambda ctx: f"<html>admin {ctx.route_path}</html>")
    monkeypatch.setattr(site_detail, "render", lambda ctx, site_id: pytest.fail("site_detail should not handle exact admin routes"))

    status_sites, _content_type, body_sites, _headers = route_response_with_headers("GET", "/sites", tmp_path / "runtime")
    status_new, _content_type, body_new, _headers = route_response_with_headers("GET", "/sites/new", tmp_path / "runtime")

    assert status_sites == HTTPStatus.OK
    assert "admin /sites" in body_sites.decode("utf-8")
    assert status_new == HTTPStatus.OK
    assert "admin /sites/new" in body_new.decode("utf-8")


def test_site_detail_route_rejects_paths_with_extra_segments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(site_detail, "render", lambda ctx, site_id: pytest.fail("extra segment should 404"))

    status, _content_type, body, _headers = route_response_with_headers("GET", "/sites/7040/extra", tmp_path / "runtime")

    assert status == HTTPStatus.NOT_FOUND
    assert "Not Found" in body.decode("utf-8")
