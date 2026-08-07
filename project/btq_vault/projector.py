from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable
from urllib import error, parse, request

import markdown

from btq_vault.contacts import (
    ACCOUNT_ESCALATION_ROLE,
    PRIMARY_SITE_CONTACT_ROLES,
    contacts_from_doc,
    first_contact_by_role_priority,
    first_contact_with_role,
)
from processing_core.slugs import lower_underscore_slug


DDOC = "btq_vault"
BROWSE_TYPES: list[str] = [
    "account",
    "location",
    "employee",
    "visit",
    "visit_gap",
    "opportunity",
    "site_issue",
    "supply_need",
    "equipment_request",
    "personnel_event",
    "shift_report",
    "journal",
    "plan",
    "monthly_summary",
    "inventory",
    "note",
    "unknown_capture",
    "operator",
]

# Fields never shown on entity detail pages: revision metadata and the
# always-identical operator id add noise without informing the reader.
# Employee home addresses are sensitive PII: they live only on the
# authorized operator surface, never in static projections.
SUPPRESSED_DETAIL_FIELDS: set[str] = {"_rev", "operator", "home_address", "home_address_source"}

# Preferred ordering for the scalar "Summary" grid on entity detail pages.
# Any field not listed here follows in alphabetical order.
SUMMARY_FIELD_ORDER: list[str] = [
    "type",
    "_id",
    "name",
    "title",
    "status",
    "confidence",
    "date",
    "timestamp",
    "hire_date",
    "site_id",
    "visited_by",
    "visit_type",
]

# Human-readable section titles for known long-text / structured fields.
FRIENDLY_FIELD_TITLES: dict[str, str] = {
    "content": "Content",
    "body": "Body",
    "notes": "Notes",
    "evidence": "Evidence",
    "original_transcript": "Original Transcript",
    "normalized_transcript": "Normalized Transcript",
}

# Markdown extensions used when rendering long-text fields.
MARKDOWN_EXTENSIONS: list[str] = ["extra", "sane_lists", "nl2br"]


class ProjectorError(Exception):
    pass


