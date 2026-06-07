from __future__ import annotations

import email.parser
import email.policy
import html
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from event_pipeline import couchdb_config
from event_pipeline.couchdb.system_defaults import load_system_defaults
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture.server import apply_role_category_filter, resolve_display_categories
from ops_dashboard.common import first_query_value
from ops_dashboard.layout import html_page


IMPORT_TOKEN_ENV = "BTQ_BATCH_IMAGE_IMPORT_TOKEN"
FIELD_CAPTURE_URL_ENV = "BTQ_FIELD_CAPTURE_INTERNAL_URL"
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class UploadedImage:
    filename: str
    content_type: str
    content: bytes


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def render(ctx: object = None) -> str:
    query = getattr(ctx, "query", {}) or {}
    selected_site_id = first_query_value(query, "site_id")
    selected_person_id = first_query_value(query, "person_id")
    selected_category = first_query_value(query, "qc_category")
    capture_id = first_query_value(query, "capture_id")
    captured_at = first_query_value(query, "captured_at")
    note = first_query_value(query, "note")
    try:
        sites = load_sites()
        people = load_people()
        data_error = ""
    except Exception as exc:  # noqa: BLE001 - dashboard should render an actionable failure.
        sites = []
        people = []
        data_error = str(exc)
    if not selected_site_id and sites:
        selected_site_id = sites[0]["site_id"]
    categories = load_categories(selected_site_id) if selected_site_id else []

    site_options = "".join(
        f'<option value="{html.escape(site["site_id"])}" {"selected" if site["site_id"] == selected_site_id else ""}>'
        f'{html.escape(site["label"])}</option>'
        for site in sites
    )
    person_options = "".join(
        f'<option value="{html.escape(person["person_id"])}" {"selected" if person["person_id"] == selected_person_id else ""}>'
        f'{html.escape(person["label"])}</option>'
        for person in people
    )
    category_options = "".join(
        f'<option value="{html.escape(category["canonical"])}" {"selected" if category["canonical"] == selected_category else ""}>'
        f'{html.escape(category["label"])}</option>'
        for category in categories
    )
    error_html = f'<section class="error"><p>{html.escape(data_error)}</p></section>' if data_error else ""
    body = f"""
    <header>
      <h1>Batch Image Import</h1>
      <p class="muted">Import WhatsApp fallback photos as one field capture.</p>
    </header>
    {getattr(ctx, "flash", lambda: "")()}
    {error_html}
    <section>
      <form method="post" action="/batch-images/upload" enctype="multipart/form-data">
        <input type="hidden" name="capture_id" value="{html.escape(capture_id)}">
        <label>Site <select name="site_id" required onchange="location.href='/batch-images?site_id='+encodeURIComponent(this.value)+'&person_id='+encodeURIComponent(this.form.person_id.value)">{site_options}</select></label>
        <label>User <select name="person_id" required>{person_options}</select></label>
        <label>QC Category <select name="qc_category" required>{category_options}</select></label>
        <label>Captured At <input name="captured_at" type="datetime-local" value="{html.escape(captured_at)}"></label>
        <label>Note <textarea name="note" rows="4">{html.escape(note)}</textarea></label>
        <label>Images <input type="file" name="images" multiple required accept="image/jpeg,image/png,image/webp"></label>
        <button>Import Images</button>
      </form>
    </section>
    """
    return html_page("Batch Image Import", body, active_section="batch_images")


def load_sites() -> list[dict[str, str]]:
    return [
        {"site_id": site["site_id"], "label": f"{site['canonical']} ({site['site_id']})"}
        for site in CouchDBSiteRegistry().list_sites()
    ]


def load_categories(site_id: str) -> list[dict[str, str]]:
    """Same QC category list the field-capture app shows for a site: per-site
    display_categories → system default → builtin fallback, plus operator-only
    categories (import token is site_admin role)."""
    try:
        site_categories = CouchDBSiteRegistry().get_display_categories(site_id) if site_id else None
    except Exception:
        site_categories = None
    try:
        default_categories = (load_system_defaults() or {}).get("default_display_categories")
    except Exception:
        default_categories = None
    resolved = resolve_display_categories(site_categories, default_categories)
    return apply_role_category_filter(resolved, "site_admin")


