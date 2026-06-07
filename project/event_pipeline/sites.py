from __future__ import annotations

import logging
import os
import re

from event_pipeline.couchdb_registry import CouchDBRegistryError, CouchDBSiteRegistry
from event_pipeline.site_registry_data import load_sites


logger = logging.getLogger(__name__)


# Real customer site data lives in the gitignored event_pipeline/site_registry.json
# (synthetic example committed as site_registry.example.json). See site_registry_data.py.
SITES = load_sites()

_registry_instance: CouchDBSiteRegistry | None = None


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def word_overlap_ratio(text: str, alias: str) -> float:
    text_words = set(normalize_for_match(text).split())
    alias_words = set(normalize_for_match(alias).split())
    if not alias_words:
        return 0.0
    return len(text_words & alias_words) / len(alias_words)


def _get_registry() -> CouchDBSiteRegistry | None:
    """
    Returns a CouchDBSiteRegistry if BTQ_COUCHDB_URL is set in the environment,
    otherwise returns None (use hardcoded SITES).
    """
    global _registry_instance
    if not os.environ.get("BTQ_COUCHDB_URL"):
        _registry_instance = None
        return None
    if _registry_instance is None:
        _registry_instance = CouchDBSiteRegistry()
    return _registry_instance


def resolve_site(text: str) -> tuple[str, str]:
    registry = _get_registry()
    if registry is not None:
        try:
            return registry.resolve_site(text)
        except CouchDBRegistryError:
            logger.error(
                "CouchDB site registry unavailable; failing closed (resolve_site). "
                "Operator: investigate registry health, do not edit sites.py as a workaround."
            )
            raise

    normalized_text = normalize_for_match(text)

    for site in SITES:
        for alias in site["aliases"]:
            if normalized_text == normalize_for_match(alias):
                return site["canonical"], "high"

    for site in SITES:
        for alias in site["aliases"]:
            normalized_alias = normalize_for_match(alias)
            if normalized_alias in normalized_text:
                return site["canonical"], "medium"

    for site in SITES:
        for alias in site["aliases"]:
            if word_overlap_ratio(normalized_text, alias) >= 0.7:
                return site["canonical"], "medium"

    return "unknown", "low"


def resolve_site_id(site_name: str) -> str | None:
    registry = _get_registry()
    if registry is not None:
        try:
            normalized_site_id = site_name.strip()
            resolve_canonical = getattr(registry, "resolve_canonical", None)
            if (
                normalized_site_id
                and callable(resolve_canonical)
                and resolve_canonical(normalized_site_id) is not None
            ):
                return normalized_site_id
            site_id = registry.resolve_site_id(site_name)
            if site_id is not None:
                return site_id
            canonical, confidence = registry.resolve_site(site_name)
            if canonical != "unknown" and confidence in {"high", "medium"}:
                return registry.resolve_site_id(canonical)
            return None
        except CouchDBRegistryError:
            logger.error(
                "CouchDB site registry unavailable; failing closed (resolve_site_id). "
                "Operator: investigate registry health, do not edit sites.py as a workaround."
            )
            raise

    normalized_name = normalize_for_match(site_name)
    for site in SITES:
        if str(site["site_id"]) == site_name.strip():
            return str(site["site_id"])
        if normalize_for_match(site["canonical"]) == normalized_name:
            return str(site["site_id"])
        for alias in site["aliases"]:
            if normalize_for_match(alias) == normalized_name:
                return str(site["site_id"])
    return None


def resolve_site_note_path(site_name: str) -> str | None:
    registry = _get_registry()
    if registry is not None:
        try:
            return registry.resolve_site_note_path(site_name)
        except CouchDBRegistryError:
            logger.error(
                "CouchDB site registry unavailable; failing closed (resolve_site_note_path). "
                "Operator: investigate registry health, do not edit sites.py as a workaround."
            )
            raise

    normalized_name = normalize_for_match(site_name)
    for site in SITES:
        if normalize_for_match(site["canonical"]) == normalized_name:
            return str(site["note_path"])
    return None


def is_site_registered(site_name: str) -> bool:
    """True if `site_name` resolves to a registered site_id (registry when
    BTQ_COUCHDB_URL is set, else the SITES fallback). Used by the drop bridge
    to fail unroutable site-keyed jobs loudly at enqueue time, separate from
    the registry-blind validate_job contract."""
    return resolve_site_id(site_name) is not None