def query_view(
    base_url: str,
    auth_headers: dict,
    database: str,
    ddoc: str,
    view: str,
    *,
    startkey: object | None = None,
    endkey: object | None = None,
    include_docs: bool = False,
    descending: bool = False,
    limit: int | None = None,
    timeout: float = 10.0,
) -> list[dict]:
    """
    Fetch rows from a CouchDB view. Returns the list of row objects.
    Raises ProjectorError on failure.
    """
    params: dict[str, str] = {}
    if startkey is not None:
        params["startkey"] = json.dumps(startkey)
    if endkey is not None:
        params["endkey"] = json.dumps(endkey)
    if include_docs:
        params["include_docs"] = "true"
    if descending:
        params["descending"] = "true"
    if limit is not None:
        params["limit"] = str(limit)

    db = parse.quote(database, safe="")
    design = parse.quote(ddoc, safe="")
    view_name = parse.quote(view, safe="")
    url = f"{base_url.rstrip('/')}/{db}/_design/{design}/_view/{view_name}"
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    headers.update(auth_headers)
    req = request.Request(url, headers=headers, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        raise ProjectorError(f"CouchDB view {ddoc}/{view} failed with HTTP {exc.code}") from exc
    except (error.URLError, OSError) as exc:
        raise ProjectorError(f"CouchDB view {ddoc}/{view} request failed") from exc
    if not 200 <= status < 300:
        raise ProjectorError(f"CouchDB view {ddoc}/{view} returned HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectorError(f"CouchDB view {ddoc}/{view} returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise ProjectorError(f"CouchDB view {ddoc}/{view} returned an invalid rows payload")
    return [row for row in payload["rows"] if isinstance(row, dict)]


def build_site_dashboard(base_url: str, auth_headers: dict, database: str, timeout: float = 10.0) -> str:
    """
    Returns HTML for the main site/account dashboard.
    Covers: all locations; staffing by site; open opportunities by site;
    recent visits by site (last 14 days); sites with no visit in 30 days;
    sites with open opportunities.
    """
    by_type_rows = query_view(base_url, auth_headers, database, DDOC, "by_type", include_docs=True, timeout=timeout)
    site_records = _build_site_records(by_type_rows)
    location_rows = query_view(base_url, auth_headers, database, DDOC, "locations_all", timeout=timeout)
    employee_rows = query_view(base_url, auth_headers, database, DDOC, "employees_by_site", timeout=timeout)
    opportunity_rows = query_view(base_url, auth_headers, database, DDOC, "opportunities_by_site_status", timeout=timeout)
    visit_rows = query_view(base_url, auth_headers, database, DDOC, "visits_by_site_date", timeout=timeout)

    for row in location_rows:
        key = row.get("key") if isinstance(row.get("key"), list) else []
        value = row.get("value") if isinstance(row.get("value"), dict) else {}
        sid = _string(value.get("site_id") or (key[2] if len(key) > 2 else row.get("id")))
        if not sid or sid in site_records:
            continue
        name = _string((key[1] if len(key) > 1 else None) or value.get("name")) or sid
        account = _string(value.get("account") or (key[0] if key else ""))
        site_records[sid] = _SiteRecord(sid, name, account)
    for rows in (employee_rows, opportunity_rows, visit_rows):
        for row in rows:
            sid = _site_id(row)
            if sid and sid not in site_records:
                site_records[sid] = _SiteRecord(sid)

    open_opportunities = _rows_by_site(_status_rows(opportunity_rows, {"open"}))
    recent_cutoff = date.today() - timedelta(days=14)
    inactive_cutoff = date.today() - timedelta(days=30)
    recent_visits = _rows_by_site(row for row in visit_rows if (_row_date(row) or date.min) >= recent_cutoff)
    visited_recently = {_site_id(row) for row in visit_rows if (_row_date(row) or date.min) >= inactive_cutoff}

    by_account: dict[str, list[_SiteRecord]] = defaultdict(list)
    for record in site_records.values():
        by_account[record.account].append(record)

    account_sections = []
    for acct in sorted(by_account, key=lambda a: (a == "", a.lower())):
        sites = sorted(by_account[acct], key=lambda r: (r.name.lower(), r.site_id))
        rows_html = []
        for rec in sites:
            rows_html.append(
                "<tr>"
                f'<td><a href="sites/{_url_path(rec.site_id)}.html">{_esc(rec.name)}</a></td>'
                f"<td>{_esc(rec.site_id)}</td>"
                f"<td>{_esc(rec.contact_name)}</td>"
                f"<td>{_esc(rec.contact_phone)}</td>"
                f"<td>{_esc(rec.contact_email)}</td>"
                "</tr>"
            )
        account_sections.append(
            _section(
                acct or "Other",
                _table(["Site", "ID", "Contact", "Phone", "Email"], rows_html),
            )
        )

    locations_list = [_Location(record.site_id, record.name, record.account) for record in site_records.values()]
    inactive_locations = [loc for loc in locations_list if loc.site_id not in visited_recently]
    sites_with_open_opportunities = [loc for loc in locations_list if open_opportunities.get(loc.site_id)]

    return _page(
        "BTQ Sites",
        "Sites",
        account_sections
        + [
            _section("Sites With No Visit In 30 Days", _location_list(inactive_locations, empty="All sites have a visit in the last 30 days.")),
            _section("Sites With Open Opportunities", _location_list(sites_with_open_opportunities, empty="No sites have open opportunities.")),
            _section("Open Opportunities By Site", _grouped_rows(open_opportunities, locations_list, _opportunity_label)),
            _section("Recent Visits By Site", _grouped_rows(recent_visits, locations_list, _visit_label)),
        ],
    )


def build_employee_dashboard(base_url: str, auth_headers: dict, database: str, timeout: float = 10.0) -> str:
    """
    Returns HTML for the employee dashboard.
    Covers: active employees; inactive/resigned; employees by site; recently hired (60 days).
    """
    status_rows = query_view(base_url, auth_headers, database, DDOC, "employees_by_status", timeout=timeout)
    site_rows = query_view(base_url, auth_headers, database, DDOC, "employees_by_site", timeout=timeout)
    locations = _locations(base_url, auth_headers, database, timeout)

    active = _status_rows(status_rows, {"active"})
    inactive = _status_rows(status_rows, {"inactive", "resigned"})
    recent_cutoff = date.today() - timedelta(days=60)
    recent_hires = [row for row in status_rows if (_value_date(row, "hire_date") or date.min) >= recent_cutoff]
    employees_by_site = _rows_by_site(site_rows)

    return _page(
        "BTQ Employee Dashboard",
        "Employee Dashboard",
        [
            _section("Active Employees", _employee_table(active, include_sites=True)),
            _section("Inactive And Resigned Employees", _employee_table(inactive, include_sites=True)),
            _section("Employees By Site", _grouped_rows(employees_by_site, locations, _employee_label)),
            _section("Recently Hired", _employee_table(recent_hires, include_sites=True)),
        ],
    )


def build_site_detail(site_id: str, base_url: str, auth_headers: dict, database: str, timeout: float = 10.0) -> str:
    """
    Returns HTML for a single site's detail page.
    Covers: employees assigned to the site; open opportunities; recent visits (last 5).
    """
    site_key = str(site_id)
    locations = {loc.site_id: loc for loc in _locations(base_url, auth_headers, database, timeout)}
    location = locations.get(site_key)
    title = location.name if location else f"Site {site_key}"
    employee_rows = [
        row
        for row in query_view(base_url, auth_headers, database, DDOC, "employees_by_site", include_docs=True, timeout=timeout)
        if _site_id(row) == site_key
    ]
    opportunity_rows = [
        row
        for row in query_view(base_url, auth_headers, database, DDOC, "opportunities_by_site_status", include_docs=True, timeout=timeout)
        if _site_id(row) == site_key and _status(row) == "open"
    ]
    visit_rows = [
        row
        for row in query_view(base_url, auth_headers, database, DDOC, "visits_by_site_date", include_docs=True, timeout=timeout)
        if _site_id(row) == site_key
    ]
    recent_visits = sorted(visit_rows, key=lambda row: _row_date(row) or date.min, reverse=True)[:5]

    return _page(
        f"BTQ Site {site_key}",
        title,
        [
            _section("Employees Assigned", _employee_table(employee_rows, include_sites=False)),
            _section("Open Opportunities", _simple_table(["Opportunity"], [_opportunity_label(row) for row in opportunity_rows])),
            _section("Recent Visits", _visit_details(recent_visits)),
        ],
        nav_prefix="../",
    )


def build_resolver(by_type_rows: list[dict]) -> dict[str, str]:
    """
    Build an id -> relative-page-path map from by_type view rows.
    Each row has key=[type, _id]; the page path is entity/<type>/<id>.html.
    Rows without a doc (or without _id) are skipped.
    The returned paths are relative to the projection root (no leading slash).
    """
    resolver: dict[str, str] = {}
    for row in by_type_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict):
            continue
        doc_id = _string(doc.get("_id"))
        if not doc_id:
            continue
        doc_type = _string(doc.get("type", "unknown")) or "unknown"
        resolver[doc_id] = f"entity/{_url_path(doc_type)}/{_url_path(doc_id)}.html"
    return resolver


def render_ref(value: object, resolver: dict[str, str], depth: int) -> str:
    """
    If value is a string that appears as a key in resolver, return an <a> link
    with href adjusted for nav depth. Otherwise delegate to render_value(value).
    depth: number of directory levels below the projection root for the current page.
           entity/<type>/<id>.html is depth=2; types/<type>.html is depth=1.
    """
    if isinstance(value, str) and value and value in resolver:
        prefix = "../" * max(depth, 0)
        return f'<a href="{prefix}{_esc(resolver[value])}">{_esc(value)}</a>'
    return render_value(value)


def build_entity_detail(
    doc: dict,
    *,
    nav_prefix: str = "",
    resolver: dict[str, str] | None = None,
    depth: int = 2,
) -> str:
    """
    Render a generic entity detail page for any btq_vault document.

    Fields are split into three groups, each in its own section:
    - Summary: short scalar fields, in SUMMARY_FIELD_ORDER then alphabetical.
    - Long-text: the markdown body ("content") and any multi-line string
      field, rendered as formatted HTML via render_markdown().
    - Structured: dict / list fields, rendered via render_value().

    Noise fields (SUPPRESSED_DETAIL_FIELDS) and empty / null values are
    omitted. Entity-id references are linked via render_ref().
    """
    doc_type = _string(doc.get("type", "unknown")) or "unknown"
    doc_id = _string(doc.get("_id"))
    active_resolver = resolver or {}

    summary_fields: dict[str, object] = {}
    longtext_fields: dict[str, object] = {}
    structured_fields: dict[str, object] = {}
    for key, value in doc.items():
        if key in SUPPRESSED_DETAIL_FIELDS:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key == "content" or (isinstance(value, str) and "\n" in value):
            longtext_fields[key] = value
        elif isinstance(value, (dict, list)):
            structured_fields[key] = value
        else:
            summary_fields[key] = value

    sections: list[str] = []

    ordered = [key for key in SUMMARY_FIELD_ORDER if key in summary_fields]
    ordered += sorted(key for key in summary_fields if key not in set(ordered))
    if ordered:
        rows = "".join(
            f"<dt>{_esc(key)}</dt>"
            f"<dd>{render_ref(summary_fields[key], active_resolver, depth)}</dd>"
            for key in ordered
        )
        sections.append(_section("Summary", f'<dl class="fields">{rows}</dl>'))

    # "content" first, then the rest alphabetically.
    for key in sorted(longtext_fields, key=lambda item: (item != "content", item)):
        title = FRIENDLY_FIELD_TITLES.get(key, _humanize(key))
        sections.append(_section(title, render_markdown(longtext_fields[key])))

    for key in sorted(structured_fields):
        value = structured_fields[key]
        title = FRIENDLY_FIELD_TITLES.get(key, _humanize(key))
        if key == "site_ids" and isinstance(value, list):
            body = "<ul>" + "".join(
                f"<li>{render_ref(item, active_resolver, depth)}</li>" for item in value
            ) + "</ul>"
        else:
            body = render_value(value)
        sections.append(_section(title, body))

    if not sections:
        sections.append(_section("Fields", '<p class="empty">No fields.</p>'))

    page_title = f"{doc_type}: {doc_id}" if doc_id else doc_type
    heading = _string(doc.get("name")) or _string(doc.get("title")) or doc_id or doc_type
    return _page(page_title, heading, sections, nav_prefix=nav_prefix)


def build_type_index(entity_type: str, docs: list[dict], *, nav_prefix: str = "../") -> str:
    """
    Build an index page for one entity type.
    Lists every document of that type as a link to its entity detail page.
    Path: types/<entity_type>.html (one level below projection root -> nav_prefix="../").
    """
    title = entity_type.replace("_", " ").title()
    if docs:
        rows = []
        for doc in docs:
            doc_id = _string(doc.get("_id"))
            label = _string(doc.get("name")) or _string(doc.get("title")) or doc_id
            href = f"../entity/{_url_path(entity_type)}/{_url_path(doc_id)}.html"
            status = _string(doc.get("status"))
            when = _string(doc.get("date") or doc.get("timestamp") or doc.get("hire_date"))
            rows.append(
                "<tr>"
                f'<td><a href="{_esc(href)}">{_esc(label)}</a></td>'
                f"<td>{_esc(doc_id)}</td>"
                f"<td>{_esc(status)}</td>"
                f"<td>{_esc(when)}</td>"
                "</tr>"
            )
        plural = "record" if len(docs) == 1 else "records"
        count = f'<p class="count">{len(docs)} {plural}.</p>'
        body = count + _table(["Name", "ID", "Status", "Date"], rows)
    else:
        body = f'<p class="empty">No {_esc(entity_type)} records.</p>'
    return _page(title, title, [_section(title, body)], nav_prefix=nav_prefix)


def build_employee_type_index(docs: list[dict], *, nav_prefix: str = "../") -> str:
    """
    Build the types/employee.html index page.
    Columns: Name (Last, First), Phone, Email, Status, Primary Site.
    Sorted alphabetically by last name then first name.
    Each name links to the employee's entity detail page.
    """
    title = "Employee"
    if docs:
        def employee_label(doc: dict) -> tuple[str, tuple[str, str]]:
            first = _string(doc.get("first"))
            last = _string(doc.get("last"))
            doc_id = _string(doc.get("_id"))
            fallback = _string(doc.get("name")) or doc_id
            if first and last:
                return f"{last}, {first}", (last.lower(), first.lower())
            if last:
                return last, (last.lower(), "")
            if first:
                return first, (first.lower(), "")
            return fallback, (fallback.lower(), "")

        rows = []
        for doc in sorted(docs, key=lambda item: employee_label(item)[1]):
            doc_id = _string(doc.get("_id"))
            display_name, _sort_key = employee_label(doc)
            href = f"../entity/employee/{_url_path(doc_id)}.html"
            phone = _format_phone(doc.get("phone"))
            email = _string(doc.get("email"))
            status = _string(doc.get("status"))
            job = _string(doc.get("job"))
            primary_site = (
                f'<a href="{nav_prefix}sites/{_url_path(job)}.html">{_esc(job)}</a>'
                if job
                else ""
            )
            rows.append(
                "<tr>"
                f'<td><a href="{_esc(href)}">{_esc(display_name)}</a></td>'
                f"<td>{_esc(phone)}</td>"
                f"<td>{_esc(email)}</td>"
                f"<td>{_esc(status)}</td>"
                f"<td>{primary_site}</td>"
                "</tr>"
            )
        plural = "record" if len(docs) == 1 else "records"
        count = f'<p class="count">{len(docs)} {plural}.</p>'
        body = count + _table(["Name", "Phone", "Email", "Status", "Primary Site"], rows)
    else:
        body = '<p class="empty">No employee records.</p>'
    return _page(title, title, [_section(title, body)], nav_prefix=nav_prefix)


def build_all_pages(base_url: str, auth_headers: dict, database: str, timeout: float = 10.0) -> dict[str, str]:
    """
    Returns a dict of {relative_path: html_string} for all projection pages:
    - "employees.html" — employee dashboard
    - "sites/<site_id>.html" — one per location in the DB
    - "entity/<type>/<doc_id>.html" — one per entity document
    - "types/<type>.html" — one index per entity type
    - "types/index.html" — entity-type browse page
    """
    pages = {
        "employees.html": build_employee_dashboard(base_url, auth_headers, database, timeout),
    }
    by_type_rows = query_view(base_url, auth_headers, database, DDOC, "by_type", include_docs=True, timeout=timeout)
    locations = _locations(base_url, auth_headers, database, timeout)
    location_map: dict[str, _Location] = {loc.site_id: loc for loc in locations}
    for row in by_type_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict):
            continue
        for site_id in _all_site_ids_from_doc(doc):
            if site_id and site_id not in location_map:
                location_map[site_id] = _Location(site_id, site_id, "")
    all_locations = sorted(
        location_map.values(),
        key=lambda loc: (loc.account.lower(), loc.name.lower(), loc.site_id),
    )
    for loc in all_locations:
        pages[f"sites/{loc.site_id}.html"] = build_site_detail(loc.site_id, base_url, auth_headers, database, timeout)
    resolver = build_resolver(by_type_rows)
    docs_by_type: dict[str, list[dict]] = defaultdict(list)
    for row in by_type_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict):
            continue
        doc_type = _string(doc.get("type", "unknown")) or "unknown"
        doc_id = _string(doc.get("_id"))
        if not doc_id:
            continue
        docs_by_type[doc_type].append(doc)
        pages[f"entity/{_url_path(doc_type)}/{_url_path(doc_id)}.html"] = build_entity_detail(
            doc,
            nav_prefix="../../",
            resolver=resolver,
        )
    type_order = [*BROWSE_TYPES, *(entity_type for entity_type in sorted(docs_by_type) if entity_type not in BROWSE_TYPES)]
    for entity_type in type_order:
        if entity_type == "employee":
            pages[f"types/{_url_path(entity_type)}.html"] = build_employee_type_index(
                docs_by_type.get(entity_type, []), nav_prefix="../"
            )
        else:
            pages[f"types/{_url_path(entity_type)}.html"] = build_type_index(
                entity_type, docs_by_type.get(entity_type, []), nav_prefix="../"
            )
    type_links = "".join(
        f'<li><a href="{_url_path(entity_type)}.html">{_esc(entity_type.replace("_", " ").title())}</a></li>'
        for entity_type in type_order
    )
    pages["types/index.html"] = _page(
        "BTQ Entity Types",
        "Entity Types",
        [_section("Browse Entity Types", f"<ul>{type_links}</ul>")],
        nav_prefix="../",
    )
    return pages


