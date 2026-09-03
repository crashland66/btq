from __future__ import annotations

import html
import json
import logging
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote

from field_capture.auth import TokenRecord, TokenStore, parse_timestamp
from ops_dashboard.common import first_query_value, humanize_key, render_table
from ops_dashboard.layout import html_page
from .employees import _display_name, _string, employee_is_active, load_employees


LOGGER = logging.getLogger(__name__)
RAW_TOKEN_FLASH: dict[str, tuple[str, float]] = {}
RAW_TOKEN_TTL_SECONDS = 300
TOKEN_SYNC_SSH_TARGET = os.environ.get("BTQ_VPS_SSH_TARGET", "deploy@vps.example.com")
TOKEN_SYNC_REMOTE_WORKDIR = "/srv/btq/apps/field-capture/project"
TOKEN_SYNC_REMOTE_DB = "/srv/btq/data/field_capture_tokens.sqlite3"
ROLE_OPTIONS = (("cleaner", "Cleaner"), ("site_admin", "Site Admin"), ("read_only", "Read-only"))


def token_store(root: Path) -> TokenStore:
    return TokenStore(root / "field_capture_tokens.sqlite3")


def bool_label(value: bool) -> str:
    return f'<span class="pill {"success" if value else "warning"}">{"Yes" if value else "No"}</span>'


def render_role_cell(value: object, _row: dict[str, object]) -> str:
    role = str(value or "cleaner")
    label = role_label(role)
    if role == "site_admin":
        return f'<span class="pill success">{html.escape(label)}</span>'
    return f'<span class="pill">{html.escape(label)}</span>'


def role_label(role: str) -> str:
    return dict(ROLE_OPTIONS).get(role, humanize_key(role))


def role_radio(current: str) -> str:
    return "".join(
        f'<label><input type="radio" name="role" value="{html.escape(value)}" {"checked" if current == value else ""}> {html.escape(label)}</label>'
        for value, label in ROLE_OPTIONS
    )


def short_token_id(token_id: str) -> str:
    return token_id if len(token_id) <= 12 else f"{token_id[:12]}..."


def render_token_id_cell(value: object, row: dict[str, object]) -> str:
    token_id_str = str(value)
    short = html.escape(short_token_id(token_id_str))
    raw = str(row.get("token_value") or "")
    id_html = f'<code title="{html.escape(token_id_str)}">{short}</code>'
    if raw:
        copy_btn = (
            f'<button type="button" class="copy-btn" '
            f'data-copy-value="{html.escape(raw)}" '
            f'title="Copy secret token to clipboard">Copy</button>'
        )
    else:
        set_raw_url = f"/tokens/set-raw?token_id={quote(token_id_str)}"
        copy_btn = (
            f'<a class="copy-btn muted" href="{html.escape(set_raw_url)}" '
            f'title="Paste the original raw token to enable Copy on this row.">Set...</a>'
        )
    return f"{id_html} {copy_btn}"


def employee_identity_keys(doc: dict) -> frozenset[str]:
    """Return every supported token person_id for an employee-like doc."""
    keys: set[str] = set()
    doc_id = str(doc.get("_id") or "").strip()
    if doc_id:
        keys.add(doc_id)
        for prefix in ("employee_", "person_", "operator_"):
            if doc_id.startswith(prefix):
                slug = doc_id[len(prefix):]
                if slug:
                    keys.add(slug)
                    keys.add(slug.replace("_", "-"))
                break
    person_id = str(doc.get("person_id") or "").strip()
    if person_id:
        keys.add(person_id)
    return frozenset(keys)


def _build_person_name_map(docs: list[dict]) -> dict[str, str]:
    """Map token person_id -> display name from canonical person/employee docs.

    Token person_ids are the de-prefixed slug of the employee doc id in either
    underscore or dash form
    (e.g. doc `_id` ``employee_mercer_glen`` -> token person_id
    ``mercer_glen`` or ``mercer-glen``). Keyed by both slug forms, the raw
    `_id`, and any `person_id` field so a token resolves however it was stored.
    Pure/synchronous for tests.
    """
    names: dict[str, str] = {}
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("type") not in {"employee", "person", "operator"}:
            continue
        first = str(doc.get("first") or "").strip()
        last = str(doc.get("last") or "").strip()
        name = str(doc.get("name") or "").strip() or f"{first} {last}".strip()
        if not name:
            continue
        for key in employee_identity_keys(doc):
            names[key] = name
    return names


