from __future__ import annotations

import json
from datetime import date, timedelta
from urllib import error

import pytest

from btq_vault import projector


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_query_view_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": "row-1", "key": ["x"], "value": {"name": "Alpha"}}]

    def fake_urlopen(_req: object, timeout: float) -> FakeResponse:
        assert timeout == 3.0
        return FakeResponse({"rows": rows})

    monkeypatch.setattr(projector.request, "urlopen", fake_urlopen)

    assert projector.query_view("http://couch", {}, "db", "ddoc", "view", timeout=3.0) == rows


def test_query_view_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: object, timeout: float) -> FakeResponse:
        raise error.HTTPError("http://couch/db", 500, "boom", {}, None)

    monkeypatch.setattr(projector.request, "urlopen", fake_urlopen)

    with pytest.raises(projector.ProjectorError):
        projector.query_view("http://couch", {}, "db", "ddoc", "view")


def test_build_site_dashboard_contains_location_names(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[
            location_row("7050", "Summit Wire", "Summitsteel"),
            location_row("7060", "Continental Metalworks", "Contworks"),
        ],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "Summit Wire" in html
    assert "Continental Metalworks" in html


def test_build_site_dashboard_lists_site_from_opportunity_when_no_location_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        opportunities=[{"id": "opp_1", "key": ["implicit_site", "open", "opp_1"], "value": {"title": "Open item"}}],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "implicit_site" in html


def test_build_site_dashboard_lists_site_from_visit_when_no_location_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        visits=[{"id": "visit_1", "key": ["site_from_visit", "2026-05-25"], "value": {"evidence": "Checked in"}}],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "site_from_visit" in html


def test_build_site_dashboard_groups_by_account(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        by_type=[
            {
                "key": ["opportunity", "opp_a"],
                "value": None,
                "doc": {
                    "type": "opportunity",
                    "_id": "opp_a",
                    "site_id": "100",
                    "location": "Alpha Site",
                    "account": "AlphaAccount",
                },
            },
            {
                "key": ["opportunity", "opp_b"],
                "value": None,
                "doc": {
                    "type": "opportunity",
                    "_id": "opp_b",
                    "site_id": "200",
                    "location": "Beta Site",
                    "account": "BetaAccount",
                },
            },
        ],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "AlphaAccount" in html
    assert "BetaAccount" in html
    assert "Alpha Site" in html
    assert "Beta Site" in html


def test_build_site_dashboard_shows_contact_from_location_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        by_type=[
            {
                "key": ["location", "location_x"],
                "value": None,
                "doc": {
                    "type": "location",
                    "_id": "location_x",
                    "job": "999",
                    "location": "Test Facility",
                    "account": "TestCo",
                    "customer_name": "Jane Doe",
                    "customer_email": "jane@testco.com",
                },
            }
        ],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "Jane Doe" in html
    assert "jane@testco.com" in html


def test_build_site_dashboard_marks_sites_without_recent_visits(monkeypatch: pytest.MonkeyPatch) -> None:
    old_date = (date.today() - timedelta(days=31)).isoformat()
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        visits=[visit_row("7050", old_date, "old visit")],
    )

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "Sites With No Visit In 30 Days" in html
    assert "Summit Wire" in html
    assert "no recent visit" in html


def test_build_employee_dashboard_lists_active_employees(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch, employees_by_status=[employee_status_row("active", "emp_1", "Ada Lovelace")])

    html = projector.build_employee_dashboard("http://couch", {}, "db")

    assert "Ada Lovelace" in html


def test_build_employee_dashboard_recently_hired_in_60_days(monkeypatch: pytest.MonkeyPatch) -> None:
    hire_date = (date.today() - timedelta(days=30)).isoformat()
    install_fake_views(monkeypatch, employees_by_status=[employee_status_row("active", "emp_1", "Ada Lovelace", hire_date=hire_date)])

    html = projector.build_employee_dashboard("http://couch", {}, "db")

    assert "Recently Hired" in html
    assert "Ada Lovelace" in html


def test_build_site_detail_shows_assigned_employees(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        employees_by_site=[employee_site_row("7050", "active", "emp_1", "Ada Lovelace")],
    )

    html = projector.build_site_detail("7050", "http://couch", {}, "db")

    assert "Ada Lovelace" in html


def test_build_site_detail_shows_open_opportunities(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        opportunities=[opportunity_row("7050", "open", "opp_1", "Add weekend coverage")],
    )

    html = projector.build_site_detail("7050", "http://couch", {}, "db")

    assert "Add weekend coverage" in html


def test_build_site_detail_shows_recent_visits(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        visits=[visit_row("7050", date.today().isoformat(), "Checked in")],
    )

    html = projector.build_site_detail("7050", "http://couch", {}, "db")

    assert "Checked in" in html


def test_build_all_pages_returns_expected_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch, locations=[location_row("7050", "Summit Wire", "Summitsteel")])

    pages = projector.build_all_pages("http://couch", {}, "db")

    assert {"employees.html", "sites/7050.html"}.issubset(set(pages))


