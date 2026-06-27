from __future__ import annotations

from field_capture.photo_vision_categories import (
    CATEGORY_AGREEMENT_MATCH,
    CATEGORY_AGREEMENT_MISMATCH,
    CATEGORY_AGREEMENT_UNVERIFIABLE,
    category_agreement_for,
    vision_category_for_area_guess,
)


def test_vision_area_guess_maps_to_discrete_qc_category_case_insensitively() -> None:
    assert vision_category_for_area_guess("restroom") == "Restrooms"
    assert vision_category_for_area_guess("OFFICE") == "Offices / Classrooms / Exam Rooms"
    assert vision_category_for_area_guess("Break_Room") == "Break Rooms / Kitchens / Cafes"
    assert vision_category_for_area_guess("corridor") == "Hallways"
    assert vision_category_for_area_guess("vestibule") == "Entryways / Lobby / Doorways"
    assert vision_category_for_area_guess("open area") == "Common / Open Areas"


def test_vision_area_guess_exact_discrete_value_maps_to_itself() -> None:
    assert vision_category_for_area_guess("Restrooms") == "Restrooms"
    assert vision_category_for_area_guess("offices / classrooms / exam rooms") == "Offices / Classrooms / Exam Rooms"


def test_vision_area_guess_unmapped_or_ambiguous_returns_none() -> None:
    assert vision_category_for_area_guess("janitor_closet") is None
    assert vision_category_for_area_guess("unknown") is None
    assert vision_category_for_area_guess("loading dock") is None
    assert vision_category_for_area_guess("") is None


def test_vision_category_resolves_against_active_category_set() -> None:
    site_categories = [
        {"label": "Restrooms", "canonical": "Restrooms"},
        {"label": "Open Floor", "canonical": "Common / Open Areas"},
    ]

    assert vision_category_for_area_guess("open floor", site_categories) == "Common / Open Areas"
    assert vision_category_for_area_guess("office", site_categories) is None


def test_category_agreement_distinguishes_match_mismatch_and_unverifiable() -> None:
    assert category_agreement_for("Restrooms", "restrooms") == CATEGORY_AGREEMENT_MATCH
    assert category_agreement_for("Restrooms", "Offices / Classrooms / Exam Rooms") == CATEGORY_AGREEMENT_MISMATCH
    assert category_agreement_for("Restrooms", "qc") == CATEGORY_AGREEMENT_UNVERIFIABLE
    assert category_agreement_for(None, "Restrooms") == CATEGORY_AGREEMENT_UNVERIFIABLE
    assert category_agreement_for("Restrooms", "") == CATEGORY_AGREEMENT_UNVERIFIABLE
