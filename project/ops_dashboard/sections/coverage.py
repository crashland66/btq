"""Potential-coverage lookup: active employees ranked by approximate
home-ZIP distance to a target site.

Decision support only. Proximity is computed locally from the bundled Census
ZIP-centroid table — no third-party geocoding — and the page never renders an
employee's address or ZIP, only the approximate distance. There is no action
here that assigns, schedules, or contacts anyone.
"""

from __future__ import annotations

import html
from typing import Any

import coverage_geo
from event_pipeline import couchdb_config
from ops_dashboard.layout import html_page
import ops_dashboard.sections.employee_detail as employee_detail
import ops_dashboard.sections.site_detail as site_detail
import ops_dashboard.sections.sites as sites

_EMPLOYEE_LIMIT = 5000


def _load_location(site_id: str) -> dict[str, Any] | None:
    try:
        doc = site_detail._load_location(site_id)  # noqa: SLF001 - same trust as the site page.
        if not isinstance(doc, dict) and site_id == "SANDBOX":
            doc = site_detail._builtin_location_doc(site_id)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - unresolved sites degrade to not-found.
        doc = None
    return doc if isinstance(doc, dict) else None


def _employee_docs() -> list[dict[str, Any]]:
    try:
        return employee_detail._mango_find(  # noqa: SLF001 - shared dashboard query helper.
            couchdb_config.vault_database(),
            {"type": "employee"},
            limit=_EMPLOYEE_LIMIT,
        )
    except Exception:  # noqa: BLE001 - a query failure renders as zero candidates, not a 500.
        return []


def _clean(value: object) -> str:
    return str(value or "").strip()


def _scalar(value: object) -> str:
    """A displayable scalar: YAML-era empty lists render as nothing, real
    lists join with commas."""
    if isinstance(value, (list, tuple)):
        return ", ".join(_clean(item) for item in value if _clean(item))
    return _clean(value)


def _bare_employee_id(doc: dict[str, Any]) -> str:
    return _clean(doc.get("_id")).removeprefix("employee_")


def _display_name(doc: dict[str, Any]) -> str:
    return _clean(doc.get("name")) or _bare_employee_id(doc)


def _distance_label(miles: float) -> str:
    return f"~{miles:.1f} mi" if miles < 10 else f"~{miles:.0f} mi"


def _site_names(site_ids: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for site_id in site_ids:
        if site_id in names:
            continue
        doc = _load_location(site_id)
        names[site_id] = (sites.canonical_name(doc) if doc else "") or site_id
    return names


def rank_candidates(
    employees: list[dict[str, Any]],
    site_centroid: tuple[float, float],
) -> tuple[list[tuple[float, dict[str, Any]]], int]:
    """Active employees with a usable home ZIP, closest first.

    Returns (ranked [(miles, doc)], count of active employees excluded for
    having no usable home location). Inactive/separated employees are never
    candidates.
    """
    ranked: list[tuple[float, dict[str, Any]]] = []
    unlocatable = 0
    for doc in employees:
        if _clean(doc.get("status")).lower() != "active":
            continue
        address = doc.get("home_address") if isinstance(doc.get("home_address"), dict) else {}
        centroid = coverage_geo.zip_centroid(address.get("postal_code"))
        if centroid is None:
            unlocatable += 1
            continue
        ranked.append((coverage_geo.haversine_miles(centroid, site_centroid), doc))
    ranked.sort(key=lambda item: (item[0], _display_name(item[1]).lower()))
    return ranked, unlocatable


_WARNING_HTML = (
    '<section class="coverage-warning"><p>'
    "<strong>Potential coverage only.</strong> Proximity is not availability, "
    "willingness, transportation, site access, training, or approval. Review "
    "each person&#x27;s record and make any contact yourself &mdash; nothing "
    "on this page schedules, assigns, or messages anyone."
    "</p></section>"
)


def _candidate_rows(ranked: list[tuple[float, dict[str, Any]]], target_site_id: str) -> str:
    all_site_ids: list[str] = []
    for _miles, doc in ranked:
        all_site_ids.extend(_clean(value) for value in (doc.get("site_ids") or []) if _clean(value))
    site_names = _site_names(all_site_ids)

    rows = []
    for miles, doc in ranked:
        bare = _bare_employee_id(doc)
        name_link = (
            f'<a href="/employees/{html.escape(bare, quote=True)}">{html.escape(_display_name(doc))}</a>'
        )
        site_ids = [_clean(value) for value in (doc.get("site_ids") or []) if _clean(value)]
        assigned = ", ".join(
            html.escape(site_names.get(site_id, site_id))
            + (" (this site)" if site_id == target_site_id else "")
            for site_id in site_ids
        ) or "&mdash;"
        role = html.escape(_scalar(doc.get("role"))) or "&mdash;"
        shift = html.escape(_scalar(doc.get("shift"))) or "&mdash;"
        rows.append(
            "<tr>"
            f"<td>{name_link}</td>"
            f"<td>{html.escape(_distance_label(miles))}</td>"
            f"<td>{role}</td>"
            f"<td>{assigned}</td>"
            f"<td>{shift}</td>"
            "</tr>"
        )
    return "".join(rows)


def render(ctx: object, site_id: str) -> str:
    doc = _load_location(site_id)
    if doc is None:
        body = (
            '<header><h1>Site not found</h1>'
            f'<p class="muted">No site with id {html.escape(site_id)}.</p>'
            '<p><a href="/sites">All sites</a></p></header>'
        )
        return html_page("Not Found — BTQ", body, active_section="site_detail")

    site_name = sites.canonical_name(doc) or site_id
    escaped_id = html.escape(site_id, quote=True)
    header = (
        '<header class="site-detail-header">'
        "<div>"
        f"<h1>Potential coverage near {html.escape(site_name)}</h1>"
        '<p class="subline">Ranked by approximate home-ZIP distance, computed locally. '
        "Addresses are never shown here.</p>"
        "</div>"
        '<p class="actions site-header-actions">'
        f'<a class="button" href="/sites/{escaped_id}">Back to site</a>'
        "</p></header>"
    )

    site_centroid = coverage_geo.zip_centroid(coverage_geo.site_postal_code(doc))
    if site_centroid is None:
        body = header + _WARNING_HTML + (
            '<section><p class="zero-state">This site has no usable postal code in its address, '
            "so distances cannot be estimated. Fix the site address to enable this view.</p></section>"
        )
        return html_page(f"Coverage — {site_name} — BTQ", body, active_section="site_detail")

    ranked, unlocatable = rank_candidates(_employee_docs(), site_centroid)
    if ranked:
        table = (
            '<section><table class="data-table">'
            "<thead><tr><th>Employee</th><th>Approx. distance</th><th>Role</th>"
            "<th>Assigned sites</th><th>Shift</th></tr></thead>"
            f"<tbody>{_candidate_rows(ranked, site_id)}</tbody>"
            "</table></section>"
        )
    else:
        table = (
            '<section><p class="zero-state">No active employees have a usable home location yet. '
            "Add home addresses from the employee pages to populate this view.</p></section>"
        )
    footnote = ""
    if unlocatable:
        footnote = (
            f'<p class="muted">{unlocatable} active employee'
            f'{"s" if unlocatable != 1 else ""} not shown &mdash; no home address with a known ZIP on file.</p>'
        )

    body = header + _WARNING_HTML + table + footnote
    return html_page(f"Coverage — {site_name} — BTQ", body, active_section="site_detail")
