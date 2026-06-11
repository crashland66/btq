from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from capture_ingest import (
    IMPORT_MAX_IMAGES,
    IngestLimits,
    SubmissionError,
    UploadedFile,
    build_capture_document_envelope,
    build_capture_media_records,
    lookup_existing_capture,
    normalize_capture_id,
    parse_multipart,
    validate_content_length,
    validate_uploaded_photos,
    write_capture_media,
)
from config import get_config
from event_pipeline import couchdb_config as couchdb_config_module
from event_pipeline.couchdb.system_defaults import load_system_defaults
from event_pipeline.couchdb_capture_writer import (
    CouchDBCaptureWriterError,
    get_field_capture_document,
    put_field_capture_document,
)
from event_pipeline.couchdb_job_draft_writer import CouchDBJobDraftWriterError, list_job_drafts
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from field_capture import my_submissions as my_submissions_module
from field_capture import photo_vision as photo_vision_module
from field_capture.auth import AuthorizedSession, TokenStore, authorize_token
from field_capture.job_draft_review import apply_job_draft_review
from field_capture.photo_vision_couchdb import query_photo_vision_by_capture_ids
from field_capture.site_viewer import resolve_media_request
from field_capture.server import (
    AUDIO_ALLOWED_EXTENSIONS,
    AUDIO_MIME_EXTENSIONS,
    OPERATOR_ONLY_CANONICALS,
    PHOTO_MIME_EXTENSIONS,
    authorized_site_ids,
    canonicalize_qc_category,
    apply_role_category_filter,
    load_prospect,
    query_captures_by_person_id,
    resolve_display_categories,
    resolve_submission_attribution,
    can_override_submission_attribution,
    TERMINAL_PROSPECT_STATUSES,
)
from queue_spec import validate_job
from shared_pwa.assets import render_service_worker_bytes, serve_static_asset
from voice_memo.core import AUDIO_CONTENT_TYPES, MAX_AUDIO_BYTES, UploadedVoiceMemo, VoiceMemoError, audio_extension, validate_audio


PACKAGE_ROOT = Path(__file__).resolve().parent
PUBLIC_ROOT = PACKAGE_ROOT / "public"
PUBLIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class UnifiedCaptureServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        *,
        token_store: TokenStore,
        vault_root: Path,
        upload_dir: Path | None = None,
        max_images: int = 6,
        max_upload_bytes: int = 10 * 1024 * 1024,
        request_max_bytes: int | None = None,
        couchdb_config: couchdb_config_module.CouchDBConfig | None = None,
        couchdb_database: str = couchdb_config_module.DEFAULT_FIELD_CAPTURES_DB,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(address, UnifiedCaptureHandler)
        self.token_store = token_store
        self.vault_root = vault_root.expanduser()
        self.data_dir = data_dir
        self.upload_dir = (upload_dir or default_upload_dir()).expanduser().resolve(strict=False)
        self.max_images = max_images
        self.max_upload_bytes = max_upload_bytes
        self.request_max_bytes = request_max_bytes or (max_images * max_upload_bytes + MAX_AUDIO_BYTES + 1024 * 1024)
        self.couchdb_config = couchdb_config
        self.couchdb_database = couchdb_database


