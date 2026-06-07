"""Tunable vocabulary for the event extractor.

The extractor and the audio semantic classifiers match transcript text against
named phrase lists. Those lists live in ``extraction_terms.yaml`` so they can
be tuned — system-wide and per site — without a code change.

This module loads that file, applies per-site adjustments, and exposes the
merged phrase lists. The public surface is intentionally small (``phrases``,
``matches_any``, ``matches_all``) so the backing store can later move from a
YAML file to CouchDB without changing callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_TERMS_PATH = Path(__file__).resolve().parent / "extraction_terms.yaml"

_SITE_OPS = ("extend", "remove", "replace")


class ExtractionTermsError(RuntimeError):
    """Raised when the extraction terms file is missing or malformed."""


class _SiteOverride:
    __slots__ = ("extend", "remove", "replace")

    def __init__(self) -> None:
        self.extend: dict[str, tuple[str, ...]] = {}
        self.remove: dict[str, tuple[str, ...]] = {}
        self.replace: dict[str, tuple[str, ...]] = {}


def _dedupe(phrases: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for phrase in phrases:
        if phrase not in seen:
            seen.add(phrase)
            ordered.append(phrase)
    return tuple(ordered)


def _coerce_phrase_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExtractionTermsError(f"{where} must be a list of strings")
    phrases: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ExtractionTermsError(f"{where} must contain only strings")
        phrases.append(item.lower())
    return _dedupe(phrases)


class ExtractionTerms:
    """Merged extraction vocabulary with per-site adjustments applied on demand."""

    def __init__(
        self,
        global_lists: dict[str, tuple[str, ...]],
        site_overrides: dict[str, _SiteOverride],
    ) -> None:
        self._global = global_lists
        self._sites = site_overrides

    def list_ids(self) -> tuple[str, ...]:
        return tuple(self._global.keys())

    def phrases(self, list_id: str, *, site_id: str | None = None) -> tuple[str, ...]:
        """Return the phrase tuple for ``list_id`` with per-site adjustments applied.

        Unknown list ids return an empty tuple so a caller referencing a list
        that does not exist degrades to "matches nothing" rather than raising.
        """
        base = self._global.get(list_id)
        if base is None:
            return ()
        override = self._sites.get(str(site_id)) if site_id is not None else None
        if override is None:
            return base
        if list_id in override.replace:
            return override.replace[list_id]
        result = list(base)
        if list_id in override.remove:
            removals = set(override.remove[list_id])
            result = [phrase for phrase in result if phrase not in removals]
        if list_id in override.extend:
            result.extend(override.extend[list_id])
        return _dedupe(result)

    def matches_any(self, list_id: str, lowered_text: str, *, site_id: str | None = None) -> bool:
        return any(phrase in lowered_text for phrase in self.phrases(list_id, site_id=site_id))

    def matches_all(self, list_id: str, lowered_text: str, *, site_id: str | None = None) -> bool:
        phrases = self.phrases(list_id, site_id=site_id)
        return bool(phrases) and all(phrase in lowered_text for phrase in phrases)


def load_extraction_terms(path: Path | None = None) -> ExtractionTerms:
    """Load and validate the extraction terms file."""
    terms_path = (path or DEFAULT_TERMS_PATH).expanduser()
    try:
        raw_text = terms_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExtractionTermsError(f"Cannot read extraction terms file {terms_path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ExtractionTermsError(f"Invalid YAML in {terms_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionTermsError(f"{terms_path} must contain a top-level mapping")

    raw_global = data.get("global")
    if not isinstance(raw_global, dict):
        raise ExtractionTermsError(f"{terms_path} must define a 'global' mapping")
    global_lists: dict[str, tuple[str, ...]] = {}
    for list_id, value in raw_global.items():
        if not isinstance(list_id, str):
            raise ExtractionTermsError("global list ids must be strings")
        global_lists[list_id] = _coerce_phrase_list(value, f"global.{list_id}")

    site_overrides: dict[str, _SiteOverride] = {}
    raw_sites = data.get("sites") or {}
    if not isinstance(raw_sites, dict):
        raise ExtractionTermsError(f"{terms_path}: 'sites' must be a mapping")
    for site_id, raw_override in raw_sites.items():
        if not isinstance(raw_override, dict):
            raise ExtractionTermsError(f"sites.{site_id} must be a mapping")
        override = _SiteOverride()
        for op, entries in raw_override.items():
            if op not in _SITE_OPS:
                raise ExtractionTermsError(
                    f"sites.{site_id}: unknown operation '{op}' (expected one of {', '.join(_SITE_OPS)})"
                )
            if not isinstance(entries, dict):
                raise ExtractionTermsError(f"sites.{site_id}.{op} must be a mapping of list id to phrases")
            target = getattr(override, op)
            for list_id, value in entries.items():
                if list_id not in global_lists:
                    raise ExtractionTermsError(
                        f"sites.{site_id}.{op} references unknown list '{list_id}'"
                    )
                target[list_id] = _coerce_phrase_list(value, f"sites.{site_id}.{op}.{list_id}")
        site_overrides[str(site_id)] = override

    return ExtractionTerms(global_lists, site_overrides)


_CACHE: dict[str, ExtractionTerms] = {}


def get_extraction_terms(path: Path | None = None) -> ExtractionTerms:
    """Return a cached ExtractionTerms for the given path (default file if None)."""
    terms_path = (path or DEFAULT_TERMS_PATH).expanduser()
    key = str(terms_path)
    cached = _CACHE.get(key)
    if cached is None:
        cached = load_extraction_terms(terms_path)
        _CACHE[key] = cached
    return cached


def reset_cache() -> None:
    """Drop the cached terms — used by tests that load alternate files."""
    _CACHE.clear()
