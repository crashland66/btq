from __future__ import annotations

import html

from event_pipeline.couchdb.migrate_vault import (
    _employee_display_name,
    _employee_site_ids,
)
from ops_dashboard.common import field_rows, humanize

_PROTECTED = frozenset({"_id", "_rev", "type", "operator", "vault_path", "site_ids", "name"})


def _section_slug(title: str) -> str:
    return title.lower().replace(" & ", "_").replace(" ", "_")


def _escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_editable_section(
    title: str,
    doc: dict,
    keys: tuple[str, ...],
    *,
    edit_active: bool,
    save_action: str,
    entity_id: str,
    long_text_keys: tuple[str, ...] = (),
) -> str:
    slug = _section_slug(title)
    escaped_title = _escaped(title)

    if not edit_active:
        fields = field_rows(doc, keys)
        return (
            "<section>\n"
            f"  <h3>{escaped_title}</h3>"
            f'<p class="actions"><a class="button" href="?edit={_escaped(slug)}">Edit</a></p>\n'
            f'  <dl class="fields">{fields}</dl>\n'
            "</section>"
        )

    controls: list[str] = []
    long_text = frozenset(long_text_keys)
    for key in keys:
        value = doc.get(key, "")
        escaped_key = _escaped(key)
        escaped_label = _escaped(humanize(key))
        escaped_value = _escaped(value)
        if key in long_text:
            controls.append(
                f"    <label>{escaped_label}\n"
                f'      <textarea name="{escaped_key}">{escaped_value}</textarea>\n'
                "    </label>"
            )
        else:
            controls.append(
                f"    <label>{escaped_label}\n"
                f'      <input type="text" name="{escaped_key}" value="{escaped_value}">\n'
                "    </label>"
            )
    rendered_controls = "\n".join(controls)
    return (
        "<section>\n"
        f"  <h3>{escaped_title}</h3>\n"
        f'  <form method="post" action="{_escaped(save_action)}">\n'
        f'    <input type="hidden" name="_rev" value="{_escaped(doc["_rev"])}">\n'
        f'    <input type="hidden" name="_entity_id" value="{_escaped(entity_id)}">\n'
        f'    <input type="hidden" name="_section" value="{_escaped(slug)}">\n'
        f"{rendered_controls}\n"
        '    <button type="submit">Save</button>\n'
        '    <a class="button" href="?">Cancel</a>\n'
        "  </form>\n"
        "</section>"
    )


def apply_section_update(
    existing: dict,
    form: dict,
    allowed_keys: frozenset[str],
) -> dict:
    result = dict(existing)
    for key in allowed_keys:
        if key in _PROTECTED:
            continue
        value = form.get(key)
        if isinstance(value, str):
            value = value.strip()
        if value == "" or value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def recompute_employee_derived(doc: dict) -> dict:
    doc["name"] = _employee_display_name(doc)
    doc["site_ids"] = _employee_site_ids(doc)
    return doc
