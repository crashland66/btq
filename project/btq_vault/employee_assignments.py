from __future__ import annotations

from collections.abc import Mapping


def _clean(value: object) -> str:
    return "" if value is None else str(value).strip()


def _string_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [text for item in value if (text := _clean(item))]
    text = _clean(value)
    return [text] if text else []


def bare_site_id(value: object) -> str:
    """Return the bare canonical id for one stored site reference."""
    text = _clean(value).strip('"')
    for prefix in ("location_", "site_"):
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def _bare_site_ids(value: object) -> list[str]:
    return [bare_site_id(item) for item in _string_values(value)]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def employee_assigned_site_ids(doc: Mapping[str, object]) -> list[str]:
    """Resolve employee assignments using the canonical-first compatibility rule."""
    site_ids = _bare_site_ids(doc.get("site_ids"))
    if not site_ids:
        site_ids = [
            *_bare_site_ids(doc.get("job")),
            *_bare_site_ids(doc.get("additional_jobs")),
        ]
        if not site_ids:
            site_ids = _bare_site_ids(doc.get("sites"))
    return _dedupe(site_ids)
