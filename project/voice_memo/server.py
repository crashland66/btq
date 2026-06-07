from __future__ import annotations

import argparse
import json
import logging
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from capture_ingest import SubmissionError, lookup_existing_capture, parse_multipart, validate_content_length
from shared_pwa.assets import render_service_worker_bytes, shared_db_bytes

from .core import MAX_AUDIO_BYTES, UploadedVoiceMemo, VoiceMemoError, parse_duration_seconds, record_upload, validate_audio
from .couchdb import VoiceMemoCouchDBError, get_voice_memo_document, query_couchdb_find, voice_memo_config_from_env
from token_store import TokenRecord, TokenStore


REQUEST_MAX_BYTES = MAX_AUDIO_BYTES + 64 * 1024
ERROR_STATUSES = {
    "couchdb_unavailable": HTTPStatus.SERVICE_UNAVAILABLE,
    "request_too_large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
}


class VoiceMemoServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        data_dir: Path,
        token_store: TokenStore,
        couchdb_put: Callable[[dict], dict] | None = None,
        couchdb_find: Callable[[str, dict], dict] | None = None,
        couchdb_get: Callable[[str], dict | None] | None = None,
    ) -> None:
        super().__init__(address, VoiceMemoHandler)
        self.data_dir = data_dir
        self.token_store = token_store
        self.couchdb_put = couchdb_put
        self.couchdb_find = couchdb_find
        self.couchdb_get = couchdb_get