class _Location:
    def __init__(self, site_id: str, name: str, account: str) -> None:
        self.site_id = site_id
        self.name = name
        self.account = account


class _SiteRecord:
    def __init__(
        self,
        site_id: str,
        name: str = "",
        account: str = "",
        contact_name: str = "",
        contact_phone: str = "",
        contact_email: str = "",
        site_contacts: list[dict[str, Any]] | None = None,
        primary_site_contact: dict[str, Any] | None = None,
        account_contacts: list[dict[str, Any]] | None = None,
        account_escalation_contact: dict[str, Any] | None = None,
    ) -> None:
        self.site_id = site_id
        self.name = name or site_id
        self.account = account
        self.contact_name = contact_name
        self.contact_phone = contact_phone
        self.contact_email = contact_email
        self.site_contacts = site_contacts or []
        self.primary_site_contact = primary_site_contact
        self.account_contacts = account_contacts or []
        self.account_escalation_contact = account_escalation_contact


def _locations(base_url: str, auth_headers: dict, database: str, timeout: float) -> list[_Location]:
    rows = query_view(base_url, auth_headers, database, DDOC, "locations_all", timeout=timeout)
    locations: list[_Location] = []
    for row in rows:
        key = row.get("key") if isinstance(row.get("key"), list) else []
        value = row.get("value") if isinstance(row.get("value"), dict) else {}
        site_id = _string(value.get("site_id") or (key[2] if len(key) > 2 else row.get("id")))
        if not site_id:
            continue
        name = _string((key[1] if len(key) > 1 else None) or value.get("name") or site_id)
        account = _string(value.get("account") or (key[0] if key else ""))
        locations.append(_Location(site_id, name, account))
    return sorted(locations, key=lambda loc: (loc.account.lower(), loc.name.lower(), loc.site_id))


