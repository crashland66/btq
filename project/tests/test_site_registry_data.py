"""Gating tests for the site-registry loader resolution order (prompt 301).

These tests pin the contract of ``event_pipeline.site_registry_data``:
the resolution order ``$BTQ_SITE_REGISTRY_PATH`` -> real ``site_registry.json``
-> committed ``site_registry.example.json``, schema validation, ``force_reload``
cache behaviour, and the ``load_sites`` / ``load_vision_contexts`` accessors.

They never depend on the contents of the real (gitignored) registry: the module
constants ``_REAL`` / ``_EXAMPLE`` and the env override are monkeypatched to
``tmp_path`` files written by the test itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import event_pipeline.site_registry_data as srd

_REGISTRY_ENV = "BTQ_SITE_REGISTRY_PATH"


def _write_registry(path: Path, *, marker: str) -> Path:
    """Write a minimal valid registry whose payload carries a unique marker."""
    payload = {
        "sites": [
            {
                "canonical": f"{marker} Site",
                "site_id": marker,
                "note_path": f"Accounts/X/Locations/{marker}/about.md",
                "aliases": [marker],
            }
        ],
        "vision_contexts": {
            marker: {
                "context_id": marker,
                "label": f"{marker} label",
                "environment": "test",
                "summary": "synthetic",
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_loader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset loader cache + clear the env override before each test.

    The repo-root conftest pins ``BTQ_SITE_REGISTRY_PATH`` to the example for the
    whole session; we deliberately clear it here so each test controls resolution
    explicitly, then force a fresh resolution.
    """
    monkeypatch.delenv(_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(srd, "_cache", None, raising=False)


def test_env_override_wins_when_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real = _write_registry(tmp_path / "real.json", marker="REAL")
    example = _write_registry(tmp_path / "example.json", marker="EXAMPLE")
    override = _write_registry(tmp_path / "override.json", marker="OVERRIDE")
    monkeypatch.setattr(srd, "_REAL", real)
    monkeypatch.setattr(srd, "_EXAMPLE", example)
    monkeypatch.setenv(_REGISTRY_ENV, str(override))

    data = srd.load_site_registry(force_reload=True)

    assert data["sites"][0]["site_id"] == "OVERRIDE"


def test_real_preferred_over_example_when_no_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real = _write_registry(tmp_path / "real.json", marker="REAL")
    example = _write_registry(tmp_path / "example.json", marker="EXAMPLE")
    monkeypatch.setattr(srd, "_REAL", real)
    monkeypatch.setattr(srd, "_EXAMPLE", example)

    data = srd.load_site_registry(force_reload=True)

    assert data["sites"][0]["site_id"] == "REAL"


def test_example_used_when_no_override_and_no_real(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # _REAL points at a path that does not exist -> must fall through to example.
    missing_real = tmp_path / "does_not_exist.json"
    example = _write_registry(tmp_path / "example.json", marker="EXAMPLE")
    monkeypatch.setattr(srd, "_REAL", missing_real)
    monkeypatch.setattr(srd, "_EXAMPLE", example)

    data = srd.load_site_registry(force_reload=True)

    assert data["sites"][0]["site_id"] == "EXAMPLE"


def test_schema_validation_raises_on_missing_sites(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"vision_contexts": {}}), encoding="utf-8")
    monkeypatch.setenv(_REGISTRY_ENV, str(bad))

    with pytest.raises(ValueError):
        srd.load_site_registry(force_reload=True)


def test_schema_validation_raises_on_missing_vision_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"sites": []}), encoding="utf-8")
    monkeypatch.setenv(_REGISTRY_ENV, str(bad))

    with pytest.raises(ValueError):
        srd.load_site_registry(force_reload=True)


def test_schema_validation_raises_on_wrong_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    # sites must be a list and vision_contexts a dict; invert both.
    bad.write_text(
        json.dumps({"sites": {}, "vision_contexts": []}), encoding="utf-8"
    )
    monkeypatch.setenv(_REGISTRY_ENV, str(bad))

    with pytest.raises(ValueError):
        srd.load_site_registry(force_reload=True)


def test_force_reload_rereads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_registry(tmp_path / "reg.json", marker="FIRST")
    monkeypatch.setenv(_REGISTRY_ENV, str(path))

    first = srd.load_site_registry(force_reload=True)
    assert first["sites"][0]["site_id"] == "FIRST"

    # Mutate the file on disk; without force_reload the cache should be returned.
    _write_registry(path, marker="SECOND")
    cached = srd.load_site_registry()
    assert cached["sites"][0]["site_id"] == "FIRST"

    # With force_reload the new contents must be picked up.
    reloaded = srd.load_site_registry(force_reload=True)
    assert reloaded["sites"][0]["site_id"] == "SECOND"


def test_load_sites_returns_file_sites(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_registry(tmp_path / "reg.json", marker="SITEMARK")
    monkeypatch.setenv(_REGISTRY_ENV, str(path))
    srd.load_site_registry(force_reload=True)

    sites = srd.load_sites()

    assert isinstance(sites, list)
    assert sites[0]["site_id"] == "SITEMARK"


def test_load_vision_contexts_returns_file_contexts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write_registry(tmp_path / "reg.json", marker="VCMARK")
    monkeypatch.setenv(_REGISTRY_ENV, str(path))
    srd.load_site_registry(force_reload=True)

    contexts = srd.load_vision_contexts()

    assert isinstance(contexts, dict)
    assert "VCMARK" in contexts
    assert contexts["VCMARK"]["label"] == "VCMARK label"
