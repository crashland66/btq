from __future__ import annotations

import html
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote

from event_pipeline import couchdb_config
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture import prospects as prospects_mod
from ops_dashboard.common import other_section, record_section
from ops_dashboard.layout import html_page
import ops_dashboard.sections.field_photos as field_photos
from queue_spec import JOB_PROMOTE_PROSPECT
from btq_vault.projector import (  # noqa: PLC2701
    _section,
    render_markdown,
)


_SUMMARY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Identity",
        (
            "prospect_id",
            "name",
            "address",
            "account",
            "area_manager",
            "lead_source",
        ),
    ),
    (
        "Lifecycle",
        (
            "status",
            "created_at",
        ),
    ),
    (
        "Promotion",
        (
            "promoted_to_site_id",
            "promoted_at",
        ),
    ),
)

_SUPPRESSED = {"_id", "_rev", "type", "doc_type", "operator", "vault_path", "content", "notes"}


def _load_prospect(prospect_id: str) -> dict | None:
    cfg = couchdb_config.from_env()
    return prospects_mod.load_prospect(cfg, prospect_id)


def _format_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(html.escape(str(item)) for item in value if str(item).strip())
    return html.escape(str(value))


def _summary_section(doc: dict[str, Any]) -> str:
    sections: list[str] = []
    ordered_keys = {key for _title, keys in _SUMMARY_GROUPS for key in keys}
    for title, keys in _SUMMARY_GROUPS:
        section = record_section(title, doc, keys, value_formatter=_format_value)
        if section:
            sections.append(section)

    other = other_section(doc, ordered_keys, _SUPPRESSED, value_formatter=_format_value)
    if other:
        sections.append(other)

    if not sections:
        return ""
    return _section("Summary", "".join(sections))


def _photos_section(ctx: object, prospect_id: str) -> str:
    cards_html, fallback = field_photos.latest_photo_cards(
        ctx,
        limit=4,
        target_type="prospect",
        target_id=prospect_id,
    )
    if fallback:
        strip = '<p class="muted">Photos unavailable.</p>'
    elif cards_html:
        strip = (
            '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));'
            f'gap:16px;margin-top:12px">{cards_html}</div>'
        )
    else:
        strip = '<p class="zero-state">No photos yet for this prospect.</p>'
    escaped_id = html.escape(prospect_id, quote=True)
    return (
        "<section>"
        "<h3>Photos</h3>"
        f"{strip}"
        f'<p><a href="/field-photos?target_type=prospect&amp;target_id={escaped_id}">'
        "See all photos for this prospect &rarr;</a></p>"
        "</section>"
    )


def _site_options() -> tuple[list[tuple[str, str]], bool]:
    try:
        registry = CouchDBSiteRegistry()
        rows = registry._get_alias_rows()  # noqa: SLF001 - registry has no public list-sites API.
        by_site_id = {row.site_id: row.canonical for row in rows if row.site_id}
        return sorted(by_site_id.items(), key=lambda item: item[0]), True
    except Exception:
        return [], False


def _promotion_section(prospect: dict[str, Any], prospect_id: str) -> str:
    status = str(prospect.get("status") or "")
    promoted_to = str(prospect.get("promoted_to_site_id") or "")
    promoted_at = str(prospect.get("promoted_at") or "")
    if status in prospects_mod.TERMINAL_PROSPECT_STATUSES:
        if status == "won" and promoted_to:
            note = f"<p>Promoted to site <code>{html.escape(promoted_to)}</code>"
            if promoted_at:
                note += f" on {html.escape(promoted_at)}"
            note += ".</p>"
        else:
            note = f"<p>Closed: status = {html.escape(status)}.</p>"
        return f"<section><h3>Promote to Site</h3>{note}</section>"

    options, registry_ok = _site_options()
    if registry_ok and options:
        options_html = '<option value="">— select an active site —</option>'
        for site_id, canonical in options:
            label = f"{site_id} — {canonical}" if canonical else site_id
            options_html += (
                f'<option value="{html.escape(site_id, quote=True)}">'
                f"{html.escape(label)}</option>"
            )
        destination = (
            "<select name=\"site_id\" required>"
            f"{options_html}"
            "</select>"
        )
    else:
        destination = '<input name="site_id" required placeholder="site id">'
    escaped_id = html.escape(prospect_id, quote=True)
    return (
        "<section>"
        "<h3>Promote to Site</h3>"
        f'<form method="POST" action="/prospects/{escaped_id}/promote">'
        "<p><label>Destination site<br>"
        f"{destination}"
        "</label></p>"
        '<p><label>Actor<br><input name="actor" required placeholder="your name"></label></p>'
        '<p><label><input type="checkbox" name="confirm" value="1" required> '
        "Promote prospect to selected site. This retargets all captures and marks status=won.</label></p>"
        '<p><button type="submit">Promote</button></p>'
        "</form>"
        "</section>"
    )


