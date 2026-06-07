from __future__ import annotations

import re
import unicodedata


def lower_dash_slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def ascii_lower_dash_slug(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return lower_dash_slug(ascii_text, fallback="")


def lower_underscore_slug(value: str, *, fallback: str) -> str:
    return lower_dash_slug(value, fallback=fallback).replace("-", "_")


def css_status_slug(value: object) -> str:
    """Preserve legacy status CSS classes: lowercase, keep -/_, remove other chars."""
    return re.sub(r"[^a-z0-9_-]", "", str(value or "").strip().lower())