def person_name_map() -> dict[str, str]:
    """Best-effort person_id -> name from the canonical CouchDB store. Returns
    {} if CouchDB is unavailable so the tokens page still renders (person_id
    only)."""
    try:
        from event_pipeline import couchdb_config
        from btq_vault.projector import DDOC, query_view

        cfg = couchdb_config.from_env()
        rows = query_view(
            cfg.base_url, cfg.auth_header(), couchdb_config.vault_database(), DDOC, "by_type", include_docs=True, timeout=5.0
        )
    except Exception as exc:  # noqa: BLE001 - best-effort name enrichment only.
        LOGGER.debug("token person-name lookup skipped: %s", exc)
        return {}
    return _build_person_name_map([row.get("doc") or {} for row in rows if isinstance(row, dict)])


def render_person_cell(value: object, row: dict[str, object]) -> str:
    person_id = str(value or "")
    name = str(row.get("person_name") or "").strip()
    id_html = f'<code class="muted">{html.escape(person_id)}</code>' if person_id else ""
    if name:
        return f'<span class="person-name">{html.escape(name)}</span>{("<br>" + id_html) if id_html else ""}'
    return id_html or '<span class="muted">—</span>'


def age_warning(record: TokenRecord) -> bool:
    if not record.last_used_at:
        return True
    try:
        last_used = parse_timestamp(record.last_used_at)
    except ValueError:
        return True
    if last_used is None:
        return True
    return (datetime.now(timezone.utc) - last_used).days > 90


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    if getattr(ctx, "route_path", "") == "/tokens/new" or first_query_value(query, "new") == "1":
        return render_new_form(query)
    if getattr(ctx, "route_path", "") == "/tokens/set-raw":
        return render_set_raw_form(query)
    if getattr(ctx, "route_path", "") == "/tokens/edit":
        return render_edit_form(ctx)
    if first_query_value(query, "issued") == "1" and first_query_value(query, "token_id"):
        return render_reveal(query)
    return render_list(ctx.runtime_root, query)


def render_reveal(query: dict[str, list[str]]) -> str:
    token_id = first_query_value(query, "token_id")
    raw = pop_raw_token(token_id)
    raw_html = html.escape(raw or "")
    missing = '<p class="error">Raw token is no longer available.</p>' if not raw else ""
    warning = sync_warning_section(query, token_id)
    message = first_query_value(query, "message")
    old_token_id = first_query_value(query, "old_token_id")
    regenerate_message = ""
    if message == "regenerated" and old_token_id:
        regenerate_message = (
            f'<p class="muted">Regenerated from <code>{html.escape(old_token_id)}</code>. '
            "The previous token is now revoked. Send the new link to the recipient.</p>"
        )
    elif message == "regenerated_revoke_failed" and old_token_id:
        regenerate_message = (
            f'<section class="error"><p>Regenerated from <code>{html.escape(old_token_id)}</code>, '
            "and the new token is live, but the previous token was not revoked. "
            "Manually revoke the old token with the Revoke button.</p></section>"
        )
    body = f"""
    <header><h1>Token Issued</h1><p class="muted">This token will not be shown again. Copy it now.</p></header>
    {warning}
    {regenerate_message}
    <section>{missing}<pre id="raw-token">{raw_html}</pre><button data-copy-target="#raw-token">Copy</button><p><a href="/tokens">Back to tokens</a></p></section>
    """
    return html_page("Token Issued", body, active_section="tokens")


def render_set_raw_form(query: dict[str, list[str]]) -> str:
    token_id = first_query_value(query, "token_id")
    error = first_query_value(query, "error")
    error_html = f'<section class="error"><p>{html.escape(error)}</p></section>' if error else ""
    body = f"""
    <header>
      <h1>Set raw value</h1>
      <p class="muted">Paste the raw <code>fc_*</code> value that was originally issued for token <code>{html.escape(token_id)}</code>. The server will verify it hashes to the stored token_hash before saving. If you paste the wrong value, the row is unchanged.</p>
    </header>
    {error_html}
    <section>
      <form method="post" action="/tokens/set-raw" class="admin-form">
        <input type="hidden" name="token_id" value="{html.escape(token_id)}">
        <label>Raw token value <input type="password" name="raw_value" required autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"></label>
        <button>Save</button>
        <a href="/tokens">Cancel</a>
      </form>
    </section>
    """
    return html_page("Set raw value", body, active_section="tokens")


