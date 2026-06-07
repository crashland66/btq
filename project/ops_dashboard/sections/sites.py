from __future__ import annotations

import html
import json
import os
import re
from typing import Any
from urllib import error, parse, request
from urllib.parse import parse_qs, quote, urlencode

from event_pipeline import couchdb_config
from ops_dashboard.common import first_query_value, humanize_key, parse_display_categories_rows, render_display_categories_editor, render_table
from ops_dashboard.layout import html_page
from site_visits import discover_site_visits


SITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def couchdb_base_url() -> str:
    raw = os.environ.get("BTQ_COUCHDB_URL")
    if not raw:
        raise couchdb_config.CouchDBConfigError("BTQ_COUCHDB_URL must be set to edit sites")
    return raw.rstrip("/")


def auth_headers() -> dict[str, str]:
    return dict(couchdb_config.from_env().auth_header())


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json"}
    headers.update(auth_headers())
    body = None
    if payload is not None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(f"{couchdb_base_url()}/{path.lstrip('/')}", data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=couchdb_config.timeout()) as response:
            status = int(getattr(response, "status", getattr(response, "code", 200)))
            raw = response.read()
    except error.HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return 404, None
        raise
    if not raw:
        return status, None
    return status, json.loads(raw.decode("utf-8"))


def doc_path(site_id: str) -> str:
    return f"{parse.quote(couchdb_config.sites_database(), safe='')}/{parse.quote(f'site_{site_id}', safe='')}"


def all_sites_path() -> str:
    query = urlencode({"include_docs": "true", "startkey": json.dumps("site_"), "endkey": json.dumps("site_\ufff0")})
    return f"{parse.quote(couchdb_config.sites_database(), safe='')}/_all_docs?{query}"


def canonical_name(doc: dict[str, Any]) -> str:
    return str(doc.get("canonical_name") or doc.get("canonical") or doc.get("account") or doc.get("location") or "")


def site_id_from_doc(doc: dict[str, Any]) -> str:
    raw = str(doc.get("site_id") or doc.get("_id") or "")
    return raw.removeprefix("site_")


def load_site(site_id: str) -> dict[str, Any] | None:
    _status, payload = request_json("GET", doc_path(site_id))
    return payload


def load_sites() -> list[dict[str, Any]]:
    _status, payload = request_json("GET", all_sites_path())
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    docs = [row.get("doc") for row in rows if isinstance(row, dict) and isinstance(row.get("doc"), dict)]
    return sorted((doc for doc in docs if str(doc.get("_id") or "").startswith("site_")), key=site_id_from_doc)


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    if getattr(ctx, "route_path", "") == "/sites/new" or first_query_value(query, "new") == "1":
        return render_form(ctx, {}, is_new=True, query=query)
    site_id = first_query_value(query, "site_id").strip()
    if site_id:
        doc = load_site(site_id)
        if not doc:
            return html_page("Sites", f"<header><h1>Sites</h1><p>Site not found: {html.escape(site_id)}</p></header>", active_section="sites")
        return render_form(ctx, doc, is_new=False, query=query)
    return render_list(ctx, query)


def render_list(ctx: object, query: dict[str, list[str]]) -> str:
    try:
        docs = load_sites()
        error_html = ""
    except Exception as exc:  # noqa: BLE001
        docs = []
        error_html = f'<section class="error"><p>{html.escape(str(exc))}</p></section>'
    active_filter = first_query_value(query, "active") or "all"
    site_filter = first_query_value(query, "site_id_contains").strip().lower()
    canonical_filter = first_query_value(query, "canonical_contains").strip().lower()
    if active_filter == "active":
        docs = [doc for doc in docs if bool(doc.get("active", True))]
    elif active_filter == "inactive":
        docs = [doc for doc in docs if not bool(doc.get("active", True))]
    if site_filter:
        docs = [doc for doc in docs if site_filter in site_id_from_doc(doc).lower()]
    if canonical_filter:
        docs = [doc for doc in docs if canonical_filter in canonical_name(doc).lower()]
    rows = []
    for doc in docs:
        aliases = doc.get("aliases") if isinstance(doc.get("aliases"), list) else []
        rows.append(
            {
                "site_id": site_id_from_doc(doc),
                "canonical_name": canonical_name(doc),
                "active": bool(doc.get("active", True)),
                "alias_count": len(aliases),
                "note_path": str(doc.get("note_path") or ""),
                "vision_context": str(doc.get("vision_context") or "")[:80],
                "rev": str(doc.get("_rev") or "")[:6],
            }
        )
    table = render_table(
        rows,
        [
            {"key": "site_id", "label": "Site ID", "format": lambda value, _row: f'<a href="/sites?site_id={quote(str(value))}">{html.escape(str(value))}</a>', "nowrap": True},
            {"key": "canonical_name", "label": "Canonical Name"},
            {"key": "active", "label": "Active", "format": lambda value, _row: f'<span class="pill {"success" if bool(value) else "warning"}">{html.escape("Active" if bool(value) else "Inactive")}</span>', "nowrap": True},
            {"key": "alias_count", "label": "Aliases", "priority": 2, "nowrap": True},
            {"key": "note_path", "label": "Note Path", "priority": 3},
            {"key": "vision_context", "label": "Vision Context", "priority": 3},
            {"key": "rev", "label": "Rev", "format": lambda value, _row: f"<code>{html.escape(str(value))}</code>", "priority": 3, "nowrap": True},
        ],
        empty_text="No sites match this filter.",
    )
    filter_html = f"""
      <form method="get" action="/sites" data-submit-on-change>
        <label><input type="radio" name="active" value="all" {'checked' if active_filter == 'all' else ''}> All</label>
        <label><input type="radio" name="active" value="active" {'checked' if active_filter == 'active' else ''}> Active</label>
        <label><input type="radio" name="active" value="inactive" {'checked' if active_filter == 'inactive' else ''}> Inactive</label>
        <label>Site ID contains <input name="site_id_contains" value="{html.escape(first_query_value(query, 'site_id_contains'))}"></label>
        <label>Canonical contains <input name="canonical_contains" value="{html.escape(first_query_value(query, 'canonical_contains'))}"></label>
        <button>Apply</button>
      </form>
    """
    body = f'<header><h1>Sites</h1><p class="muted">Edit one CouchDB site document at a time. Edit CouchDB site documents.</p><p><a class="button" href="/sites/new">New site</a></p></header>{ctx.flash(query)}{error_html}<div class="content-with-rail"><aside class="filter-rail"><section><h2>Filters</h2>{filter_html}</section></aside><section><h2>All sites</h2>{table}</section></div>'
    return html_page("Sites", body, active_section="sites")