def test_build_all_pages_omits_index_html(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch)
    pages = projector.build_all_pages("http://localhost:5984", {}, "btq_vault")
    assert "index.html" not in pages


def test_vault_page_nav_home_link_is_root() -> None:
    html = projector._page("T", "H", [])
    nav_section = html.split('<nav')[1].split('</nav>')[0]
    assert 'href="/"' in nav_section
    assert "index.html" not in nav_section


def test_render_value_scalar_returns_escaped_text() -> None:
    assert projector.render_value("hello") == "hello"


def test_render_value_dict_renders_as_dl() -> None:
    html = projector.render_value({"key": "val"})

    assert "<dl>" in html
    assert "<dt>key</dt>" in html


def test_render_value_list_renders_as_ul() -> None:
    html = projector.render_value(["a", "b"])

    assert "<ul>" in html
    assert "<li>a</li>" in html


def test_render_value_nested_dict_in_list() -> None:
    html = projector.render_value([{"k": "v"}])

    assert "<ul>" in html
    assert "<dl>" in html


def test_visit_evidence_dict_not_repr_in_visit_details() -> None:
    row = {
        "key": ["7050", "2026-05-25"],
        "value": {
            "evidence": {"photos": ["p1.jpg"], "note": "clean"},
            "visit_type": "site",
            "visited_by": None,
        },
    }

    html = projector._visit_details([row])

    assert "{'photos'" not in html
    assert "photos" in html


def test_grouped_rows_well_formed_site_unchanged() -> None:
    rows = {"7050": [{"value": {"title": "Open item"}}]}
    locations = [projector._Location("7050", "Summit Wire", "Summitsteel")]

    html = projector._grouped_rows(rows, locations, projector._opportunity_label)

    assert html == "<h3>Summit Wire</h3><ul><li>Open item</li></ul>"


def test_grouped_rows_null_site_renders_unknown_site() -> None:
    rows = {
        None: [
            {
                "key": [None, "2026-05-25"],
                "value": {"visit_type": "site", "visited_by": "Jordan"},
            }
        ]
    }

    html = projector._grouped_rows(rows, [], projector._visit_label)

    assert "<h3>Unknown site</h3>" in html
    assert "<h3></h3>" not in html
    assert "<li>2026-05-25 - site - Jordan</li>" in html


def test_grouped_rows_unmatched_site_id_renders_raw_id() -> None:
    rows = {"site_from_visit": [{"value": {"title": "Open item"}}]}

    html = projector._grouped_rows(rows, [], projector._opportunity_label)

    assert html == "<h3>site_from_visit</h3><ul><li>Open item</li></ul>"


def test_build_entity_detail_renders_type_and_id() -> None:
    html = projector.build_entity_detail({"type": "visit", "_id": "visit_001", "date": "2026-05-25"})

    assert "visit" in html
    assert "visit_001" in html


def test_build_entity_detail_renders_all_fields() -> None:
    html = projector.build_entity_detail({"type": "visit", "_id": "visit_001", "date": "2026-05-25"})

    assert "date" in html
    assert "2026-05-25" in html


def test_build_entity_detail_omits_rev() -> None:
    html = projector.build_entity_detail({"type": "visit", "_id": "v1", "_rev": "1-abc"})

    assert "_rev" not in html


def test_build_entity_detail_structured_field_renders_as_dl() -> None:
    html = projector.build_entity_detail({"type": "visit", "_id": "v1", "evidence": {"photos": ["p.jpg"], "note": "ok"}})

    assert "<dl>" in html