def _flash(query: dict[str, list[str]]) -> str:
    message = (query.get("message") or [""])[0]
    site_id = (query.get("site_id") or [""])[0]
    error = (query.get("error") or [""])[0]
    parts = []
    if message == "staged":
        parts.append(
            '<section class="notice success">'
            f"<p>Promotion queued for {html.escape(site_id)}. Pipeline will pick it up on next cycle.</p>"
            "</section>"
        )
    elif message:
        parts.append(f'<section class="notice success"><p>{html.escape(message)}</p></section>')
    if error:
        parts.append(f'<section class="error"><p>{html.escape(error)}</p></section>')
    return "".join(parts)


def _not_found(prospect_id: str) -> str:
    escaped_id = html.escape(prospect_id)
    return html_page(
        "Prospect not found",
        f"<header><h1>Prospects</h1><p>Prospect not found: {escaped_id}</p></header>",
        active_section="prospect_detail",
    )


def _degraded(prospect_id: str, exc: Exception) -> str:
    escaped_id = html.escape(prospect_id)
    return html_page(
        f"Prospect {prospect_id} — BTQ",
        (
            f"<header><h1>Prospect {escaped_id}</h1></header>"
            f'<section class="error"><p>{html.escape(str(exc))}</p></section>'
        ),
        active_section="prospect_detail",
    )


def render(ctx: object, prospect_id: str) -> str:
    try:
        prospect = _load_prospect(prospect_id)
        if not isinstance(prospect, dict) or prospect.get("doc_type") != "prospect":
            return _not_found(prospect_id)

        name = str(prospect.get("name") or prospect_id)
        escaped_id = html.escape(prospect_id, quote=True)
        sections = [
            _flash(getattr(ctx, "query", {})),
            (
                f"<header><h1>{html.escape(name)} · {html.escape(prospect_id)}</h1>"
                '<p class="actions">'
                f'<a class="button" href="/field-photos?target_type=prospect&amp;target_id={escaped_id}">'
                "Field Photos for this prospect</a>"
                "</p></header>"
            ),
        ]
        summary = _summary_section(prospect)
        if summary:
            sections.append(summary)
        notes = str(prospect.get("notes") or "")
        if notes.strip():
            sections.append(_section("About", render_markdown(notes)))
        sections.append(_photos_section(ctx, prospect_id))
        sections.append(_promotion_section(prospect, prospect_id))
        return html_page(f"Prospect {prospect_id} — BTQ", "".join(sections), active_section="prospect_detail")
    except Exception as exc:  # noqa: BLE001
        return _degraded(prospect_id, exc)


def handle_promote_post(ctx: object, body: bytes, *, prospect_id: str) -> tuple:
    from ops_dashboard import audit

    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    runtime_root = Path(getattr(ctx, "runtime_root", Path("."))).expanduser().resolve(strict=False)

    def _redirect(location: str) -> tuple:
        return (
            303,
            "text/html; charset=utf-8",
            f'<a href="{html.escape(location)}">Return</a>'.encode(),
            {"Location": location},
        )

    if (form.get("confirm") or [""])[0] != "1":
        return _redirect(f"/prospects/{prospect_id}?error=confirm_required")

    site_id = (form.get("site_id") or [""])[0].strip()
    actor = (form.get("actor") or [""])[0].strip()
    if not site_id or not actor:
        return _redirect(f"/prospects/{prospect_id}?error=missing_field")

    suffix = str(uuid.uuid4())
    job = {
        "job_id": f"promote-prospect-{suffix}",
        "job_type": JOB_PROMOTE_PROSPECT,
        "payload": {
            "prospect_id": prospect_id,
            "site_id": site_id,
            "actor": actor,
        },
    }
    queue_dir = runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"promote-prospect-{suffix}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)

    audit.append_audit(
        runtime_root,
        {
            "route": f"/prospects/{prospect_id}/promote",
            "actor": actor,
            "payload": {"prospect_id": prospect_id, "site_id": site_id},
            "result_summary": f"staged queue_path={queue_path}",
        },
    )

    return _redirect(f"/prospects/{prospect_id}?message=staged&site_id={quote(site_id)}")
