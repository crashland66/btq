"""Verifier tests for 418 ops-dashboard helper dedup.

The three deep-analysis helpers (photo_vision_couchdb_config,
DEEP_ANALYSIS_LABELS, deep_analysis_prompt_label) are now defined ONCE in
ops_dashboard.common and imported by both section modules. These tests pin the
behavior of the shared helpers and guard against re-introducing local copies.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from field_capture.deep_analysis import DEEP_ANALYSIS_PRESETS
from ops_dashboard import common
from ops_dashboard.common import (
    DEEP_ANALYSIS_LABELS,
    deep_analysis_prompt_label,
    humanize_key,
    photo_vision_couchdb_config,
)
from ops_dashboard.sections import captures, field_photos

_SECTIONS_DIR = Path(common.__file__).resolve().parent / "sections"
_FIELD_PHOTOS_SRC = _SECTIONS_DIR / "field_photos.py"
_CAPTURES_SRC = _SECTIONS_DIR / "captures.py"


# --- DEEP_ANALYSIS_LABELS ---------------------------------------------------


def test_deep_analysis_labels_matches_presets_with_str_cast():
    expected = {str(p["id"]): str(p["label"]) for p in DEEP_ANALYSIS_PRESETS}
    assert DEEP_ANALYSIS_LABELS == expected
    # str-cast preserved: every key/value is a str
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in DEEP_ANALYSIS_LABELS.items())
    assert DEEP_ANALYSIS_LABELS  # non-empty (7 presets)


# --- deep_analysis_prompt_label: all four branches --------------------------


def test_label_explicit_label_wins_over_everything():
    # explicit label beats a known preset id and "custom"
    assert (
        deep_analysis_prompt_label({"label": "My Label", "prompt_id": "condition_detail"})
        == "My Label"
    )
    assert deep_analysis_prompt_label({"label": "My Label", "prompt_id": "custom"}) == "My Label"


def test_label_custom_prompt_id_returns_custom():
    assert deep_analysis_prompt_label({"prompt_id": "custom"}) == "Custom"
    assert deep_analysis_prompt_label({"prompt_id": "custom", "label": ""}) == "Custom"


def test_label_known_preset_id_returns_preset_label():
    preset = DEEP_ANALYSIS_PRESETS[0]
    assert (
        deep_analysis_prompt_label({"prompt_id": preset["id"]})
        == preset["label"]
    )
    # exercise every preset for completeness
    for p in DEEP_ANALYSIS_PRESETS:
        assert deep_analysis_prompt_label({"prompt_id": p["id"]}) == str(p["label"])


def test_label_unknown_id_falls_back_to_humanize_key():
    assert deep_analysis_prompt_label({"prompt_id": "some_unknown_id"}) == humanize_key(
        "some_unknown_id"
    )
    # sanity: humanize_key actually transforms it (not an identity passthrough)
    assert deep_analysis_prompt_label({"prompt_id": "some_unknown_id"}) != "some_unknown_id"


def test_label_empty_returns_custom():
    assert deep_analysis_prompt_label({}) == "Custom"
    assert deep_analysis_prompt_label({"prompt_id": "", "label": ""}) == "Custom"
    assert deep_analysis_prompt_label({"prompt_id": None, "label": None}) == "Custom"


# --- photo_vision_couchdb_config -------------------------------------------


def test_couchdb_config_none_when_url_unset(monkeypatch):
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    assert photo_vision_couchdb_config() is None


def test_couchdb_config_none_when_url_blank(monkeypatch):
    monkeypatch.setenv("BTQ_COUCHDB_URL", "   ")
    assert photo_vision_couchdb_config() is None


def test_couchdb_config_returns_config_when_url_set(monkeypatch):
    sentinel = object()
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://example.invalid:5984")
    monkeypatch.setattr(common.couchdb_config, "from_env", lambda: sentinel)
    assert photo_vision_couchdb_config() is sentinel


# --- single-definition guard: identity -------------------------------------


def test_sections_import_shared_helpers_by_identity():
    assert field_photos.deep_analysis_prompt_label is common.deep_analysis_prompt_label
    assert captures.deep_analysis_prompt_label is common.deep_analysis_prompt_label
    assert field_photos._photo_vision_couchdb_config is common.photo_vision_couchdb_config
    assert captures._photo_vision_couchdb_config is common.photo_vision_couchdb_config
    assert field_photos.DEEP_ANALYSIS_LABELS is common.DEEP_ANALYSIS_LABELS
    assert captures.DEEP_ANALYSIS_LABELS is common.DEEP_ANALYSIS_LABELS


# --- single-definition guard: AST (no local re-definitions) -----------------

_BANNED_FUNCS = {
    "deep_analysis_prompt_label",
    "_deep_analysis_prompt_label",
    "photo_vision_couchdb_config",
    "_photo_vision_couchdb_config",
}
_BANNED_ASSIGN_NAMES = {"DEEP_ANALYSIS_LABELS", "_DEEP_ANALYSIS_LABELS"}


@pytest.mark.parametrize("src_path", [_FIELD_PHOTOS_SRC, _CAPTURES_SRC])
def test_no_local_definitions_in_section(src_path):
    tree = ast.parse(src_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in _BANNED_FUNCS, (
                f"{src_path.name} still defines function {node.name} locally"
            )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assert target.id not in _BANNED_ASSIGN_NAMES, (
                        f"{src_path.name} still assigns {target.id} locally"
                    )
