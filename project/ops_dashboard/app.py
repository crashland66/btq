from __future__ import annotations

import argparse
import json
import mimetypes
import os
import resource
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config import get_config
from field_capture.site_viewer import UnsafeMediaPath, resolve_media_request
from ops_dashboard.common import SectionContext
from ops_dashboard.layout import html_page
from ops_dashboard.sections import admin, audio, batch_images, candidates, captures, drafts, employee_detail, employees, equipment, failed, field_photos, health, health_pipeline, help as help_section, home, inbox, issues, photos, prospect_detail, site_detail, sites, supplies, system, tokens
from shared_pwa.assets import serve_static_asset

# Cap concurrent request workers. stdlib ThreadingHTTPServer spawns an
# unbounded thread per request, which lets a burst of concurrent hits both
# wedge the dashboard (request pileup) and inflate Python's per-thread state
# without bound. A small fixed pool reuses workers and bounds the high-water
# mark of in-flight rendering.
MAX_REQUEST_WORKERS = 8
# Self-restart threshold. macOS malloc holds onto large freed arenas (vmmap
# shows them as MALLOC_LARGE "empty") rather than returning them to the OS,
# so a long-lived dashboard process accretes resident memory across the day.
# Exit cleanly when RSS exceeds this; launchd's KeepAlive=true brings it
# right back up, and the user sees at most one ~6s cold load.
MEMORY_WATCHDOG_RSS_BYTES = 512 * 1024 * 1024
MEMORY_WATCHDOG_INTERVAL_SECONDS = 60


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


OPS_DASHBOARD_MAX_REQUEST_BYTES = _env_int("BTQ_OPS_DASHBOARD_MAX_REQUEST_BYTES", 1_000_000)
# Match field-capture's request cap: 6 images at 10 MiB each plus multipart overhead.
OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES = _env_int("BTQ_OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES", 70 * 1024 * 1024)
BATCH_UPLOAD_POST_PATHS = frozenset({"/batch-images/upload"})

def build_status(runtime_root: Path, *, log_lines: int = 40) -> dict[str, object]:
    return health.build_status(request_context("/api/status", runtime_root), log_lines=log_lines)


SECTION_ROUTES = {
    "/": home, "/inbox": inbox, "/admin": admin, "/health": health, "/health/pipeline": health_pipeline,
    "/candidates": candidates, "/field-capture/review": candidates, "/field-capture/issues": issues,
    "/supplies": supplies, "/supplies/mark-ordered-confirm": supplies, "/supplies/mark-delivered-confirm": supplies,
    "/supplies/mark-stocked-confirm": supplies, "/supplies/mark-no-action-needed-confirm": supplies,
    "/equipment": equipment, "/equipment/mark-approved-confirm": equipment, "/equipment/mark-denied-confirm": equipment,
    "/equipment/mark-ordered-confirm": equipment, "/equipment/mark-provided-confirm": equipment,
    "/equipment/mark-no-action-needed-confirm": equipment, "/drafts": drafts, "/drafts/stage-preview": drafts,
    "/failed": failed, "/captures": captures, "/audio": audio, "/batch-images": batch_images,
    "/photos": photos, "/field-photos": field_photos, "/sites": sites, "/sites/new": sites,
    "/employees": employees, "/tokens": tokens, "/tokens/new": tokens, "/tokens/set-raw": tokens,
    "/system": system, "/help": help_section,
}

def request_context(path: str, runtime_root: Path) -> SectionContext:
    parsed = urlsplit(path)
    ctx = SectionContext(runtime_root, get_config)
    ctx.query = parse_qs(parsed.query, keep_blank_values=True)
    ctx.route_path = parsed.path
    return ctx


def max_request_bytes_for_path(path: str) -> int:
    return OPS_DASHBOARD_BATCH_UPLOAD_MAX_BYTES if urlsplit(path).path in BATCH_UPLOAD_POST_PATHS else OPS_DASHBOARD_MAX_REQUEST_BYTES


def json_response(payload: object, status: HTTPStatus = HTTPStatus.OK) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return status, "application/json; charset=utf-8", json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"), {}


VAULT_PROJECTION_DIR_ENV = "BTQ_VAULT_PROJECTION_DIR"
VAULT_PROJECTION_DIR_DEFAULT = "~/btq_projection"


