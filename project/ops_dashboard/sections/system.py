from __future__ import annotations

import html
from urllib.parse import parse_qs, quote

from event_pipeline.couchdb import system_defaults
from event_pipeline.couchdb.system_defaults import SystemDefaultsConflictError
from ops_dashboard.common import first_query_value, parse_display_categories_rows, render_display_categories_editor
from ops_dashboard.layout import html_page


# Stage G1 (SPA wiring of system_defaults) is intentionally
# deferred. The SPA currently ignores this document; it will
# consume it in a later prompt.


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    try:
        doc = system_defaults.load_system_defaults()
        return render_form(doc, query=query)
    except Exception as exc:  # noqa: BLE001
        body = f'<header><h1>System</h1></header><section class="error"><p>{html.escape(str(exc))}</p></section>'
        return html_page("System", body, active_section="system")


def render_form(doc: dict[str, object], *, query: dict[str, list[str]] | None = None, notice: str = "") -> str:
    query = query or {}
    message = first_query_value(query, "message")
    error = first_query_value(query, "error")
    rev = str(doc.get("_rev") or "")
    banner = "" if rev else '<section class="notice"><p>system_defaults document not yet created - save this form to create it.</p></section>'
    notice_html = f'<section class="notice"><p>{html.escape(notice)}</p></section>' if notice else ""
    message_html = f'<section class="notice success"><p>{html.escape(message)}</p></section>' if message else ""
    error_html = f'<section class="error"><p>{html.escape(error)}</p></section>' if error else ""
    advice = "\n".join(str(item) for item in doc.get("default_capture_advice", []) if str(item).strip()) if isinstance(doc.get("default_capture_advice"), list) else ""
    body = f"""
    <header><h1>System</h1><p class="muted">Edit the single system_defaults CouchDB document.</p></header>
    {message_html}{error_html}{banner}{notice_html}
    <section>
      <form method="post" action="/system/save">
        <label>Vision Context <textarea name="default_vision_context">{html.escape(str(doc.get('default_vision_context') or ''))}</textarea></label>
        <label>Capture Guidance <textarea name="default_capture_guidance">{html.escape(str(doc.get('default_capture_guidance') or ''))}</textarea></label>
        <label>Display Categories {render_display_categories_editor("default_display_categories", doc.get('default_display_categories') if isinstance(doc.get('default_display_categories'), list) else [])}</label>
        <label>Voice Note Formula <input name="default_voice_note_formula" value="{html.escape(str(doc.get('default_voice_note_formula') or system_defaults.DEFAULT_VOICE_NOTE_FORMULA))}"></label>
        <label>Capture Advice <textarea name="default_capture_advice">{html.escape(advice)}</textarea></label>
        <input type="hidden" name="_rev" value="{html.escape(rev)}">
        <button>Save defaults</button>
      </form>
    </section>
    """
    return html_page("System", body, active_section="system")


def form_doc(form: dict[str, list[str]]) -> dict[str, object]:
    return {
        "_id": system_defaults.DOC_ID,
        "type": system_defaults.DOC_TYPE,
        "default_vision_context": first_query_value(form, "default_vision_context"),
        "default_capture_guidance": first_query_value(form, "default_capture_guidance"),
        "default_display_categories": parse_display_categories_rows(form, "default_display_categories"),
        "default_voice_note_formula": first_query_value(form, "default_voice_note_formula").strip() or system_defaults.DEFAULT_VOICE_NOTE_FORMULA,
        "default_capture_advice": [line.strip() for line in first_query_value(form, "default_capture_advice").splitlines() if line.strip()],
        "schema_version": system_defaults.SCHEMA_VERSION,
    }


def submitted_display_categories(form: dict[str, list[str]], field_prefix: str) -> list[dict[str, str]]:
    return [
        {"label": str(label or ""), "canonical": str(canonical or "")}
        for label, canonical in zip(form.get(f"{field_prefix}_label", []), form.get(f"{field_prefix}_canonical", []))
    ]


def partial_form_doc(form: dict[str, list[str]]) -> dict[str, object]:
    return {
        "default_vision_context": first_query_value(form, "default_vision_context"),
        "default_capture_guidance": first_query_value(form, "default_capture_guidance"),
        "default_display_categories": submitted_display_categories(form, "default_display_categories"),
        "default_voice_note_formula": first_query_value(form, "default_voice_note_formula").strip() or system_defaults.DEFAULT_VOICE_NOTE_FORMULA,
        "default_capture_advice": [line.strip() for line in first_query_value(form, "default_capture_advice").splitlines() if line.strip()],
    }


def audit_payload(form: dict[str, list[str]]) -> dict[str, object]:
    payload = {key: first_query_value(form, key) for key in form}
    for key in ("default_vision_context", "default_capture_guidance"):
        if key in payload:
            payload[key] = str(payload[key])[:200]
    return payload


def handle_save_post(ctx: object, body: bytes):
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    try:
        doc = form_doc(form)
        system_defaults.save_system_defaults(doc, first_query_value(form, "_rev").strip() or None)
        ctx.audit("/system/save", audit_payload(form), "success: saved system_defaults")
        return ctx.redirect("/system?message=saved")
    except SystemDefaultsConflictError as exc:
        ctx.audit("/system/save", audit_payload(form), "failed: conflict system_defaults")
        return 200, "text/html; charset=utf-8", render_form(exc.current_doc, notice="system_defaults was edited elsewhere; reapply your changes").encode(), {}
    except ValueError as exc:
        ctx.audit("/system/save", audit_payload(form), f"failed: {exc}")
        return 200, "text/html; charset=utf-8", render_form(partial_form_doc(form), notice=str(exc)).encode(), {}
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/system/save", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/system?error={quote(str(exc))}")