def render_person_id_field(selected: str = "") -> str:
    """Render an active-employee picker, failing closed without a roster."""
    try:
        employees = load_employees()
    except Exception:  # noqa: BLE001 — the empty state deliberately hides issuance.
        return '<p class="error">Employee roster unavailable — try again.</p>'

    eligible = [doc for doc in employees if employee_is_active(doc)]
    if not eligible:
        return '<p class="muted">No active employees to issue a token for.</p>'

    option_rows: list[tuple[dict, str, str]] = []
    for doc in eligible:
        person_id = _string(doc.get("person_id")) or _string(doc.get("_id")).removeprefix("employee_")
        if not person_id:
            continue
        name = _display_name(doc)
        label = f"{name} ({person_id})" if name and name != person_id else person_id
        option_rows.append((doc, person_id, label))
    if not option_rows:
        return '<p class="muted">No active employees to issue a token for.</p>'

    selected_index = (
        next(
            (
                index
                for index, (doc, _person_id, _label) in enumerate(option_rows)
                if selected in employee_identity_keys(doc)
            ),
            None,
        )
        if selected
        else None
    )
    options = [f'<option value="" disabled{" selected" if selected_index is None else ""}>Select a person…</option>']
    for index, (_doc, person_id, label) in enumerate(option_rows):
        chosen = " selected" if index == selected_index else ""
        options.append(f'<option value="{html.escape(person_id)}"{chosen}>{html.escape(label)}</option>')
    return f'<label>Person ID <select name="person_id" required>{"".join(options)}</select></label>'


def render_new_form(query: dict[str, list[str]] | None = None) -> str:
    query = query or {}
    error = first_query_value(query, "error")
    error_html = f'<section class="error"><p>{html.escape(error)}</p></section>' if error else ""
    selected_person = first_query_value(query, "person_id")
    body = f"""
    <header><h1>Issue Token</h1><p class="muted">Creates one field-capture token row. Coming in prompt references are complete here.</p></header>
    {error_html}
    <section>
      <form method="post" action="/tokens/new" class="admin-form">
        {render_person_id_field(selected_person)}
        <label>Label <input name="label" required></label>
        <label><input type="radio" name="token_type" value="capture" checked> Capture</label>
        <label><input type="radio" name="token_type" value="client_viewer"> Client Viewer</label>
        <label><input type="radio" name="token_type" value="admin_viewer"> Admin Viewer</label>
        <label><input type="radio" name="token_type" value="import"> Import</label>
        <fieldset><legend>Role</legend>{role_radio("cleaner")}</fieldset>
        <p class="muted">Use Site Admin for operator capture tokens that need QC, Baseline, or Pre-Engagement categories. Use Read-only for reporting links that must not submit captures.</p>
        <label><input type="checkbox" name="can_submit" value="1" checked> Can Submit</label>
        <label><input type="checkbox" name="can_view_site" value="1" checked> Can View Site</label>
        <label>Site IDs <textarea name="site_ids">*</textarea></label>
        <label>Expires At <input name="expires_at" placeholder="2026-06-01T00:00:00Z"></label>
        <button>Issue token</button>
      </form>
    </section>
    """
    return html_page("Issue Token", body, active_section="tokens")


def _checked(value: bool) -> str:
    return " checked" if value else ""