def test_build_all_pages_includes_entity_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        by_type=[
            {
                "key": ["visit", "visit_001"],
                "value": None,
                "doc": {"type": "visit", "_id": "visit_001", "date": "2026-05-25", "site_id": "7050"},
            }
        ],
    )

    pages = projector.build_all_pages("http://couch", {}, "db")

    assert "entity/visit/visit_001.html" in pages


def test_build_all_pages_generates_site_page_for_entity_doc_site_id(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        by_type=[
            {
                "key": ["visit", "visit_001"],
                "value": None,
                "doc": {"type": "visit", "_id": "visit_001", "site_id": "entity_site"},
            }
        ],
    )

    pages = projector.build_all_pages("http://couch", {}, "db")

    assert "sites/entity_site.html" in pages


def test_build_all_pages_generates_site_page_for_employee_site_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        by_type=[
            {
                "key": ["employee", "employee_001"],
                "value": None,
                "doc": {"type": "employee", "_id": "employee_001", "site_ids": ["site_alpha", "site_beta"]},
            }
        ],
    )

    pages = projector.build_all_pages("http://couch", {}, "db")

    assert "sites/site_alpha.html" in pages
    assert "sites/site_beta.html" in pages


def test_build_resolver_maps_doc_ids_to_paths() -> None:
    rows = [{"key": ["visit", "v1"], "value": None, "doc": {"type": "visit", "_id": "v1"}}]

    assert projector.build_resolver(rows) == {"v1": "entity/visit/v1.html"}


def test_build_resolver_skips_rows_without_doc() -> None:
    rows = [{"key": ["visit", "v1"], "value": None}]

    assert projector.build_resolver(rows) == {}


def test_render_ref_known_id_returns_link() -> None:
    html = projector.render_ref("v1", {"v1": "entity/visit/v1.html"}, 2)

    assert '<a href="../../entity/visit/v1.html">v1</a>' in html


def test_render_ref_unknown_id_returns_plain_text() -> None:
    html = projector.render_ref("unknown_id", {}, 2)

    assert html == "unknown_id"
    assert "<a" not in html


def test_entity_detail_site_id_renders_as_link() -> None:
    html = projector.build_entity_detail(
        {"type": "visit", "_id": "v1", "site_id": "7050"},
        resolver={"7050": "sites/7050.html"},
        depth=2,
    )

    assert '<a href="../../sites/7050.html">' in html


def test_build_type_index_contains_entity_links() -> None:
    html = projector.build_type_index("visit", [{"type": "visit", "_id": "v1", "date": "2026-05-25"}])

    assert "v1" in html
    assert "../entity/visit/v1.html" in html


def test_build_type_index_empty_state() -> None:
    html = projector.build_type_index("visit", [])

    assert "empty" in html


def test_employee_type_index_displays_last_first_name() -> None:
    docs = [{"type": "employee", "_id": "employee_smith_jane", "first": "Jane", "last": "Smith", "status": "active"}]

    html = projector.build_employee_type_index(docs)

    assert "Smith, Jane" in html


def test_employee_type_index_sorts_by_last_name() -> None:
    docs = [
        {"type": "employee", "_id": "employee_z", "first": "Zara", "last": "Zola", "status": "active"},
        {"type": "employee", "_id": "employee_a", "first": "Ada", "last": "Abel", "status": "active"},
    ]

    html = projector.build_employee_type_index(docs)

    assert html.index("Abel") < html.index("Zola")


def test_employee_type_index_formats_phone_and_shows_email() -> None:
    docs = [
        {
            "type": "employee",
            "_id": "employee_p",
            "first": "Pat",
            "last": "Lee",
            "phone": "8145550102",
            "email": "pat@example.com",
            "status": "active",
        }
    ]

    html = projector.build_employee_type_index(docs)

    assert "(814) 555-0102" in html
    assert "pat@example.com" in html


def test_employee_type_index_links_primary_site() -> None:
    docs = [{"type": "employee", "_id": "employee_q", "first": "Quinn", "last": "Park", "job": "7050", "status": "active"}]

    html = projector.build_employee_type_index(docs)

    assert "sites/7050.html" in html
    assert "7050" in html


