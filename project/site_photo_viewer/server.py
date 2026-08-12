from __future__ import annotations

import argparse
import html
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, unquote, urlsplit, urlunsplit

from event_pipeline import couchdb_config
from event_pipeline.couchdb_capture_reader import (
    query_capture_target_by_upload_id,
    query_captures_by_site_id,
)
from event_pipeline.couchdb_registry import CouchDBSiteRegistry
from instance_config import get_instance_config
from media_store import MediaStore, get_media_store
from token_store import TokenRecord, TokenStore

from .read_model import (
    CapturePhotoProjection,
    SitePhotoCorpus,
    SitePhotoPage,
    TokenSiteScope,
    TokenSiteScopeError,
    VisionByCaptureProjection,
)


APP_NAME = "site_photo_viewer"
APP_VERSION = 1
DEFAULT_TOKEN_DB = Path("/srv/btq/data/field_capture_tokens.sqlite3")
DEFAULT_UPLOAD_ROOT = Path("/srv/btq/data/uploads")
VIEWER_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self' https:; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Robots-Tag": "noindex,nofollow,noarchive",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": VIEWER_CSP,
}

CaptureReader = Callable[..., list[dict[str, Any]]]
TargetLookupReader = Callable[..., dict[str, str] | None]
LabelResolver = Callable[[str], str | None]
Response = tuple[HTTPStatus, str, bytes, dict[str, str]]


@dataclass(frozen=True)
class ViewerDependencies:
    config: couchdb_config.CouchDBConfig
    captures_database: str
    upload_root: Path
    media_store: MediaStore
    capture_reader: CaptureReader = query_captures_by_site_id
    target_lookup_reader: TargetLookupReader = query_capture_target_by_upload_id
    label_resolver: LabelResolver | None = None


class SitePhotoViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token_store: TokenStore,
        dependencies: ViewerDependencies,
    ) -> None:
        super().__init__(address, SitePhotoViewerHandler)
        self.token_store = token_store
        self.dependencies = dependencies


