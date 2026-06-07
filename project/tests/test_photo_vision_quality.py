from __future__ import annotations

import unittest
from pathlib import Path

from field_capture.photo_vision import (
    ADVISORY_WARNING,
    JUDGMENT_LANGUAGE_REMOVED_WARNING,
    QUALITY_FLAGS_ENUM,
    SCREENSHOT_OR_APP_UI_WARNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    VisionDescription,
    derive_quality,
    normalize_vision_description,
)


def _base_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": STATUS_COMPLETED,
        "description": "A tiled restroom with three urinals.",
        "confidence": 0.9,
        "needs_human_review": False,
        "warnings": [ADVISORY_WARNING],
        "quality_flags": [],
    }
    payload.update(overrides)
    return payload


class QualityFlagsEnumTests(unittest.TestCase):
    def test_closed_set_contents(self) -> None:
        expected = {
            "blurry", "motion_blur", "out_of_frame", "partly_obscured",
            "too_dark", "too_bright", "glare", "low_resolution",
            "contains_people", "unanalyzable",
        }
        self.assertEqual(QUALITY_FLAGS_ENUM, expected)


class DeriveQualityModelSourceTests(unittest.TestCase):
    def test_empty_flags_ok_severity(self) -> None:
        result = derive_quality(_base_payload())
        self.assertEqual(result["severity"], "ok")
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["flags"], [])
        self.assertTrue(result["analyzable"])

    def test_model_flags_present_degraded(self) -> None:
        result = derive_quality(_base_payload(quality_flags=["blurry"]))
        self.assertEqual(result["severity"], "degraded")
        self.assertEqual(result["source"], "model")
        self.assertEqual(result["flags"], ["blurry"])

    def test_model_flags_unknown_values_dropped(self) -> None:
        result = derive_quality(_base_payload(quality_flags=["blurry", "nonexistent_flag"]))
        self.assertEqual(result["flags"], ["blurry"])

    def test_unanalyzable_flag_unusable(self) -> None:
        result = derive_quality(_base_payload(quality_flags=["unanalyzable"]))
        self.assertEqual(result["severity"], "unusable")
        self.assertFalse(result["analyzable"])

    def test_contains_people_degraded(self) -> None:
        result = derive_quality(_base_payload(quality_flags=["contains_people"]))
        self.assertEqual(result["severity"], "degraded")
        self.assertIn("contains_people", result["flags"])

    def test_all_flags_preserved(self) -> None:
        all_flags = list(QUALITY_FLAGS_ENUM - {"unanalyzable"})
        result = derive_quality(_base_payload(quality_flags=all_flags))
        self.assertEqual(sorted(result["flags"]), sorted(all_flags))
        self.assertEqual(result["severity"], "degraded")