def _build_site_records(by_type_rows: list[dict]) -> dict[str, _SiteRecord]:
    records: dict[str, _SiteRecord] = {}
    account_docs = _account_docs_by_id(by_type_rows)

    ordered = sorted(
        (row for row in by_type_rows if isinstance(row.get("doc"), dict)),
        key=lambda row: 0 if row["doc"].get("type") == "location" else 1,
    )
    for row in ordered:
        doc = row["doc"]

        site_id = _string(doc.get("job") or doc.get("site_id"))
        if not site_id:
            continue

        existing = records.get(site_id)
        name = _string(doc.get("location") or doc.get("name"))
        account = _string(doc.get("account"))

        if doc.get("type") == "location":
            site_contacts = contacts_from_doc(doc, "site_contacts")
            account_contacts = contacts_from_doc(_account_doc_for_location(doc, account_docs), "account_contacts")
            records[site_id] = _SiteRecord(
                site_id=site_id,
                name=name or site_id,
                account=account,
                contact_name=_string(doc.get("customer_name")),
                contact_phone=_string(doc.get("customer_phone")),
                contact_email=_string(doc.get("customer_email")),
                site_contacts=site_contacts,
                primary_site_contact=first_contact_by_role_priority(site_contacts, PRIMARY_SITE_CONTACT_ROLES),
                account_contacts=account_contacts,
                account_escalation_contact=first_contact_with_role(account_contacts, ACCOUNT_ESCALATION_ROLE),
            )
        elif existing is None:
            records[site_id] = _SiteRecord(
                site_id=site_id,
                name=name or site_id,
                account=account,
            )
        else:
            if name and (not existing.name or existing.name == existing.site_id):
                existing.name = name
            if account and not existing.account:
                existing.account = account

    return records


