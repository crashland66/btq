"""Golden contract for prompt 342: SANDBOX is a FIRST-CLASS static registry site.

Authored by the INDEPENDENT VERIFIER (not the executor). These tests pin the
behaviour 342 introduced so that SANDBOX resolves in EVERY path -- not only via
the canonical CouchDB ``btq_sites`` registry, but also through the static
``event_pipeline.site_registry_data`` loader and the ``event_pipeline.sites``
fallback matcher. Before 342, in the no-CouchDB path (CI / fresh clones / the
hermetic test suite) ``resolve_site_id("SANDBOX")`` returned ``None`` whenever
the active registry file omitted SANDBOX, so a SANDBOX field-capture note could
not build a valid ``log_supply_need`` queue job.

The repo-root ``conftest.py`` makes this hermetic: it unsets ``BTQ_COUCHDB_URL``
(so resolution takes the static-file fallback branch) and pins
``BTQ_SITE_REGISTRY_PATH`` at the committed synthetic ``site_registry.example.json``.
These tests therefore run WITHOUT CouchDB.

Implementation under test (342):
  * ``site_registry_data.BUILTIN_SANDBOX_SITE`` + ``BUILTIN_SANDBOX_VISION_CONTEXT``
    injected by ``load_site_registry()`` via ``_ensure_builtin_sandbox`` when the
    active registry file does not already carry SANDBOX (idempotent).
  * ``site_registry.example.json`` also carries the same SANDBOX site +
    vision_context.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import event_pipeline.site_registry_data as srd
import event_pipeline.sites as sites_module
from field_capture.text_semantics import run_text_semantic_pipeline
from processing_core.capture_semantics import RuleCaptureEngine
from queue_spec import validate_job


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _restore_static_registry() -> None:
    """Re-pin the example registry + rebind the ``sites`` fallback snapshot.

    ``event_pipeline.sites.SITES`` is an import-time snapshot of ``load_sites()``.
    Tests here that repoint ``BTQ_SITE_REGISTRY_PATH`` and ``force_reload`` must
    rebind it so ``resolve_site_id`` sees the current registry; this fixture also
    restores both the loader cache and the snapshot to the pinned example after
    each test so the rest of the session is unaffected.
    """
    srd.load_site_registry(force_reload=True)
    sites_module.SITES = srd.load_sites()
    yield
    srd.load_site_registry(force_reload=True)
    sites_module.SITES = srd.load_sites()


def _write_registry(path: Path, *, site_ids: list[str]) -> Path:
    sites = []
    contexts: dict[str, dict[str, str]] = {}
    for sid in site_ids:
        sites.append(
            {
                "canonical": f"{sid} Site",
                "site_id": sid,
                "note_path": f"Accounts/X/Locations/{sid}/about.md",
                "aliases": [sid, f"{sid} site"],
            }
        )
        contexts[sid] = {
            "context_id": sid,
            "label": f"{sid} label",
            "environment": "test",
            "summary": "synthetic",
        }
    path.write_text(
        json.dumps({"sites": sites, "vision_contexts": contexts}), encoding="utf-8"
    )
    return path


def _reload_with_registry(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("BTQ_SITE_REGISTRY_PATH", str(path))
    data = srd.load_site_registry(force_reload=True)
    sites_module.SITES = srd.load_sites()
    return data


# --------------------------------------------------------------------------- #
# resolve_site_id: SANDBOX + aliases resolve in the static (no-CouchDB) path.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name",
    ["SANDBOX", "sandbox", "Sandbox Site", "the sandbox", "sandbox-site", "sandbox site"],
)
def test_resolve_site_id_sandbox_and_aliases(name: str) -> None:
    assert sites_module.resolve_site_id(name) == "SANDBOX"


def test_resolve_site_canonical_sandbox() -> None:
    canonical, confidence = sites_module.resolve_site("SANDBOX")
    assert canonical == "Sandbox Site"
    assert confidence in {"high", "medium"}


# --------------------------------------------------------------------------- #
# load_sites / load_vision_contexts carry exactly one SANDBOX.
# --------------------------------------------------------------------------- #
def test_load_sites_has_exactly_one_sandbox() -> None:
    sandbox_entries = [s for s in srd.load_sites() if s.get("site_id") == "SANDBOX"]
    assert len(sandbox_entries) == 1, sandbox_entries
    entry = sandbox_entries[0]
    assert entry["canonical"] == "Sandbox Site"
    assert "the sandbox" in entry["aliases"]


def test_load_vision_contexts_has_sandbox() -> None:
    contexts = srd.load_vision_contexts()
    assert "SANDBOX" in contexts
    assert contexts["SANDBOX"]["label"] == "Sandbox Site"


# --------------------------------------------------------------------------- #
# Idempotency.
# --------------------------------------------------------------------------- #
def test_force_reload_twice_no_duplicate_sandbox() -> None:
    srd.load_site_registry(force_reload=True)
    srd.load_site_registry(force_reload=True)
    sandbox_entries = [s for s in srd.load_sites() if s.get("site_id") == "SANDBOX"]
    assert len(sandbox_entries) == 1, sandbox_entries


def test_registry_already_listing_sandbox_not_duplicated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A registry file that ALREADY carries SANDBOX must not get a second entry."""
    reg = _write_registry(tmp_path / "with_sandbox.json", site_ids=["9000", "SANDBOX"])
    data = _reload_with_registry(reg, monkeypatch)
    sandbox_entries = [s for s in data["sites"] if s.get("site_id") == "SANDBOX"]
    assert len(sandbox_entries) == 1, sandbox_entries
    # And the file's own SANDBOX (not the built-in) is preserved -- the built-in
    # is only injected when absent.
    assert sandbox_entries[0]["canonical"] == "SANDBOX Site"


