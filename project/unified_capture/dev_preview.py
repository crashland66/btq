#!/usr/bin/env python3
"""Dependency-free preview server for the unified-capture PWA.

Purpose: look at the UI (especially the approval inbox) in a real browser
without standing up CouchDB, a token store, or the full ingest stack. It serves
the static files in ``public/`` and stubs just enough of the API for the shell
to render:

  GET /api/session  -> a fake authorized session (so the app enables) including
                       ``inbox_count`` so the mail-glyph badge lights up.
  GET /api/health   -> ok
  GET /api/my-submissions -> empty (so that panel doesn't error if opened)

The inbox itself runs in mock mode (INBOX_USE_MOCK = true in inbox.js), so its
cards come from the in-file fixture, not from any API. That means you can click
through badge -> open -> approve/reject -> already_decided -> empty with this
preview alone.

This is NOT the production server and writes nothing. Do not deploy it.
Production entrypoint remains ``python -m unified_capture.server``.

Run (from the project/ directory):

    python -m unified_capture.dev_preview
    # then open the URL it prints, e.g. http://127.0.0.1:8095/?token=preview

Bind to the tailnet instead of localhost so you can open it on your phone:

    python -m unified_capture.dev_preview --host 0.0.0.0 --port 8095
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC_ROOT = Path(__file__).resolve().parent / "public"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

# A fake session shaped like the real /api/session payload, plus inbox_count.
# inbox_count is set to match the three cards in inbox.js's MOCK so the badge
# and the opened list agree.
FAKE_SESSION = {
    "person": {"person_id": "preview-person", "name": "Preview Operator"},
    "token": {"token_id": "preview", "label": "Preview token"},
    "sites": [
        {"site_id": "7050", "label": "7050 — Summit Wire", "display_categories": []},
        {"site_id": "7060", "label": "7060 — Continental Metalworks", "display_categories": []},
    ],
    "can_submit": True,
    "inbox_count": 3,
}


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "app": "unified_capture_preview"})
            return
        if path == "/api/session":
            self._json(FAKE_SESSION)
            return
        if path == "/api/my-submissions":
            self._json({"submissions": [], "quality_summary": None})
            return
        if path == "/api/inbox":
            # Not used in mock mode, but provide a harmless empty live response
            # so flipping INBOX_USE_MOCK=false here degrades gracefully.
            self._json({"count": 0, "items": []})
            return
        if self._serve_static(path):
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        # Accept and ack inbox decisions so live-mode poking here doesn't 404.
        path = urlsplit(self.path).path
        if path in ("/api/inbox/approve", "/api/inbox/reject"):
            self._json({"status": "approved" if path.endswith("approve") else "rejected"})
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, path: str) -> bool:
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (PUBLIC_ROOT / relative).resolve()
        if candidate != PUBLIC_ROOT and PUBLIC_ROOT not in candidate.parents:
            return False
        if not candidate.is_file():
            return False
        content_type = CONTENT_TYPES.get(candidate.suffix)
        if content_type is None:
            return False
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # Quiet by default; uncomment for request logging.
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview the unified-capture PWA (no CouchDB).")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach from your phone over the tailnet.")
    parser.add_argument("--port", type=int, default=8095)
    args = parser.parse_args()

    if not PUBLIC_ROOT.is_dir():
        print(f"public dir not found: {PUBLIC_ROOT}")
        return 1

    server = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    url = f"http://{args.host}:{args.port}/?token=preview"
    print("unified-capture PREVIEW (static + stubbed session; no CouchDB, writes nothing)")
    print(f"serving {PUBLIC_ROOT}")
    print(f"open: {url}")
    print("the inbox runs in mock mode — click the ✉ glyph to see the cards. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