def _account_docs_by_id(by_type_rows: list[dict]) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for row in by_type_rows:
        doc = row.get("doc")
        if not isinstance(doc, dict) or doc.get("type") != "account":
            continue
        doc_id = _string(doc.get("_id"))
        if doc_id:
            docs[doc_id] = doc
    return docs


def _account_doc_for_location(doc: dict[str, Any], account_docs: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for doc_id in _account_doc_id_candidates(doc):
        account_doc = account_docs.get(doc_id)
        if account_doc is not None:
            return account_doc
    return None


def _account_doc_id_candidates(doc: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for field in ("account_id", "account_doc_id", "parent_account_id"):
        raw = _string(doc.get(field))
        if raw:
            candidates.append(raw if raw.startswith("account_") else f"account_{lower_underscore_slug(raw, fallback='')}")
    account = _string(doc.get("account"))
    if account:
        candidates.append(account if account.startswith("account_") else f"account_{lower_underscore_slug(account, fallback='')}")
    return [candidate for candidate in dict.fromkeys(candidates) if candidate != "account_"]


def _all_site_ids_from_doc(doc: dict) -> set[str]:
    """Return every site_id referenced by a single entity document."""
    ids: set[str] = set()
    site_id = _string(doc.get("site_id"))
    if site_id:
        ids.add(site_id)
    for site_id in _site_ids_from_value(doc):
        ids.add(site_id)
    return ids


def _augment_locations(locations: list[_Location], *row_sets: list[dict]) -> list[_Location]:
    """
    Return locations plus stub entries for site IDs referenced by view rows.

    Each row's site ID is taken from key[0] via _site_id().
    """
    covered = {loc.site_id for loc in locations}
    extra: dict[str, _Location] = {}
    for rows in row_sets:
        for row in rows:
            site_id = _site_id(row)
            if site_id and site_id not in covered and site_id not in extra:
                extra[site_id] = _Location(site_id, site_id, "")
    result = list(locations) + list(extra.values())
    return sorted(result, key=lambda loc: (loc.account.lower(), loc.name.lower(), loc.site_id))


def _page(
    title: str,
    heading: str,
    sections: list[str],
    *,
    nav_prefix: str = "",
    nav_links: list[tuple[str, str]] | None = None,
) -> str:
    if nav_links is None:
        nav_links = [
            ("Home", "/"),
        ] + [
            (entity_type.replace("_", " ").title(), f"{nav_prefix}types/{_url_path(entity_type)}.html")
            for entity_type in BROWSE_TYPES
        ]
    nav = "".join(f'<a href="{_esc(href)}"><span class="nav-label">{_esc(label)}</span></a>' for label, href in nav_links)
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_PAGE_STYLE}</style>\n"
        "<script>try{if(localStorage.getItem('btq-vault-nav-collapsed')==='1')"
        "{document.documentElement.classList.add('nav-collapsed-pending');}}catch(_){}</script>\n"
        "</head>\n"
        "<body>\n"
        "<div class=\"vault-shell\">\n"
        "<nav class=\"vault-nav\" aria-label=\"Vault sections\">\n"
        "<div class=\"nav-top\">"
        "<button class=\"nav-toggle\" type=\"button\" aria-label=\"Toggle navigation\" aria-expanded=\"true\">☰</button>"
        "<span class=\"nav-brand nav-label\">BTQ Vault</span>"
        "</div>\n"
        f"{nav}\n"
        "</nav>\n"
        "<main class=\"vault-content\">\n"
        f"<h1>{_esc(heading)}</h1>\n"
        + "\n".join(sections)
        + "\n</main>\n"
        "</div>\n"
        "<script>\n"
        "const vs=document.querySelector('.vault-shell');\n"
        "const vt=document.querySelector('.nav-toggle');\n"
        "try{if(document.documentElement.classList.contains('nav-collapsed-pending')){\n"
        "  vs?.classList.add('nav-collapsed');\n"
        "  document.documentElement.classList.remove('nav-collapsed-pending');\n"
        "}}catch(_){}\n"
        "const syncVT=()=>{if(!vt||!vs)return;\n"
        "  vt.setAttribute('aria-expanded',vs.classList.contains('nav-collapsed')?'false':'true');};\n"
        "syncVT();\n"
        "vt?.addEventListener('click',()=>{\n"
        "  vs?.classList.toggle('nav-collapsed');\n"
        "  syncVT();\n"
        "  try{localStorage.setItem('btq-vault-nav-collapsed',\n"
        "    vs?.classList.contains('nav-collapsed')?'1':'0');}catch(_){}\n"
        "});\n"
        "</script>\n"
        "</body>\n</html>\n"
    )