class UnifiedCaptureHandler(BaseHTTPRequestHandler):
    server: UnifiedCaptureServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.write_json({"status": "ok", "app": "unified_capture"}, HTTPStatus.OK)
            return
        if path == "/api/session":
            self.handle_session()
            return
        if path == "/api/inbox":
            self.handle_inbox()
            return
        if path == "/api/my-submissions":
            self.handle_my_submissions()
            return
        if path == "/sw.js":
            self.write_bytes(
                render_service_worker_bytes("unified_capture"),
                "application/javascript; charset=utf-8",
                cache_control="public, max-age=300",
            )
            return
        if path.startswith("/static/"):
            if self.try_serve_shared_static(path):
                return
            self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/media/"):
            self.handle_media(path[len("/media/"):])
            return
        if self.try_serve_public(path):
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/submit":
            self.handle_submit()
            return
        if path == "/api/inbox/approve":
            self.handle_inbox_review("approve")
            return
        if path == "/api/inbox/reject":
            self.handle_inbox_review("reject")
            return
        if path == "/api/inbox/approve-set":
            self.handle_inbox_review_set()
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

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
            morsel = cookie.get("btq_field_viewer_token") or cookie.get("unifiedCaptureToken")
            if morsel is not None:
                return morsel.value.strip()
        return None

    def handle_submit(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, self.server.vault_root, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return
        couchdb_config = self.server.couchdb_config
        if couchdb_config is None:
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        # The dedicated batch-image import token may submit a large WhatsApp-fallback
        # dump as one capture; regular capture tokens keep the app's small ceiling.
        effective_max_images = (
            max(int(getattr(self.server, "max_images")), IMPORT_MAX_IMAGES)
            if can_override_submission_attribution(session)
            else None
        )
        try:
            fields, photos, audio_files = self.read_multipart_submission(max_images=effective_max_images)
            self.validate_submit_authorization(session, fields)
            capture_id = normalize_capture_id(fields.get("capture_id", ""), fallback_prefix="cap-unified-")
            if fields.get("capture_id"):
                try:
                    existing = lookup_existing_capture(
                        capture_id,
                        lambda existing_capture_id: get_field_capture_document(
                            couchdb_config,
                            self.server.couchdb_database,
                            existing_capture_id,
                        ),
                    )
                except CouchDBCaptureWriterError as error:
                    self.log_message("CouchDB capture lookup failed capture_id=%s error=%s", capture_id, error)
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

            doc, media_records = self.build_submit_document(fields, photos, audio_files, session, capture_id)
            write_capture_media(media_records, photos + audio_files, self.server.upload_dir)
        except SubmissionError as error:
            self.write_json({"error": error.code, "message": error.message}, error.status)
            return

        try:
            response = put_field_capture_document(couchdb_config, doc, database=self.server.couchdb_database)
        except CouchDBCaptureWriterError as error:
            self.log_message("CouchDB capture ingress unavailable capture_id=%s error=%s", doc.get("capture_id", ""), error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB capture ingress unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        couchdb_doc_id = str(response.get("id") or doc["_id"])
        self.write_json(
            {
                "status": "submitted",
                "job_id": str(fields.get("job_id") or ""),
                "capture_id": str(doc["capture_id"]),
                "couchdb_doc_id": couchdb_doc_id,
                "photo_count": len(photos),
                "audio_count": len(audio_files),
            },
            HTTPStatus.CREATED,
        )

    def handle_media(self, media_path: str) -> None:
        # Captured media can include resident-area photos, so never serve it
        # anonymously. A valid session (token via Authorization header, ?token=,
        # or the unifiedCaptureToken cookie that <img> requests carry) is required.
        if self.authorize_token_from_request() is None:
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return
        try:
            path = resolve_media_request(unquote(media_path), self.server.upload_dir)
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.write_bytes(body, content_type, cache_control="private, max-age=300")

    def handle_my_submissions(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, self.server.vault_root, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return

        couchdb_config = self.server.couchdb_config
        if couchdb_config is None:
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        person_id = str(session.person.person_id) if session.person else ""
        if not person_id:
            self.write_json({"submissions": [], "quality_summary": None}, HTTPStatus.OK)
            return

        runtime_root = self.server.upload_dir.parent
        photo_vision_dir = photo_vision_module.default_photo_vision_dir(runtime_root)
        candidates_dir = runtime_root / "reviews" / "action_candidates" / "field_capture"
        try:
            capture_docs = query_captures_by_person_id(
                couchdb_config,
                person_id,
                database=self.server.couchdb_database,
            )
        except Exception as error:
            self.log_message("CouchDB my-submissions lookup unavailable person_id=%s error=%s", person_id, error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB lookup unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        capture_ids = [
            str(doc.get("capture_id") or doc.get("_id") or "").strip()
            for doc in capture_docs
            if isinstance(doc, dict) and str(doc.get("capture_id") or doc.get("_id") or "").strip()
        ]
        try:
            photo_vision_couchdb_docs = query_photo_vision_by_capture_ids(couchdb_config, capture_ids)
        except Exception as error:
            self.log_message("CouchDB photo-vision lookup unavailable person_id=%s error=%s", person_id, error)
            photo_vision_couchdb_docs = {}

        submissions = my_submissions_module.collect_my_submissions(
            person_id,
            capture_docs,
            upload_dir=self.server.upload_dir,
            photo_vision_dir=photo_vision_dir,
            candidates_dir=candidates_dir,
            can_retarget=session.record.role == "site_admin",
            photo_vision_couchdb_docs=photo_vision_couchdb_docs,
        )
        quality_summary = my_submissions_module.rolling_quality_summary(
            person_id,
            capture_docs,
            photo_vision_dir,
        )
        self.write_json({"submissions": submissions, "quality_summary": quality_summary}, HTTPStatus.OK)

    def authorize_token_from_request(self) -> AuthorizedSession | None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return None
        try:
            session = authorize_token(self.server.token_store, self.server.vault_root, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return None
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return None
        return session

    def authorize_inbox_operator(self) -> AuthorizedSession | None:
        session = self.authorize_token_from_request()
        if session is None:
            return None
        # Worker-token-403 gate: inbox review endpoints are operator-only.
        if session.record.token_type != "admin_viewer":
            self.write_json({"error": "not_authorized"}, HTTPStatus.FORBIDDEN)
            return None
        return session

    def handle_inbox(self) -> None:
        if self.authorize_inbox_operator() is None:
            return
        couchdb_config = self.server.couchdb_config
        if couchdb_config is None:
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB inbox unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        try:
            items = self.inbox_items()
        except CouchDBJobDraftWriterError as error:
            self.log_message("CouchDB inbox lookup unavailable error=%s", error)
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB inbox unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self.write_json({"count": len(items), "items": items}, HTTPStatus.OK)

    def handle_inbox_review(self, action: str) -> None:
        session = self.authorize_inbox_operator()
        if session is None:
            return
        couchdb_config = self.server.couchdb_config
        if couchdb_config is None:
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB inbox unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        payload = self.read_json_payload(max_bytes=10_000)
        if payload is None:
            return
        draft_id = str(payload.get("draft_id") or "").strip()
        expected_rev = str(payload.get("_rev") or "").strip()
        if not draft_id or not expected_rev:
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return
        reason = payload.get("reason")
        result = apply_job_draft_review(
            couchdb_config,
            self.server.couchdb_database,
            draft_id,
            action=action,
            reviewer=str(session.person.person_id),
            expected_rev=expected_rev,
            rationale=str(reason).strip() if reason is not None else None,
        )
        if result.already_decided:
            self.write_json({"error": "already_decided"}, HTTPStatus.OK)
            return
        if result.error:
            self.write_json({"error": "review_failed", "message": result.error}, HTTPStatus.BAD_REQUEST)
            return
        status = "approved" if action == "approve" else "rejected"
        self.write_json({"ok": True, "draft_id": result.draft_id, "status": status}, HTTPStatus.OK)

    def handle_inbox_review_set(self) -> None:
        session = self.authorize_inbox_operator()
        if session is None:
            return
        couchdb_config = self.server.couchdb_config
        if couchdb_config is None:
            self.write_json(
                {"error": "couchdb_unavailable", "message": "CouchDB inbox unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        payload = self.read_json_payload(max_bytes=100_000)
        if payload is None:
            return
        drafts = payload.get("drafts")
        if not isinstance(drafts, list):
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return

        results: list[dict[str, object]] = []
        approved = 0
        rejected = 0
        already_decided = 0
        for entry in drafts:
            if not isinstance(entry, dict):
                self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
                return
            draft_id = str(entry.get("draft_id") or "").strip()
            expected_rev = str(entry.get("_rev") or "").strip()
            if not draft_id or not expected_rev:
                self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
                return
            checked = bool(entry.get("checked"))
            action = "approve" if checked else "reject"
            result = apply_job_draft_review(
                couchdb_config,
                self.server.couchdb_database,
                draft_id,
                action=action,
                reviewer=str(session.person.person_id),
                expected_rev=expected_rev,
                rationale="Approved in PWA checklist." if checked else "Unchecked in PWA checklist.",
            )
            result_payload: dict[str, object] = {
                "draft_id": result.draft_id,
                "action": action,
                "status": "already_decided" if result.already_decided else result.review_status,
            }
            if result.new_rev:
                result_payload["_rev"] = result.new_rev
            if result.already_decided:
                already_decided += 1
                result_payload["error"] = "already_decided"
            elif result.error:
                result_payload["error"] = "review_failed"
                result_payload["message"] = result.error
            elif action == "approve":
                approved += 1
            else:
                rejected += 1
            results.append(result_payload)

        self.write_json(
            {
                "ok": True,
                "approved": approved,
                "rejected": rejected,
                "already_decided": already_decided,
                "results": results,
            },
            HTTPStatus.OK,
        )

    def read_json_payload(self, *, max_bytes: int) -> dict[str, object] | None:
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "0")
        except ValueError:
            self.write_json({"error": "invalid_content_length"}, HTTPStatus.BAD_REQUEST)
            return None
        if content_length <= 0 or content_length > max_bytes:
            self.write_json({"error": "invalid_body"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.write_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
            return None
        if not isinstance(payload, dict):
            self.write_json({"error": "invalid_payload"}, HTTPStatus.BAD_REQUEST)
            return None
        return payload

    def inbox_items(self) -> list[dict[str, object]]:
        docs = list_job_drafts(
            self.server.couchdb_config,
            self.server.couchdb_database,
            review_status="pending_approval",
        )
        registry = self.site_registry()
        items: list[dict[str, object]] = []
        for doc in docs:
            item = self.inbox_item(doc, registry)
            if item is not None:
                items.append(item)
        return items

    def inbox_count(self) -> int:
        couchdb_config = getattr(self.server, "couchdb_config", None)
        if couchdb_config is None:
            return 0
        try:
            return len(self.inbox_items())
        except Exception as error:
            self.log_message("WARNING: inbox_count unavailable: %s", error)
            return 0

    def inbox_item(self, doc: dict[str, object], registry: object | None) -> dict[str, object] | None:
        job_type = str(doc.get("job_type") or "").strip()
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            if not validate_job({"job_type": job_type, "payload": dict(payload)}):
                return None
        except Exception:
            return None

        site_id = str(doc.get("site_id") or "").strip()
        message = str(doc.get("message") or "").strip()
        return {
            "draft_id": str(doc.get("draft_id") or doc.get("_id") or ""),
            "_rev": str(doc.get("_rev") or ""),
            "source_capture_id": str(doc.get("source_capture_id") or ""),
            "source": self.inbox_source(doc),
            "message": message,
            "evidence": self.inbox_evidence(doc),
            "site": self.site_label(site_id, registry),
            "site_id": site_id,
            "group_id": str(doc.get("group_id") or ""),
            "submitter_name": str(doc.get("submitter_name") or ""),
            "created_at": str(doc.get("created_at") or ""),
            "job_type": job_type,
            "payload": dict(payload),
        }

    def inbox_source(self, doc: dict[str, object]) -> str:
        source_kind = str(doc.get("source_kind") or "").lower()
        if "voice" in source_kind or "audio" in source_kind:
            return "voice"
        if "photo" in source_kind or "image" in source_kind:
            return "photo"
        if "text" in source_kind:
            return "text"
        return "note"

    def inbox_evidence(self, doc: dict[str, object]) -> str:
        evidence = doc.get("evidence")
        if isinstance(evidence, dict):
            for key in ("source_text", "source_context", "rationale"):
                value = str(evidence.get(key) or "").strip()
                if value:
                    return value
        return ""

    def site_registry(self) -> object | None:
        registry = getattr(self.server, "site_registry", None)
        if registry is not None:
            return registry
        if os.environ.get("BTQ_COUCHDB_URL"):
            try:
                return CouchDBSiteRegistry()
            except Exception as error:
                self.log_message("WARNING: site registry unavailable for /api/inbox: %s", error)
        return None

    def site_label(self, site_id: str, registry: object | None) -> str:
        if not site_id:
            return ""
        if registry is not None:
            try:
                canonical = registry.resolve_canonical(site_id)
                if canonical:
                    return str(canonical)
            except Exception as error:
                self.log_message("WARNING: site label lookup failed site_id=%s error=%s", site_id, error)
        return site_id

    def read_multipart_submission(
        self, *, max_images: int | None = None
    ) -> tuple[dict[str, str], list[UploadedFile], list[UploadedFile]]:
        request_max_bytes = int(getattr(self.server, "request_max_bytes"))
        content_length = validate_content_length(self.headers.get("Content-Length"), request_max_bytes)
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "expected_multipart", "Expected multipart form data")
        fields, uploads = parse_multipart(self.rfile.read(content_length), content_type)
        photos = [upload for upload in uploads if upload.field_name == "photos"]
        audio_files = [upload for upload in uploads if upload.field_name == "audio"]
        note = fields.get("note", "").strip()
        if len(photos) + len(audio_files) < 1 and not note:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "missing_asset", "Add at least one photo, voice note, or text note")
        if photos:
            validate_uploaded_photos(photos, self.photo_ingest_limits(max_images=max_images))
        self.validate_uploaded_audio(audio_files)
        return fields, photos, audio_files

    def photo_ingest_limits(self, *, max_images: int | None = None) -> IngestLimits:
        return IngestLimits(
            max_images=int(max_images if max_images is not None else getattr(self.server, "max_images")),
            max_upload_bytes=int(getattr(self.server, "max_upload_bytes")),
            request_max_bytes=int(getattr(self.server, "request_max_bytes")),
            photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
            audio_mime_extensions=AUDIO_MIME_EXTENSIONS,
            audio_allowed_extensions=AUDIO_ALLOWED_EXTENSIONS,
        )

    def validate_uploaded_audio(self, audio_files: list[UploadedFile]) -> None:
        if len(audio_files) > 1:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "too_many_audio_files", "At most one voice note may be submitted")
        for audio in audio_files:
            try:
                validate_audio(
                    UploadedVoiceMemo(
                        filename=audio.filename,
                        content_type=audio.content_type,
                        content=audio.content,
                    )
                )
            except VoiceMemoError as exc:
                status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code == "request_too_large" else HTTPStatus.BAD_REQUEST
                raise SubmissionError(status, exc.code, exc.message) from exc

    def validate_submit_authorization(self, session: object, fields: dict[str, str]) -> None:
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
        registry = getattr(self.server, "site_registry", None)
        display_categories = None
        if registry is not None:
            try:
                display_categories = registry.get_display_categories(site_id)
            except Exception as error:
                self.log_message("WARNING: display_categories lookup failed site_id=%s error=%s", site_id, error)
        default_categories = None
        try:
            system_defaults = load_system_defaults()
            default_categories = system_defaults.get("default_display_categories") if isinstance(system_defaults, dict) else None
        except Exception as error:
            self.log_message("WARNING: system_defaults unavailable for /api/submit: %s", error)
        return apply_role_category_filter(resolve_display_categories(display_categories, default_categories), "site_admin")

    def build_submit_document(
        self,
        fields: dict[str, str],
        photos: list[UploadedFile],
        audio_files: list[UploadedFile],
        session: object,
        capture_id: str,
    ) -> tuple[dict[str, object], list[object]]:
        site = self.required_field(fields, "site")
        qc_category = self.required_field(fields, "qc_category")
        captured_at = self.required_field(fields, "captured_at")
        exported_at = self.required_field(fields, "exported_at")
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
            upload_dir=self.server.upload_dir,
            limits=self.media_record_limits(audio_files),
            audio_duration_seconds=fields.get("audio_duration_seconds", "").strip(),
        )
        attribution = resolve_submission_attribution(session, fields, self.server.vault_root)
        domain_fields: dict[str, object] = {
            "site": site,
            "site_id": str(site_id or ""),
            "target_type": target_type,
            "target_id": target_id,
            "person_id": attribution.person_id,
            "person_name": attribution.person_name,
            "source": "unified_capture_app",
            "field_capture_token_id": session.record.token_id,
            "field_capture_token_label": session.record.label,
            "qc_category": qc_category,
            "note": fields.get("note", ""),
            "captured_at": captured_at,
            "exported_at": exported_at,
        }
        doc = build_capture_document_envelope(
            capture_id=capture_id,
            doc_type="field_capture",
            domain_fields=domain_fields,
            photos=media_paths.photo_records,
            audio=media_paths.audio_records,
        )
        return doc, media_paths.photo_records + media_paths.audio_records

    def media_record_limits(self, audio_files: list[UploadedFile]) -> IngestLimits:
        audio_mime_extensions = dict(AUDIO_CONTENT_TYPES)
        audio_allowed_extensions = {mime: {ext} for mime, ext in AUDIO_CONTENT_TYPES.items()}
        for audio in audio_files:
            audio_mime_extensions[audio.content_type] = audio_extension(
                UploadedVoiceMemo(
                    filename=audio.filename,
                    content_type=audio.content_type,
                    content=audio.content,
                )
            )
            audio_allowed_extensions[audio.content_type] = {audio_mime_extensions[audio.content_type]}
        return IngestLimits(
            max_images=int(getattr(self.server, "max_images")),
            max_upload_bytes=MAX_AUDIO_BYTES,
            request_max_bytes=int(getattr(self.server, "request_max_bytes")),
            photo_mime_extensions=PHOTO_MIME_EXTENSIONS,
            audio_mime_extensions=audio_mime_extensions,
            audio_allowed_extensions=audio_allowed_extensions,
        )

    def required_field(self, fields: dict[str, str], key: str) -> str:
        value = fields.get(key, "").strip()
        if not value:
            raise SubmissionError(HTTPStatus.BAD_REQUEST, "missing_field", f"{key} is required")
        return value

    def handle_session(self) -> None:
        token_value = self.extract_token()
        if not token_value:
            self.write_json({"error": "missing_token"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            session = authorize_token(self.server.token_store, self.server.vault_root, token_value)
        except Exception:
            self.write_json({"error": "authorization_failed"}, HTTPStatus.FORBIDDEN)
            return
        if session is None:
            self.write_json({"error": "invalid_token"}, HTTPStatus.UNAUTHORIZED)
            return

        system_defaults: object = {}
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
        default_categories = system_defaults.get("default_display_categories") if isinstance(system_defaults, dict) else None

        payload: dict[str, object] = {
            "person": {
                "person_id": str(session.person.person_id),
                "name": session.person.canonical_name,
            },
            "token": {
                "token_id": session.record.token_id,
                "label": session.record.label,
            },
            "sites": [
                self.session_site_payload(site, registry, default_categories, session.record.role) for site in session.sites
            ],
            "can_submit": session.record.can_submit,
            "can_review": session.record.token_type == "admin_viewer",
            "max_images": int(getattr(self.server, "max_images", 6)),
        }
        payload["inbox_count"] = self.inbox_count() if session.record.token_type == "admin_viewer" else 0
        self.write_json(payload, HTTPStatus.OK)

    def session_site_payload(
        self,
        site: object,
        registry: object | None,
        default_categories: object,
        role: str,
    ) -> dict[str, object]:
        site_id = str(site.site_id)
        display_categories = None
        capture_guidance = None
        if registry is not None:
            try:
                display_categories = registry.get_display_categories(site_id)
            except Exception as error:
                self.log_message("WARNING: display_categories lookup failed site_id=%s error=%s", site_id, error)
            try:
                capture_guidance = registry.get_capture_guidance(site_id)
            except Exception as error:
                self.log_message("WARNING: capture_guidance lookup failed site_id=%s error=%s", site_id, error)
        return {
            "site_id": site_id,
            "label": site.canonical_name,
            "capture_guidance": str(capture_guidance or ""),
            "display_categories": apply_role_category_filter(
                resolve_display_categories(display_categories, default_categories),
                role,
            ),
        }

    def try_serve_shared_static(self, path: str) -> bool:
        try:
            content_type, body = serve_static_asset(path, PUBLIC_ROOT / "static")
        except FileNotFoundError:
            return False
        self.write_bytes(body, content_type, cache_control="public, max-age=300")
        return True

    def try_serve_public(self, path: str) -> bool:
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (PUBLIC_ROOT / relative).resolve()
        if candidate != PUBLIC_ROOT and PUBLIC_ROOT not in candidate.parents:
            return False
        if not candidate.is_file():
            return False
        content_type = PUBLIC_CONTENT_TYPES.get(candidate.suffix)
        if content_type is None:
            return False
        self.write_bytes(candidate.read_bytes(), content_type, cache_control="public, max-age=300")
        return True

    def write_json(self, payload: dict[str, object], status: HTTPStatus, *, cache_control: str = "no-store") -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.write_bytes(body, "application/json; charset=utf-8", status=status, cache_control=cache_control)

    def write_bytes(
        self,
        body: bytes,
        content_type: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            logging.info("client disconnected while writing response")

    def log_message(self, format: str, *args: object) -> None:
        return


def default_db_path() -> Path:
    return get_config().runtime_root / "field_capture_tokens.sqlite3"


def default_upload_dir() -> Path:
    return get_config().runtime_root / "uploads"


def run(
    host: str,
    port: int,
    db_path: Path,
    vault_root: Path,
    upload_dir: Path,
    max_images: int,
    max_upload_bytes: int,
    request_max_bytes: int | None,
    data_dir: Path | None = None,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if data_dir is not None and (not data_dir.exists() or not os.access(data_dir, os.W_OK)):
        logging.error("data dir must exist and be writable: %s", data_dir)
        return 1
    if not os.environ.get("BTQ_COUCHDB_URL"):
        print(
            "unified-capture: BTQ_COUCHDB_URL must be set; /api/submit writes to CouchDB field captures",
            file=sys.stderr,
        )
        return 2
    couchdb_config = couchdb_config_module.from_env()
    couchdb_database = couchdb_config_module.field_captures_database()
    token_store = TokenStore(db_path)
    token_store.initialize()
    server = UnifiedCaptureServer(
        (host, port),
        token_store=token_store,
        vault_root=vault_root,
        upload_dir=upload_dir,
        max_images=max_images,
        max_upload_bytes=max_upload_bytes,
        request_max_bytes=request_max_bytes,
        couchdb_config=couchdb_config,
        couchdb_database=couchdb_database,
        data_dir=data_dir,
    )
    logging.info("unified capture listening on http://%s:%s", host, port)
    logging.info("unified capture ingress: couchdb (db=%s)", couchdb_database)
    logging.info("token database: %s", db_path.expanduser())
    logging.info("vault root: %s", vault_root.expanduser())
    logging.info("upload dir: %s", upload_dir.expanduser())
    server.serve_forever()
    return 0


def main() -> int:
    config = get_config()
    parser = argparse.ArgumentParser(description="Run the unified field-capture server.")
    parser.add_argument("--host", default=os.environ.get("UNIFIED_CAPTURE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("UNIFIED_CAPTURE_PORT", "8093")))
    parser.add_argument("--db", type=Path, default=default_db_path(), help="SQLite token database path.")
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir, help="Vault root used for people/site authorization.")
    parser.add_argument("--upload-dir", type=Path, default=default_upload_dir(), help="Directory for submitted capture uploads.")
    parser.add_argument("--max-images", type=int, default=6, help="Maximum photos per submission.")
    parser.add_argument("--max-upload-bytes", type=int, default=10 * 1024 * 1024, help="Maximum bytes per uploaded photo.")
    parser.add_argument(
        "--request-max-bytes",
        type=int,
        default=None,
        help="Maximum request body size. Defaults to enough room for photos plus one 50 MB voice note.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("UNIFIED_CAPTURE_DATA_DIR", "/srv/btq/field-capture/data")),
    )
    args = parser.parse_args()
    return run(
        args.host,
        args.port,
        args.db,
        args.vault_root,
        args.upload_dir,
        args.max_images,
        args.max_upload_bytes,
        args.request_max_bytes,
        args.data_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