def test_build_all_pages_includes_type_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(
        monkeypatch,
        locations=[location_row("7050", "Summit Wire", "Summitsteel")],
        by_type=[
            {
                "key": ["visit", "visit_001"],
                "value": None,
                "doc": {"type": "visit", "_id": "visit_001", "date": "2026-05-25", "site_id": "7050"},
            }
        ],
    )

    pages = projector.build_all_pages("http://couch", {}, "db")

    assert any(path.startswith("types/") and path.endswith(".html") for path in pages)


def test_page_nav_includes_type_links(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch, locations=[location_row("7050", "Summit Wire", "Summitsteel")])

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert "types/visit.html" in html


def test_page_has_sidebar_nav_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch, locations=[location_row("7050", "Summit Wire", "Summitsteel")])

    html = projector.build_site_dashboard("http://couch", {}, "db")

    assert 'class="vault-shell"' in html
    assert 'class="vault-nav"' in html


def test_page_nav_shows_employee_type_link_not_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_views(monkeypatch, locations=[location_row("7050", "Summit Wire", "Summitsteel")])

    html = projector.build_site_dashboard("http://couch", {}, "db")
    nav_head = html.split("<main", 1)[0]

    # types/employee.html (with detail-page links) is the nav entry; raw dashboard is not
    assert "types/employee.html" in nav_head
    assert "employees.html" not in nav_head


def test_employee_dashboard_name_falls_back_to_person_id(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "id": "employee_alice",
        "key": ["active", "employee_alice"],
        "value": {"name": None, "person_id": "alice", "site_ids": [], "hire_date": None},
    }
    install_fake_views(monkeypatch, employees_by_status=[row])

    html = projector.build_employee_dashboard("http://couch", {}, "db")

    assert "alice" in html


def test_employee_by_site_label_falls_back_to_person_id(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "id": "employee_bob",
        "key": ["site_x", "active", "employee_bob"],
        "value": {"name": None, "person_id": "bob", "site_ids": ["site_x"], "hire_date": None},
    }
    install_fake_views(monkeypatch, employees_by_site=[row])

    html = projector.build_employee_dashboard("http://couch", {}, "db")

    assert "bob" in html


def test_render_markdown_renders_headings_and_lists() -> None:
    html = projector.render_markdown("## Notes\n\n- one\n- two")

    assert '<div class="md">' in html
    assert "<h2>Notes</h2>" in html
    assert "<li>one</li>" in html


def test_render_markdown_empty_returns_empty_string() -> None:
    assert projector.render_markdown("") == ""
    assert projector.render_markdown("   ") == ""
    assert projector.render_markdown(None) == ""


def test_render_markdown_hashtags_become_chips_not_headings() -> None:
    html = projector.render_markdown("#unknown #needs-review")

    assert '<span class="tag">#unknown</span>' in html
    assert '<span class="tag">#needs-review</span>' in html
    assert "<h1>" not in html


def test_render_markdown_preserves_real_headings_with_space() -> None:
    html = projector.render_markdown("# Real Heading")

    assert "<h1>Real Heading</h1>" in html


def test_build_entity_detail_renders_content_as_markdown() -> None:
    html = projector.build_entity_detail(
        {"type": "note", "_id": "note_1", "content": "## Body\n\ntext here"}
    )

    assert "<h2>Body</h2>" in html
    assert "## Body" not in html


def test_build_entity_detail_suppresses_operator_and_null_fields() -> None:
    html = projector.build_entity_detail(
        {"type": "visit", "_id": "v1", "operator": "operator_greg", "last_attempted": None}
    )

    assert "operator_greg" not in html
    assert "last_attempted" not in html


def test_build_entity_detail_groups_scalars_in_summary_grid() -> None:
    html = projector.build_entity_detail({"type": "visit", "_id": "v1", "status": "open"})

    assert '<dl class="fields">' in html
    assert "Summary" in html


def test_build_type_index_renders_table_with_columns() -> None:
    html = projector.build_type_index(
        "visit", [{"type": "visit", "_id": "v1", "status": "open", "date": "2026-05-25"}]
    )

    assert "<table>" in html
    assert "../entity/visit/v1.html" in html
    assert "open" in html
    assert "1 record." in html


