from __future__ import annotations

import html

from btq_vault.employee_assignments import employee_assigned_site_ids
from event_pipeline.couchdb.migrate_vault import _employee_display_name
from ops_dashboard.common import field_rows, humanize

_PROTECTED = frozenset({"_id", "_rev", "type", "operator", "vault_path", "site_ids", "name"})


def _section_slug(title: str) -> str:
    return title.lower().replace(" & ", "_").replace(" ", "_")


def _escaped(value: object) -> str:
    return html.escape(str(value), quote=True)


def form_value(value: object) -> str:
    """Normalise a stored doc value for a form control.

    Lists join with ", " — frontmatter_list splits comma strings back into
    lists on the read side, so the round-trip preserves multi-value fields.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value)


def render_field_controls(
    doc: dict,
    keys: tuple[str, ...],
    *,
    long_text_keys: tuple[str, ...] = (),
    select_fields: dict[str, tuple[str, ...]] | None = None,
) -> str:
    controls: list[str] = []
    long_text = frozenset(long_text_keys)
    selects = select_fields or {}
    for key in keys:
        value = form_value(doc.get(key, ""))
        escaped_key = _escaped(key)
        escaped_label = _escaped(humanize(key))
        escaped_value = _escaped(value)
        if key in selects:
            choices = list(selects[key])
            if value and value not in choices:
                # A stored value outside the standard vocabulary stays selectable
                # so a save never silently rewrites it.
                choices.append(value)
            options = [] if value else ['<option value="" selected>&mdash;</option>']
            options.extend(
                f'<option value="{_escaped(choice)}"{" selected" if choice == value else ""}>{_escaped(choice)}</option>'
                for choice in choices
            )
            control = f'<select name="{escaped_key}">{"".join(options)}</select>'
        elif key in long_text:
            control = f'<textarea name="{escaped_key}">{escaped_value}</textarea>'
        else:
            control = f'<input type="text" name="{escaped_key}" value="{escaped_value}">'
        controls.append(
            f'    <label class="field-row"><span class="field-row-label">{escaped_label}</span>\n'
            f"      {control}\n"
            "    </label>"
        )
    return "\n".join(controls)


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

    rendered_controls = render_field_controls(doc, keys, long_text_keys=long_text_keys)
    return (
        "<section>\n"
        f"  <h3>{escaped_title}</h3>\n"
        f'  <form method="post" action="{_escaped(save_action)}" class="admin-form entity-edit-form">\n'
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
    current_site_ids = employee_assigned_site_ids({"site_ids": doc.get("site_ids")})
    if not current_site_ids:
        doc["site_ids"] = employee_assigned_site_ids(doc)
    return doc