def render_form(ctx: object, doc: dict[str, Any], *, is_new: bool, query: dict[str, list[str]] | None = None, notice: str = "") -> str:
    query = query or {}
    site_id = site_id_from_doc(doc)
    aliases = "\n".join(str(item) for item in doc.get("aliases", []) if str(item).strip()) if isinstance(doc.get("aliases"), list) else ""
    action = "/sites/new" if is_new else "/sites/save"
    site_field = f'<input name="site_id" required value="{html.escape(site_id)}">' if is_new else f'<p><code>{html.escape(site_id)}</code></p><input type="hidden" name="site_id" value="{html.escape(site_id)}">'
    notice_html = f'<section class="notice"><p>{html.escape(notice)}</p></section>' if notice else ""
    visits_html = "" if is_new else render_visits_panel(ctx, site_id)
    body = f"""
    <header><h1>{'New Site' if is_new else 'Edit Site'}</h1><p class="muted">Single-document CouchDB write. No delete route.</p></header>
    {ctx.flash(query)}{notice_html}
    <section>
      <form method="post" action="{action}" class="admin-form">
        <label>{humanize_key("canonical_name")} <input name="canonical_name" required value="{html.escape(canonical_name(doc))}"></label>
        <label>{humanize_key("site_id")} {site_field}</label>
        <label><input type="checkbox" name="active" value="1" {'checked' if bool(doc.get('active', True)) else ''}> Active</label>
        <label>{humanize_key("aliases")} <textarea name="aliases">{html.escape(aliases)}</textarea></label>
        <label>{humanize_key("note_path")} <input name="note_path" required value="{html.escape(str(doc.get('note_path') or ''))}"></label>
        <label>{humanize_key("vision_context")} <textarea name="vision_context">{html.escape(str(doc.get('vision_context') or ''))}</textarea></label>
        <label>{humanize_key("capture_guidance")} <textarea name="capture_guidance">{html.escape(str(doc.get('capture_guidance') or ''))}</textarea></label>
        <label>{humanize_key("display_categories")} {render_display_categories_editor("display_categories", doc.get('display_categories') if isinstance(doc.get('display_categories'), list) else [])}</label>
        <input type="hidden" name="_rev" value="{html.escape(str(doc.get('_rev') or ''))}">
        <button>Save site</button>
      </form>
    </section>
    {visits_html}
    """
    return html_page("Sites", body, active_section="sites")


def visit_display_name(visit: dict[str, Any]) -> str:
    first = str(visit.get("visited_by_first_name") or "").strip()
    if first:
        return first
    raw = str(visit.get("visited_by") or "").strip()
    return raw


def render_visits_panel(ctx: object, site_id: str) -> str:
    try:
        config = ctx.config
        result = discover_site_visits(config.vault_dir, site_id=site_id, limit=5)
        visits = result.get("visits") if isinstance(result.get("visits"), list) else []
    except Exception:  # noqa: BLE001
        visits = []

    if not visits:
        return '<section><h2>Recent Visits</h2><p class="muted">No visits recorded yet.</p></section>'

    rows = []
    for visit in visits:
        confidence = str(visit.get("confidence") or "").strip()
        evidence = str(visit.get("evidence") or "").strip()
        date = str(visit.get("date") or "").strip()
        rows.append(
            {
                "date": date,
                "visited_by": visit_display_name(visit),
                "confidence": confidence,
                "evidence": evidence[:120] + ("..." if len(evidence) > 120 else ""),
            }
        )

    table = render_table(
        rows,
        [
            {"key": "date", "label": "Date"},
            {"key": "visited_by", "label": "Visited by"},
            {
                "key": "confidence",
                "label": "Confidence",
                "format": lambda value, _row: f'<span class="pill muted">{html.escape(str(value))}</span>',
            },
            {"key": "evidence", "label": "Evidence", "priority": 2},
        ],
        empty_text="No visits recorded yet.",
    )
    return f"<section><h2>Recent Visits</h2>{table}</section>"


