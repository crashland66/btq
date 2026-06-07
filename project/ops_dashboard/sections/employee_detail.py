from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote
from urllib import parse as urlparse, request as urlrequest

from event_pipeline import couchdb_config
from ops_dashboard.common import first_query_value
from ops_dashboard.layout import html_page
import ops_dashboard.sections.entity_edit as entity_edit
import ops_dashboard.sections.site_detail as site_detail
import ops_dashboard.sections.sites as sites
from btq_vault.projector import render_markdown

_cdb = site_detail._cdb


def _load_vault_doc(doc_id: str) -> dict[str, Any] | None:
    base, headers, database, timeout = _cdb()
    url = f"{base.rstrip('/')}/{urlparse.quote(database, safe='')}/{urlparse.quote(doc_id, safe='')}"
    req = urlrequest.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


_EMPLOYEE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Identity", ("first", "last", "preferred_name", "person_id", "status")),
    ("Assignment", ("job", "additional_jobs", "sites", "role")),
    ("Contact", ("phone", "email")),
)

_EMPLOYEE_SUPPRESSED = frozenset({
    "_id", "_rev", "type", "operator", "vault_path", "content", "name", "site_ids",
})


def _demote_about_headings(rendered_html: str) -> str:
    def replace(match: re.Match[str]) -> str:
        closing, level, attrs = match.groups()
        demoted_level = min(int(level) + 3, 6)
        if closing:
            return f"</h{demoted_level}>"
        return f"<h{demoted_level}{attrs}>"

    return re.sub(r"<(/?)h([1-6])(\b[^>]*)>", replace, rendered_html, flags=re.IGNORECASE)


def _not_found(employee_id: str) -> str:
    body = (
        f'<header><h1>Employee not found</h1>'
        f'<p class="muted">No employee with id {html.escape(employee_id)}.</p>'
        '<p><a href="/">Return to Inbox</a></p></header>'
    )
    return html_page("Not Found — BTQ", body, active_section="employee_detail")


def render(ctx: object, employee_id: str) -> str:
    try:
        edit_section = first_query_value(getattr(ctx, "query", {}), "edit")
        doc = _load_vault_doc(f"employee_{employee_id}")
        if not isinstance(doc, dict) or doc.get("type") != "employee":
            return _not_found(employee_id)

        name = html.escape(str(doc.get("name") or employee_id))
        person_id = html.escape(str(doc.get("person_id") or employee_id))
        eid = html.escape(employee_id, quote=True)

        primary_site = str(doc.get("job") or "")
        primary_btn = (
            f'<a class="button" href="/sites/{html.escape(primary_site, quote=True)}">Primary site</a>'
            if primary_site else ""
        )
        # Static entity pages are keyed by the FULL CouchDB doc _id
        # (projector emits entity/employee/<doc_id>.html), so the vault link must
        # use employee_<id>, not the bare route id. (Unlike /vault/sites/<bare_id>,
        # which has its own dedicated projection.)
        vault_doc_id = html.escape(str(doc.get("_id") or f"employee_{employee_id}"), quote=True)
        vault_btn = f'<a class="button" href="/vault/entity/employee/{vault_doc_id}.html">Vault page</a>'
        all_employees_btn = '<a class="button" href="/employees">All employees</a>'
        header = (
            f'<header><h1>{name} · {person_id}</h1>'
            f'<p class="actions">{all_employees_btn}{primary_btn}{vault_btn}</p></header>'
        )

        sections = [header]

        editable_sections = {
            "Identity": "identity",
            "Assignment": "assignment",
            "Contact": "contact",
        }
        summary_sections = []
        for title, keys in _EMPLOYEE_GROUPS:
            slug = editable_sections[title]
            section_html = entity_edit.render_editable_section(
                title,
                doc,
                keys,
                edit_active=(edit_section == slug),
                save_action=f"/employees/{html.escape(employee_id, quote=True)}/save-section",
                entity_id=employee_id,
            )
            summary_sections.append(section_html)
        sections.append(f"<section><h2>Summary</h2>{''.join(summary_sections)}</section>")

        raw_content = str(doc.get("content") or "")
        if edit_section == "about":
            eid = html.escape(employee_id, quote=True)
            rev = html.escape(str(doc.get("_rev", "")), quote=True)
            sections.append(
                '<section><h3>About</h3>'
                f'<form method="post" action="/employees/{eid}/save-section">'
                f'<input type="hidden" name="_rev" value="{rev}">'
                f'<input type="hidden" name="_entity_id" value="{eid}">'
                '<input type="hidden" name="_section" value="about">'
                f'<textarea name="content">{html.escape(raw_content)}</textarea>'
                '<button type="submit">Save</button>'
                f'<a class="button" href="/employees/{eid}">Cancel</a>'
                '</form></section>'
            )
        elif raw_content.strip():
            sections.append(
                f'<section><h3>About</h3>{_demote_about_headings(render_markdown(raw_content))}</section>'
            )

        site_ids: list[str] = doc.get("site_ids") or []
        if site_ids:
            links = "".join(
                f'<li><a href="/sites/{html.escape(s, quote=True)}">{html.escape(s)}</a></li>'
                for s in site_ids
            )
            sections.append(
                f'<section><h3>Assigned Sites</h3><ul>{links}</ul></section>'
            )

        body = "".join(sections)
        return html_page(f"Employee {employee_id} — BTQ", body, active_section="employee_detail")
    except Exception as exc:  # noqa: BLE001
        body = (
            f'<header><h1>Error loading employee {html.escape(employee_id)}</h1>'
            f'<p class="muted">{html.escape(str(exc))}</p></header>'
        )
        return html_page("Error — BTQ", body, active_section="employee_detail")