def render_edit_form(ctx: object) -> str:
    query = getattr(ctx, "query", {}) or {}
    token_id = first_query_value(query, "token_id").strip()
    error = first_query_value(query, "error")
    error_html = f'<section class="error"><p>{html.escape(error)}</p></section>' if error else ""
    record = token_store(ctx.runtime_root).get_token(token_id) if token_id else None
    if record is None or record.revoked:
        body = """
        <header><h1>Edit Token</h1></header>
        <section class="error"><p>Token not found or already revoked.</p></section>
        <p><a href="/tokens">Back to tokens</a></p>
        """
        return html_page("Edit Token", body, active_section="tokens")
    site_ids = "\n".join(record.site_ids)
    role_radios = role_radio(record.role)
    token_type_radios = radio("token_type", record.token_type, ["capture", "viewer", "client_viewer", "admin_viewer", "import"])
    body = f"""
    <header><h1>Edit Token</h1><p class="muted">Updates token scope and role without rotating the secret.</p></header>
    {error_html}
    <section>
      <form method="post" action="/tokens/edit" class="admin-form">
        <label>Token ID <input name="token_id" value="{html.escape(record.token_id)}" readonly></label>
        <label>Person ID <input name="person_id" value="{html.escape(record.person_id)}" readonly></label>
        <fieldset><legend>Role</legend>{role_radios}</fieldset>
        <label>Site IDs <textarea name="site_ids">{html.escape(site_ids)}</textarea></label>
        <label><input type="checkbox" name="can_submit" value="1"{_checked(record.can_submit)}> Can Submit</label>
        <label><input type="checkbox" name="can_view_site" value="1"{_checked(record.can_view_site)}> Can View Site</label>
        <fieldset><legend>Token Type</legend>{token_type_radios}</fieldset>
        <button>Save token</button>
        <a href="/tokens">Cancel</a>
      </form>
    </section>
    """
    return html_page("Edit Token", body, active_section="tokens")


def render_list(root: Path, query: dict[str, list[str]]) -> str:
    records = token_store(root).list_tokens()
    sync_warning = sync_warning_section(query, first_query_value(query, "token_id"))
    set_raw_banner = (
        '<section class="success"><p>Raw token saved. The Copy button on that row is now active.</p></section>'
        if first_query_value(query, "set_raw") == "1"
        else ""
    )
    token_type = first_query_value(query, "token_type") or "all"
    revoked = first_query_value(query, "revoked") or "active"
    label_filter = first_query_value(query, "label_contains").strip().lower()
    if token_type != "all":
        records = [record for record in records if record.token_type == token_type]
    if revoked == "active":
        records = [record for record in records if not record.revoked]
    elif revoked == "revoked":
        records = [record for record in records if record.revoked]
    if label_filter:
        records = [record for record in records if label_filter in record.label.lower()]
    records = sorted(records, key=lambda record: record.created_at, reverse=True)
    records = sorted(records, key=lambda record: bool(record.revoked))
    names = person_name_map()
    rows = [
        {
            "token_id": record.token_id,
            "label": record.label,
            "person_id": record.person_id,
            "person_name": names.get(record.person_id, ""),
            "token_type": record.token_type,
            "role": record.role,
            "site_scope": ",".join(record.site_ids),
            "can_submit": record.can_submit,
            "can_view_site": record.can_view_site,
            "created_at": record.created_at,
            "expires_at": record.expires_at or "",
            "last_used": record.last_used_at or "never",
            "revoked": record.revoked,
            "token_value": record.token_value or "",
            "actions": record.token_id,
            "last_used_warning": age_warning(record),
        }
        for record in records
    ]
    columns_mode = first_query_value(query, "columns") or "compact"
    show_all_columns = columns_mode == "all"
    # `detail` columns are hidden in the compact default and revealed by the
    # Columns: All toggle, so the table fits without the right side scrolling
    # away. The pinned Actions column stays visible in both modes.
    all_columns = [
        {"key": "token_id", "label": "Token ID", "format": lambda value, row: render_token_id_cell(value, row), "nowrap": True},
        {"key": "person_id", "label": "Person", "format": render_person_cell, "nowrap": True},
        {"key": "token_type", "label": "Token Type", "format": lambda value, _row: html.escape(humanize_key(value)), "priority": 2, "nowrap": True, "detail": True},
        {"key": "role", "label": "Role", "format": render_role_cell, "nowrap": True},
        {"key": "label", "label": "Label"},
        {"key": "site_scope", "label": "Site Scope", "priority": 2, "nowrap": True},
        {"key": "can_submit", "label": "Can Submit", "format": lambda value, _row: bool_label(bool(value)), "priority": 3, "nowrap": True, "detail": True},
        {"key": "can_view_site", "label": "Can View Site", "format": lambda value, _row: bool_label(bool(value)), "priority": 3, "nowrap": True, "detail": True},
        {"key": "created_at", "label": "Created At", "priority": 3, "nowrap": True, "detail": True},
        {"key": "expires_at", "label": "Expires At", "priority": 3, "nowrap": True, "detail": True},
        {"key": "last_used", "label": "Last Used", "format": lambda value, row: f'<span class="pill warning">{html.escape(str(value))}</span>' if row.get("last_used_warning") else html.escape(str(value)), "priority": 2, "nowrap": True, "detail": True},
        {"key": "revoked", "label": "Active", "format": lambda value, _row: bool_label(not bool(value)), "priority": 2},
        {"key": "actions", "label": "Actions", "format": render_actions_cell, "class": "col-sticky-right", "nowrap": True},
    ]
    visible_columns = [column for column in all_columns if show_all_columns or not column.get("detail")]
    table = render_table(
        rows,
        visible_columns,
        empty_text="No tokens match this filter.",
    )
    filters = f"""
    <form method="get" action="/tokens" data-submit-on-change>
      {radio('token_type', token_type, ['all', 'capture', 'client_viewer', 'admin_viewer', 'import'])}
      {radio('revoked', revoked, ['all', 'active', 'revoked'])}
      <label>Label contains <input name="label_contains" value="{html.escape(first_query_value(query, 'label_contains'))}"></label>
      <fieldset class="filter-columns"><legend>Columns</legend>{radio('columns', columns_mode, ['compact', 'all'])}</fieldset>
      <button>Apply</button>
    </form>
    """
    body = f'<header><h1>Tokens</h1><p class="muted">Token metadata only; raw token values are shown once after issuance. Coming in prompt references are complete here.</p><p><a class="button" href="/tokens/new">Issue token</a></p></header>{sync_warning}{set_raw_banner}<div class="content-with-rail"><aside class="filter-rail"><section><h2>Filters</h2>{filters}</section></aside><section><h2>Tokens</h2>{table}</section></div>'
    return html_page("Tokens", body, active_section="tokens")


