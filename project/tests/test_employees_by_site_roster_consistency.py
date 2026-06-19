"""INDEPENDENT VERIFIER gating tests (prompt 398) — site roster <-> employee
page consistency.

The site page roster comes from the CouchDB ``employees_by_site`` view ``map``
in ``event_pipeline/couchdb/design_btq_vault.json``. The employee page resolves
a person's sites from ``employee_detail._assigned_site_ids`` (a PRIORITY
FALLBACK: site_ids -> then job+additional_jobs -> then sites). Before prompt 398
the view emitted only from ``doc.site_ids``, so a person assigned via ``job``
(empty site_ids) showed on her own page but was MISSING from the site roster.

These tests pin the view map to ``_assigned_site_ids``:

  * The map string shipped in the JSON is executed AS LITERAL JS via ``node``
    (true fidelity — not a Python re-implementation) against fixture docs, and
    the emitted bare site ids are compared to the real ``_assigned_site_ids``.
  * A structural assertion on the JS source guards the priority-fallback shape
    so a future flat-union regression is caught even if node disappears.

Sandbox identity only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from ops_dashboard.sections.employee_detail import _assigned_site_ids

# --------------------------------------------------------------------------- #
# Locate the shipped design doc and extract the REAL map string.
# --------------------------------------------------------------------------- #
_DESIGN_DOC = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_vault.json"
)


def _load_map_source() -> str:
    doc = json.loads(_DESIGN_DOC.read_text(encoding="utf-8"))
    return doc["views"]["employees_by_site"]["map"]


_MAP_SOURCE = _load_map_source()

_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="node not on PATH")


def _run_map(doc: dict) -> list[list]:
    """Execute the LITERAL shipped JS map against ``doc`` via node.

    Returns the list of emitted keys (each a 3-element [site, status, _id] list).
    Also asserts the emitted VALUE shape is unchanged for every row.
    """
    assert _NODE is not None
    harness = textwrap.dedent(
        """
        const mapFn = (%(map_src)s);
        const doc = %(doc_json)s;
        const rows = [];
        function emit(key, value) { rows.push({key: key, value: value}); }
        mapFn(doc);
        process.stdout.write(JSON.stringify(rows));
        """
    ) % {
        "map_src": _MAP_SOURCE,
        "doc_json": json.dumps(doc),
    }
    out = subprocess.run(
        [_NODE, "-e", harness],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if out.returncode != 0:
        raise AssertionError(f"node map execution failed: {out.stderr}")
    rows = json.loads(out.stdout)
    # Criterion 7: value shape unchanged.
    for row in rows:
        assert set(row["value"].keys()) == {"name", "person_id", "hire_date"}, row
        assert len(row["key"]) == 3, row
    return [row["key"] for row in rows]


def _emitted_bare_sites(doc: dict) -> list:
    """Bare site ids the map emits (the [0] element of each key), unassigned
    rows surface as the literal None/null."""
    return [key[0] for key in _run_map(doc)]


# --------------------------------------------------------------------------- #
# Representative sandbox employee docs (battery reused by the parity test).
# --------------------------------------------------------------------------- #
_DOC_ADRIANA_JOB = {
    "_id": "forsythe_adriana",
    "type": "employee",
    "name": "Adriana Forsythe",
    "person_id": "forsythe_adriana",
    "status": "active",
    "hire_date": "2024-01-02",
    "site_ids": [],
    "job": "789",  # Brookville, assigned via job, empty site_ids
}

_DOC_SITE_IDS_ONLY = {
    "_id": "emp_site_ids",
    "type": "employee",
    "name": "Site Ids Only",
    "person_id": "emp_site_ids",
    "status": "active",
    "site_ids": ["123"],
    "job": "789",  # MUST be ignored (priority fallback)
    "additional_jobs": ["456"],  # MUST be ignored
    "sites": ["999"],  # MUST be ignored
}

_DOC_PREFIX_LOCATION = {
    "_id": "emp_prefix_location",
    "type": "employee",
    "name": "Prefix Location",
    "person_id": "emp_prefix_location",
    "status": "active",
    "site_ids": ["location_789"],
}

_DOC_PREFIX_SITE = {
    "_id": "emp_prefix_site",
    "type": "employee",
    "name": "Prefix Site",
    "person_id": "emp_prefix_site",
    "status": "active",
    "site_ids": ["site_789"],
}

_DOC_SITES_FALLBACK = {
    "_id": "emp_sites_fallback",
    "type": "employee",
    "name": "Sites Fallback",
    "person_id": "emp_sites_fallback",
    "status": "active",
    "site_ids": [],
    "job": "",
    "additional_jobs": [],
    "sites": ["654"],
}

_DOC_UNASSIGNED = {
    "_id": "emp_unassigned",
    "type": "employee",
    "name": "Unassigned",
    "person_id": "emp_unassigned",
    "status": "active",
    "site_ids": [],
}

_DOC_DEDUPE = {
    "_id": "emp_dedupe",
    "type": "employee",
    "name": "Dedupe",
    "person_id": "emp_dedupe",
    "status": "active",
    "site_ids": ["789", "location_789", "site_789", "  789  "],
}

_DOC_JOB_AND_ADDITIONAL = {
    "_id": "emp_job_and_additional",
    "type": "employee",
    "name": "Job And Additional",
    "person_id": "emp_job_and_additional",
    "status": "active",
    "site_ids": [],
    "job": "789",
    "additional_jobs": ["456", "location_321"],
}

_BATTERY = [
    _DOC_ADRIANA_JOB,
    _DOC_SITE_IDS_ONLY,
    _DOC_PREFIX_LOCATION,
    _DOC_PREFIX_SITE,
    _DOC_SITES_FALLBACK,
    _DOC_UNASSIGNED,
    _DOC_DEDUPE,
    _DOC_JOB_AND_ADDITIONAL,
]


# --------------------------------------------------------------------------- #
# Criterion 1: Adriana shape — job resolves to 789 when site_ids empty.
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion1_adriana_emits_under_job_site():
    assert _emitted_bare_sites(_DOC_ADRIANA_JOB) == ["789"]


# --------------------------------------------------------------------------- #
# Criterion 2 (THE GATE): priority fallback — non-empty site_ids wins; job /
# additional_jobs / sites are IGNORED (no over-emit). This is the test the
# mutation check (flat union) must turn RED.
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion2_site_ids_present_ignores_job_additional_sites():
    emitted = _emitted_bare_sites(_DOC_SITE_IDS_ONLY)
    assert emitted == ["123"], emitted
    # Explicit no-over-emit assertions (these are what a flat union would break).
    assert "789" not in emitted  # job
    assert "456" not in emitted  # additional_jobs
    assert "999" not in emitted  # sites


# --------------------------------------------------------------------------- #
# Criterion 3: prefix normalization to the bare /sites/<id> filter id.
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion3_prefix_normalization_location():
    assert _emitted_bare_sites(_DOC_PREFIX_LOCATION) == ["789"]


@requires_node
def test_criterion3_prefix_normalization_site():
    assert _emitted_bare_sites(_DOC_PREFIX_SITE) == ["789"]


# --------------------------------------------------------------------------- #
# Criterion 4: sites-only fallback + unassigned -> [null, ...].
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion4_sites_only_fallback():
    assert _emitted_bare_sites(_DOC_SITES_FALLBACK) == ["654"]


@requires_node
def test_criterion4_unassigned_emits_null_key():
    keys = _run_map(_DOC_UNASSIGNED)
    assert len(keys) == 1
    assert keys[0][0] is None  # null bare-site key
    assert keys[0][2] == "emp_unassigned"  # _id preserved in key


# --------------------------------------------------------------------------- #
# Criterion 5: dedupe — same site reachable several ways emitted once.
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion5_dedupe():
    assert _emitted_bare_sites(_DOC_DEDUPE) == ["789"]


# --------------------------------------------------------------------------- #
# Criterion 6 (PARITY LOCK): for every battery doc, the set of bare site ids the
# MAP emits == _assigned_site_ids(doc) (the real Python function). This is the
# anti-drift gate between the two definitions.
# --------------------------------------------------------------------------- #
@requires_node
@pytest.mark.parametrize("doc", _BATTERY, ids=lambda d: d["_id"])
def test_criterion6_parity_with_assigned_site_ids(doc):
    canonical = _assigned_site_ids(doc)
    emitted = _emitted_bare_sites(doc)
    if canonical:
        # Map emits exactly the canonical bare ids (order preserved, no null row).
        assert emitted == canonical, (emitted, canonical)
    else:
        # Unassigned: canonical empty -> single null-key row.
        assert emitted == [None], emitted


# --------------------------------------------------------------------------- #
# Criterion 7: key/value shape + guards. (_run_map already asserts value shape
# and key arity on every row; here we pin the guards explicitly.)
# --------------------------------------------------------------------------- #
@requires_node
def test_criterion7_non_employee_doc_emits_nothing():
    doc = {"_id": "x", "type": "location", "site_ids": ["789"]}
    assert _run_map(doc) == []


@requires_node
def test_criterion7_deleted_doc_emits_nothing():
    doc = {"_id": "x", "type": "employee", "_deleted": True, "site_ids": ["789"]}
    assert _run_map(doc) == []


@requires_node
def test_criterion7_value_carries_status_in_key():
    keys = _run_map(_DOC_ADRIANA_JOB)
    assert keys == [["789", "active", "forsythe_adriana"]]


# --------------------------------------------------------------------------- #
# Structural guard (node-independent): the shipped JS encodes the PRIORITY
# FALLBACK, not a flat union. If node is unavailable, this still catches a
# flat-union regression; with node it is belt-and-suspenders.
# --------------------------------------------------------------------------- #
def test_map_source_encodes_priority_fallback_structurally():
    src = _MAP_SOURCE
    # site_ids is resolved first, then the fallback is GATED on it being empty.
    assert "doc.site_ids" in src
    assert "if (siteIds.length === 0)" in src
    # The gated branch concats job + additional_jobs ...
    assert "doc.job" in src
    assert "doc.additional_jobs" in src
    # ... and only then (nested) falls back to sites.
    assert "doc.sites" in src
    # Nested sites fallback sits INSIDE the first emptiness gate (two occurrences
    # of the emptiness check: outer site_ids guard + inner job/additional guard).
    assert src.count("if (siteIds.length === 0)") >= 2, src
    # Prefix / quote normalization present.
    assert "location_" in src and "site_" in src
    # Guards intact.
    assert "doc._deleted" in src
    assert "doc.type !== 'employee'" in src


def test_design_doc_still_parses_as_json():
    json.loads(_DESIGN_DOC.read_text(encoding="utf-8"))
