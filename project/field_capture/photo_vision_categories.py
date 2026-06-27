from __future__ import annotations

import re
from collections.abc import Sequence

from field_capture.display_categories import resolve_display_categories

CATEGORY_AGREEMENT_MATCH = "match"
CATEGORY_AGREEMENT_MISMATCH = "mismatch"
CATEGORY_AGREEMENT_UNVERIFIABLE = "unverifiable"
CATEGORY_AGREEMENT_VALUES = frozenset({
    CATEGORY_AGREEMENT_MATCH,
    CATEGORY_AGREEMENT_MISMATCH,
    CATEGORY_AGREEMENT_UNVERIFIABLE,
})

VISION_CATEGORY_LEXICON: dict[str, tuple[str, ...]] = {
    "Restrooms": ("restroom", "bathroom", "toilet", "washroom", "lavatory", "urinal"),
    "Offices / Classrooms / Exam Rooms": ("office", "classroom", "exam room", "exam_room"),
    "Break Rooms / Kitchens / Cafes": (
        "kitchen",
        "break room",
        "breakroom",
        "cafe",
        "cafeteria",
        "dining",
    ),
    "Hallways": ("hallway", "hall", "corridor"),
    "Entryways / Lobby / Doorways": (
        "entryway",
        "entry",
        "entrance",
        "lobby",
        "doorway",
        "vestibule",
        "foyer",
    ),
    "Common / Open Areas": ("common area", "common", "open area", "open_area", "open space"),
}

AMBIGUOUS_AREA_GUESSES = frozenset({
    "closet",
    "janitor",
    "janitor closet",
    "janitor_closet",
    "storage",
    "storage room",
    "storage_room",
    "supply closet",
    "supply_closet",
    "utility",
    "utility room",
    "utility_room",
    "unknown",
    "other",
})
GENERIC_QC_CATEGORY = "qc"


def default_qc_categories() -> list[dict[str, str]]:
    return resolve_display_categories(None, None)


def _category_canonical_map(categories: Sequence[dict[str, str]] | None = None) -> dict[str, str]:
    active = list(categories) if categories is not None else default_qc_categories()
    values: dict[str, str] = {}
    for entry in active:
        canonical = str(entry.get("canonical") or "").strip()
        label = str(entry.get("label") or "").strip()
        if canonical:
            values[canonical.casefold()] = canonical
        if label and canonical:
            values[label.casefold()] = canonical
    return values


def _normalize_area_guess(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[_-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.casefold()


def vision_category_for_area_guess(
    area_guess: object,
    categories: Sequence[dict[str, str]] | None = None,
) -> str | None:
    """Map vision's free-form area guess into the active QC category set."""
    category_map = _category_canonical_map(categories)
    raw = str(area_guess or "").strip()
    if not raw:
        return None

    exact = category_map.get(raw.casefold())
    if exact:
        return exact

    normalized = _normalize_area_guess(raw)
    if not normalized or normalized in AMBIGUOUS_AREA_GUESSES:
        return None

    for category, terms in VISION_CATEGORY_LEXICON.items():
        resolved_category = category_map.get(category.casefold())
        if not resolved_category:
            continue
        for term in terms:
            normalized_term = _normalize_area_guess(term)
            if normalized == normalized_term or re.search(rf"\b{re.escape(normalized_term)}\b", normalized):
                return resolved_category
    return None


def category_agreement_for(
    vision_category: object,
    qc_category: object,
) -> str:
    vision = str(vision_category or "").strip()
    qc = str(qc_category or "").strip()
    if not vision or not qc or qc.casefold() == GENERIC_QC_CATEGORY:
        return CATEGORY_AGREEMENT_UNVERIFIABLE
    if vision.casefold() == qc.casefold():
        return CATEGORY_AGREEMENT_MATCH
    return CATEGORY_AGREEMENT_MISMATCH


def derive_vision_category_fields(
    area_guess: object,
    qc_category: object,
    categories: Sequence[dict[str, str]] | None = None,
) -> dict[str, object]:
    vision_category = vision_category_for_area_guess(area_guess, categories)
    return {
        "vision_category": vision_category,
        "category_agreement": category_agreement_for(vision_category, qc_category),
    }