def test_registry_without_sandbox_gets_builtin_injected_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A registry file WITHOUT SANDBOX gets the built-in injected -- exactly one."""
    reg = _write_registry(tmp_path / "no_sandbox.json", site_ids=["9000", "9001"])
    data = _reload_with_registry(reg, monkeypatch)
    sandbox_entries = [s for s in data["sites"] if s.get("site_id") == "SANDBOX"]
    assert len(sandbox_entries) == 1, sandbox_entries
    assert sandbox_entries[0]["canonical"] == "Sandbox Site"  # the built-in
    assert "SANDBOX" in data["vision_contexts"]
    # Reloading again does not pile on a second SANDBOX.
    data2 = srd.load_site_registry(force_reload=True)
    assert len([s for s in data2["sites"] if s.get("site_id") == "SANDBOX"]) == 1
    # SANDBOX resolves even though the file omitted it (the real 342 win).
    assert sites_module.resolve_site_id("SANDBOX") == "SANDBOX"
    assert sites_module.resolve_site_id("the sandbox") == "SANDBOX"


# --------------------------------------------------------------------------- #
# End-to-end: a SANDBOX supply note builds a VALID log_supply_need queue job.
# --------------------------------------------------------------------------- #
def _sandbox_supply_actions(note: str) -> list[dict]:
    artifact = run_text_semantic_pipeline(
        note,
        site_id="SANDBOX",
        upload_id="cap-sb",
        area="Supply Levels",
        engine=RuleCaptureEngine(),
    )
    artifact = json.loads(json.dumps(artifact))  # tuple -> list round-trip
    return [
        action
        for action in artifact["extracted_actions"]
        if action.get("action_key") == "log_supply_need"
    ]


def test_sandbox_supply_note_builds_valid_queue_job() -> None:
    actions = _sandbox_supply_actions("we are low on paper towels and trash liners")
    assert len(actions) == 1, actions
    action = actions[0]
    proposed = action.get("proposed_queue_job")
    assert isinstance(proposed, dict), action
    assert not action.get("proposed_queue_job_error"), action
    # The job validates against the canonical queue spec.
    assert validate_job(proposed) is True
    assert proposed["job_type"] == "log_supply_need"
    payload = proposed["payload"]
    # site_id resolved to SANDBOX -- the thing that was impossible pre-342.
    assert payload["site_id"] == "SANDBOX"
    # Clean item name (the actual items, no vague preamble).
    assert payload["item_name"] == "paper towels, trash liners"
    assert payload["requested_by"].strip()


def test_sandbox_supply_job_invalid_if_site_id_blanked() -> None:
    """Sanity-anchor: the validity hinges on site_id; blanking it must fail spec."""
    actions = _sandbox_supply_actions("we are low on paper towels and trash liners")
    proposed = actions[0]["proposed_queue_job"]
    broken = {"job_type": proposed["job_type"], "payload": dict(proposed["payload"])}
    broken["payload"]["site_id"] = ""
    assert validate_job(broken) is False
