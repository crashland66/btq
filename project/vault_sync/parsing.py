from __future__ import annotations

import datetime as _datetime
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from processing_core.slugs import ascii_lower_dash_slug


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
SITE_ID_RE = re.compile(r"^\d+(?:-\d+)?$")


class VaultParsingError(Exception):
    pass


def read_frontmatter(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logging.warning("could not read vault file %s: %s", path, exc)
        return None
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logging.warning("malformed YAML frontmatter in %s: %s", path, exc)
        return None
    if not isinstance(parsed, dict):
        return None
    return _json_safe(parsed)


def _json_safe(value: Any) -> Any:
    if isinstance(value, _datetime.datetime):
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, _datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def is_site_record(fm: dict[str, Any]) -> bool:
    if fm.get("type") != "location":
        return False
    try:
        site_id_for(fm)
    except VaultParsingError:
        return False
    return True


def is_person_record(fm: dict[str, Any]) -> bool:
    if fm.get("type") != "employee":
        return False
    try:
        person_id_for(fm)
    except VaultParsingError:
        return False
    return True


def site_id_for(fm: dict[str, Any]) -> str:
    value = str(fm.get("job", "")).strip()
    if not value or not SITE_ID_RE.match(value):
        raise VaultParsingError("location record is missing a usable numeric job id")
    return value


def person_id_for(fm: dict[str, Any]) -> str:
    last = slugify(fm.get("last", ""))
    first = slugify(fm.get("first", ""))
    if not first or not last:
        raise VaultParsingError("employee record is missing first or last name")
    return f"{last}-{first}"


def slugify(value: object) -> str:
    return ascii_lower_dash_slug(value)
