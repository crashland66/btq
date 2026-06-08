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