def handle_save_section(ctx: object, employee_id: str, body: bytes):
    from urllib.parse import parse_qs, quote
    from ops_dashboard.common import first_query_value
    import ops_dashboard.sections.entity_edit as entity_edit

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    form_flat = {k: v[0] for k, v in form.items()}
    section = first_query_value(form, "_section").strip()

    _ALLOWED_KEYS: dict[str, frozenset[str]] = {
        "identity": frozenset(("first", "last", "preferred_name", "person_id", "status")),
        "assignment": frozenset(("job", "additional_jobs", "sites", "role")),
        "contact": frozenset(("phone", "email")),
        "about": frozenset(("content",)),
    }
    allowed_keys = _ALLOWED_KEYS.get(section)
    if allowed_keys is None:
        return 400, "text/plain; charset=utf-8", b"Unknown section", {}

    existing = _load_vault_doc(f"employee_{employee_id}")
    if not existing or existing.get("type") != "employee":
        return ctx.redirect(f"/employees/{quote(employee_id)}?error=not_found")

    updated = entity_edit.apply_section_update(existing, form_flat, allowed_keys)

    # ALWAYS recompute derived fields after any employee section save.
    # A stale name/site_ids breaks the home directory and employees_by_site view.
    entity_edit.recompute_employee_derived(updated)

    # Assert validate_doc_update contract (btq_vault requires type + non-empty operator)
    assert updated.get("type") == "employee"
    assert updated.get("operator")

    updated["_rev"] = existing["_rev"]

    vault_path = f"{couchdb_config.vault_database()}/employee_{employee_id}"
    try:
        sites.request_json("PUT", vault_path, updated)
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            {"section": section},
            f"success: updated section={section}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}")
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == 409:
            ctx.audit(
                f"/employees/{employee_id}/save-section",
                {"section": section},
                "failed: conflict",
            )
            return (
                200, "text/html; charset=utf-8",
                render(ctx, employee_id).encode("utf-8"),
                {},
            )
        ctx.audit(
            f"/employees/{employee_id}/save-section",
            {"section": section},
            f"failed: {exc}",
        )
        return ctx.redirect(f"/employees/{quote(employee_id)}?error={quote(str(exc))}")