class SitePhotoViewerHandler(BaseHTTPRequestHandler):
    server: SitePhotoViewerServer

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        try:
            response_parts = route_response(
                method,
                self.path,
                self.server.token_store,
                dependencies=self.server.dependencies,
            )
        except Exception as exc:
            logging.warning("site photo viewer request failed closed: %s", type(exc).__name__)
            response_parts = html_error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "The photo viewer is temporarily unavailable.",
            )
        self._write_response(response_parts, suppress_body=method == "HEAD")

    def _write_response(self, response_parts: Response, *, suppress_body: bool) -> None:
        status, content_type, body, headers = response_parts
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not suppress_body and body:
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                logging.info("viewer client disconnected while writing response")

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Keep parser errors and unknown HTTP methods inside the header policy."""
        del message, explain
        status = HTTPStatus.METHOD_NOT_ALLOWED if code == HTTPStatus.NOT_IMPLEMENTED else HTTPStatus(code)
        headers = {"Allow": "GET, HEAD"} if status == HTTPStatus.METHOD_NOT_ALLOWED else None
        response_parts = html_error(status, status.phrase)
        if headers:
            response_parts[3].update(headers)
        self._write_response(response_parts, suppress_body=getattr(self, "command", "") == "HEAD")

    def log_message(self, format: str, *args: object) -> None:
        sanitized_args = tuple(redact_token_text(arg) if isinstance(arg, str) else arg for arg in args)
        super().log_message(format, *sanitized_args)


def route_response(
    method: str,
    path: str,
    token_store: TokenStore,
    *,
    dependencies: ViewerDependencies | None = None,
    config: couchdb_config.CouchDBConfig | None = None,
    captures_database: str | None = None,
    upload_root: Path = DEFAULT_UPLOAD_ROOT,
    media_store: MediaStore | None = None,
    capture_reader: CaptureReader = query_captures_by_site_id,
    target_lookup_reader: TargetLookupReader = query_capture_target_by_upload_id,
    label_resolver: LabelResolver | None = None,
) -> Response:
    """Return a complete response without relying on handler state.

    The explicit dependency arguments are a small test seam; production passes a
    single ``ViewerDependencies`` instance from ``SitePhotoViewerServer``.
    """
    normalized_method = method.upper()
    if normalized_method not in {"GET", "HEAD"}:
        return json_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            extra_headers={"Allow": "GET, HEAD"},
        )

    try:
        parsed = urlsplit(path)
    except ValueError:
        return html_error(HTTPStatus.BAD_REQUEST, "Invalid request.")
    if parsed.path == "/api/health":
        return json_response({"app": APP_NAME, "version": APP_VERSION})
    if parsed.path != "/" and not parsed.path.startswith("/media/"):
        return html_error(HTTPStatus.NOT_FOUND, "Not found")

    query = parse_qs(parsed.query, keep_blank_values=True)
    token = first_query_value(query, "token").strip()
    try:
        record = token_store.authenticate(token) if token else None
    except Exception as exc:
        logging.warning("site photo viewer token store request failed: %s", type(exc).__name__)
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")
    if record is None:
        return html_error(HTTPStatus.UNAUTHORIZED, "Access requires a valid viewer token.")

    if dependencies is None:
        if config is None:
            config = couchdb_config.from_env()
        dependencies = ViewerDependencies(
            config=config,
            captures_database=captures_database or couchdb_config.field_captures_database(),
            upload_root=upload_root,
            media_store=media_store if media_store is not None else _UnavailableMediaStore(),
            capture_reader=capture_reader,
            target_lookup_reader=target_lookup_reader,
            label_resolver=label_resolver,
        )

    if parsed.path.startswith("/media/"):
        return route_media(parsed.path, token, record, dependencies)
    return route_page(query, token, record, dependencies)


def route_page(
    query: Mapping[str, Sequence[str]],
    token: str,
    record: TokenRecord,
    dependencies: ViewerDependencies,
) -> Response:
    site_was_supplied = "site" in query
    selected_site = first_query_value(query, "site").strip() if site_was_supplied else None
    try:
        base_scope = TokenSiteScope.from_token(record, label_resolver=dependencies.label_resolver)
    except TokenSiteScopeError:
        return html_error(HTTPStatus.FORBIDDEN, "This token is not authorized for the viewer.")
    except Exception as exc:
        logging.warning("site photo viewer site label lookup failed: %s", type(exc).__name__)
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")

    if len(base_scope.allowed_site_ids) == 1 and site_was_supplied:
        return html_error(HTTPStatus.FORBIDDEN, "This request is not authorized.")
    if len(base_scope.allowed_site_ids) > 1 and site_was_supplied and not selected_site:
        return html_error(HTTPStatus.FORBIDDEN, "This request is not authorized.")

    try:
        scope = TokenSiteScope.from_token(
            record,
            selected_site_id=selected_site,
            label_resolver=dependencies.label_resolver,
        )
    except TokenSiteScopeError:
        return html_error(HTTPStatus.FORBIDDEN, "This request is not authorized.")
    except Exception as exc:
        logging.warning("site photo viewer site label lookup failed: %s", type(exc).__name__)
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")

    if scope.selected_site_id is None:
        return html_response(render_site_picker(scope, token))

    try:
        captures = dependencies.capture_reader(
            dependencies.config,
            scope.selected_site_id,
            database=dependencies.captures_database,
        )
        capture_photos = CapturePhotoProjection.from_capture_rows(
            captures,
            site_id=scope.selected_site_id,
            site_label=scope.site_labels[scope.selected_site_id],
            upload_root=dependencies.upload_root,
        )
        corpus = SitePhotoCorpus.join(capture_photos, VisionByCaptureProjection.from_mapping({}))
        page = SitePhotoPage.from_corpus(corpus)
    except Exception as exc:
        logging.warning("site photo viewer capture read failed: %s", type(exc).__name__)
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")
    return html_response(render_latest_page(scope, token, page))


def route_media(
    route_path: str,
    token: str,
    record: TokenRecord,
    dependencies: ViewerDependencies,
) -> Response:
    try:
        scope = TokenSiteScope.from_token(record)
    except TokenSiteScopeError:
        return html_error(HTTPStatus.FORBIDDEN, "This token is not authorized for the viewer.")

    encoded_id = route_path.removeprefix("/media/")
    try:
        media_key = unquote(encoded_id, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return html_error(HTTPStatus.NOT_FOUND, "Not found")
    if not _valid_media_key(media_key):
        return html_error(HTTPStatus.NOT_FOUND, "Not found")

    ownership = prove_authorized_photo_key(scope, media_key, dependencies)
    if ownership == HTTPStatus.FORBIDDEN:
        return html_error(HTTPStatus.FORBIDDEN, "This request is not authorized.")
    if ownership == HTTPStatus.SERVICE_UNAVAILABLE:
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")
    if ownership != HTTPStatus.OK:
        return html_error(HTTPStatus.NOT_FOUND, "Not found")

    try:
        if not dependencies.media_store.exists(media_key):
            return html_error(HTTPStatus.NOT_FOUND, "Not found")
        location = dependencies.media_store.url_for(media_key)
    except Exception as exc:
        logging.warning("site photo viewer media store request failed: %s", type(exc).__name__)
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")
    if not isinstance(location, str) or not location.startswith("https://"):
        logging.warning("site photo viewer media signing returned an invalid location")
        return html_error(HTTPStatus.SERVICE_UNAVAILABLE, "The photo viewer is temporarily unavailable.")
    return response(HTTPStatus.FOUND, "text/plain; charset=utf-8", b"", {"Location": location})


def prove_authorized_photo_key(
    scope: TokenSiteScope,
    media_key: str,
    dependencies: ViewerDependencies,
) -> HTTPStatus:
    """Prove that ``media_key`` is a photo in canonical capture metadata.

    The upload-id view narrows modern records to their canonical owner. Old
    records without emitted upload IDs fall back to each explicitly authorized
    site's canonical capture rows. The photo-row verification also prevents an
    audio upload from being exposed through the photo endpoint.
    """
    target: dict[str, str] | None = None
    target_lookup_failed = False
    try:
        target = dependencies.target_lookup_reader(
            dependencies.config,
            media_key,
            database=dependencies.captures_database,
        )
    except Exception as exc:
        target_lookup_failed = True
        logging.warning("site photo viewer media ownership lookup failed: %s", type(exc).__name__)

    if target is not None:
        target_type = str(target.get("target_type") or "").strip()
        target_site = str(target.get("site_id") or "").strip()
        if target_type != "location":
            return HTTPStatus.NOT_FOUND
        if not target_site or target_site not in scope.allowed_site_ids:
            return HTTPStatus.FORBIDDEN
        sites_to_check = (target_site,)
    else:
        sites_to_check = scope.allowed_site_ids

    canonical_lookup_failed = False
    for site_id in sites_to_check:
        try:
            captures = dependencies.capture_reader(
                dependencies.config,
                site_id,
                database=dependencies.captures_database,
            )
        except Exception as exc:
            canonical_lookup_failed = True
            logging.warning("site photo viewer canonical media lookup failed: %s", type(exc).__name__)
            continue
        photos = CapturePhotoProjection.from_capture_rows(
            captures,
            site_id=site_id,
            site_label=site_id,
            upload_root=dependencies.upload_root,
        )
        if any(photo.media_key == media_key for photo in photos):
            return HTTPStatus.OK

    if canonical_lookup_failed or (target_lookup_failed and not sites_to_check):
        return HTTPStatus.SERVICE_UNAVAILABLE
    return HTTPStatus.NOT_FOUND


def render_site_picker(scope: TokenSiteScope, token: str) -> str:
    items = []
    for site_id in scope.allowed_site_ids:
        href = viewer_url(token, site_id=site_id)
        items.append(
            f'<li><a href="{html.escape(href, quote=True)}">'
            f"{html.escape(scope.site_labels[site_id])}</a></li>"
        )
    body = "<h1>Choose a site</h1><ul>" + "".join(items) + "</ul>"
    return html_document("Choose a site", body)


def render_latest_page(scope: TokenSiteScope, token: str, page: SitePhotoPage) -> str:
    assert scope.selected_site_id is not None
    label = html.escape(scope.site_labels[scope.selected_site_id])
    items: list[str] = []
    for photo in page.photos:
        filename = html.escape(photo.filename or "Photo")
        if photo.media_key:
            media_href = media_url(photo.media_key, token)
            media_markup = (
                f'<a href="{html.escape(media_href, quote=True)}">'
                f'<img src="{html.escape(media_href, quote=True)}" alt="{filename}" loading="lazy"></a>'
            )
        else:
            media_markup = "<span>Media reference is invalid.</span>"
        items.append(f"<li>{media_markup}<p>{filename}</p></li>")
    empty = "<p>No photos are available.</p>" if not items else ""
    picker_link = ""
    if len(scope.allowed_site_ids) > 1:
        picker_link = f'<p><a href="{html.escape(viewer_url(token), quote=True)}">Choose another site</a></p>'
    body = (
        f"<h1>{label}</h1><h2>Latest photos</h2>{picker_link}"
        f"<p>{page.total_results} photos</p>{empty}<ul>{''.join(items)}</ul>"
    )
    return html_document(f"Latest photos — {scope.site_labels[scope.selected_site_id]}", body)


def html_document(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta name=\"robots\" content=\"noindex,nofollow,noarchive\">"
        "<meta name=\"color-scheme\" content=\"light dark\">"
        f"<title>{html.escape(title)}</title></head><body><main>{body}</main></body></html>"
    )


def viewer_url(token: str, *, site_id: str | None = None) -> str:
    values = {"token": token}
    if site_id is not None:
        values["site"] = site_id
    return f"/?{urlencode(values)}"


def media_url(media_key: str, token: str) -> str:
    return f"/media/{quote(media_key, safe='')}?{urlencode({'token': token})}"


def first_query_value(query: Mapping[str, Sequence[str]], key: str) -> str:
    values = query.get(key, ())
    return str(values[0]) if values else ""


def _valid_media_key(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def redact_token_text(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.query:
        return value
    query = parse_qs(parts.query, keep_blank_values=True)
    if "token" not in query:
        return value
    query["token"] = ["[redacted]"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


def response(
    status: HTTPStatus,
    content_type: str,
    body: bytes,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    headers = dict(SECURITY_HEADERS)
    headers.update(extra_headers or {})
    return status, content_type, body, headers


def html_response(document: str, status: HTTPStatus = HTTPStatus.OK) -> Response:
    return response(status, "text/html; charset=utf-8", document.encode("utf-8"))


def html_error(status: HTTPStatus, message: str) -> Response:
    return html_response(html_document(status.phrase, f"<h1>{html.escape(status.phrase)}</h1><p>{html.escape(message)}</p>"), status)


def json_response(payload: Mapping[str, object], status: HTTPStatus = HTTPStatus.OK) -> Response:
    body = (json.dumps(dict(payload)) + "\n").encode("utf-8")
    return response(status, "application/json; charset=utf-8", body)


def json_error(
    status: HTTPStatus,
    code: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> Response:
    body = (json.dumps({"error": code}, separators=(",", ":")) + "\n").encode("utf-8")
    return response(status, "application/json; charset=utf-8", body, extra_headers)


class _UnavailableMediaStore:
    def write(self, key: str, data: bytes) -> None:
        raise RuntimeError("media store is unavailable")

    def read(self, key: str) -> bytes:
        raise RuntimeError("media store is unavailable")

    def exists(self, key: str) -> bool:
        raise RuntimeError("media store is unavailable")

    def url_for(self, key: str) -> str:
        raise RuntimeError("media store is unavailable")


def required_s3_media_store(upload_root: Path) -> MediaStore:
    instance = get_instance_config()
    if str(instance.media_store).strip().lower() != "s3":
        raise RuntimeError("BTQ_MEDIA_STORE must be s3 for the site photo viewer")
    return get_media_store(upload_root, instance)


def build_dependencies(upload_root: Path) -> ViewerDependencies:
    config = couchdb_config.from_env()
    registry = CouchDBSiteRegistry(
        base_url=config.base_url,
        username=config.username,
        password=config.password,
        database=couchdb_config.vault_database(),
        timeout=config.timeout,
    )
    return ViewerDependencies(
        config=config,
        captures_database=couchdb_config.field_captures_database(),
        upload_root=upload_root.expanduser().resolve(strict=False),
        media_store=required_s3_media_store(upload_root),
        label_resolver=registry.resolve_canonical,
    )


def run(host: str, port: int, token_db: Path, upload_root: Path = DEFAULT_UPLOAD_ROOT) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        dependencies = build_dependencies(upload_root)
        if not token_db.expanduser().is_file():
            raise RuntimeError(f"token database not found: {token_db}")
        server = SitePhotoViewerServer(
            (host, port),
            token_store=TokenStore(token_db),
            dependencies=dependencies,
        )
    except Exception as exc:
        logging.error("site photo viewer startup refused: %s", _one_line_reason(exc))
        return 1

    logging.info("site photo viewer listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _one_line_reason(exc: Exception) -> str:
    return " ".join(str(exc).splitlines()).strip() or type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the token-gated site photo viewer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument(
        "--token-db",
        type=Path,
        default=Path(os.environ.get("BTQ_SITE_PHOTO_VIEWER_TOKEN_DB", str(DEFAULT_TOKEN_DB))),
    )
    parser.add_argument(
        "--upload-root",
        type=Path,
        default=Path(os.environ.get("BTQ_SITE_PHOTO_VIEWER_UPLOAD_ROOT", str(DEFAULT_UPLOAD_ROOT))),
    )
    args = parser.parse_args()
    return run(args.host, args.port, args.token_db, args.upload_root)


if __name__ == "__main__":
    raise SystemExit(main())
