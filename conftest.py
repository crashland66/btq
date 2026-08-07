"""Repo-root pytest configuration.

Determinism guard for the site registry: real customer site data lives in the
gitignored ``project/event_pipeline/site_registry.json`` and is absent on CI /
fresh clones. The committed synthetic ``site_registry.example.json`` is the
fixture identity set the test suite is written against. Pin the loader to the
example file for the whole session so the suite is deterministic regardless of
whether a real ``site_registry.json`` happens to be present on the dev box.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ``pythonpath = ["project"]`` is applied by pytest from pyproject, but the
# session-scoped autouse fixture below runs during collection setup; make sure
# the project dir is importable for the ``event_pipeline`` import regardless.
_PROJECT_DIR = Path(__file__).resolve().parent / "project"
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

_EXAMPLE_REGISTRY = (
    _PROJECT_DIR / "event_pipeline" / "site_registry.example.json"
).resolve()

# Set the override at conftest IMPORT time (before any test module imports
# ``event_pipeline.sites``, whose module-level ``SITES = load_sites()`` would
# otherwise bind the real registry list if a local ``site_registry.json``
# exists). conftest at the rootdir is imported by pytest very early in startup.
os.environ["BTQ_SITE_REGISTRY_PATH"] = str(_EXAMPLE_REGISTRY)

_EXAMPLE_BRANDS = (
    _PROJECT_DIR / "event_pipeline" / "brand_keywords.example.json"
).resolve()
# Pin synthetic brand keywords too (prompt 305) — real brands live in the
# gitignored brand_keywords.json on prod boxes; tests use the synthetic example
# so results are identical whether or not a real file is present on the dev box.
os.environ["BTQ_BRAND_KEYWORDS_PATH"] = str(_EXAMPLE_BRANDS)


@pytest.fixture(scope="session", autouse=True)
def _pin_synthetic_site_registry() -> None:
    """Force the synthetic example registry + brand keywords for the test session.

    The env vars are already set at import time above; this fixture re-asserts them
    and forces a cache reload as a belt-and-suspenders guard so the loader caches
    reflect the synthetic data even if something reloaded them earlier.
    """
    os.environ["BTQ_SITE_REGISTRY_PATH"] = str(_EXAMPLE_REGISTRY)
    os.environ["BTQ_BRAND_KEYWORDS_PATH"] = str(_EXAMPLE_BRANDS)
    import event_pipeline.site_registry_data as srd

    srd.load_site_registry(force_reload=True)
    srd.load_brand_keywords("supply", force_reload=True)


# --------------------------------------------------------------------------- #
# Prompt 308b: shared CouchDB candidate-review double (INDEPENDENT VERIFIER).
# Re-exported here so the ops-dashboard + swipe test trees can request the
# `couchdb_review` fixture that drives the new CouchDB `_rev` review contract.
# --------------------------------------------------------------------------- #
from test_helpers.couchdb_review_double import couchdb_review  # noqa: E402,F401

# --------------------------------------------------------------------------- #
# Prompt 334b: shared CouchDB job_draft emission double (INDEPENDENT VERIFIER).
# The repointed emission sites now write job_draft docs; this fixture installs a
# faithful in-memory job_draft writer so the retargeted tests drive the new
# (job_draft) contract -- including the exists->skip no-clobber guard.
# --------------------------------------------------------------------------- #
from test_helpers.couchdb_job_draft_double import couchdb_job_drafts  # noqa: E402,F401

# --------------------------------------------------------------------------- #
# Prompt 337a: shared CouchDB job_draft REVIEW-SURFACE double (INDEPENDENT
# VERIFIER). The ops-dashboard review surfaces (swipe/candidates/inbox) now read
# job_draft docs and approve/reject via the 335 write path; this fixture installs
# a faithful in-memory draft read+review transport so the retargeted surface
# tests drive the new contract without a real CouchDB (patches the transport
# bindings the DRAFT path resolves, which the candidate double does not reach).
# --------------------------------------------------------------------------- #
from test_helpers.couchdb_job_draft_review_double import couchdb_job_draft_review  # noqa: E402,F401

# --------------------------------------------------------------------------- #
# Hermetic CouchDB guard: no test may open a real CouchDB (:5984) connection.
#
# CouchDB clients build their target from BTQ_COUCHDB_URL (default
# localhost:5984). On a dev box where that env var points at a reachable
# CouchDB — or where code reads ~/.config/btq/config.json — tests that forget
# the in-memory fixtures (recording_vault_store / couchdb_review) would silently
# READ, and a few (review approve/reject/client-informed) would WRITE,
# PRODUCTION data. An audit found 100+ tests making real :5984 calls that pass
# only because the default target happens to be unreachable on dev boxes.
#
# This raises the same "connection refused" the clients already handle, so
# degrade-when-down tests keep passing while a live CouchDB is unreachable by
# construction. Integration tests that genuinely need one mark
# @pytest.mark.real_couchdb.
# --------------------------------------------------------------------------- #
import urllib.error as _urlerr  # noqa: E402
import urllib.request as _urlreq  # noqa: E402


@pytest.fixture(autouse=True)
def _block_real_couchdb(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("real_couchdb"):
        return
    # Force the hermetic "no CouchDB configured" condition regardless of the dev's
    # shell env or ~/.config/btq/config.json. Lots of code branches on whether
    # BTQ_COUCHDB_URL is set (CouchDB path vs filesystem fallback); unsetting it
    # here makes every test take the same fallback branch as CI. Tests that
    # exercise the configured-CouchDB path set these themselves afterward.
    for _var in ("BTQ_COUCHDB_URL", "BTQ_COUCHDB_USER", "BTQ_COUCHDB_PASSWORD"):
        monkeypatch.delenv(_var, raising=False)
    _real_urlopen = _urlreq.urlopen

    def _guard(url, *args, **kwargs):
        target = url.full_url if hasattr(url, "full_url") else str(url)
        if ":5984" in target:
            raise _urlerr.URLError(
                ConnectionRefusedError(
                    "Real CouchDB blocked in tests (hermetic guard in conftest.py). "
                    "Use the recording_vault_store / couchdb_review fixtures, or mark "
                    "@pytest.mark.real_couchdb for a live integration test."
                )
            )
        return _real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(_urlreq, "urlopen", _guard)


@pytest.fixture()
def enqueue_capture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture jobs authored through the unified CouchDB enqueue boundary.

    Since the file-queue retirement, every authoring path writes btq_queue
    docs via event_pipeline.btq_client.enqueue. Tests asserting "a job was
    staged" use this fixture and inspect the captured jobs instead of
    globbing runtime queue files.
    """
    from event_pipeline import btq_client

    captured: list[dict] = []
    seen_ids: set[str] = set()

    def fake_enqueue(job: dict, created_by: str = "greg", **_kwargs) -> dict:
        job_id = str(job.get("job_id") or "")
        # Mirror CouchDB's 409-on-existing-_id: a repeated deterministic id is
        # an idempotent duplicate, exactly like the real transport.
        if job_id and job_id in seen_ids:
            return {"ok": True, "duplicate": True, "id": job_id, "status": 409}
        if job_id:
            seen_ids.add(job_id)
        captured.append({"job": job, "created_by": created_by})
        return {"ok": True, "id": job_id or "queue-doc"}

    monkeypatch.setattr(btq_client, "enqueue", fake_enqueue)
    return captured
