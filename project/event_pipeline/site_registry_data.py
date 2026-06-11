"""Loader for the site registry seed/fallback data.

Real customer site data is kept OUT of git in ``site_registry.json`` (gitignored,
shipped to prod boxes out-of-band). A synthetic ``site_registry.example.json`` is
committed so dev/CI and fresh clones run on fictional data. Runtime resolution
still prefers the canonical CouchDB ``btq_sites`` registry; this data is the
fallback matcher (``event_pipeline.sites.SITES``) and the seed source
(``migrate_sites.SITE_VISION_CONTEXTS``).

Resolution order for the data file:
  1. ``$BTQ_SITE_REGISTRY_PATH`` if set (operator override).
  2. ``site_registry.json`` next to this module (real data, gitignored) if present.
  3. ``site_registry.example.json`` next to this module (synthetic, committed).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_REAL = _DIR / "site_registry.json"
_EXAMPLE = _DIR / "site_registry.example.json"

BUILTIN_SANDBOX_SITE: dict[str, Any] = {
    "canonical": "Sandbox Site",
    "site_id": "SANDBOX",
    "note_path": "Accounts/Sandbox/Locations/SANDBOX - Sandbox Site/about.md",
    "aliases": ["sandbox", "sandbox site", "sandbox-site", "the sandbox", "SANDBOX"],
}
BUILTIN_SANDBOX_VISION_CONTEXT: dict[str, str] = {
    "context_id": "SANDBOX",
    "label": "Sandbox Site",
    "environment": "demo / test sandbox site",
    "summary": (
        "Demo/test sandbox site for exercising capture, semantic extraction, and review end-to-end. "
        "Treat as a generic commercial cleaning site with the usual restrooms, break rooms, offices, "
        "entryways, and supply areas."
    ),
}

_cache: dict[str, Any] | None = None


def _resolve_path() -> Path:
    override = os.environ.get("BTQ_SITE_REGISTRY_PATH", "").strip()
    if override:
        return Path(override)
    if _REAL.exists():
        return _REAL
    return _EXAMPLE


def _ensure_builtin_sandbox(data: dict[str, Any]) -> dict[str, Any]:
    sites = data["sites"]
    contexts = data["vision_contexts"]
    if not any(site.get("site_id") == "SANDBOX" for site in sites if isinstance(site, dict)):
        sandbox_site = dict(BUILTIN_SANDBOX_SITE)
        sandbox_site["aliases"] = list(BUILTIN_SANDBOX_SITE["aliases"])
        sites.append(sandbox_site)
    if "SANDBOX" not in contexts:
        contexts["SANDBOX"] = dict(BUILTIN_SANDBOX_VISION_CONTEXT)
    return data


def load_site_registry(*, force_reload: bool = False) -> dict[str, Any]:
    """Return the parsed registry: ``{"sites": [...], "vision_contexts": {...}}``."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    path = _resolve_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    sites = data.get("sites")
    contexts = data.get("vision_contexts")
    if not isinstance(sites, list) or not isinstance(contexts, dict):
        raise ValueError(f"site registry at {path} must have list 'sites' and dict 'vision_contexts'")
    _cache = _ensure_builtin_sandbox({"sites": sites, "vision_contexts": contexts})
    return _cache


def load_sites() -> list[dict[str, Any]]:
    return load_site_registry()["sites"]


def load_vision_contexts() -> dict[str, dict[str, str]]:
    return load_site_registry()["vision_contexts"]


# --- Supply/equipment brand keywords (real brands kept out of git; prompt 305) ---
_BRAND_REAL = _DIR / "brand_keywords.json"
_BRAND_EXAMPLE = _DIR / "brand_keywords.example.json"
_brand_cache: dict[str, Any] | None = None


def _resolve_brand_path() -> Path:
    override = os.environ.get("BTQ_BRAND_KEYWORDS_PATH", "").strip()
    if override:
        return Path(override)
    if _BRAND_REAL.exists():
        return _BRAND_REAL
    return _BRAND_EXAMPLE


def load_brand_keywords(category: str, *, force_reload: bool = False) -> tuple[str, ...]:
    """Return the real brand-name match keywords for a category (supply|equipment).

    Real brands live in the gitignored brand_keywords.json (shipped to prod boxes);
    a synthetic brand_keywords.example.json is committed for dev/CI.
    """
    global _brand_cache
    if _brand_cache is None or force_reload:
        path = _resolve_brand_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"brand keywords at {path} must be a JSON object")
        _brand_cache = data
    value = _brand_cache.get(category, [])
    if not isinstance(value, list):
        raise ValueError(f"brand keywords category '{category}' must be a list")
    return tuple(str(v) for v in value)