def render_actions_cell(value: object, row: dict[str, object]) -> str:
    raw_token_id = str(value)
    token_id = html.escape(raw_token_id)
    edit = ""
    regenerate = ""
    if not row.get("revoked"):
        edit_url = f"/tokens/edit?token_id={quote(raw_token_id)}"
        edit = f'<a href="{html.escape(edit_url)}" class="icon-btn" title="Edit token" aria-label="Edit token">✎</a> '
        regenerate = (
            f'<form method="post" action="/tokens/regenerate" style="display:inline">'
            f'<input type="hidden" name="token_id" value="{token_id}">'
            '<button type="submit" class="icon-btn" '
            'title="Replace: issue a new token with the same person/site/label and revoke this one" '
            'aria-label="Replace token">↻</button>'
            f"</form> "
        )
    revoke = (
        f'<form method="post" action="/tokens/revoke" style="display:inline">'
        f'<input type="hidden" name="token_id" value="{token_id}">'
        '<button type="submit" class="icon-btn icon-btn--danger" '
        'title="Revoke this token" aria-label="Revoke token">✕</button>'
        f"</form>"
    )
    return f"{edit}{regenerate}{revoke}"


def radio(name: str, current: str, values: list[str]) -> str:
    return "".join(f'<label><input type="radio" name="{html.escape(name)}" value="{html.escape(value)}" {"checked" if current == value else ""}> {html.escape("All" if value == "all" else humanize_key(value))}</label>' for value in values)


def pop_raw_token(token_id: str) -> str:
    raw = RAW_TOKEN_FLASH.pop(token_id, None)
    if raw is None:
        return ""
    token, created_at = raw
    if time.time() - created_at > RAW_TOKEN_TTL_SECONDS:
        return ""
    return token


def parse_site_ids(raw: str) -> tuple[str, ...]:
    items = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    return tuple(items)


def audit_payload(form: dict[str, list[str]], record: TokenRecord | None = None) -> dict[str, object]:
    return {
        "label": first_query_value(form, "label") or (record.label if record else ""),
        "person_id": first_query_value(form, "person_id") or (record.person_id if record else ""),
        "token_type": first_query_value(form, "token_type") or (record.token_type if record else ""),
        "role": first_query_value(form, "role") or (record.role if record else ""),
        "site_scope": first_query_value(form, "site_ids") or (",".join(record.site_ids) if record else ""),
    }