class VoiceMemoHandler(BaseHTTPRequestHandler):
    server: VoiceMemoServer

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            self.write_json({"status": "ok"}, HTTPStatus.OK)
            return
        if path == "/api/sites":
            self.write_picker_response("sites", lambda: load_sites(self.server.couchdb_find))
            return
        if path == "/api/employees":
            self.write_picker_response("employees", lambda: load_employees(self.server.couchdb_find))
            return
        if self.try_serve_static(path):
            return
        self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/api/upload":
            self.write_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        record = self.authenticated_token()
        if record is None:
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            fields, memo = self.read_submission()
            capture_id = str(fields.get("capture_id") or "").strip()
            if capture_id:
                try:
                    couchdb_get = getattr(self.server, "couchdb_get", None)
                    existing = lookup_existing_capture(
                        capture_id,
                        couchdb_get
                        if couchdb_get is not None
                        else lambda existing_capture_id: get_voice_memo_document(
                            voice_memo_config_from_env(),
                            existing_capture_id,
                        ),
                    )
                except VoiceMemoCouchDBError:
                    logging.exception("voice memo idempotency lookup failed capture_id=%s", capture_id)
                    self.write_json(
                        {"error": "couchdb_unavailable", "message": "Voice memo storage is unavailable."},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                if existing is not None:
                    self.write_json(
                        {
                            "status": "saved",
                            "capture_id": str(existing.get("_id") or capture_id),
                            "duration_seconds": existing.get("duration_seconds"),
                            "idempotent_replay": True,
                        },
                        HTTPStatus.OK,
                    )
                    return
            result = save_submission(
                self.server.data_dir,
                fields,
                memo,
                self.server.couchdb_put,
                self.server.couchdb_find,
                person_id=record.person_id,
            )
        except VoiceMemoError as error:
            status = ERROR_STATUSES.get(error.code, HTTPStatus.BAD_REQUEST)
            self.write_json({"error": error.code, "message": error.message}, status)
            return
        except Exception:
            logging.exception("voice memo upload failed")
            self.write_json({"error": "processing_failed", "message": "Voice memo could not be processed."}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        logging.info(
            "voice memo saved capture_id=%s duration_seconds=%s audio_size_bytes=%s processing_state=%s",
            result.record.get("_id", ""),
            result.record.get("duration_seconds"),
            result.record.get("audio_size_bytes"),
            result.record.get("processing_state", ""),
        )
        self.write_json(
            {
                "status": "saved",
                "capture_id": result.record["_id"],
                "duration_seconds": result.record.get("duration_seconds"),
            },
            HTTPStatus.CREATED,
        )

    def authenticated_token(self) -> TokenRecord | None:
        return authenticate_request(self.headers.get("Authorization", ""), self.server.token_store)

    def is_authorized(self) -> bool:
        return self.authenticated_token() is not None

    def write_picker_response(self, key: str, loader) -> None:
        if not self.is_authorized():
            self.write_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            rows = loader()
        except VoiceMemoCouchDBError:
            logging.exception("picker query failed path=%s status=503", urlsplit(self.path).path)
            self.write_json({"error": "couchdb_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        logging.info("picker query path=%s status=200 count=%s", urlsplit(self.path).path, len(rows))
        self.write_json({key: rows}, HTTPStatus.OK, cache_control="public, max-age=300")

    def read_submission(self) -> tuple[dict[str, str], UploadedVoiceMemo]:
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = validate_content_length(raw_length, REQUEST_MAX_BYTES)
        except SubmissionError as exc:
            raise VoiceMemoError(exc.code, voice_memo_submission_message(exc.code)) from exc
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise VoiceMemoError("expected_multipart", "Expected a form upload.")
        try:
            fields, uploads = parse_multipart(self.rfile.read(content_length), content_type)
        except SubmissionError as exc:
            raise VoiceMemoError(exc.code, voice_memo_submission_message(exc.code)) from exc
        audio = None
        for upload in uploads:
            if upload.field_name == "audio":
                candidate = UploadedVoiceMemo(
                    filename=upload.filename,
                    content_type=upload.content_type,
                    content=upload.content,
                )
                validate_audio(candidate)
                audio = candidate
        if audio is None:
            raise VoiceMemoError("missing_audio", "Record a voice memo before saving.")
        return fields, audio

    def write_json(self, payload: dict[str, object], status: HTTPStatus, *, cache_control: str = "no-store") -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            logging.info("client disconnected while writing response")

    def try_serve_static(self, path: str) -> bool:
        shared_assets = {
            "/db.js": shared_db_bytes,
            "/sw.js": lambda: render_service_worker_bytes("voice_memo"),
        }
        body_factory = shared_assets.get(path)
        if body_factory is None:
            return False
        try:
            body = body_factory()
        except OSError:
            return False
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)
        return True

    def log_message(self, format: str, *args: object) -> None:
        return


def voice_memo_submission_message(code: str) -> str:
    if code == "invalid_content_length":
        return "Invalid upload size."
    if code == "empty_request":
        return "Record a voice memo before saving."
    if code == "request_too_large":
        return "Keep voice memos under 50 MB."
    if code == "expected_multipart":
        return "Expected a form upload."
    return "Voice memo could not be processed."


def bearer_token(header: str) -> str | None:
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    return header[len(prefix) :].strip() or None


def authenticate_request(header: str, token_store: TokenStore) -> TokenRecord | None:
    """Authenticate a voice-memo request against the shared field-capture
    token store.

    Returns the record for a valid, unrevoked, unexpired token that carries
    submit permission; otherwise None. This is the same per-person token used
    by field capture, so a person needs only one token for both surfaces.
    """
    token = bearer_token(header)
    if token is None:
        return None
    record = token_store.authenticate(token)
    if record is None or not record.can_submit:
        return None
    return record


def default_couchdb_find(database: str, selector: dict) -> dict:
    return query_couchdb_find(voice_memo_config_from_env(), database, selector)


def load_sites(couchdb_find: Callable[[str, dict], dict] | None = None) -> list[dict]:
    finder = couchdb_find or default_couchdb_find
    response = finder(
        "btq_sites",
        {
            "selector": {"synced_from_vault": True, "status": "active"},
            "fields": ["_id", "job", "account", "location", "synced_from_vault", "status"],
            "limit": 1000,
        },
    )
    rows = [adapt_site(doc) for doc in response.get("docs", []) if is_active_vault_doc(doc)]
    return sorted(
        rows,
        key=lambda item: (
            f"{item.get('account', '')} - {item.get('location', '')}".lower(),
            numeric_sort_key(str(item.get("id", ""))),
        ),
    )


def load_employees(couchdb_find: Callable[[str, dict], dict] | None = None) -> list[dict]:
    finder = couchdb_find or default_couchdb_find
    response = finder(
        "btq_people",
        {
            "selector": {"synced_from_vault": True, "status": "active"},
            "fields": ["_id", "first", "last", "preferred_name", "job", "synced_from_vault", "status"],
            "limit": 1000,
        },
    )
    rows = [adapt_employee(doc) for doc in response.get("docs", []) if is_active_vault_doc(doc)]
    return sorted(rows, key=lambda item: (str(item.get("last") or "").lower(), str(item.get("first") or "").lower()))


def is_active_vault_doc(doc: object) -> bool:
    return isinstance(doc, dict) and doc.get("synced_from_vault") is True and doc.get("status") == "active"


def numeric_sort_key(value: str) -> tuple[int, str]:
    try:
        return (int(str(value).split("-", 1)[0]), str(value))
    except ValueError:
        return (10**9, str(value))


def adapt_site(doc: dict) -> dict:
    site_id = str(doc.get("_id") or doc.get("job") or "").strip()
    account = str(doc.get("account") or "").strip()
    location = str(doc.get("location") or "").strip()
    return {
        "id": site_id,
        "label": f"{account} - {location} ({site_id})",
        "account": account,
        "location": location,
    }


def adapt_employee(doc: dict) -> dict:
    first = str(doc.get("first") or "").strip()
    last = str(doc.get("last") or "").strip()
    preferred = doc.get("preferred_name")
    display_first = str(preferred or first).strip()
    job = str(doc.get("job") or "").strip()
    return {
        "id": str(doc.get("_id") or "").strip(),
        "label": f"{display_first} {last} — {job}".strip(),
        "first": first,
        "last": last,
        "preferred_name": preferred if preferred not in {"", None} else None,
        "job": job,
    }


def save_submission(
    data_dir: Path,
    fields: dict[str, str],
    memo: UploadedVoiceMemo,
    couchdb_put: Callable[[dict], dict] | None = None,
    couchdb_find: Callable[[str, dict], dict] | None = None,
    *,
    person_id: str = "",
):
    return record_upload(
        data_dir=data_dir,
        captured_at=fields.get("captured_at", ""),
        note=fields.get("note", ""),
        duration_seconds=parse_duration_seconds(fields.get("duration_seconds", "")),
        memo=memo,
        mode=fields.get("mode", "operations"),
        site_id=fields.get("site_id", ""),
        employee_slugs=fields.get("employee_slugs", ""),
        geolocation_lat=fields.get("geolocation_lat"),
        geolocation_lng=fields.get("geolocation_lng"),
        geolocation_accuracy_m=fields.get("geolocation_accuracy_m"),
        geolocation_captured_at=fields.get("geolocation_captured_at"),
        capture_id=fields.get("capture_id"),
        sites_lookup=lambda: load_sites(couchdb_find),
        employees_lookup=lambda: load_employees(couchdb_find),
        couchdb_put=couchdb_put,
        person_id=person_id,
    )


def run(host: str, port: int, data_dir: Path, token_db: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not token_db.exists():
        logging.error("token database not found: %s", token_db)
        return 1
    if not data_dir.exists() or not os.access(data_dir, os.W_OK):
        logging.error("data dir must exist and be writable: %s", data_dir)
        return 1
    server = VoiceMemoServer((host, port), data_dir=data_dir, token_store=TokenStore(token_db))
    logging.info("voice memo intake listening on http://%s:%s", host, port)
    server.serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the voice memo intake server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("BTQ_VOICE_DATA_DIR", "/srv/btq/voice/data")))
    parser.add_argument(
        "--token-db",
        type=Path,
        default=Path(os.environ.get("VOICE_MEMO_TOKEN_DB", "/srv/btq/data/field_capture_tokens.sqlite3")),
        help="Path to the shared field-capture token database.",
    )
    args = parser.parse_args()
    return run(args.host, args.port, args.data_dir, args.token_db)


if __name__ == "__main__":
    raise SystemExit(main())
