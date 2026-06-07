from __future__ import annotations

from pathlib import Path
from typing import Any

from vault_markdown import frontmatter_str, read_markdown_note


def discover_site_visits(
    vault_root: Path,
    *,
    site_id: str | None = None,
    limit: int = 10,
) -> dict[str, object]:
    """Read visit records from Visits/<date>.md vault files.

    Returns {"visits": list[dict], "warnings": list[dict]}.
    Results are sorted newest-first, limited to `limit` entries.
    """
    visits: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    root = vault_root.expanduser().resolve(strict=False)
    accounts_root = root / "Accounts"
    if not accounts_root.exists():
        return {"visits": [], "warnings": []}

    for path in sorted(accounts_root.glob("*/Locations/*/Visits/*.md")):
        try:
            fm, _body, _has_fm = read_markdown_note(path)
        except Exception as exc:  # noqa: BLE001
            warnings.append({"path": str(path), "reason": str(exc)})
            continue
        if clean_string(fm.get("type")) != "visit":
            continue
        record_site_id = clean_string(fm.get("site"))
        if site_id is not None and record_site_id != str(site_id):
            continue
        visits.append(
            {
                "site_id": record_site_id,
                "date": clean_string(fm.get("date")),
                "timestamp": clean_string(fm.get("timestamp")),
                "confidence": clean_string(fm.get("confidence")),
                "source": clean_string(fm.get("source")),
                "evidence": clean_string(fm.get("evidence")),
                "visit_key": clean_string(fm.get("visit_key")),
                "visited_by": clean_string(fm.get("visited_by")),
                "path": str(path),
            }
        )

    person_first_names = load_person_first_names(root)
    for visit in visits:
        person_id = visit.get("visited_by") or ""
        visit["visited_by_first_name"] = person_first_names.get(person_id, "")

    visits.sort(key=lambda v: v["date"], reverse=True)
    return {"visits": visits[:limit], "warnings": warnings}


def clean_string(value: Any) -> str:
    text = frontmatter_str({"value": value}, "value") or str(value or "").strip()
    if text in {"", '""', "''", "null", "None"}:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def load_person_first_names(vault_root: Path) -> dict[str, str]:
    """Walk vault_root/People/*.md and return {person_id: first_name}."""
    from field_capture.identity import first_name_from_canonical

    result: dict[str, str] = {}
    people_dir = vault_root.expanduser().resolve(strict=False) / "People"
    if not people_dir.exists():
        return result
    for path in sorted(people_dir.glob("*.md")):
        try:
            fm, _body, _has_fm = read_markdown_note(path)
        except Exception:  # noqa: BLE001
            continue
        person_id = clean_string(fm.get("person_id"))
        name = clean_string(fm.get("name"))
        if not person_id or not name:
            continue
        first = first_name_from_canonical(name)
        if first:
            result[person_id] = first
    return result
