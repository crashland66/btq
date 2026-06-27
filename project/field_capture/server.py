from __future__ import annotations

import argparse
import html
import re
import json
import mimetypes
import os
import sys
from http.cookies import SimpleCookie
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from capture_ingest import (
    IMPORT_MAX_IMAGES,
    IngestLimits,
    SubmissionError,
    UploadedFile,
    UploadedPhoto,
    build_capture_document_envelope,
    build_capture_media_records,
    local_date_from_iso,
    lookup_existing_capture,
    normalize_capture_id,
    parse_multipart,
    read_multipart_submission as read_capture_multipart_submission,
    utc_now_iso,
    validate_uploaded_audio as capture_ingest_validate_uploaded_audio,
    validate_uploaded_photos as capture_ingest_validate_uploaded_photos,
    write_capture_media,
)
from config import get_config
from event_pipeline import couchdb_config as couchdb_config_module
from event_pipeline.couchdb.system_defaults import load_system_defaults
from event_pipeline.couchdb_capture_reader import (
    CouchDBCaptureReaderError,
    query_captures_by_site_id,
    query_captures_by_target,
    query_capture_target_by_upload_id,
    query_site_id_by_upload_id,
)
from event_pipeline.couchdb_capture_writer import (
    CouchDBCaptureWriterError,
    get_field_capture_document,
    put_field_capture_document,
)
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from btq_vault.couch_store import CouchDBEntityStore
from field_capture.auth import (
    AuthorizedSession,
    TokenStore,
    authorize_token,
    load_person_from_canonical,
    normalize_site_ids,
    parse_timestamp,
    site_from_registry,
    token_records_as_rows,
)
from field_capture.display_categories import (
    BUILTIN_FALLBACK_CATEGORIES,
    OPERATOR_ONLY_CANONICALS,
    OPERATOR_ONLY_CATEGORIES,
    OPERATOR_ONLY_LABELS,
    apply_role_category_filter,
    canonicalize_qc_category,
    normalize_display_categories,
    resolve_display_categories,
)
from field_capture.identity import first_name_from_canonical
from field_capture import my_submissions as my_submissions_module
from field_capture.action_candidates import couchdb_candidate_payloads
from field_capture import photo_vision as photo_vision_module
from field_capture.prospects import TERMINAL_PROSPECT_STATUSES, list_active_prospects, load_prospect
from field_capture.site_viewer import SiteUploadsNotFound, build_prospect_payload, build_site_payload, render_prospect_page, render_site_page, resolve_media_request
from queue_spec import JOB_RETARGET_CAPTURE
from shared_pwa.assets import render_service_worker_bytes, shared_db_bytes
from vault_errors import NotFoundError


