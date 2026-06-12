from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


SHARED_PWA_ROOT = Path(__file__).resolve().parent
DB_JS_PATH = SHARED_PWA_ROOT / "db.js"
SW_TEMPLATE_PATH = SHARED_PWA_ROOT / "sw.js.template"
STATIC_ASSET_CONTENT_TYPES = {
    "/static/admin.css": "text/css; charset=utf-8",
    "/static/db.js": "application/javascript; charset=utf-8",
    "/static/recorder.js": "application/javascript; charset=utf-8",
}


@dataclass(frozen=True)
class ServiceWorkerConfig:
    product_label: str
    api_endpoint: str
    success_check: str
    permanent_failure_check: str = (
        "response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429"
    )
    sync_tag: str = "field-capture-drain"
    token_key: str = "fieldCaptureToken"
    # App-shell assets the SW precaches for offline cold-launch. Canonical paths
    # (no version query); the SW matches with ignoreSearch so `?v=…` URLs hit.
    shell_assets: tuple[str, ...] = ()


SERVICE_WORKERS = {
    "field_capture": ServiceWorkerConfig(
        product_label="Field-capture",
        api_endpoint="/api/submit",
        success_check="!response.ok",
        shell_assets=(
            "/",
            "/index.html",
            "/app.js",
            "/db.js",
            "/styles.css",
            "/manifest.webmanifest",
            "/assets/icon-180.png",
            "/assets/icon-192.png",
            "/assets/icon-512.png",
            "/assets/app-logo.png",
        ),
    ),
    "voice_memo": ServiceWorkerConfig(
        product_label="Voice-memo",
        api_endpoint="/api/upload",
        success_check="response.status !== 201 && response.status !== 200",
    ),
    "unified_capture": ServiceWorkerConfig(
        product_label="Field-capture",
        api_endpoint="/api/submit",
        success_check="!response.ok",
        permanent_failure_check=(
            "response.status >= 400 && response.status < 500 && response.status !== 404 && "
            "response.status !== 408 && response.status !== 429"
        ),
        sync_tag="unified-capture-drain",
        token_key="unifiedCaptureToken",
        shell_assets=(
            "/",
            "/index.html",
            "/app.js",
            "/inbox.js",
            "/styles.css",
            "/static/db.js",
            "/static/recorder.js",
            "/manifest.webmanifest",
            "/assets/icon.svg",
            "/assets/icon-180.png",
            "/assets/icon-192.png",
            "/assets/icon-512.png",
            "/assets/field-capture-header-light.svg",
            "/assets/field-capture-header-dark.svg",
        ),
    ),
}


def shared_db_bytes() -> bytes:
    return DB_JS_PATH.read_bytes()


def serve_static_asset(route_path: str, static_dir: Path) -> tuple[str, bytes]:
    content_type = STATIC_ASSET_CONTENT_TYPES.get(route_path)
    if content_type is None:
        raise FileNotFoundError(route_path)
    if route_path == "/static/db.js":
        return content_type, shared_db_bytes()
    asset = static_dir / Path(route_path).name
    return content_type, asset.read_bytes()


def render_service_worker(product: str) -> str:
    config = SERVICE_WORKERS[product]
    template = SW_TEMPLATE_PATH.read_text(encoding="utf-8")
    replacements = {
        "__PRODUCT_LABEL__": config.product_label,
        "__SYNC_TAG__": config.sync_tag,
        "__TOKEN_KEY__": config.token_key,
        "__API_ENDPOINT__": config.api_endpoint,
        "__SUCCESS_CHECK__": config.success_check,
        "__PERMANENT_FAILURE_CHECK__": config.permanent_failure_check,
        "__SHELL_ASSETS__": json.dumps(list(config.shell_assets)),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def render_service_worker_bytes(product: str) -> bytes:
    return render_service_worker(product).encode("utf-8")