def truthy_env(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(normalized) and normalized not in {"0", "false", "no", "off"}


def record_sync_row(record: TokenRecord) -> dict[str, object]:
    return {
        "token_id": record.token_id,
        "token_hash": record.token_hash,
        "person_id": record.person_id,
        "created_at": record.created_at,
        "expires_at": record.expires_at,
        "revoked": 1 if record.revoked else 0,
        "label": record.label,
        "last_used_at": record.last_used_at,
        "can_submit": 1 if record.can_submit else 0,
        "can_view_site": 1 if record.can_view_site else 0,
        "role": record.role,
        "token_type": record.token_type,
        "site_ids": json.dumps(list(record.site_ids)),
    }


def sync_token_to_vps(action: str, payload: dict[str, object]) -> tuple[bool, str]:
    if truthy_env(os.environ.get("BTQ_TOKEN_SYNC_DISABLED")):
        LOGGER.info("field_capture_token_sync disabled action=%s", action)
        return True, "sync disabled by env"

    target = os.environ.get("BTQ_TOKEN_SYNC_SSH_TARGET") or TOKEN_SYNC_SSH_TARGET
    workdir = os.environ.get("BTQ_TOKEN_SYNC_REMOTE_WORKDIR") or TOKEN_SYNC_REMOTE_WORKDIR
    db = os.environ.get("BTQ_TOKEN_SYNC_REMOTE_DB") or TOKEN_SYNC_REMOTE_DB
    remote = f"cd {shlex.quote(workdir)} && /usr/bin/python3 -m field_capture.sync_token --db {shlex.quote(db)}"
    try:
        result = subprocess.run(
            ["ssh", target, remote],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOGGER.error("field_capture_token_sync timed out action=%s", action)
        return False, "ssh sync timed out"
    except OSError as exc:
        LOGGER.error("field_capture_token_sync failed action=%s error=%s", action, exc)
        return False, str(exc)
    output = (result.stderr or result.stdout or "").strip()
    if result.returncode == 0:
        row = payload.get("row")
        token_id = payload.get("token_id") or (row.get("token_id") if isinstance(row, dict) else "")
        LOGGER.info("field_capture_token_sync success action=%s token_id=%s", action, token_id)
        return True, ""
    LOGGER.error("field_capture_token_sync failed action=%s returncode=%s output=%s", action, result.returncode, output)
    return False, output


def sync_error_excerpt(query: dict[str, list[str]]) -> str:
    return first_query_value(query, "sync_error")[:300]


def sync_warning_section(query: dict[str, list[str]], token_id: str) -> str:
    if first_query_value(query, "sync_status") != "failed":
        return ""
    target = os.environ.get("BTQ_TOKEN_SYNC_SSH_TARGET") or TOKEN_SYNC_SSH_TARGET
    workdir = os.environ.get("BTQ_TOKEN_SYNC_REMOTE_WORKDIR") or TOKEN_SYNC_REMOTE_WORKDIR
    db = os.environ.get("BTQ_TOKEN_SYNC_REMOTE_DB") or TOKEN_SYNC_REMOTE_DB
    command = f"ssh {target} 'cd {workdir} && python3 -m field_capture.sync_token --db {db}' < /tmp/{token_id}.json"
    return f"""
    <section class="warning">
      <h2>VPS sync failed.</h2>
      <p>This token is stored on the Pro but the VPS auth DB was not updated. The token will NOT authenticate against {html.escape(os.environ.get("BTQ_PUBLIC_PHOTOS_HOST", "photos.example.com"))} until you sync manually:</p>
      <pre>{html.escape(command)}</pre>
      <p>(Error: <code>{html.escape(sync_error_excerpt(query))}</code>)</p>
    </section>
    """


def sync_redirect_query(token_id: str, ok: bool, detail: str, *, action: str | None = None) -> str:
    query = f"token_id={quote(token_id)}&sync_status={'ok' if ok else 'failed'}"
    if action:
        query += f"&action={quote(action)}"
    if not ok and detail:
        query += f"&sync_error={quote(detail[:300])}"
    return query


def handle_new_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    person_id = first_query_value(form, "person_id").strip()
    label = first_query_value(form, "label").strip()
    role = first_query_value(form, "role") or "cleaner"
    try:
        if not person_id:
            raise ValueError("person_id_required")
        if not label:
            raise ValueError("label_required")
        try:
            employees = load_employees()
        except Exception as exc:  # noqa: BLE001 — token issuance must fail closed.
            raise ValueError("roster_unavailable") from exc
        employee = next(
            (doc for doc in employees if isinstance(doc, dict) and person_id in employee_identity_keys(doc)),
            None,
        )
        if employee is None:
            raise ValueError("unknown_employee")
        if not employee_is_active(employee):
            raise ValueError("employee_not_active")
        created = token_store(root).create_token(
            person_id=person_id,
            label=label,
            expires_at=parse_timestamp(first_query_value(form, "expires_at")),
            can_submit=first_query_value(form, "can_submit") == "1",
            can_view_site=role == "read_only" or first_query_value(form, "can_view_site") == "1",
            role=role,
            token_type=first_query_value(form, "token_type") or "capture",
            site_ids=parse_site_ids(first_query_value(form, "site_ids")),
        )
        RAW_TOKEN_FLASH[created.record.token_id] = (created.token_value, time.time())
        ctx.audit("/tokens/new", audit_payload(form, created.record), f"success: created token_id={created.record.token_id}")
        sync_payload = {"action": "upsert", "row": record_sync_row(created.record)}
        sync_ok, sync_detail = sync_token_to_vps("upsert", sync_payload)
        ctx.audit("/tokens/sync", audit_payload(form, created.record), f"{'success' if sync_ok else 'failed'}: upsert token_id={created.record.token_id} {sync_detail}".strip())
        return ctx.redirect(f"/tokens?issued=1&message=created&{sync_redirect_query(created.record.token_id, sync_ok, sync_detail)}")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/tokens/new", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/tokens/new?error={quote(str(exc))}")


def handle_edit_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    token_id = first_query_value(form, "token_id").strip()
    store = token_store(root)
    role = first_query_value(form, "role") or "cleaner"
    try:
        updated = store.update_token(
            token_id,
            role=role,
            site_ids=parse_site_ids(first_query_value(form, "site_ids")),
            can_submit=first_query_value(form, "can_submit") == "1",
            can_view_site=role == "read_only" or first_query_value(form, "can_view_site") == "1",
            token_type=first_query_value(form, "token_type") or "capture",
        )
        if updated is None:
            ctx.audit("/tokens/edit", audit_payload(form), f"failed: not_found token_id={token_id}")
            return ctx.redirect("/tokens?error=not_found")
        ctx.audit("/tokens/edit", audit_payload(form, updated), f"success: edited token_id={token_id}")
        sync_payload = {"action": "upsert", "row": record_sync_row(updated)}
        sync_ok, sync_detail = sync_token_to_vps("upsert", sync_payload)
        ctx.audit("/tokens/sync", audit_payload(form, updated), f"{'success' if sync_ok else 'failed'}: upsert token_id={token_id} {sync_detail}".strip())
        if not sync_ok:
            return ctx.redirect(f"/tokens?sync_status=failed&sync_error={quote(sync_detail[:300])}&token_id={quote(token_id)}")
        return ctx.redirect(f"/tokens?token_id={quote(token_id)}&edited=1")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/tokens/edit", audit_payload(form), f"failed: {exc}")
        return ctx.redirect(f"/tokens/edit?token_id={quote(token_id)}&error={quote(str(exc))}")


def handle_revoke_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    token_id = first_query_value(form, "token_id").strip()
    store = token_store(root)
    record = store.get_token(token_id)
    try:
        revoked = store.revoke_token(token_id)
        if not revoked:
            raise ValueError("token_not_revoked")
        ctx.audit("/tokens/revoke", audit_payload(form, record), f"success: revoked token_id={token_id}")
        sync_payload = {"action": "revoke", "token_id": token_id}
        sync_ok, sync_detail = sync_token_to_vps("revoke", sync_payload)
        ctx.audit("/tokens/sync", audit_payload(form, record), f"{'success' if sync_ok else 'failed'}: revoke token_id={token_id} {sync_detail}".strip())
        return ctx.redirect(f"/tokens?message=revoked&{sync_redirect_query(token_id, sync_ok, sync_detail, action='revoked')}")
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/tokens/revoke", audit_payload(form, record), f"failed: {exc}")
        return ctx.redirect(f"/tokens?error={quote(str(exc))}&token_id={quote(token_id)}")


def handle_regenerate_post(ctx: object, body: bytes):
    root = ctx.runtime_root
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    old_token_id = first_query_value(form, "token_id").strip()
    if not old_token_id:
        return ctx.redirect("/tokens?error=missing_token_id")
    store = token_store(root)
    source = store.get_token(old_token_id)
    if source is None:
        return ctx.redirect(f"/tokens?error=unknown_token&token_id={quote(old_token_id)}")
    if source.revoked:
        return ctx.redirect(f"/tokens?error=token_already_revoked&token_id={quote(old_token_id)}")
    try:
        # Issue the replacement first. If anything goes wrong, the source
        # stays active so the operator can retry without locking the recipient out.
        created = store.create_token(
            person_id=source.person_id,
            label=source.label or "",
            expires_at=parse_timestamp(source.expires_at) if source.expires_at else None,
            can_submit=bool(source.can_submit),
            can_view_site=bool(source.can_view_site),
            role=source.role,
            token_type=source.token_type or "capture",
            site_ids=source.site_ids or (),
        )
        RAW_TOKEN_FLASH[created.record.token_id] = (created.token_value, time.time())
        new_sync_payload = {"action": "upsert", "row": record_sync_row(created.record)}
        new_sync_ok, new_sync_detail = sync_token_to_vps("upsert", new_sync_payload)
        ctx.audit(
            "/tokens/regenerate",
            {"token_id": old_token_id, "replacement_token_id": created.record.token_id},
            f"success: created replacement token_id={created.record.token_id} for old token_id={old_token_id}",
        )
        ctx.audit(
            "/tokens/sync",
            {"token_id": created.record.token_id},
            f"{'success' if new_sync_ok else 'failed'}: upsert token_id={created.record.token_id} {new_sync_detail}".strip(),
        )
        revoked = store.revoke_token(old_token_id)
        if revoked:
            revoke_sync_ok, revoke_sync_detail = sync_token_to_vps("revoke", {"action": "revoke", "token_id": old_token_id})
            ctx.audit(
                "/tokens/revoke",
                {"token_id": old_token_id, "via": "regenerate"},
                f"success: revoked token_id={old_token_id} (regenerate)",
            )
            ctx.audit(
                "/tokens/sync",
                {"token_id": old_token_id},
                f"{'success' if revoke_sync_ok else 'failed'}: revoke token_id={old_token_id} {revoke_sync_detail}".strip(),
            )
        else:
            return ctx.redirect(
                f"/tokens?issued=1&token_id={quote(created.record.token_id)}"
                f"&message=regenerated_revoke_failed&old_token_id={quote(old_token_id)}"
                f"&{sync_redirect_query(created.record.token_id, new_sync_ok, new_sync_detail)}"
            )
        return ctx.redirect(
            f"/tokens?issued=1&token_id={quote(created.record.token_id)}"
            f"&message=regenerated&old_token_id={quote(old_token_id)}"
            f"&{sync_redirect_query(created.record.token_id, new_sync_ok, new_sync_detail)}"
        )
    except Exception as exc:  # noqa: BLE001
        ctx.audit("/tokens/regenerate", {"token_id": old_token_id}, f"failed: {exc}")
        return ctx.redirect(f"/tokens?error={quote(str(exc))}&token_id={quote(old_token_id)}")


def handle_set_raw_post(ctx: object, body: bytes):
    form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    token_id = first_query_value(form, "token_id").strip()
    raw_value = (form.get("raw_value") or [""])[0]
    if not token_id:
        return ctx.redirect("/tokens?error=missing_token_id")
    root = ctx.runtime_root
    ok = token_store(root).set_token_value(token_id, raw_value)
    ctx.audit("/tokens/set-raw", {"token_id": token_id}, "success" if ok else "hash_mismatch")
    if not ok:
        return ctx.redirect(
            f"/tokens/set-raw?token_id={quote(token_id)}&error=Raw+value+did+not+match+the+stored+token+hash.+Row+unchanged."
        )
    return ctx.redirect("/tokens?set_raw=1")