PHOTO_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
AUDIO_MIME_EXTENSIONS = {
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
AUDIO_ALLOWED_EXTENSIONS = {
    "audio/webm": {".webm"},
    "audio/mp4": {".m4a"},
    "audio/m4a": {".m4a"},
    "audio/wav": {".wav"},
    "audio/x-wav": {".wav"},
}


def field_capture_ingest_limits(server: object, *, max_images: int | None = None) -> IngestLimits:
    return IngestLimits(
        max_images=int(max_images if max_images is not None else getattr(server, "max_images")),
        max_upload_bytes=int(getattr(server, "max_upload_bytes")),
        request_max_bytes=int(getattr(server, "request_max_bytes")),
        photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
        audio_mime_extensions=AUDIO_MIME_EXTENSIONS,
        audio_allowed_extensions=AUDIO_ALLOWED_EXTENSIONS,
    )


def resolve_site_from_canonical(site_registry: object | None, site_id: str) -> object:
    registry = site_registry if site_registry is not None else CouchDBSiteRegistry()
    try:
        site = site_from_registry(registry, site_id)
    except Exception as exc:
        raise NotFoundError(f"Unknown site: {site_id}") from exc
    if site is None:
        raise NotFoundError(f"Unknown site: {site_id}")
    return site


@dataclass(frozen=True)
class SubmissionAttribution:
    person_id: str
    person_name: str
    source: str


def resolve_capture_guidance(site_guidance: object, default_guidance: object) -> str:
    if isinstance(site_guidance, str) and site_guidance.strip():
        return site_guidance.strip()
    if isinstance(default_guidance, str) and default_guidance.strip():
        return default_guidance.strip()
    return ""


def resolve_submit_categories(site_registry, site_id, log) -> list[dict[str, str]]:
    registry = site_registry
    display_categories = None
    if registry is not None:
        try:
            display_categories = registry.get_display_categories(site_id)
        except Exception as error:
            log("WARNING: display_categories lookup failed site_id=%s error=%s", site_id, error)
    default_categories = None
    try:
        system_defaults = load_system_defaults()
        default_categories = system_defaults.get("default_display_categories") if isinstance(system_defaults, dict) else None
    except Exception as error:
        log("WARNING: system_defaults unavailable for /api/submit: %s", error)
    return apply_role_category_filter(resolve_display_categories(display_categories, default_categories), "site_admin")


@dataclass(frozen=True)
class CapturePaths:
    capture_id: str
    site_id: str | None
    target_type: str
    target_id: str
    site: str
    qc_category: str
    captured_at: str
    exported_at: str
    note: str
    upload_root: Path
    photo_records: list[dict[str, str]]
    audio_records: list[dict[str, object]]


class FieldCaptureServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        token_store: TokenStore,
        upload_dir: Path,
        max_images: int,
        max_upload_bytes: int,
        request_max_bytes: int,
        couchdb_config: couchdb_config_module.CouchDBConfig,
        couchdb_database: str = couchdb_config_module.DEFAULT_FIELD_CAPTURES_DB,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.token_store = token_store
        self.upload_dir = upload_dir.expanduser().resolve(strict=False)
        self.max_images = max_images
        self.max_upload_bytes = max_upload_bytes
        self.request_max_bytes = request_max_bytes
        self.couchdb_config = couchdb_config
        self.couchdb_database = couchdb_database


class FieldCaptureHandler(BaseHTTPRequestHandler):
    server: FieldCaptureServer

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/health":
            self.write_json({"status": "ok"})
            return
        if path == "/api/session":
            self.handle_session()
            return
        if path == "/api/my-submissions":
            self.handle_my_submissions()
            return
        if path == "/api/retarget-preview":
            self.handle_retarget_preview(parts.query)
            return
        if path == "/" or path == "":
            self.handle_root()
            return
        if path == "/manifest.webmanifest":
            self.handle_manifest(parts.query)
            return
        if path.startswith("/site/"):
            self.handle_site_view(path.removeprefix("/site/"), parts.query)
            return
        if path.startswith("/prospect/"):
            self.handle_prospect_view(path.removeprefix("/prospect/"), parts.query)
            return
        if path.startswith("/media/"):
            self.handle_media(path.removeprefix("/media/"))
            return
        if self.try_serve_static(path):
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def try_serve_static(self, path: str) -> bool:
        """Serve a file from project/field_capture/public/ if path matches.

        Restores the static-serving behavior that was removed in
        commit 1bd4df4 (prompt 12) but never actually shipped via
        Caddy. Bookmarks to /?token=... and the PWA shell
        (/manifest.webmanifest, /app.js, /styles.css, /assets/*,
        /robots.txt, /favicon.ico) rely on this.
        """
        public_root = Path(__file__).resolve().parent / "public"
        shared_assets = {
            "/db.js": ("application/javascript; charset=utf-8", shared_db_bytes),
            "/sw.js": ("application/javascript; charset=utf-8", lambda: render_service_worker_bytes("field_capture")),
        }
        shared = shared_assets.get(path)
        if shared is not None:
            content_type, body_factory = shared
            try:
                body = body_factory()
            except OSError:
                return False
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(body)
            return True
        if path in ("/", ""):
            target_rel = "index.html"
        else:
            target_rel = path.lstrip("/")
        if not target_rel or ".." in target_rel.split("/"):
            return False
        candidate = (public_root / target_rel).resolve(strict=False)
        try:
            candidate.relative_to(public_root.resolve(strict=False))
        except ValueError:
            return False
        if not candidate.is_file():
            return False
        content_type = mimetypes.guess_type(candidate.name)[0]
        if content_type is None:
            if candidate.suffix == ".webmanifest":
                content_type = "application/manifest+json"
            else:
                content_type = "application/octet-stream"
        if content_type.startswith(("text/", "application/javascript", "application/json", "application/manifest")):
            content_type = f"{content_type}; charset=utf-8" if "charset" not in content_type else content_type
        try:
            body = candidate.read_bytes()
        except OSError:
            return False
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/submit":
            self.handle_submit()
            return
        if path == "/api/retarget":
            self.handle_retarget()
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        sanitized_args = tuple(sanitize_token_text(arg) if isinstance(arg, str) else arg for arg in args)
        super().log_message(format, *sanitized_args)

    def end_headers(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/") and not path.startswith("/site/") and not path.startswith("/prospect/") and not path.startswith("/media/"):
            self.send_header("Cache-Control", "no-cache, max-age=0")
        super().end_headers()

    def extract_token(self) -> str | None:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return authorization.removeprefix("Bearer ").strip()
        query = parse_qs(urlsplit(self.path).query)
        token_values = query.get("token", [])
        if token_values:
            return token_values[0].strip()
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            morsel = cookie.get("btq_field_viewer_token")
            if morsel is not None:
                return morsel.value.strip()
        return None

    def handle_session(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        system_defaults = {}
        try:
            system_defaults = load_system_defaults()
        except Exception as error:
            self.log_message("WARNING: system_defaults unavailable for /api/session: %s", error)
        registry = getattr(self.server, "site_registry", None)
        if registry is None and os.environ.get("BTQ_COUCHDB_URL"):
            try:
                registry = CouchDBSiteRegistry()
            except Exception as error:
                self.log_message("WARNING: site registry unavailable for /api/session: %s", error)
        default_guidance = system_defaults.get("default_capture_guidance") if isinstance(system_defaults, dict) else None
        default_categories = system_defaults.get("default_display_categories") if isinstance(system_defaults, dict) else None
        prospects_payload: list[dict[str, str]] = []
        if session.record.role == "site_admin":
            try:
                prospects_payload = [
                    {
                        "prospect_id": str(prospect.get("prospect_id") or ""),
                        "name": str(prospect.get("name") or ""),
                        "address": str(prospect.get("address") or ""),
                        "account": str(prospect.get("account") or ""),
                        "viewer_url": f"/prospect/{prospect.get('prospect_id')}?token={token_value}",
                    }
                    for prospect in list_active_prospects(self.server.couchdb_config)
                    if str(prospect.get("prospect_id") or "").strip() and str(prospect.get("name") or "").strip()
                ]
            except Exception as error:
                self.log_message("WARNING: prospects unavailable for /api/session: %s", error)
        self.write_json(
            {
                "person": {
                    "person_id": str(session.person.person_id),
                    "name": session.person.canonical_name,
                    "first": first_name_from_canonical(session.person.canonical_name),
                },
                "token": {
                    "token_id": session.record.token_id,
                    "label": session.record.label,
                    "expires_at": session.record.expires_at,
                    "token_type": session.record.token_type,
                    "can_submit": session.record.can_submit,
                    "can_view_site": session.record.can_view_site,
                    "role": session.record.role,
                },
                "sites": [
                    self.session_site_payload(site, token_value, registry, default_guidance, default_categories, session.record.role)
                    for site in session.sites
                ],
                "prospects": prospects_payload,
            }
        )

    def handle_my_submissions(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        person_id = str(session.person.person_id) if session.person else ""
        if not person_id:
            self.write_json({"submissions": [], "quality_summary": None})
            return
        runtime_root = self.server.upload_dir.parent
        photo_vision_dir = photo_vision_module.default_photo_vision_dir(runtime_root)
        candidates_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
        try:
            capture_docs = query_captures_by_person_id(
                self.server.couchdb_config,
                person_id,
                database=self.server.couchdb_database,
            )
        except Exception as error:
            self.log_error("CouchDB my-submissions lookup unavailable person_id=%s error=%s", person_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        submissions = my_submissions_module.collect_my_submissions(
            person_id,
            capture_docs,
            upload_dir=self.server.upload_dir,
            photo_vision_dir=photo_vision_dir,
            candidates_dir=candidates_dir,
            can_retarget=session.record.role == "site_admin",
        )
        quality_summary = my_submissions_module.rolling_quality_summary(
            person_id,
            capture_docs,
            photo_vision_dir,
        )
        self.write_json({"submissions": submissions, "quality_summary": quality_summary})

    def _authorize_site_admin(self) -> AuthorizedSession | None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return None
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return None
        if str(session.record.role) != "site_admin":
            self.write_json(
                {"error": "role_not_allowed", "message": "Retargeting requires site_admin role."},
                HTTPStatus.FORBIDDEN,
            )
            return None
        return session

    def handle_retarget_preview(self, raw_query: str) -> None:
        session = self._authorize_site_admin()
        if session is None:
            return
        capture_id = str((parse_qs(raw_query or "").get("capture_id") or [""])[0]).strip()
        if not capture_id:
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            capture = get_field_capture_document(
                self.server.couchdb_config,
                self.server.couchdb_database,
                capture_id,
            )
        except CouchDBCaptureWriterError as error:
            self.log_error("CouchDB capture lookup for retarget preview failed capture_id=%s error=%s", capture_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if capture is None:
            self.write_json({"error": "capture_not_found"}, HTTPStatus.NOT_FOUND)
            return

        stage = _retarget_stage_for_capture(self.server, capture_id)
        candidates = _retarget_candidates_for_capture(self.server, capture_id)
        pending_count = sum(1 for candidate in candidates if candidate["auto_action_on_retarget"] == "withdraw")
        warnings: list[str] = []
        if pending_count:
            suffix = "candidate will" if pending_count == 1 else "candidates will"
            warnings.append(f"{pending_count} pending {suffix} be auto-withdrawn on retarget.")
        if stage == "acted_on":
            warnings.append("This capture has already been acted on. Resolve via correction notes instead.")

        self.write_json(
            {
                "capture_id": capture_id,
                "current_target": _current_target_summary(capture, session, self.server.couchdb_config),
                "stage": stage,
                "retargetable": stage != "acted_on",
                "downstream": {
                    "candidates": candidates,
                    "journal_projections": [],
                    # TODO: Count hard-coded media URLs once export artifacts track capture lineage.
                    "media_urls_in_exports": 0,
                },
                "warnings": warnings,
            }
        )

    def handle_retarget(self) -> None:
        session = self._authorize_site_admin()
        if session is None:
            return

        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "0")
        except ValueError:
            self.write_json({"error": "invalid_content_length"}, HTTPStatus.BAD_REQUEST)
            return
        if content_length <= 0 or content_length > 10_000:
            self.write_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            body = self.rfile.read(content_length)
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.write_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return

        capture_id = str(payload.get("capture_id") or "").strip()
        new_target_type = str(payload.get("new_target_type") or "").strip()
        new_target_id = str(payload.get("new_target_id") or "").strip()
        if not capture_id or new_target_type not in ("location", "prospect"):
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return
        if invalid_retarget_target_id(new_target_id):
            self.write_json({"error": "invalid_target_id"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            capture = get_field_capture_document(
                self.server.couchdb_config,
                self.server.couchdb_database,
                capture_id,
            )
        except CouchDBCaptureWriterError as error:
            self.log_error("CouchDB capture lookup for retarget failed capture_id=%s error=%s", capture_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if capture is None:
            self.write_json({"error": "capture_not_found"}, HTTPStatus.NOT_FOUND)
            return

        capture_stage = _retarget_stage_for_capture(self.server, capture_id)
        if capture_stage == "acted_on":
            self.write_json(
                {
                    "error": "stage_not_retargetable",
                    "message": "This capture has already been acted on. Resolve via correction notes instead.",
                },
                HTTPStatus.CONFLICT,
            )
            return

        target_error = self.validate_retarget_destination(session, new_target_type, new_target_id)
        if target_error is not None:
            error_code, status, message = target_error
            payload_out: dict[str, object] = {"error": error_code}
            if message:
                payload_out["message"] = message
            self.write_json(payload_out, status)
            return

        job = {
            "job_id": f"{utc_now_iso().replace(':', '-').replace('.', '-')}__retarget-capture-{safe_slug(capture_id)}",
            "job_type": JOB_RETARGET_CAPTURE,
            "payload": {
                "capture_id": capture_id,
                "new_target_type": new_target_type,
                "new_target_id": new_target_id,
                "actor": session.person.canonical_name,
            },
        }
        stage_queue_job(self.server, job)

        self.write_json(
            {
                "status": "queued",
                "job_id": job["job_id"],
                "capture_id": capture_id,
                "new_target_type": new_target_type,
                "new_target_id": new_target_id,
            },
            HTTPStatus.ACCEPTED,
        )

    def validate_retarget_destination(
        self,
        session: AuthorizedSession,
        new_target_type: str,
        new_target_id: str,
    ) -> tuple[str, HTTPStatus, str] | None:
        if new_target_type == "location":
            if not any(str(site.site_id) == new_target_id for site in session.sites):
                return ("site_not_registered", HTTPStatus.BAD_REQUEST, f"Site is not available: {new_target_id}")
            return None
        try:
            prospect = load_prospect(self.server.couchdb_config, new_target_id)
        except Exception:
            return ("prospect_lookup_failed", HTTPStatus.SERVICE_UNAVAILABLE, "Prospect lookup unavailable")
        if prospect is None:
            return ("prospect_not_found", HTTPStatus.BAD_REQUEST, f"Prospect not found: {new_target_id}")
        if str(prospect.get("status") or "") in TERMINAL_PROSPECT_STATUSES:
            return ("prospect_terminal", HTTPStatus.CONFLICT, f"Prospect is terminal: {new_target_id}")
        return None

    def handle_manifest(self, raw_query: str) -> None:
        """Serve the PWA manifest dynamically.

        iOS isolates localStorage AND cookies between Safari and the standalone
        PWA container that "Add to Home Screen" creates. The only durable
        per-user state that crosses the boundary is what's baked into the
        manifest's start_url at install time. If the request URL includes
        ?token=<value>, embed it so the home-screen icon launches
        /?token=<value> and the SPA picks the token off the URL on launch.
        """
        manifest_path = Path(__file__).resolve().parent / "public" / "manifest.webmanifest"
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.write_json({"error": "manifest_unavailable"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        tokens = parse_qs(raw_query or "").get("token", [])
        if tokens and tokens[0]:
            data["start_url"] = f"/?token={quote(tokens[0])}"
        body = json.dumps(data).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/manifest+json")
        # no-store so iOS re-fetches at Add-to-Home-Screen time; otherwise a
        # cached token-less manifest would defeat the dynamic start_url.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_root(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_root_landing_not_found()
            return
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        sites = list(session.sites)
        if not sites:
            self.write_root_landing_not_found(message="This token has no sites assigned.")
            return
        if len(sites) == 1:
            site_id = str(sites[0].site_id)
            location = f"/site/{site_id}?token={token_value}"
            body = b"Redirecting to your site..."
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.write_root_site_picker(token_value, sites)

    def write_root_site_picker(self, token_value: str, sites: list[object]) -> None:
        items = "\n".join(
            f'<li><a href="/site/{html.escape(str(site.site_id))}?token={html.escape(token_value)}">'
            f'{html.escape(str(getattr(site, "account", "") or ""))}'
            f'{" - " if getattr(site, "account", None) and getattr(site, "canonical_name", None) else ""}'
            f'{html.escape(str(getattr(site, "canonical_name", "") or ""))}'
            f' (site {html.escape(str(site.site_id))})'
            "</a></li>"
            for site in sites
        )
        body = (
            '<!doctype html><html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive">'
            "<title>Pick a site</title>"
            "<style>body{font-family:-apple-system,Segoe UI,sans-serif;max-width:520px;margin:48px auto;padding:0 16px;color:#1f2933}"
            "h1{font-size:1.25rem;margin:0 0 16px}ul{padding-left:0;list-style:none}"
            "li{margin:8px 0}a{display:block;padding:12px 14px;border:1px solid #d9e2ec;border-radius:8px;text-decoration:none;color:#1e40af}"
            "a:hover{background:#eef4ff}</style>"
            "</head><body>"
            "<h1>Your sites</h1>"
            f"<ul>{items}</ul>"
            "</body></html>"
        )
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_root_landing_not_found(self, *, message: str = "Use a site URL like /site/<id>?token=<your-token>.") -> None:
        body = (
            '<!doctype html><html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow,noarchive">'
            "<title>Site URL required</title>"
            "<style>body{font-family:-apple-system,Segoe UI,sans-serif;max-width:520px;margin:48px auto;padding:0 16px;color:#1f2933}"
            "h1{font-size:1.25rem;margin:0 0 16px}p{line-height:1.5}</style>"
            "</head><body>"
            "<h1>Site URL required</h1>"
            f"<p>{html.escape(message)}</p>"
            "</body></html>"
        )
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def session_site_payload(
        self,
        site: object,
        token_value: str,
        registry: object | None,
        default_guidance: object,
        default_categories: object,
        role: str,
    ) -> dict[str, object]:
        site_id = str(site.site_id)
        capture_guidance = None
        display_categories = None
        if registry is not None:
            try:
                capture_guidance = registry.get_capture_guidance(site_id)
            except Exception as error:
                self.log_message("WARNING: capture_guidance lookup failed site_id=%s error=%s", site_id, error)
            try:
                display_categories = registry.get_display_categories(site_id)
            except Exception as error:
                self.log_message("WARNING: display_categories lookup failed site_id=%s error=%s", site_id, error)
        return {
            "site_id": site_id,
            "name": site.canonical_name,
            "account": site.account,
            "viewer_url": f"/site/{site.site_id}?token={token_value}",
            "capture_guidance": resolve_capture_guidance(capture_guidance, default_guidance),
            "display_categories": apply_role_category_filter(
                resolve_display_categories(display_categories, default_categories),
                role,
            ),
        }

    def handle_site_view(self, site_id: str, raw_query: str) -> None:
        site_id = site_id.strip("/")
        if not site_id:
            self.write_json({"error": "missing_site_id"}, HTTPStatus.NOT_FOUND)
            return
        site_name = ""
        known_site_ids: tuple[str, ...] | None = None
        try:
            site = resolve_site_from_canonical(getattr(self.server, "site_registry", None), site_id)
            known_site_ids = (str(site.site_id),)
            site_name = site.canonical_name
        except NotFoundError:
            self.write_json({"error": "site_uploads_not_found"}, HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(raw_query)
        wants_json = query.get("format", [""])[0] == "json" or "application/json" in self.headers.get("Accept", "")
        session = self.authorize_site_view(site_id)
        if session is None:
            if wants_json:
                self.write_json({"error": "access_required"}, HTTPStatus.FORBIDDEN, noindex=True)
            else:
                self.write_access_required(site_id)
            return
        try:
            capture_reader = getattr(self.server, "capture_reader", query_captures_by_site_id)
            captures = capture_reader(
                self.server.couchdb_config,
                site_id,
                database=self.server.couchdb_database,
            )
        except CouchDBCaptureReaderError as error:
            self.log_error("CouchDB capture lookup unavailable site_id=%s error=%s", site_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                noindex=True,
            )
            return
        try:
            payload = build_site_payload(
                site_id,
                captures,
                self.server.upload_dir,
                known_site_ids=known_site_ids,
                site_name=site_name,
            )
        except SiteUploadsNotFound:
            self.write_json({"error": "site_uploads_not_found"}, HTTPStatus.NOT_FOUND)
            return
        if wants_json:
            self.write_json(payload, noindex=True)
            return
        body = render_site_page(site_id, payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_viewer_security_headers()
        token_value = self.extract_token()
        if token_value:
            self.send_viewer_cookie(token_value)
        self.end_headers()
        self.wfile.write(body)

    def handle_prospect_view(self, prospect_id: str, raw_query: str) -> None:
        prospect_id = prospect_id.strip("/")
        if not prospect_id:
            self.write_json({"error": "missing_prospect_id"}, HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(raw_query)
        wants_json = query.get("format", [""])[0] == "json" or "application/json" in self.headers.get("Accept", "")
        try:
            prospect = load_prospect(self.server.couchdb_config, prospect_id)
        except Exception as error:
            self.log_error("CouchDB prospect lookup unavailable prospect_id=%s error=%s", prospect_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB prospect lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                noindex=True,
            )
            return
        if prospect is None:
            self.write_json({"error": "prospect_not_found"}, HTTPStatus.NOT_FOUND)
            return
        session = self.authorize_prospect_view(prospect_id)
        if session is None:
            if wants_json:
                self.write_json({"error": "access_required"}, HTTPStatus.FORBIDDEN, noindex=True)
            else:
                self.write_access_required(prospect_id)
            return
        try:
            capture_reader = getattr(self.server, "prospect_capture_reader", query_captures_by_target)
            captures = capture_reader(
                self.server.couchdb_config,
                "prospect",
                prospect_id,
                database=self.server.couchdb_database,
            )
        except CouchDBCaptureReaderError as error:
            self.log_error("CouchDB capture lookup unavailable prospect_id=%s error=%s", prospect_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                noindex=True,
            )
            return
        payload = build_prospect_payload(prospect, captures, self.server.upload_dir)
        if wants_json:
            self.write_json(payload, noindex=True)
            return
        body = render_prospect_page(prospect_id, payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_viewer_security_headers()
        token_value = self.extract_token()
        if token_value:
            self.send_viewer_cookie(token_value)
        self.end_headers()
        self.wfile.write(body)

    def handle_media(self, media_path: str) -> None:
        try:
            path = resolve_media_request(media_path, self.server.upload_dir)
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            media_upload_id = path.expanduser().resolve(strict=False).relative_to(self.server.upload_dir).as_posix()
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        target_lookup_reader = getattr(
            self.server,
            "target_lookup_reader",
            query_capture_target_by_upload_id,
        )
        try:
            target = target_lookup_reader(
                self.server.couchdb_config,
                media_upload_id,
                database=self.server.couchdb_database,
            )
        except CouchDBCaptureReaderError as error:
            self.log_error("CouchDB media target lookup unavailable upload_id=%s error=%s", media_upload_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
                noindex=True,
            )
            return
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        target_type = target["target_type"]
        target_id = target["target_id"]

        if target_type == "location":
            if self.authorize_site_view(target["site_id"]) is None:
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_viewer_security_headers()
                self.end_headers()
                self.wfile.write(b"access required")
                return
        elif target_type == "prospect":
            if self.authorize_prospect_view(target_id) is None:
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_viewer_security_headers()
                self.end_headers()
                self.wfile.write(b"access required")
                return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_viewer_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def handle_submit(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        # The dedicated batch-image import token may submit a large WhatsApp-fallback
        # dump as one capture; regular capture tokens keep the app's small ceiling.
        effective_max_images = (
            max(self.server.max_images, IMPORT_MAX_IMAGES)
            if can_override_submission_attribution(session)
            else self.server.max_images
        )
        try:
            fields, photos, audio_files = self.read_multipart_submission(max_images=effective_max_images)
            self.validate_submit_authorization(session, fields)
            # Idempotency: if a document with this capture_id already
            # exists, return the original success response without
            # writing media or PUTing a new revision. This lets the PWA
            # safely retry POSTs after network-loss-during-response.
            capture_id = str(fields.get("capture_id") or "").strip()
            if capture_id:
                try:
                    existing = lookup_existing_capture(
                        capture_id,
                        lambda existing_capture_id: get_field_capture_document(
                            self.server.couchdb_config,
                            self.server.couchdb_database,
                            existing_capture_id,
                        ),
                    )
                except CouchDBCaptureWriterError as error:
                    self.log_error(
                        "CouchDB capture lookup failed capture_id=%s error=%s",
                        capture_id, error,
                    )
                    self.write_json(
                        {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                if existing is not None:
                    existing_photos = existing.get("photos") if isinstance(existing.get("photos"), list) else []
                    existing_audio = existing.get("audio") if isinstance(existing.get("audio"), list) else []
                    self.write_json(
                        {
                            "status": "submitted",
                            "job_id": str(fields.get("job_id") or ""),
                            "capture_id": capture_id,
                            "couchdb_doc_id": str(existing.get("_id") or capture_id),
                            "photo_count": len(existing_photos),
                            "audio_count": len(existing_audio),
                            "idempotent_replay": True,
                        },
                        HTTPStatus.OK,
                    )
                    return
            attribution = resolve_submission_attribution(session, fields)
            job = build_submission_job(fields, photos, audio_files, session, self.server.upload_dir, attribution=attribution)
        except SubmissionError as error:
            self.write_json({"error": error.code, "message": error.message}, error.status)
            return
        try:
            payload = job.get("payload")
            if not isinstance(payload, dict):
                raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_payload", "Invalid payload")
            photo_records = payload.get("photos")
            audio_records = payload.get("audio", [])
            if not isinstance(photo_records, list) or not isinstance(audio_records, list):
                raise SubmissionError(HTTPStatus.BAD_REQUEST, "invalid_media", "Invalid media")
            write_capture_media(photo_records + audio_records, photos + audio_files, self.server.upload_dir)
        except SubmissionError as error:
            self.write_json({"error": error.code, "message": error.message}, error.status)
            return
        metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
        fields_for_doc = dict(fields)
        fields_for_doc["capture_id"] = str(metadata.get("capture_id") or fields_for_doc.get("capture_id") or "")
        doc = build_capture_document(fields_for_doc, photos, audio_files, session, self.server.upload_dir, attribution=attribution)
        try:
            response = put_field_capture_document(self.server.couchdb_config, doc, database=self.server.couchdb_database)
        except CouchDBCaptureWriterError as error:
            self.log_error("CouchDB capture ingress unavailable capture_id=%s error=%s", doc.get("capture_id", ""), error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        couchdb_doc_id = str(response.get("id") or doc["_id"])
        self.write_json(
            {
                "status": "submitted",
                "job_id": job["job_id"],
                "capture_id": str(job.get("metadata", {}).get("capture_id", "")) if isinstance(job.get("metadata"), dict) else "",
                "couchdb_doc_id": couchdb_doc_id,
                "photo_count": len(photos),
                "audio_count": len(audio_files),
            },
            HTTPStatus.CREATED,
        )

    def read_multipart_submission(
        self, *, max_images: int | None = None
    ) -> tuple[dict[str, str], list[UploadedFile], list[UploadedFile]]:
        return read_capture_multipart_submission(
            self.headers.get("Content-Length"),
            self.headers.get("Content-Type", ""),
            self.rfile.read,
            field_capture_ingest_limits(self.server, max_images=max_images),
        )

    def authorize_site_view(self, site_id: str) -> AuthorizedSession | None:
        token_value = self.extract_token()
        if not token_value:
            return None
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            return None
        if session is None or not session.record.can_view_site:
            return None
        if site_id not in authorized_site_ids(session):
            return None
        return session

    def authorize_prospect_view(self, prospect_id: str) -> AuthorizedSession | None:
        token_value = self.extract_token()
        if not token_value or not prospect_id:
            return None
        try:
            session = authorize_token(self.server.token_store, token_value)
        except Exception:
            return None
        if session is None or not session.record.can_view_site or session.record.role != "site_admin":
            return None
        return session

    def validate_submit_authorization(self, session: AuthorizedSession, fields: dict[str, str]) -> None:
        if not session.record.can_submit:
            raise SubmissionError(HTTPStatus.FORBIDDEN, "submit_not_allowed", "This token cannot submit captures")
        site_id = fields.get("site_id", "").strip()
        target_type = fields.get("target_type", "").strip() or "location"
        target_id = fields.get("target_id", "").strip()
        if target_type == "prospect":
            if session.record.role != "site_admin":
                raise SubmissionError(HTTPStatus.FORBIDDEN, "prospect_not_allowed", "This token cannot submit for that prospect")
            prospect = load_prospect(self.server.couchdb_config, target_id)
            if prospect is None:
                raise SubmissionError(HTTPStatus.NOT_FOUND, "prospect_not_found", "Prospect not found")
            if str(prospect.get("status") or "") in TERMINAL_PROSPECT_STATUSES:
                raise SubmissionError(HTTPStatus.FORBIDDEN, "prospect_not_allowed", "This token cannot submit for that prospect")
            return
        if target_type != "location":
            raise SubmissionError(HTTPStatus.FORBIDDEN, "target_not_allowed", "This token cannot submit for that target")
        if target_id:
            site_id = target_id
        if not site_id or site_id not in authorized_site_ids(session):
            raise SubmissionError(HTTPStatus.FORBIDDEN, "site_not_allowed", "This token cannot submit for that site")
        qc_category = canonicalize_qc_category(
            fields.get("qc_category", ""),
            self.resolve_submit_categories(site_id),
        )
        if qc_category in OPERATOR_ONLY_CANONICALS and session.record.role != "site_admin":
            raise SubmissionError(
                HTTPStatus.FORBIDDEN,
                "category_not_allowed",
                "This token cannot submit captures in this category",
            )

    def resolve_submit_categories(self, site_id: str) -> list[dict[str, str]]:
        return resolve_submit_categories(getattr(self.server, "site_registry", None), site_id, self.log_message)

    def send_viewer_security_headers(self) -> None:
        self.send_header("X-Robots-Tag", "noindex,nofollow,noarchive")
        self.send_header("Cache-Control", "private, no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_viewer_cookie(self, token_value: str) -> None:
        cookie = SimpleCookie()
        cookie["btq_field_viewer_token"] = token_value
        cookie["btq_field_viewer_token"]["path"] = "/"
        cookie["btq_field_viewer_token"]["httponly"] = True
        cookie["btq_field_viewer_token"]["samesite"] = "Strict"
        self.send_header("Set-Cookie", cookie.output(header="").strip())

    def write_access_required(self, site_id: str) -> None:
        body = render_access_required_page(site_id).encode("utf-8")
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_viewer_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def write_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK, *, noindex: bool = False) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if noindex:
            self.send_viewer_security_headers()
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def invalid_retarget_target_id(value: str) -> bool:
    return not value or value.lower() in {"null", "none", "discard"}


def stage_queue_job(server: object, job: dict[str, object]) -> Path:
    runtime_root = Path(getattr(server, "upload_dir")).expanduser().resolve(strict=False).parent
    queue_dir = runtime_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    safe_job_id = safe_slug(str(job.get("job_id") or "retarget-capture"))
    queue_path = queue_dir / f"{safe_job_id}.json"
    temp_path = queue_path.with_name(f".{queue_path.name}.tmp")
    temp_path.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(queue_path)
    return queue_path


def _retarget_stage_for_capture(server: object, capture_id: str) -> str:
    runtime_root = Path(getattr(server, "upload_dir")).expanduser().resolve(strict=False).parent
    candidates_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
    stage, _label = my_submissions_module.derive_track_b_stage(capture_id, candidates_dir)
    return stage


def _retarget_candidates_for_capture(server: object, capture_id: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for _path, candidate in couchdb_candidate_payloads(status=None):
        if str(candidate.get("capture_id") or "") != capture_id:
            continue
        status = str(candidate.get("status") or "")
        candidates.append({
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "kind": str(candidate.get("job_type") or candidate.get("candidate_type") or candidate.get("type") or ""),
            "stage": status,
            "auto_action_on_retarget": "withdraw" if status == "pending_review" else "none",
        })
    return candidates


def _current_target_summary(
    capture: dict[str, object],
    session: AuthorizedSession,
    config: couchdb_config_module.CouchDBConfig,
) -> dict[str, str]:
    target_type = str(capture.get("target_type") or "location")
    target_id = str(capture.get("target_id") or "")
    if not target_id and target_type == "location":
        target_id = str(capture.get("site_id") or "")
    label = str(capture.get("site_name") or capture.get("site") or target_id)
    if target_type == "location":
        for site in session.sites:
            if str(site.site_id) == target_id:
                label = site.canonical_name
                break
    elif target_type == "prospect" and target_id:
        try:
            prospect = load_prospect(config, target_id)
            if prospect is not None:
                label = str(prospect.get("name") or label)
        except Exception:
            # Deliberate UI fallback: label lookup must not block target summary rendering.
            pass
    return {"target_type": target_type, "target_id": target_id, "label": label}


def query_captures_by_person_id(
    config: couchdb_config_module.CouchDBConfig,
    person_id: str,
    *,
    database: str,
    limit: int = 50,
) -> list[dict]:
    """Query CouchDB btq_field_captures for documents matching person_id."""
    from urllib import error as url_error, request as url_request

    normalized = str(person_id).strip()
    if not normalized:
        return []
    # No "sort" key — CouchDB requires a Mango index to sort, and we don't
    # have one on person_id+captured_at. collect_my_submissions sorts in Python.
    payload = json.dumps({
        "selector": {"person_id": normalized},
        "limit": limit,
    }).encode("utf-8")
    db = quote(database, safe="")
    url = f"{config.base_url}/{db}/_find"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers.update(config.auth_header())
    req = url_request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with url_request.urlopen(req, timeout=config.timeout) as response:
            body = response.read()
            data = json.loads(body)
    except url_error.HTTPError as exc:
        raise CouchDBCaptureReaderError(f"CouchDB _find HTTP {exc.code}") from exc
    except Exception as exc:
        raise CouchDBCaptureReaderError(f"CouchDB _find failed: {exc}") from exc
    docs = data.get("docs")
    if not isinstance(docs, list):
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def authorized_site_ids(session: AuthorizedSession) -> set[str]:
    return {str(site.site_id) for site in session.sites}


def render_access_required_page(site_id: str) -> str:
    safe_site = re.sub(r"[^A-Za-z0-9._-]+", "", site_id) or "site"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex,nofollow,noarchive" />
    <title>Access Required</title>
    <style>
      body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #171a1f; }}
      main {{ width: min(640px, calc(100vw - 32px)); margin: 12vh auto 0; padding: 24px; border: 1px solid #d9dee7; border-radius: 8px; background: white; }}
      h1, p {{ margin: 0; }}
      h1 {{ font-size: 1.5rem; margin-bottom: 10px; }}
      p {{ color: #3a4656; line-height: 1.5; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Access Required</h1>
      <p>Site {safe_site} photos require a valid field-capture or viewer link.</p>
    </main>
  </body>
</html>
"""


def validate_uploaded_photos(photos: list[UploadedPhoto], *, max_images: int, max_upload_bytes: int) -> None:
    limits = IngestLimits(
        max_images=max_images,
        max_upload_bytes=max_upload_bytes,
        request_max_bytes=0,
        photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
        audio_mime_extensions=AUDIO_MIME_EXTENSIONS,
        audio_allowed_extensions=AUDIO_ALLOWED_EXTENSIONS,
    )
    capture_ingest_validate_uploaded_photos(photos, limits)


def validate_uploaded_audio(audio_files: list[UploadedFile], *, max_upload_bytes: int) -> None:
    limits = IngestLimits(
        max_images=0,
        max_upload_bytes=max_upload_bytes,
        request_max_bytes=0,
        photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
        audio_mime_extensions=AUDIO_MIME_EXTENSIONS,
        audio_allowed_extensions=AUDIO_ALLOWED_EXTENSIONS,
    )
    capture_ingest_validate_uploaded_audio(audio_files, limits)


def required_field(fields: dict[str, str], key: str) -> str:
    value = fields.get(key, "").strip()
    if not value:
        raise SubmissionError(HTTPStatus.BAD_REQUEST, "missing_field", f"{key} is required")
    return value


def compute_capture_paths(
    fields: dict[str, str],
    photos: list[UploadedFile],
    audio_files: list[UploadedFile],
    upload_dir: Path,
) -> CapturePaths:
    site = required_field(fields, "site")
    qc_category = required_field(fields, "qc_category")
    captured_at = required_field(fields, "captured_at")
    exported_at = required_field(fields, "exported_at")
    note = fields.get("note", "")
    capture_id = normalize_capture_id(fields.get("capture_id", ""), fallback_prefix="cap-photo-")
    site_id = fields.get("site_id", "").strip() or None
    target_type = fields.get("target_type", "").strip() or "location"
    target_id = fields.get("target_id", "").strip() or str(site_id or "")
    if target_type == "prospect":
        site_id = None
    elif target_type != "location":
        target_type = "location"
        target_id = str(site_id or "")
    media_paths = build_capture_media_records(
        captured_at=captured_at,
        capture_id=capture_id,
        photos=photos,
        audio_files=audio_files,
        upload_dir=upload_dir,
        limits=IngestLimits(
            max_images=0,
            max_upload_bytes=0,
            request_max_bytes=0,
            photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
            audio_mime_extensions=AUDIO_MIME_EXTENSIONS,
            audio_allowed_extensions=AUDIO_ALLOWED_EXTENSIONS,
        ),
        audio_duration_seconds=fields.get("audio_duration_seconds", "").strip(),
    )
    return CapturePaths(
        capture_id=capture_id,
        site_id=site_id,
        target_type=target_type,
        target_id=target_id,
        site=site,
        qc_category=qc_category,
        captured_at=captured_at,
        exported_at=exported_at,
        note=note,
        upload_root=media_paths.upload_root,
        photo_records=media_paths.photo_records,
        audio_records=media_paths.audio_records,
    )


def can_override_submission_attribution(session: object) -> bool:
    """Only a dedicated import token may override submitter identity/source."""
    record = getattr(session, "record", None)
    token_type = str(getattr(record, "token_type", "") or "")
    return token_type == "import"


def resolve_submission_attribution(session: object, fields: dict[str, str]) -> SubmissionAttribution:
    person = getattr(session, "person")
    person_id = str(getattr(person, "person_id"))
    person_name = str(getattr(person, "canonical_name"))
    source = "field_capture_app"
    if can_override_submission_attribution(session):
        requested_person_id = str(fields.get("person_id") or "").strip()
        if requested_person_id:
            try:
                resolved_person = load_person_from_canonical(CouchDBEntityStore.from_env(), requested_person_id)
            except NotFoundError as exc:
                raise SubmissionError(HTTPStatus.BAD_REQUEST, "unknown_person", "person_id is not recognized") from exc
            person_id = str(resolved_person.person_id)
            person_name = resolved_person.canonical_name
        requested_source = str(fields.get("source") or "").strip()
        if requested_source:
            source = safe_slug(requested_source)
    return SubmissionAttribution(person_id=person_id, person_name=person_name, source=source)


def build_submission_job(
    fields: dict[str, str],
    photos: list[UploadedFile],
    audio_files: list[UploadedFile],
    session: object,
    upload_dir: Path,
    *,
    attribution: SubmissionAttribution | None = None,
) -> dict[str, object]:
    paths = compute_capture_paths(fields, photos, audio_files, upload_dir)
    job_id = fields.get("job_id", "").strip() or f"{paths.exported_at.replace(':', '-').replace('.', '-')}__photo-capture-{safe_slug(paths.site)}"
    attribution = attribution or resolve_submission_attribution(session, fields)

    return {
        "job_id": job_id,
        "job_type": "photo_capture",
        "metadata": {
            "capture_id": paths.capture_id,
            "source": attribution.source,
            "person_id": attribution.person_id,
            "person_name": attribution.person_name,
            "field_capture_token_id": session.record.token_id,
            "field_capture_token_label": session.record.label,
            "site_id": paths.site_id,
            "target_type": paths.target_type,
            "target_id": paths.target_id,
        },
        "payload": {
            "site": paths.site,
            "qc_category": paths.qc_category,
            "note": paths.note,
            "captured_at": paths.captured_at,
            "exported_at": paths.exported_at,
            "photos": paths.photo_records,
            "audio": paths.audio_records,
        },
    }


def build_capture_document(
    fields: dict[str, str],
    photos: list[UploadedFile],
    audio_files: list[UploadedFile],
    session: object,
    upload_dir: Path,
    *,
    attribution: SubmissionAttribution | None = None,
) -> dict[str, object]:
    paths = compute_capture_paths(fields, photos, audio_files, upload_dir)
    attribution = attribution or resolve_submission_attribution(session, fields)
    domain_fields: dict[str, object] = {
        "site": paths.site,
        "site_id": str(paths.site_id or ""),
        "target_type": paths.target_type,
        "target_id": paths.target_id,
        "person_id": attribution.person_id,
        "person_name": attribution.person_name,
        "source": attribution.source,
        "field_capture_token_id": session.record.token_id,
        "field_capture_token_label": session.record.label,
        "qc_category": paths.qc_category,
        "note": paths.note,
        "captured_at": paths.captured_at,
        "exported_at": paths.exported_at,
    }
    return build_capture_document_envelope(
        capture_id=paths.capture_id,
        doc_type="field_capture",
        domain_fields=domain_fields,
        photos=paths.photo_records,
        audio=paths.audio_records,
    )


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:120] or "field-capture"


def sanitize_token_text(value: str) -> str:
    parts = urlsplit(value)
    if not parts.query:
        return value
    query = parse_qs(parts.query, keep_blank_values=True)
    if "token" in query:
        query["token"] = ["[redacted]"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def default_db_path() -> Path:
    return get_config().runtime_root / "field_capture_tokens.sqlite3"


def default_upload_dir() -> Path:
    return get_config().runtime_root / "uploads"


def run_server(
    host: str,
    port: int,
    db_path: Path,
    upload_dir: Path,
    max_images: int,
    max_upload_bytes: int,
    request_max_bytes: int,
) -> int:
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("audio/webm", ".webm")
    mimetypes.add_type("audio/mp4", ".m4a")
    mimetypes.add_type("audio/wav", ".wav")
    if not os.environ.get("BTQ_COUCHDB_URL"):
        print(
            "field-capture: BTQ_COUCHDB_URL must be set; the queue-file fallback was removed in prompt 14",
            file=sys.stderr,
        )
        return 2
    couchdb_config = couchdb_config_module.from_env()
    couchdb_database = couchdb_config_module.field_captures_database()
    token_store = TokenStore(db_path)
    token_store.initialize()
    server = FieldCaptureServer(
        (host, port),
        FieldCaptureHandler,
        token_store=token_store,
        upload_dir=upload_dir,
        max_images=max_images,
        max_upload_bytes=max_upload_bytes,
        request_max_bytes=request_max_bytes,
        couchdb_config=couchdb_config,
        couchdb_database=couchdb_database,
    )
    print(f"Field Capture listening on http://{host}:{port}")
    print(f"field-capture ingress: couchdb (db={couchdb_database})")
    print(f"Token database: {db_path.expanduser()}")
    print(f"Upload dir: {upload_dir.expanduser()}")
    server.serve_forever()
    return 0


def parse_expires_at(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return parse_timestamp(raw)


def create_token(args: argparse.Namespace) -> int:
    person = load_person_from_canonical(CouchDBEntityStore.from_env(), args.person)
    viewer_only = bool(getattr(args, "viewer_only", False))
    token_type = getattr(args, "token_type", None) or ("viewer" if viewer_only else "capture")
    site_ids = ("*",) if getattr(args, "all_sites", False) else tuple(getattr(args, "site_id", None) or [])
    created = TokenStore(args.db).create_token(
        str(person.person_id),
        label=args.label or "",
        expires_at=parse_expires_at(args.expires_at),
        can_submit=not viewer_only,
        can_view_site=True,
        role=getattr(args, "role", "cleaner") or "cleaner",
        token_type=token_type,
        site_ids=site_ids,
    )
    print(f"token_id: {created.record.token_id}")
    print(f"person_id: {created.record.person_id}")
    print(f"token_type: {created.record.token_type}")
    print(f"can_submit: {'yes' if created.record.can_submit else 'no'}")
    print(f"can_view_site: {'yes' if created.record.can_view_site else 'no'}")
    print(f"role: {created.record.role}")
    print(f"site_ids: {','.join(created.record.site_ids)}")
    print(f"label: {created.record.label}")
    print(f"expires_at: {created.record.expires_at or ''}")
    print(f"token: {created.token_value}")
    return 0


def revoke_token(args: argparse.Namespace) -> int:
    revoked = TokenStore(args.db).revoke_token(args.token_id)
    if not revoked:
        raise SystemExit(f"Token not found or already revoked: {args.token_id}")
    print(f"revoked: {args.token_id}")
    return 0


def list_tokens(args: argparse.Namespace) -> int:
    for row in token_records_as_rows(TokenStore(args.db).list_tokens()):
        print(row)
    return 0


def edit_token(args: argparse.Namespace) -> int:
    store = TokenStore(args.db)
    record = store.get_token(args.token_id)
    if record is None or record.revoked:
        print(f"Token not found or already revoked: {args.token_id}", file=sys.stderr)
        return 1

    if getattr(args, "all_sites", False):
        site_ids = ("*",)
    else:
        current_sites = set(record.site_ids)
        for site_id in getattr(args, "add_site", None) or []:
            if site_id.strip():
                current_sites.add(site_id.strip())
        for site_id in getattr(args, "remove_site", None) or []:
            current_sites.discard(site_id.strip())
        site_ids = normalize_site_ids(current_sites)

    can_submit: bool | None = None
    if getattr(args, "can_submit", False):
        can_submit = True
    elif getattr(args, "read_only", False):
        can_submit = False

    try:
        updated = store.update_token(
            args.token_id,
            role=getattr(args, "role", None),
            site_ids=site_ids,
            can_submit=can_submit,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if updated is None:
        print(f"Token not found or already revoked: {args.token_id}", file=sys.stderr)
        return 1
    for row in token_records_as_rows([updated]):
        print(row)
    return 0


def audit_tokens(args: argparse.Namespace) -> int:
    """Flag tokens whose ``person_id`` no longer resolves to a live employee.

    The SQLite token store is separate from canonical CouchDB, so a person_id
    rename/merge in the vault can leave a token pointing at a person that no
    longer exists. ``authenticate`` still succeeds (the secret is valid), but
    ``authorize_token`` then fails person resolution and the surface returns
    ``invalid_token`` — exactly the drift that broke the batch-image import after
    the ULID→lastname_firstname migration. Run this after any person_id change.

    Exit status is non-zero when at least one checked token is stale, so a cron
    or CI gate can alarm on it.
    """
    include_revoked = bool(getattr(args, "include_revoked", False))
    records = TokenStore(args.db).list_tokens()
    if not include_revoked:
        records = [record for record in records if not record.revoked]
    try:
        store = CouchDBEntityStore.from_env()
    except Exception as exc:  # noqa: BLE001 - audit can only run against a reachable canonical store.
        raise SystemExit(f"audit unavailable: canonical store unreachable ({exc})")

    status_by_person: dict[str, str] = {}

    def resolve_status(person_id: str) -> str:
        if person_id not in status_by_person:
            try:
                load_person_from_canonical(store, person_id)
                status_by_person[person_id] = "ok"
            except NotFoundError:
                status_by_person[person_id] = "MISSING"
            except Exception as exc:  # noqa: BLE001 - report lookup errors without treating them as stale.
                status_by_person[person_id] = f"error: {exc}"
        return status_by_person[person_id]

    stale = 0
    print("status | token_id | person_id | type | revoked | label")
    for record in records:
        status = resolve_status(record.person_id)
        if status == "MISSING":
            stale += 1
        print(
            " | ".join(
                [
                    status,
                    record.token_id,
                    record.person_id,
                    record.token_type,
                    "yes" if record.revoked else "no",
                    record.label,
                ]
            )
        )
    if stale:
        print(
            f"\n{stale} token(s) reference a person_id with no live employee doc — "
            "re-mint onto the current person_id or revoke."
        )
    return 1 if stale else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run field-capture and manage access tokens.")
    parser.set_defaults(command="serve")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite token database path.")
    parser.add_argument("--upload-dir", type=Path, default=default_upload_dir(), help="Directory for submitted photo uploads.")
    parser.add_argument("--max-images", type=int, default=100, help="Maximum photos per submission.")
    parser.add_argument("--max-upload-bytes", type=int, default=10 * 1024 * 1024, help="Maximum bytes per uploaded photo.")
    parser.add_argument("--request-max-bytes", type=int, default=512 * 1024 * 1024, help="Maximum request body size.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for the default server command.")
    parser.add_argument("--port", type=int, default=8765, help="Port for the default server command.")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run the field-capture web server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    token_parser = subparsers.add_parser("token", help="Manage field-capture tokens.")
    token_subparsers = token_parser.add_subparsers(dest="token_command", required=True)

    create_parser = token_subparsers.add_parser("create", help="Create a token for a vault person.")
    create_parser.add_argument("--person", required=True, help="Person id, name, or alias from the vault.")
    create_parser.add_argument("--label", default="", help="Operational label, such as device owner.")
    create_parser.add_argument("--expires-at", help="Optional ISO timestamp. Example: 2026-06-01T00:00:00Z")
    create_parser.add_argument("--viewer-only", action="store_true", help="Create a token that can view assigned sites but cannot submit captures.")
    create_parser.add_argument("--role", choices=["cleaner", "site_admin"], default="cleaner", help="Capture role for category access.")
    create_parser.add_argument("--token-type", choices=["capture", "viewer", "client_viewer", "admin_viewer", "import"], help="Operational token type metadata.")
    create_parser.add_argument("--site-id", action="append", help="Optional explicit site scope. Repeat for multiple sites.")
    create_parser.add_argument("--all-sites", action="store_true", help="Give this token universal site scope.")
    create_parser.set_defaults(func=create_token)

    revoke_parser = token_subparsers.add_parser("revoke", help="Revoke a token by token_id.")
    revoke_parser.add_argument("token_id")
    revoke_parser.set_defaults(func=revoke_token)

    edit_parser = token_subparsers.add_parser("edit", help="Edit mutable token metadata without rotating the secret.")
    edit_parser.add_argument("--token-id", required=True)
    edit_parser.add_argument("--role", choices=["cleaner", "site_admin"])
    edit_parser.add_argument("--add-site", action="append", help="Add a site id to the token scope. Repeat for multiple sites.")
    edit_parser.add_argument("--remove-site", action="append", help="Remove a site id from the token scope. Repeat for multiple sites.")
    edit_parser.add_argument("--all-sites", action="store_true", help="Replace scope with universal site access.")
    submit_group = edit_parser.add_mutually_exclusive_group()
    submit_group.add_argument("--can-submit", action="store_true", help="Allow capture submissions.")
    submit_group.add_argument("--read-only", action="store_true", help="Disallow capture submissions.")
    edit_parser.set_defaults(func=edit_token)

    list_parser = token_subparsers.add_parser("list", help="List token metadata without raw token values.")
    list_parser.set_defaults(func=list_tokens)

    audit_parser = token_subparsers.add_parser(
        "audit",
        help="Flag tokens whose person_id no longer resolves to a live employee (exit 1 if any are stale).",
    )
    audit_parser.add_argument(
        "--include-revoked",
        action="store_true",
        help="Also check revoked tokens (default: active tokens only).",
    )
    audit_parser.set_defaults(func=audit_tokens)

    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", "serve") == "serve":
        return run_server(
            args.host,
            args.port,
            args.db,
            args.upload_dir,
            args.max_images,
            args.max_upload_bytes,
            args.request_max_bytes,
        )
    if getattr(args, "command", None) == "token" and hasattr(args, "func"):
        return args.func(args)
    parser.print_help()
    return 2


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