def test_string_returns_empty_for_none() -> None:
    assert projector._string(None) == ""


def test_string_returns_empty_for_empty_string() -> None:
    assert projector._string("") == ""


def test_string_strips_whitespace() -> None:
    assert projector._string("  7060  ") == "7060"


def test_string_passes_through_bare_scalar() -> None:
    assert projector._string("7060") == "7060"
    assert projector._string(7060) == "7060"


def test_string_lifts_list_of_one() -> None:
    assert projector._string(["7060"]) == "7060"


def test_string_returns_empty_for_list_of_one_none() -> None:
    assert projector._string([None]) == ""


def test_string_returns_empty_for_empty_list() -> None:
    assert projector._string([]) == ""


def test_string_returns_empty_for_multi_element_list() -> None:
    assert projector._string(["7060", "7050"]) == ""


def test_string_strips_json_quote_wrap() -> None:
    assert projector._string('"7060"') == "7060"


def test_string_handles_outer_whitespace_then_quote_wrap() -> None:
    assert projector._string('  "7060"  ') == "7060"


def test_string_does_not_strip_unbalanced_quote() -> None:
    assert projector._string('"unbalanced') == '"unbalanced'


def test_string_handles_nested_list_of_one_quoted() -> None:
    assert projector._string(['"7060"']) == "7060"


def test_string_dict_falls_through_to_str() -> None:
    text = projector._string({"a": 1})

    assert text
    assert text == "{'a': 1}"


def install_fake_views(
    monkeypatch: pytest.MonkeyPatch,
    *,
    locations: list[dict] | None = None,
    employees_by_site: list[dict] | None = None,
    employees_by_status: list[dict] | None = None,
    visits: list[dict] | None = None,
    opportunities: list[dict] | None = None,
    by_type: list[dict] | None = None,
) -> None:
    view_rows = {
        "locations_all": locations or [],
        "employees_by_site": employees_by_site or [],
        "employees_by_status": employees_by_status or [],
        "visits_by_site_date": visits or [],
        "opportunities_by_site_status": opportunities or [],
        "issues_by_site_status": [],
        "by_type": by_type or [],
    }

    def fake_query_view(
        _base_url: str,
        _auth_headers: dict,
        _database: str,
        _ddoc: str,
        view: str,
        **_kwargs: object,
    ) -> list[dict]:
        return view_rows[view]

    monkeypatch.setattr(projector, "query_view", fake_query_view)


def location_row(site_id: str, name: str, account: str) -> dict:
    return {"id": f"location_{site_id}", "key": [account, name, f"location_{site_id}"], "value": {"site_id": site_id, "account": account}}


def employee_site_row(site_id: str, status: str, doc_id: str, name: str) -> dict:
    return {
        "id": doc_id,
        "key": [site_id, status, doc_id],
        "value": {"name": name, "person_id": doc_id, "hire_date": None},
        "doc": {
            "type": "employee",
            "_id": doc_id,
            "name": name,
            "person_id": doc_id,
            "site_ids": [site_id],
            "status": status,
            "hire_date": None,
        },
    }


def employee_status_row(status: str, doc_id: str, name: str, *, hire_date: str | None = None) -> dict:
    return {"id": doc_id, "key": [status, doc_id], "value": {"name": name, "person_id": doc_id, "site_ids": ["7050"], "hire_date": hire_date}}


def opportunity_row(site_id: str, status: str, doc_id: str, title: str) -> dict:
    return {
        "id": doc_id,
        "key": [site_id, status, doc_id],
        "value": {"title": title},
        "doc": {"type": "opportunity", "_id": doc_id, "title": title, "site_id": site_id, "status": status},
    }


def visit_row(site_id: str, visit_date: str, evidence: str) -> dict:
    return {
        "id": f"visit_{visit_date}",
        "key": [site_id, visit_date],
        "value": {"evidence": evidence, "confidence": "high", "visited_by": "Jordan", "visit_type": "site"},
        "doc": {
            "type": "visit",
            "_id": f"visit_{visit_date}",
            "site_id": site_id,
            "date": visit_date,
            "evidence": evidence,
            "confidence": "high",
            "visited_by": "Jordan",
            "visit_type": "site",
        },
    }
