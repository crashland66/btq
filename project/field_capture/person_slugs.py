from __future__ import annotations

from pathlib import Path
from typing import Any

from processing_core.slugs import lower_dash_slug


def person_slug(value: str) -> str:
    return lower_dash_slug(value, fallback="unknown")


def last_first_person_slug(*, first: str, last: str) -> str:
    return person_slug(f"{last}-{first}")


def _add_name_order_slugs(candidates: set[str], *, first: str, last: str) -> None:
    if first and last:
        candidates.add(person_slug(f"{last}-{first}"))
        candidates.add(person_slug(f"{first}-{last}"))


def employee_slug_candidates(
    *,
    first: Any = "",
    last: Any = "",
    filename_stem: str = "",
    vault_path: str = "",
) -> set[str]:
    candidates: set[str] = set()
    first_text = str(first or "").strip()
    last_text = str(last or "").strip()
    _add_name_order_slugs(candidates, first=first_text, last=last_text)

    stem = filename_stem.strip() or Path(vault_path).stem.strip()
    if "," in stem:
        filename_last, filename_first = stem.split(",", 1)
        filename_last = filename_last.strip()
        filename_first = filename_first.strip()
        if filename_first and filename_last:
            _add_name_order_slugs(candidates, first=filename_first, last=filename_last)
            first_word = filename_first.split()[0]
            if first_word:
                candidates.add(person_slug(f"{filename_last}-{first_word}"))
                candidates.add(person_slug(f"{first_word}-{filename_last}"))
    if stem:
        candidates.add(person_slug(stem))
    return candidates