def load_people() -> list[dict[str, str]]:
    cfg = couchdb_config.from_env()
    db = couchdb_config.people_database()
    url = f"{cfg.base_url.rstrip('/')}/{url_parse.quote(db, safe='')}/_find"
    payload = {
        "selector": {"synced_from_vault": True, "status": "active"},
        "fields": ["_id", "person_id", "first", "last", "preferred_name", "name", "status", "synced_from_vault"],
        "limit": 1000,
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(cfg.auth_header())
    req = url_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with url_request.urlopen(req, timeout=cfg.timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    people: list[dict[str, str]] = []
    for doc in data.get("docs", []):
        if not isinstance(doc, dict):
            continue
        person_id = str(doc.get("person_id") or doc.get("_id") or "").strip()
        if not person_id:
            continue
        first = str(doc.get("preferred_name") or doc.get("first") or "").strip()
        last = str(doc.get("last") or "").strip()
        name = str(doc.get("name") or "").strip() or f"{first} {last}".strip() or person_id
        people.append({"person_id": person_id, "label": f"{name} ({person_id})"})
    return sorted(people, key=lambda item: item["label"].lower())


def handle_batch_upload_post(ctx: object, body: bytes, content_type: str = "") -> tuple:
    from urllib.parse import urlencode

    try:
        fields: dict[str, str] = {}
        capture_id = ""
        fields, images = parse_batch_multipart(body, content_type)
        if not images:
            raise ValueError("Add at least one image.")
        for image in images:
            if image.content_type not in ALLOWED_IMAGE_TYPES:
                raise ValueError(f"Unsupported image type: {image.content_type}")
        site_id = str(fields.get("site_id") or "").strip()
        person_id = str(fields.get("person_id") or "").strip()
        qc_category = str(fields.get("qc_category") or "").strip()
        if not site_id or not person_id or not qc_category:
            raise ValueError("Site, user, and QC category are required.")
        site_name = CouchDBSiteRegistry().resolve_canonical(site_id)
        if not site_name:
            raise ValueError("Unknown site.")
        exported_at = utc_now_iso()
        captured_at = normalize_captured_at(fields.get("captured_at", ""), exported_at)
        capture_id = str(fields.get("capture_id") or "").strip() or make_capture_id(site_id, exported_at)
        submit_fields = {
            "capture_id": capture_id,
            "site": site_name,
            "site_id": site_id,
            "target_type": "location",
            "target_id": site_id,
            "qc_category": qc_category,
            "captured_at": captured_at,
            "exported_at": exported_at,
            "note": str(fields.get("note") or "").strip(),
            "person_id": person_id,
            "source": "whatsapp_import",
        }
        response = submit_to_field_capture(submit_fields, images)
        status = int(response.get("status_code", 0))
        if status not in {HTTPStatus.CREATED, HTTPStatus.OK}:
            raise ValueError(str(response.get("message") or response.get("error") or f"submit failed ({status})"))
        ctx.audit(
            "/batch-images/upload",
            {"capture_id": capture_id, "site_id": site_id, "person_id": person_id, "photo_count": len(images)},
            "success",
        )
        message = f"imported {len(images)} photo(s) as {capture_id}"
        return ctx.redirect(f"/batch-images?{urlencode({'message': message, 'site_id': site_id})}")
    except Exception as exc:  # noqa: BLE001 - redirect with retry context.
        retry_fields: dict[str, str] = {}
        try:
            retry_fields, _images = parse_batch_multipart(body, content_type)
        except Exception:
            pass
        if capture_id and not retry_fields.get("capture_id"):
            retry_fields["capture_id"] = capture_id
        retry_query = {
            "error": str(exc),
            "site_id": str(retry_fields.get("site_id") or ""),
            "person_id": str(retry_fields.get("person_id") or ""),
            "qc_category": str(retry_fields.get("qc_category") or ""),
            "captured_at": str(retry_fields.get("captured_at") or ""),
            "note": str(retry_fields.get("note") or ""),
            "capture_id": str(retry_fields.get("capture_id") or ""),
        }
        ctx.audit("/batch-images/upload", {"site_id": retry_query["site_id"], "capture_id": retry_query["capture_id"]}, f"failed: {exc}")
        return ctx.redirect(f"/batch-images?{urlencode(retry_query)}")


def parse_batch_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[UploadedImage]]:
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("Expected multipart form data.")
    full_message = b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body
    parser = email.parser.BytesFeedParser(policy=email.policy.compat32)
    parser.feed(full_message)
    msg = parser.close()
    fields: dict[str, str] = {}
    images: list[UploadedImage] = []
    for part in msg.walk():
        name = part.get_param("name", header="content-disposition") or ""
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if name == "images" and filename:
            images.append(UploadedImage(filename=filename, content_type=part.get_content_type() or "", content=payload))
        elif not filename:
            fields[name] = payload.decode("utf-8", errors="replace").strip()
    return fields, images


def normalize_captured_at(raw: str, fallback: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return fallback
    if "T" not in value:
        value = f"{value}T00:00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_capture_id(site_id: str, exported_at: str) -> str:
    stamp = exported_at.replace(":", "").replace("-", "").replace(".", "").replace("Z", "")
    return f"cap-import-{site_id}-{stamp}-{secrets.token_hex(3)}"


def field_capture_submit_url() -> str:
    base = (os.environ.get(FIELD_CAPTURE_URL_ENV) or "").strip().rstrip("/")
    if not base:
        raise ValueError(f"{FIELD_CAPTURE_URL_ENV} is not configured")
    return f"{base}/api/submit"


def import_token() -> str:
    token = (os.environ.get(IMPORT_TOKEN_ENV) or "").strip()
    if not token:
        raise ValueError(f"{IMPORT_TOKEN_ENV} is not configured")
    return token


def submit_to_field_capture(fields: dict[str, str], images: list[UploadedImage]) -> dict[str, object]:
    body, content_type = build_submit_multipart(fields, images)
    url = f"{field_capture_submit_url()}?{url_parse.urlencode({'token': import_token()})}"
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Accept": "application/json",
        # urllib's default "Python-urllib/x" UA is blocked by Cloudflare bot protection
        # in front of the public origin; a descriptive non-bot UA passes.
        "User-Agent": "btq-ops-dashboard/1.0 (+internal batch import)",
    }
    req = url_request.Request(url, data=body, headers=headers, method="POST")
    try:
        with url_request.urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            payload["status_code"] = int(getattr(response, "status", getattr(response, "code", 200)))
            return payload
    except url_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw.strip() or f"HTTP {exc.code}"}
        payload["status_code"] = exc.code
        return payload


def build_submit_multipart(fields: dict[str, str], images: list[UploadedImage]) -> tuple[bytes, str]:
    boundary = f"----btq-batch-image-import-{secrets.token_hex(12)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for image in images:
        filename = Path(image.filename.replace("\\", "/")).name or "image"
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="photos"; filename="{filename}"\r\n'.encode("utf-8"))
        chunks.append(f"Content-Type: {image.content_type}\r\n\r\n".encode("utf-8"))
        chunks.append(image.content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