def serve_vault_response(route_path: str) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    projection_dir_raw = os.environ.get(VAULT_PROJECTION_DIR_ENV, VAULT_PROJECTION_DIR_DEFAULT).strip()
    projection_dir = Path(projection_dir_raw).expanduser().resolve()
    rel = route_path.removeprefix("/vault").lstrip("/")
    if not rel or rel.endswith("/"):
        rel = (rel or "") + "index.html"
    # Guard against path traversal.
    target = (projection_dir / rel).resolve()
    if not str(target).startswith(str(projection_dir) + os.sep) and target != projection_dir:
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    if target.is_dir():
        target = target / "index.html"
    if not target.exists() or not target.is_file():
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    try:
        return HTTPStatus.OK, content_type, target.read_bytes(), {"Cache-Control": "no-store"}
    except OSError:
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)


def static_response(route_path: str) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    try:
        content_type, body = serve_static_asset(route_path, Path(__file__).resolve().parent / "static")
        return HTTPStatus.OK, content_type, body, {"Cache-Control": "public, max-age=300"}
    except OSError:
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)


def serve_media_response(media_path: str, runtime_root: Path) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    upload_dir = runtime_root.expanduser().resolve(strict=False) / "uploads"
    try:
        path = resolve_media_request(media_path, upload_dir)
    except UnsafeMediaPath:
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    if not path.exists() or not path.is_file():
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        return HTTPStatus.OK, content_type, path.read_bytes(), {}
    except OSError:
        return json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)


def render_section(module: object, ctx: SimpleNamespace) -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    return HTTPStatus.OK, "text/html; charset=utf-8", module.render(ctx).encode("utf-8"), {}


