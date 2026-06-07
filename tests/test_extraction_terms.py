from __future__ import annotations

from pathlib import Path

import pytest

from event_pipeline.extraction_terms import (
    DEFAULT_TERMS_PATH,
    ExtractionTermsError,
    load_extraction_terms,
)


def write_terms(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "terms.yaml"
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE = """
version: 1
global:
  staffing_risk:
    - "short staffed"
    - "resigned"
  site_obs_extra: []
sites:
  "7050":
    extend:
      site_obs_extra: ["coal dust", "iron dust"]
    remove:
      staffing_risk: ["resigned"]
  "7020":
    replace:
      staffing_risk: []
"""


def test_default_file_loads_and_validates() -> None:
    terms = load_extraction_terms()
    assert DEFAULT_TERMS_PATH.exists()
    assert "staffing_risk" in terms.list_ids()
    assert terms.phrases("staffing_risk")  # non-empty


def test_global_lookup_returns_tuple(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    result = terms.phrases("staffing_risk")
    assert result == ("short staffed", "resigned")
    assert isinstance(result, tuple)


def test_unknown_list_returns_empty(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    assert terms.phrases("does_not_exist") == ()


def test_site_extend_appends_phrases(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    assert terms.phrases("site_obs_extra") == ()
    assert terms.phrases("site_obs_extra", site_id="7050") == ("coal dust", "iron dust")


def test_site_remove_drops_phrases(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    assert terms.phrases("staffing_risk", site_id="7050") == ("short staffed",)


def test_site_replace_overrides_list(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    # replace: [] disables the list for that site.
    assert terms.phrases("staffing_risk", site_id="7020") == ()


def test_site_without_override_uses_global(tmp_path: Path) -> None:
    terms = load_extraction_terms(write_terms(tmp_path, SAMPLE))
    assert terms.phrases("staffing_risk", site_id="9999") == ("short staffed", "resigned")


def test_matches_any_and_all(tmp_path: Path) -> None:
    body = """
global:
  access_badge_gate_elevator: ["badge", "gate", "elevator"]
  staffing_risk: ["short staffed", "resigned"]
"""
    terms = load_extraction_terms(write_terms(tmp_path, body))
    assert terms.matches_any("staffing_risk", "we are short staffed today")
    assert not terms.matches_any("staffing_risk", "everything is fine")
    assert terms.matches_all("access_badge_gate_elevator", "badge through the gate to the elevator")
    assert not terms.matches_all("access_badge_gate_elevator", "badge and gate only")
    assert not terms.matches_all("missing_list", "anything")


def test_phrases_are_lowercased(tmp_path: Path) -> None:
    body = """
global:
  staffing_risk: ["Short Staffed", "RESIGNED"]
"""
    terms = load_extraction_terms(write_terms(tmp_path, body))
    assert terms.phrases("staffing_risk") == ("short staffed", "resigned")


def test_duplicate_phrases_are_deduped(tmp_path: Path) -> None:
    body = """
global:
  staffing_risk: ["resigned", "resigned", "short staffed"]
"""
    terms = load_extraction_terms(write_terms(tmp_path, body))
    assert terms.phrases("staffing_risk") == ("resigned", "short staffed")


def test_site_override_unknown_list_raises(tmp_path: Path) -> None:
    body = """
global:
  staffing_risk: ["resigned"]
sites:
  "7050":
    extend:
      typo_list: ["x"]
"""
    with pytest.raises(ExtractionTermsError, match="unknown list 'typo_list'"):
        load_extraction_terms(write_terms(tmp_path, body))


def test_unknown_site_operation_raises(tmp_path: Path) -> None:
    body = """
global:
  staffing_risk: ["resigned"]
sites:
  "7050":
    bogus:
      staffing_risk: ["x"]
"""
    with pytest.raises(ExtractionTermsError, match="unknown operation 'bogus'"):
        load_extraction_terms(write_terms(tmp_path, body))


def test_missing_global_raises(tmp_path: Path) -> None:
    with pytest.raises(ExtractionTermsError, match="'global' mapping"):
        load_extraction_terms(write_terms(tmp_path, "version: 1\n"))


def test_non_list_global_value_raises(tmp_path: Path) -> None:
    body = """
global:
  staffing_risk: "not a list"
"""
    with pytest.raises(ExtractionTermsError, match="must be a list"):
        load_extraction_terms(write_terms(tmp_path, body))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ExtractionTermsError, match="Cannot read"):
        load_extraction_terms(tmp_path / "nope.yaml")