_PAGE_STYLE = (
    ":root{--ink:#1f2933;--muted:#667085;--line:#d8dee4;--bg:#f6f8fa;"
    "--accent:#2563eb;--card:#fff}"
    "*{box-sizing:border-box}"
    "body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.55;"
    "margin:0;color:var(--ink);background:var(--bg)}"
    ".vault-shell{display:grid;grid-template-columns:200px minmax(0,1fr);min-height:100vh;"
    "transition:grid-template-columns 120ms ease}"
    "html.nav-collapsed-pending .vault-shell,"
    ".vault-shell.nav-collapsed{grid-template-columns:48px minmax(0,1fr)}"
    ".vault-nav{position:sticky;top:0;height:100vh;overflow-y:auto;overflow-x:hidden;"
    "border-right:1px solid var(--line);background:var(--card);"
    "display:flex;flex-direction:column;padding:.6rem .4rem}"
    ".vault-nav a{display:flex;align-items:center;padding:.45rem .55rem;border-radius:6px;"
    "color:var(--muted);text-decoration:none;font-weight:600;font-size:.83rem;"
    "white-space:nowrap;overflow:hidden}"
    ".vault-nav a:hover{color:var(--ink);background:#eef4ff}"
    ".vault-shell.nav-collapsed .vault-nav a{justify-content:center;padding:.45rem 0}"
    ".vault-shell.nav-collapsed .nav-label,.vault-shell.nav-collapsed .nav-brand{display:none}"
    ".vault-shell.nav-collapsed .nav-top{justify-content:center}"
    ".nav-top{display:flex;align-items:center;gap:.4rem;margin-bottom:.4rem;"
    "padding-bottom:.4rem;border-bottom:1px solid var(--line);overflow:hidden}"
    ".nav-toggle{background:none;border:1px solid var(--line);border-radius:6px;"
    "cursor:pointer;font-size:1rem;padding:.15rem .3rem;color:var(--ink);flex-shrink:0}"
    ".nav-toggle:hover{background:var(--bg)}"
    ".nav-brand{font-weight:700;font-size:.82rem;overflow:hidden;white-space:nowrap}"
    ".nav-label{overflow:hidden;white-space:nowrap}"
    ".vault-content{padding:1.5rem 1.5rem 4rem;min-width:0}"
    "h1{font-size:1.6rem;margin:.25rem 0 1.25rem}"
    "h2{font-size:1.05rem;margin:0 0 .75rem;padding-bottom:.4rem;border-bottom:1px solid var(--line)}"
    "h3{font-size:.95rem;margin:1rem 0 .4rem}"
    "section{background:var(--card);border:1px solid var(--line);border-radius:8px;"
    "padding:1rem 1.15rem;margin:0 0 1.1rem}"
    "a{color:var(--accent)}"
    "table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.25rem 0}"
    "th,td{border:1px solid var(--line);padding:.45rem .6rem;text-align:left;vertical-align:top}"
    "th{background:var(--bg);font-weight:600}"
    "tbody tr:nth-child(even){background:#fafbfc}"
    "ul{margin:.3rem 0;padding-left:1.3rem}li{margin:.15rem 0}"
    ".empty{color:var(--muted);font-style:italic;margin:.2rem 0}"
    ".count{color:var(--muted);font-size:.85rem;margin:0 0 .55rem}"
    ".badge{font-weight:600}"
    "dl{margin:.25rem 0}dt{font-weight:600;margin-top:.25rem}dd{margin:0 0 .35rem 1rem}"
    "dl.fields{display:grid;grid-template-columns:minmax(8rem,max-content) 1fr;"
    "gap:.4rem 1.1rem;margin:0}"
    "dl.fields dt{margin:0;font-size:.85rem;color:var(--muted);font-weight:600}"
    "dl.fields dd{margin:0;word-break:break-word}"
    ".md{font-size:.95rem}"
    ".md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}"
    ".md h1,.md h2,.md h3,.md h4{border:0;padding:0;margin:1rem 0 .4rem;font-size:1rem}"
    ".md h1{font-size:1.15rem}.md h2{font-size:1.05rem}"
    ".md p{margin:.5rem 0}"
    ".md ul,.md ol{margin:.4rem 0;padding-left:1.4rem}"
    ".md code{background:var(--bg);padding:.1rem .3rem;border-radius:4px;font-size:.85em}"
    ".md pre{background:var(--bg);padding:.75rem;border-radius:6px;overflow:auto}"
    ".md pre code{background:none;padding:0}"
    ".md blockquote{margin:.5rem 0;padding-left:.8rem;border-left:3px solid var(--line);"
    "color:var(--muted)}"
    ".md .tag{display:inline-block;font-size:.78rem;font-weight:600;color:#3730a3;"
    "background:#eef2ff;border:1px solid #c7d2fe;border-radius:999px;"
    "padding:.05rem .5rem;margin:.06rem .15rem .06rem 0}"
    ".evidence{margin:.5rem 0;padding:.6rem .75rem;background:var(--bg);border-radius:6px}"
    "details{margin:.35rem 0}summary{cursor:pointer;font-weight:600}"
)


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"