def route_response_with_headers(method: str, path: str, runtime_root: Path, body: bytes = b"", content_type: str = "") -> tuple[HTTPStatus, str, bytes, dict[str, str]]:
    parsed = urlsplit(path)
    route_path = parsed.path
    ctx = request_context(path, runtime_root)
    if method == "POST" and route_path in {"/field-capture/review/approve", "/field-capture/review/reject"}:
        ctx.action = "approve" if route_path.endswith("/approve") else "reject"
        return candidates.handle_review_post(ctx, body)
    if method == "POST" and route_path == "/field-capture/review/client-informed":
        return candidates.handle_client_informed(ctx, body)
    if method == "POST" and route_path == "/field-capture/review/resolve":
        return candidates.handle_resolve(ctx, body)
    if method == "POST" and route_path == "/field-capture/review/dismiss-completion":
        return candidates.handle_completion_dismiss_post(ctx, body)
    if method == "POST" and route_path == "/drafts/generate":
        return drafts.handle_generate_post(ctx, body)
    if method == "POST" and route_path == "/drafts/stage":
        return drafts.handle_stage_post(ctx, body)
    # Supply status transition staging.
    if method == "POST" and route_path == "/supplies/mark-ordered":
        return supplies.handle_mark_supply_ordered(ctx, body)
    if method == "POST" and route_path == "/supplies/mark-delivered":
        return supplies.handle_mark_supply_delivered(ctx, body)
    if method == "POST" and route_path == "/supplies/mark-stocked":
        return supplies.handle_mark_supply_stocked(ctx, body)
    if method == "POST" and route_path == "/supplies/mark-no-action-needed":
        return supplies.handle_mark_supply_no_action_needed(ctx, body)
    # Equipment status transition staging.
    if method == "POST" and route_path == "/equipment/mark-approved":
        return equipment.handle_mark_equipment_approved(ctx, body)
    if method == "POST" and route_path == "/equipment/mark-denied":
        return equipment.handle_mark_equipment_denied(ctx, body)
    if method == "POST" and route_path == "/equipment/mark-ordered":
        return equipment.handle_mark_equipment_ordered(ctx, body)
    if method == "POST" and route_path == "/equipment/mark-provided":
        return equipment.handle_mark_equipment_provided(ctx, body)
    if method == "POST" and route_path == "/equipment/mark-no-action-needed":
        return equipment.handle_mark_equipment_no_action_needed(ctx, body)
    if method == "POST" and route_path == "/failed/retry-sidecar":
        return failed.handle_retry_sidecar_post(ctx, body)
    if method == "POST" and route_path == "/sites/save":
        return sites.handle_save_post(ctx, body)
    if method == "POST" and route_path.endswith("/save-section"):
        for prefix, handler in (("/sites/", site_detail.handle_save_section), ("/employees/", employee_detail.handle_save_section)):
            entity_id = route_path.removeprefix(prefix).removesuffix("/save-section") if route_path.startswith(prefix) else ""
            if entity_id and "/" not in entity_id: return handler(ctx, entity_id, body)
    if method == "POST" and route_path == "/sites/new":
        return sites.handle_new_post(ctx, body)
    if method == "POST" and route_path == "/tokens/new":
        return tokens.handle_new_post(ctx, body)
    if method == "POST" and route_path == "/tokens/revoke":
        return tokens.handle_revoke_post(ctx, body)
    if method == "POST" and route_path == "/tokens/regenerate":
        return tokens.handle_regenerate_post(ctx, body)
    if method == "POST" and route_path == "/tokens/set-raw":
        return tokens.handle_set_raw_post(ctx, body)
    if method == "POST" and route_path == "/system/save":
        return system.handle_save_post(ctx, body)
    if method == "POST" and route_path == "/vault-home/voice-memo":
        return home.handle_voice_memo_post(ctx, body, content_type=content_type)
    if method == "POST" and route_path == "/batch-images/upload": return batch_images.handle_batch_upload_post(ctx, body, content_type=content_type)
    if method == "POST" and route_path.startswith("/prospects/") and route_path not in SECTION_ROUTES:
        rest = route_path.removeprefix("/prospects/").rstrip("/")
        prospect_id, _sep, tail = rest.partition("/")
        if prospect_id and tail == "promote":
            return prospect_detail.handle_promote_post(ctx, body, prospect_id=prospect_id)
    if method != "GET":
        return json_response({"error": "read_only"}, HTTPStatus.METHOD_NOT_ALLOWED)
    if route_path == "/healthz":
        return json_response({"ok": True})
    if route_path == "/api/status":
        return json_response(build_status(runtime_root))
    if route_path == "/api/inbox.json":
        return json_response(inbox.inbox_payload(ctx))
    if route_path == "/vault" or route_path.startswith("/vault/"):
        return serve_vault_response(route_path)
    if route_path.startswith("/static/"):
        return static_response(route_path)
    if route_path.startswith("/media/"):
        return serve_media_response(route_path.removeprefix("/media/"), runtime_root)
    if route_path == "/runtime-file":
        return failed.handle_runtime_file(ctx)
    if route_path.startswith("/sites/") and route_path not in SECTION_ROUTES:
        site_id = route_path.removeprefix("/sites/").rstrip("/")
        if site_id and "/" not in site_id:
            return (HTTPStatus.OK, "text/html; charset=utf-8", site_detail.render(ctx, site_id).encode("utf-8"), {})
    if route_path.startswith("/employees/") and route_path not in SECTION_ROUTES:
        employee_id = route_path.removeprefix("/employees/").rstrip("/")
        if employee_id and "/" not in employee_id:
            return (HTTPStatus.OK, "text/html; charset=utf-8", employee_detail.render(ctx, employee_id).encode("utf-8"), {})
    if route_path.startswith("/prospects/") and route_path not in SECTION_ROUTES:
        prospect_id = route_path.removeprefix("/prospects/").rstrip("/")
        if prospect_id and "/" not in prospect_id:
            return (HTTPStatus.OK, "text/html; charset=utf-8", prospect_detail.render(ctx, prospect_id).encode("utf-8"), {})
    section = SECTION_ROUTES.get(route_path)
    if section is not None:
        return render_section(section, ctx)
    not_found_body = (
        '<header><h1>Not Found</h1>'
        '<p class="muted">This page does not exist.</p>'
        '<p><a href="/">Return to Inbox</a></p></header>'
    )
    return (
        HTTPStatus.NOT_FOUND,
        "text/html; charset=utf-8",
        html_page("Not Found", not_found_body, active_section="").encode("utf-8"),
        {},
    )


def route_response(method: str, path: str, runtime_root: Path, body: bytes = b"") -> tuple[HTTPStatus, str, bytes]:
    status, content_type, response_body, _headers = route_response_with_headers(method, path, runtime_root, body)
    return status, content_type, response_body


class OpsDashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], runtime_root: Path) -> None:
        super().__init__(server_address, OpsDashboardHandler)
        self.runtime_root = runtime_root
        self._executor = ThreadPoolExecutor(max_workers=MAX_REQUEST_WORKERS, thread_name_prefix="ops-dashboard")

    def process_request(self, request: object, client_address: tuple[str, int]) -> None:
        # Override the per-request thread spawn from ThreadingMixIn so requests
        # run on a bounded pool instead. The pool reuses workers, so neither
        # the thread count nor the per-thread allocations balloon under load.
        self._executor.submit(self._process_one, request, client_address)

    def _process_one(self, request: object, client_address: tuple[str, int]) -> None:
        try:
            self.process_request_thread(request, client_address)
        except Exception:
            # Match ThreadingMixIn behavior - the per-request thread swallows
            # exceptions; otherwise an unhandled error in one request would
            # take down the pool worker for all subsequent requests.
            self.handle_error(request, client_address)
            self.shutdown_request(request)

    def server_close(self) -> None:
        self._executor.shutdown(wait=False)
        super().server_close()


def _current_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is in bytes on Darwin and kilobytes on Linux.
    return int(usage) if sys.platform == "darwin" else int(usage) * 1024


def start_memory_watchdog() -> None:
    def watch() -> None:
        while True:
            time.sleep(MEMORY_WATCHDOG_INTERVAL_SECONDS)
            try:
                rss = _current_rss_bytes()
            except Exception:
                continue
            if rss > MEMORY_WATCHDOG_RSS_BYTES:
                print(
                    f"memory watchdog: RSS {rss // (1024 * 1024)} MB > "
                    f"{MEMORY_WATCHDOG_RSS_BYTES // (1024 * 1024)} MB limit, "
                    f"exiting for launchd-managed restart",
                    flush=True,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    thread = threading.Thread(target=watch, name="memory-watchdog", daemon=True)
    thread.start()


class OpsDashboardHandler(BaseHTTPRequestHandler):
    server: OpsDashboardServer

    def do_GET(self) -> None:
        status, content_type, body, headers = route_response_with_headers("GET", self.path, self.server.runtime_root)
        self.write_response(status, content_type, body, headers)

    def do_POST(self) -> None:
        raw_content_length = self.headers.get("Content-Length", "")
        try:
            content_length = int(raw_content_length or "0")
        except ValueError:
            self.write_response(*json_response({"error": "invalid_content_length"}, HTTPStatus.BAD_REQUEST))
            return
        if content_length < 0:
            self.write_response(*json_response({"error": "invalid_content_length"}, HTTPStatus.BAD_REQUEST))
            return
        max_request_bytes = max_request_bytes_for_path(self.path)
        if content_length > max_request_bytes:
            self.write_response(*json_response({"error": "request_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE))
            return
        body = self.rfile.read(content_length) if content_length > 0 else b""
        req_content_type = self.headers.get("Content-Type", "")
        status, content_type, response_body, headers = route_response_with_headers("POST", self.path, self.server.runtime_root, body, req_content_type)
        self.write_response(status, content_type, response_body, headers)

    def write_response(self, status: HTTPStatus, content_type: str, body: bytes, headers: dict[str, str] | None = None) -> None:
        headers = headers or {}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(host: str, port: int, runtime_root: Path) -> int:
    server = OpsDashboardServer((host, port), runtime_root.expanduser())
    start_memory_watchdog()
    for line in startup_lines(host, port, runtime_root):
        print(line)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBTQ Ops Dashboard stopped.")
    finally:
        server.server_close()
    return 0


def startup_lines(host: str, port: int, runtime_root: Path) -> list[str]:
    expanded_runtime_root = runtime_root.expanduser()
    return [
        "BTQ Ops Dashboard",
        f"URL: http://{host}:{port}/",
        f"Host: {host}",
        f"Port: {port}",
        f"Runtime root: {expanded_runtime_root}",
        "Operator UI: inbox, candidates, drafts, failed jobs, captures, health, and help.",
        "No direct vault writes and no queue processor invocation. "
        "POST actions may stage queue jobs, edit CouchDB admin docs, "
        "mutate token DB rows, or trigger configured token sync.",
        "Press Ctrl-C to stop.",
    ]


def build_parser() -> argparse.ArgumentParser:
    config = get_config()
    parser = argparse.ArgumentParser(
        description="Run the local BTQ ops dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind. Use localhost by default.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    parser.add_argument("--runtime-root", type=Path, default=config.runtime_root, help="BTQ runtime root to inspect.")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(args.host, args.port, args.runtime_root)

def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
