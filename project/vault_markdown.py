from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from processing_core.slugs import lower_dash_slug
from queue_processor.idempotency import parse_frontmatter_text, serialize_frontmatter


def normalize_lookup(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.strip().lower())).strip()


def slugify_identifier(value: str) -> str:
    return lower_dash_slug(value, fallback="unknown")


def read_markdown_note(path: Path) -> tuple[dict[str, Any], str, bool]:
    frontmatter, body, has_frontmatter = parse_frontmatter_text(path.read_text(encoding="utf-8"))
    return dict(frontmatter), body, has_frontmatter


def read_typed_markdown_note(path: Path, expected_type: str) -> tuple[dict[str, Any] | None, str, dict[str, str] | None]:
    try:
        frontmatter, body, has_frontmatter = read_markdown_note(path)
    except OSError as exc:
        return None, "", {"path": str(path), "reason": f"unreadable: {exc}"}
    if not has_frontmatter:
        return None, "", {"path": str(path), "reason": "missing_frontmatter"}
    if frontmatter_str(frontmatter, "type") != expected_type:
        return None, "", None
    return frontmatter, body, None


def markdown_with_frontmatter(frontmatter: dict[str, Any], body: str) -> str:
    normalized_body = body if body.endswith("\n") else f"{body}\n"
    return f"{serialize_frontmatter(frontmatter)}\n{normalized_body}"


def frontmatter_str(frontmatter: dict[str, Any], key: str) -> str | None:
    value = frontmatter.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def frontmatter_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if not isinstance(value, str) or not value.strip():
        return ()
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return ()
        return tuple(item.strip().strip("'\"") for item in inner.split(",") if item.strip().strip("'\""))
    if "," in stripped:
        return tuple(item.strip() for item in stripped.split(",") if item.strip())
    return (stripped,)


def frontmatter_float(frontmatter: dict[str, Any], key: str) -> float | None:
    value = frontmatter_str(frontmatter, key)
    if value is None or value.lower() == "null":
        return None
    try:
        return float(value)
    except ValueError:
        return None