class DeriveQualityDerivedSourceTests(unittest.TestCase):
    def test_no_quality_flags_key_derives_from_warnings(self) -> None:
        payload = {
            "status": STATUS_COMPLETED,
            "description": "A blurry restroom.",
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [ADVISORY_WARNING, "image is blurry"],
        }
        result = derive_quality(payload)
        self.assertEqual(result["source"], "derived")
        self.assertIn("blurry", result["flags"])
        self.assertEqual(result["severity"], "degraded")

    def test_advisory_warning_not_mapped(self) -> None:
        payload = {
            "status": STATUS_COMPLETED,
            "description": "A restroom.",
            "confidence": 0.9,
            "needs_human_review": False,
            "warnings": [ADVISORY_WARNING],
        }
        result = derive_quality(payload)
        self.assertEqual(result["source"], "derived")
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["severity"], "ok")

    def test_failure_closed_warning_not_mapped(self) -> None:
        payload = {
            "status": STATUS_FAILED,
            "description": "",
            "confidence": None,
            "needs_human_review": True,
            "warnings": [ADVISORY_WARNING, "Vision description failed closed: TimeoutError: timed out"],
        }
        result = derive_quality(payload)
        self.assertEqual(result["severity"], "unusable")
        self.assertNotIn("blurry", result["flags"])
        self.assertNotIn("too_dark", result["flags"])

    def test_dark_warning_maps_to_too_dark(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "image is dark"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("too_dark", result["flags"])

    def test_motion_warning_maps_to_motion_blur(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "motion blur detected"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("motion_blur", result["flags"])

    def test_out_of_frame_warning(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "subject partly out of frame"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("out_of_frame", result["flags"])

    def test_partly_out_warning(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "partly out of shot"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("out_of_frame", result["flags"])

    def test_person_warning_maps_to_contains_people(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "person visible in frame"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("contains_people", result["flags"])

    def test_people_warning_maps_to_contains_people(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "people are visible"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("contains_people", result["flags"])

    def test_judgment_language_removed_not_mapped(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, JUDGMENT_LANGUAGE_REMOVED_WARNING])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertEqual(result["flags"], [])

    def test_glare_warning(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "glare visible on surface"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("glare", result["flags"])

    def test_reflection_warning(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "reflection visible"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("glare", result["flags"])

    def test_overexposed_warning(self) -> None:
        payload = _base_payload(quality_flags=None, warnings=[ADVISORY_WARNING, "image is overexposed"])
        del payload["quality_flags"]
        result = derive_quality(payload)
        self.assertIn("too_bright", result["flags"])


class DeriveQualitySeverityTests(unittest.TestCase):
    def test_failed_status_is_unusable(self) -> None:
        payload = {
            "status": STATUS_FAILED,
            "description": "",
            "confidence": None,
            "needs_human_review": True,
            "warnings": [ADVISORY_WARNING],
            "quality_flags": [],
        }
        result = derive_quality(payload)
        self.assertEqual(result["severity"], "unusable")
        self.assertFalse(result["analyzable"])

    def test_malformed_status_is_unusable(self) -> None:
        payload = {
            "status": "malformed",
            "description": "",
            "confidence": None,
            "needs_human_review": True,
            "warnings": [],
            "quality_flags": [],
        }
        result = derive_quality(payload)
        self.assertEqual(result["severity"], "unusable")

    def test_low_confidence_empty_description_unusable(self) -> None:
        result = derive_quality(_base_payload(confidence=0.2, description=""))
        self.assertEqual(result["severity"], "unusable")

    def test_low_confidence_with_description_degraded(self) -> None:
        result = derive_quality(_base_payload(confidence=0.2, description="A restroom."))
        self.assertEqual(result["severity"], "degraded")

    def test_confidence_below_0_6_degraded(self) -> None:
        result = derive_quality(_base_payload(confidence=0.55))
        self.assertEqual(result["severity"], "degraded")

    def test_confidence_at_0_6_ok(self) -> None:
        result = derive_quality(_base_payload(confidence=0.6))
        self.assertEqual(result["severity"], "ok")

    def test_needs_human_review_degraded(self) -> None:
        result = derive_quality(_base_payload(needs_human_review=True))
        self.assertEqual(result["severity"], "degraded")

    def test_ok_all_clear(self) -> None:
        result = derive_quality(_base_payload(confidence=0.85, needs_human_review=False))
        self.assertEqual(result["severity"], "ok")

    def test_confidence_none_ok_when_no_other_flags(self) -> None:
        result = derive_quality(_base_payload(confidence=None, description="A restroom.", needs_human_review=False))
        self.assertEqual(result["severity"], "ok")


class NormalizeVisionDescriptionQualityFlagsTests(unittest.TestCase):
    def test_quality_flags_parsed_and_filtered(self) -> None:
        payload = {
            "description": "Restroom interior.",
            "area_guess": "restroom",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [],
            "quality_flags": ["blurry", "invalid_flag", "too_dark"],
        }
        result = normalize_vision_description(payload)
        self.assertIn("blurry", result.quality_flags)
        self.assertIn("too_dark", result.quality_flags)
        self.assertNotIn("invalid_flag", result.quality_flags)

    def test_missing_quality_flags_defaults_to_empty(self) -> None:
        payload = {
            "description": "Restroom interior.",
            "area_guess": "restroom",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [],
        }
        result = normalize_vision_description(payload)
        self.assertEqual(result.quality_flags, [])

    def test_non_list_quality_flags_defaults_to_empty(self) -> None:
        payload = {
            "description": "Restroom interior.",
            "area_guess": "restroom",
            "visible_objects": [],
            "possible_conditions": [],
            "possible_issues": [],
            "confidence": 0.8,
            "needs_human_review": False,
            "warnings": [],
            "quality_flags": "blurry",
        }
        result = normalize_vision_description(payload)
        self.assertEqual(result.quality_flags, [])

    def test_screenshot_warning_forces_non_operational_classification(self) -> None:
        payload = {
            "description": "A phone screenshot of the Field Capture app with restroom thumbnails.",
            "area_guess": "restroom",
            "visible_objects": ["phone screenshot", "restroom thumbnail"],
            "possible_conditions": ["app UI visible"],
            "possible_issues": ["something"],
            "confidence": 0.9,
            "needs_human_review": False,
            "warnings": [SCREENSHOT_OR_APP_UI_WARNING],
            "quality_flags": [],
        }
        result = normalize_vision_description(payload)
        self.assertEqual(result.area_guess, "other")
        self.assertIn("unanalyzable", result.quality_flags)
        self.assertTrue(result.needs_human_review)
        self.assertEqual(result.possible_issues, [])
        self.assertIn(SCREENSHOT_OR_APP_UI_WARNING, result.warnings)

    def test_normal_restroom_payload_not_forced_to_other(self) -> None:
        payload = {
            "description": "Restroom interior with a sink and mirror.",
            "area_guess": "restroom",
            "visible_objects": ["sink", "mirror"],
            "possible_conditions": ["paper towel on counter"],
            "possible_issues": ["paper towel on counter"],
            "confidence": 0.85,
            "needs_human_review": False,
            "warnings": [],
            "quality_flags": [],
        }
        result = normalize_vision_description(payload)
        self.assertEqual(result.area_guess, "restroom")
        self.assertNotIn("unanalyzable", result.quality_flags)
        self.assertFalse(result.needs_human_review)
        self.assertEqual(result.possible_issues, ["paper towel on counter"])
        self.assertNotIn(SCREENSHOT_OR_APP_UI_WARNING, result.warnings)


if __name__ == "__main__":
    unittest.main()
