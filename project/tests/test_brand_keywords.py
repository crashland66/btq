"""Gating tests for the prompt-305 brand-keyword loader.

Real supply/equipment brand match-keywords live in a gitignored
``brand_keywords.json`` (shipped to prod boxes out-of-band); a synthetic
``brand_keywords.example.json`` is committed for dev/CI. These tests pin the
resolution order ($BTQ_BRAND_KEYWORDS_PATH -> real -> example), force_reload,
category lookup, and list validation so the PII-scrub swap cannot silently
regress prod recognition.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_pipeline import site_registry_data as srd


def _write_brand_file(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_env_override_wins(tmp_path, monkeypatch):
    override = tmp_path / "override.json"
    _write_brand_file(override, {"supply": ["zeta"], "equipment": ["omega"]})
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(override))
    assert srd.load_brand_keywords("supply", force_reload=True) == ("zeta",)
    assert srd.load_brand_keywords("equipment") == ("omega",)


def test_resolution_prefers_real_over_example(tmp_path, monkeypatch):
    # No env override -> _resolve_brand_path must prefer the real file when present.
    monkeypatch.delenv("BTQ_BRAND_KEYWORDS_PATH", raising=False)
    real = tmp_path / "brand_keywords.json"
    example = tmp_path / "brand_keywords.example.json"
    _write_brand_file(real, {"supply": ["realbrand"], "equipment": []})
    _write_brand_file(example, {"supply": ["examplebrand"], "equipment": []})
    monkeypatch.setattr(srd, "_BRAND_REAL", real)
    monkeypatch.setattr(srd, "_BRAND_EXAMPLE", example)
    assert srd._resolve_brand_path() == real
    assert srd.load_brand_keywords("supply", force_reload=True) == ("realbrand",)


def test_falls_back_to_example_when_real_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("BTQ_BRAND_KEYWORDS_PATH", raising=False)
    real = tmp_path / "brand_keywords.json"  # intentionally not created
    example = tmp_path / "brand_keywords.example.json"
    _write_brand_file(example, {"supply": ["examplebrand"], "equipment": []})
    monkeypatch.setattr(srd, "_BRAND_REAL", real)
    monkeypatch.setattr(srd, "_BRAND_EXAMPLE", example)
    assert srd._resolve_brand_path() == example
    assert srd.load_brand_keywords("supply", force_reload=True) == ("examplebrand",)


def test_force_reload_picks_up_changes(tmp_path, monkeypatch):
    override = tmp_path / "override.json"
    _write_brand_file(override, {"supply": ["first"], "equipment": []})
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(override))
    assert srd.load_brand_keywords("supply", force_reload=True) == ("first",)
    # Without force_reload the cache is returned even after the file changes.
    _write_brand_file(override, {"supply": ["second"], "equipment": []})
    assert srd.load_brand_keywords("supply") == ("first",)
    assert srd.load_brand_keywords("supply", force_reload=True) == ("second",)


def test_unknown_category_returns_empty_tuple(tmp_path, monkeypatch):
    override = tmp_path / "override.json"
    _write_brand_file(override, {"supply": ["x"], "equipment": ["y"]})
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(override))
    assert srd.load_brand_keywords("nope", force_reload=True) == ()


def test_non_dict_payload_raises(tmp_path, monkeypatch):
    override = tmp_path / "override.json"
    override.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(override))
    with pytest.raises(ValueError):
        srd.load_brand_keywords("supply", force_reload=True)


def test_non_list_category_raises(tmp_path, monkeypatch):
    override = tmp_path / "override.json"
    _write_brand_file(override, {"supply": "brightwash", "equipment": []})
    monkeypatch.setenv("BTQ_BRAND_KEYWORDS_PATH", str(override))
    with pytest.raises(ValueError):
        srd.load_brand_keywords("supply", force_reload=True)