def _table(headers: list[str], rows: list[str]) -> str:
    if not rows:
        return "<p class=\"empty\">No records.</p>"
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _simple_table(headers: list[str], values: list[str]) -> str:
    rows = [f"<tr><td>{_esc(value)}</td></tr>" for value in values]
    return _table(headers, rows)


def _employee_table(rows: list[dict], *, include_sites: bool, name_href: Callable[[dict], str] | None = None) -> str:
    """Employee roster table. name_href, when given, receives the view row and
    returns an href for the name cell (empty string = plain text) — the
    dashboard links names to live employee records while static projections
    keep their own link scheme."""
    headers = ["Name", "Person ID", "Status", "Hire Date"]
    if include_sites:
        headers.append("Site IDs")
    table_rows = []
    for row in rows:
        value = _doc_or_value(row)
        name_text = _string(value.get("name") or value.get("person_id") or row.get("id"))
        name_cell = _esc(name_text)
        if name_href is not None:
            href = name_href(row)
            if href:
                name_cell = f'<a href="{_esc(href)}">{_esc(name_text)}</a>'
        cells = [
            name_cell,
            _string(value.get("person_id")),
            _status(row),
            _string(value.get("hire_date")),
        ]
        if include_sites:
            site_ids = _site_ids_from_value(value)
            if site_ids:
                cells.append(", ".join(f'<a href="sites/{_url_path(site_id)}.html">{_esc(site_id)}</a>' for site_id in site_ids))
            else:
                cells.append("")
        # Name (cells[0]) is pre-rendered HTML; the trailing site-links cell is
        # raw HTML only when include_sites; everything between is escaped.
        rendered = [f"<td>{cells[0]}</td>"]
        rendered.extend(f"<td>{_esc(cell)}</td>" for cell in cells[1:-1])
        rendered.append(f"<td>{cells[-1]}</td>" if include_sites else f"<td>{_esc(cells[-1])}</td>")
        table_rows.append("<tr>" + "".join(rendered) + "</tr>")
    return _table(headers, table_rows)


def _location_list(locations: list[_Location], *, empty: str, site_prefix: str = "sites/") -> str:
    if not locations:
        return f"<p class=\"empty\">{_esc(empty)}</p>"
    items = [
        f"<li><a href=\"{site_prefix}{_url_path(loc.site_id)}.html\">{_esc(loc.name)}</a> "
        f"<span class=\"badge\">no recent visit</span></li>"
        for loc in locations
    ]
    return f"<ul>{''.join(items)}</ul>"


def _site_header(site_id: str | None, names: dict[str, str]) -> str:
    if site_id is None or site_id == "":
        return "Unknown site"
    return names.get(site_id, site_id)