def form_doc(form: dict[str, list[str]], *, existing: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    site_id = first_query_value(form, "site_id").strip()
    canonical = first_query_value(form, "canonical_name").strip()
    note_path = first_query_value(form, "note_path").strip()
    if not canonical:
        raise ValueError("canonical_name_required")
    if not note_path:
        raise ValueError("note_path_required")
    categories = parse_display_categories_rows(form, "display_categories")
    doc = dict(existing or {})
    doc.update(
        {
            "_id": f"site_{site_id}",
            "type": "site",
            "site_id": site_id,
            "canonical_name": canonical,
            "active": first_query_value(form, "active") == "1",
            "aliases": [line.strip() for line in first_query_value(form, "aliases").splitlines() if line.strip()],
            "note_path": note_path,
            "vision_context": first_query_value(form, "vision_context"),
            "capture_guidance": first_query_value(form, "capture_guidance"),
            "display_categories": categories,
        }
    )
    return site_id, doc


def submitted_display_categories(form: dict[str, list[str]], field_prefix: str) -> list[dict[str, str]]:
    return [
        {"label": str(label or ""), "canonical": str(canonical or "")}
        for label, canonical in zip(form.get(f"{field_prefix}_label", []), form.get(f"{field_prefix}_canonical", []))
    ]


def partial_form_doc(form: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "site_id": first_query_value(form, "site_id").strip(),
        "canonical_name": first_query_value(form, "canonical_name").strip(),
        "active": first_query_value(form, "active") == "1",
        "aliases": [line.strip() for line in first_query_value(form, "aliases").splitlines() if line.strip()],
        "note_path": first_query_value(form, "note_path").strip(),
        "vision_context": first_query_value(form, "vision_context"),
        "capture_guidance": first_query_value(form, "capture_guidance"),
        "display_categories": submitted_display_categories(form, "display_categories"),
    }


def audit_payload(form: dict[str, list[str]]) -> dict[str, object]:
    payload = {key: first_query_value(form, key) for key in form}
    if "vision_context" in payload:
        payload["vision_context"] = str(payload["vision_context"])[:200]
    return payload


def handle_save_post(ctx: object, body: bytes):
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    site_id = first_query_value(form, "site_id").strip()
    try:
        current = load_site(site_id) or {}
        _site_id, doc = form_doc(form, existing=current)
        rev = first_query_value(form, "_rev").strip()
        if rev:
            doc["_rev"] = rev
        request_json("PUT", doc_path(site_id), doc)
        ctx.audit("/sites/save", audit_payload(form), f"success: saved site_id={site_id}")
        return ctx.redirect(f"/sites?site_id={quote(site_id)}&message=saved")
    except error.HTTPError as exc:
        if exc.code == 409:
            server_doc = load_site(site_id) or {}
            ctx.audit("/sites/save", audit_payload(form), f"failed: conflict site_id={site_id}")
            return 200, "text/html; charset=utf-8", render_form(ctx, server_doc, is_new=False, notice="site was edited elsewhere; reapply your changes").encode(), {}
        ctx.audit("/sites/save", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/sites?site_id={quote(site_id)}&error={quote(str(exc))}")
    except ValueError as exc:
        ctx.audit("/sites/save", audit_payload(form), f"failed: {exc}")
        return 200, "text/html; charset=utf-8", render_form(ctx, partial_form_doc(form), is_new=False, notice=str(exc)).encode(), {}
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/sites/save", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/sites?site_id={quote(site_id)}&error={quote(str(exc))}")


def handle_new_post(ctx: object, body: bytes):
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    site_id = first_query_value(form, "site_id").strip()
    try:
        if not SITE_ID_RE.match(site_id):
            raise ValueError("invalid_site_id_pattern")
        if load_site(site_id):
            ctx.audit("/sites/new", audit_payload(form), f"failed: duplicate site_id={site_id}")
            return ctx.redirect(f"/sites/new?error=site_id_already_exists&site_id={quote(site_id)}")
        _site_id, doc = form_doc(form)
        request_json("PUT", doc_path(site_id), doc)
        ctx.audit("/sites/new", audit_payload(form), f"success: created site_id={site_id}")
        return ctx.redirect(f"/sites?site_id={quote(site_id)}&message=created")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/sites/new", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/sites/new?error={quote(str(exc))}&site_id={quote(site_id)}")
