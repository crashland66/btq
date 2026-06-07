from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def render_with_doc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, doc: dict[str, object]) -> str:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: doc)
    monkeypatch.setattr(site_detail, "_related_sections", lambda site_id: [])
    monkeypatch.setattr(site_detail.field_photos, "render_filter_form", lambda **kwargs: "<form>filter</form>")
    monkeypatch.setattr(site_detail.field_photos, "latest_photo_cards", lambda ctx, limit, *, site_id="": ("", False))
    return site_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime"), "7040")


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


def test_format_value_strips_double_quotes() -> None:
    assert site_detail._format_value('"19:01"') == "19:01"


def test_format_value_strips_single_quotes() -> None:
    assert site_detail._format_value("'hello'") == "hello"


def test_format_value_leaves_embedded_quotes_alone() -> None:
    assert site_detail._format_value('say "hi" please') == "say &quot;hi&quot; please"


def test_format_value_leaves_mismatched_bookends_alone() -> None:
    assert site_detail._format_value('"abc\'') == "&quot;abc&#x27;"


def test_format_value_leaves_short_strings_alone() -> None:
    assert site_detail._format_value('"') == "&quot;"


def test_format_value_list_branch_unchanged() -> None:
    assert site_detail._format_value(["a", "b"]) == "a, b"


def test_site_detail_render_includes_capture_provenance_when_lists_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(btq_job_ids=["a", "b"]))

    assert "<details><summary>Capture provenance</summary>" in html
    assert "btq_job_ids: a, b" in html


def test_site_detail_render_includes_about_section_when_content_nonempty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    html = render_with_doc(monkeypatch, tmp_path, location_doc(content="## Operational Notes\n- foo"))

    assert "<h2>About</h2>" in html
    assert "<h2>Operational Notes</h2>" in html
    assert "<li>foo</li>" in html


def test_site_detail_render_renders_photos_panel_with_filter_form(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(site_detail, "_load_location", lambda site_id: location_doc())
    monkeypatch.setattr(site_detail, "_related_sections", lambda site_id: [])
    monkeypatch.setattr(site_detail.field_photos, "render_filter_form", lambda **kwargs: "<form>sentinel filter</form>")
    monkeypatch.setattr(
        site_detail.field_photos,
        "latest_photo_cards",
        lambda ctx, limit, *, site_id="": ("<div class='card'>photo</div>", False),
    )

    html = site_detail.render(SimpleNamespace(runtime_root=tmp_path / "runtime"), "7040")

    assert "<h3>Photos</h3>" in html
    assert "<form>sentinel filter</form>" in html
    assert "<div class='card'>photo</div>" in html


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