def _grouped_rows(rows_by_site: dict[str, list[dict]], locations: list[_Location], labeler: Any) -> str:
    if not rows_by_site:
        return "<p class=\"empty\">No records.</p>"
    names = {loc.site_id: loc.name for loc in locations}
    parts = []
    for site_id in sorted(rows_by_site, key=lambda item: (_site_header(item, names).lower(), item or "")):
        labels = "".join(f"<li>{_esc(labeler(row))}</li>" for row in rows_by_site[site_id])
        parts.append(f"<h3>{_esc(_site_header(site_id, names))}</h3><ul>{labels}</ul>")
    return "".join(parts)


def _visit_details(rows: list[dict]) -> str:
    if not rows:
        return "<p class=\"empty\">No visits.</p>"
    details = []
    for row in rows:
        value = _doc_or_value(row)
        date_text = _string(_key_part(row, 1))
        evidence = value.get("evidence")
        evidence_html = (
            f"<div class=\"evidence\">{render_value(evidence)}</div>"
            if evidence is not None
            else ""
        )
        details.append(
            "<details>"
            f"<summary>{_esc(date_text or 'Undated visit')}</summary>"
            f"<p>{_esc(_visit_label(row))}</p>"
            f"{evidence_html}"
            "</details>"
        )
    return "".join(details)


def _staffing_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if _status(row) == "active":
            counts[_site_id(row)] += 1
    return dict(counts)


def _status_rows(rows: list[dict], statuses: set[str]) -> list[dict]:
    return [row for row in rows if _status(row) in statuses]


def _rows_by_site(rows: Any) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[_site_id(row)].append(row)
    return dict(grouped)


def _employee_label(row: dict) -> str:
    value = _doc_or_value(row)
    return _string(value.get("name") or value.get("person_id") or row.get("id"))


def _opportunity_label(row: dict) -> str:
    value = _doc_or_value(row)
    return _string(value.get("title") or row.get("id"))


def _visit_label(row: dict) -> str:
    value = _doc_or_value(row)
    parts = [
        _string(_key_part(row, 1)),
        _string(value.get("visit_type")),
        _string(value.get("visited_by")),
    ]
    return " - ".join(part for part in parts if part)


def _status(row: dict) -> str:
    return _string(_key_part(row, 1 if len(row.get("key", [])) >= 3 else 0)).lower()


def _site_id(row: dict) -> str:
    return _string(_key_part(row, 0))


def _row_date(row: dict) -> date | None:
    return _parse_date(_key_part(row, 1))


def _value_date(row: dict, key: str) -> date | None:
    return _parse_date(_value(row).get(key))


def _value(row: dict) -> dict[str, Any]:
    value = row.get("value")
    return value if isinstance(value, dict) else {}


def _doc_or_value(row: dict) -> dict:
    doc = row.get("doc")
    if isinstance(doc, dict):
        return doc
    return _value(row)


def _key_part(row: dict, index: int) -> object:
    key = row.get("key")
    if isinstance(key, list) and len(key) > index:
        return key[index]
    return None


def _site_ids_from_value(value: dict[str, Any]) -> list[str]:
    raw = value.get("site_ids")
    if isinstance(raw, list):
        return [_string(item) for item in raw if _string(item)]
    return []


def _parse_date(value: object) -> date | None:
    text = _string(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def _string(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        if len(value) == 1:
            value = value[0]
            if value is None:
                return ""
        else:
            return ""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def _format_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", _string(raw))
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return _string(raw)


def render_value(value: object) -> str:
    """
    Return an HTML fragment representing value.
    - dict  -> <dl> with one <dt>/<dd> pair per key, values rendered recursively
    - list  -> <ul> with one <li> per item, items rendered recursively
    - other -> _esc(value) (plain escaped text)
    """
    if isinstance(value, dict):
        items = "".join(f"<dt>{_esc(key)}</dt><dd>{render_value(val)}</dd>" for key, val in value.items())
        return f"<dl>{items}</dl>"
    if isinstance(value, list):
        items = "".join(f"<li>{render_value(item)}</li>" for item in value)
        return f"<ul>{items}</ul>"
    return _esc(_string(value))


# Obsidian-style hashtag: a "#" followed by a letter, not preceded by a word
# character (so URL fragments like "page#frag" are left alone). Vault notes use
# these as tags; without this they would be mis-parsed as markdown headings.
_TAG_RE = re.compile(r"(?<![\w/&])#([A-Za-z][\w/-]*)")


def _linkify_tags(text: str) -> str:
    """Wrap Obsidian-style #hashtags in <span class="tag"> chips."""
    return _TAG_RE.sub(lambda m: f'<span class="tag">#{m.group(1)}</span>', text)


def render_markdown(text: object) -> str:
    """
    Render a markdown string to an HTML fragment wrapped in <div class="md">.
    Obsidian-style #hashtags are rendered as tag chips rather than headings.
    Empty / blank input returns an empty string.
    """
    raw = _string(text)
    if not raw:
        return ""
    prepared = _linkify_tags(raw)
    rendered = markdown.markdown(prepared, extensions=MARKDOWN_EXTENSIONS, output_format="html")
    return f'<div class="md">{rendered}</div>'


def _humanize(key: str) -> str:
    """Turn a snake_case field key into a Title Case label."""
    text = _string(key).replace("_", " ").strip()
    return text.title() if text else _string(key)


def _url_path(value: str) -> str:
    return parse.quote(value, safe="")


def _esc(value: object) -> str:
    return html.escape(_string(value), quote=True)
