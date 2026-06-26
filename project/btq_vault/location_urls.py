from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


LOCATION_URL_KINDS: frozenset[str] = frozenset(
    {
        "official_location_page",
        "client_homepage",
        "maps",
        "portal",
        "document",
        "other",
    }
)
LOCATION_URL_STATUSES: frozenset[str] = frozenset({"reference", "verified", "stale", "deprecated"})
LOCATION_URL_ACTIONS: frozenset[str] = frozenset({"add", "edit", "remove"})


class LocationUrlError(ValueError):
    """Raised when a location URL entry does not match the canonical shape."""


def is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_location_url_entry(entry: dict[str, Any]) -> dict[str, Any]:
    url = str(entry.get("url") or "").strip()
    kind = str(entry.get("kind") or "").strip()
    if not is_http_url(url):
        raise LocationUrlError("location url entries require an http(s) url")
    if kind not in LOCATION_URL_KINDS:
        raise LocationUrlError("location url entries require an allowed kind")

    status = str(entry.get("status") or "reference").strip() or "reference"
    if status not in LOCATION_URL_STATUSES:
        raise LocationUrlError("location url entries require an allowed status")

    normalized: dict[str, Any] = {
        "url": url,
        "label": str(entry.get("label") or "").strip(),
        "kind": kind,
        "status": status,
        "last_verified_at": _nullable_string(entry.get("last_verified_at")),
        "last_verified_by": _nullable_string(entry.get("last_verified_by")),
        "verification_note": str(entry.get("verification_note") or "").strip(),
    }
    return normalized


def location_urls_from_doc(doc: dict[str, Any] | None, *, include_deprecated: bool = False) -> list[dict[str, Any]]:
    raw_urls = (doc or {}).get("urls")
    if raw_urls is None:
        return []
    if not isinstance(raw_urls, list):
        return []

    urls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_entry in raw_urls:
        if not isinstance(raw_entry, dict):
            continue
        try:
            entry = normalize_location_url_entry(raw_entry)
        except LocationUrlError:
            continue
        if not include_deprecated and entry["status"] == "deprecated":
            continue
        key = entry["url"]
        if key in seen:
            continue
        urls.append(entry)
        seen.add(key)
    return urls


def _nullable_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
