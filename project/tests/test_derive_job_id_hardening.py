"""Independent behavioral gating tests for the derive_job_id hardening (prompt 367).

Authored by a verifier who did NOT write the implementation. The hardening
appends a payload-content hash (`__{h}`, 8 hex chars from sha256 of the
sort_keys JSON dump) to the derived job id, so a same-second batch of jobs with
DIFFERENT payloads can no longer collide on `_id`.

The two surfaces under test MUST stay byte-compatible:
  - project/event_pipeline/btq_client.py   derive_job_id / build_queue_doc
  - scripts/btq-enqueue                     derive_job_id / build_doc

Network is never touched here — these probe the pure id-derivation/doc-build
functions under a FIXED clock.

Run ONLY this file from repo root:
    project/.venv/bin/python -m pytest project/tests/test_derive_job_id_hardening.py -q
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.machinery
import importlib.util
import json
import re
from pathlib import Path

import pytest

from event_pipeline import btq_client as client


REPO_ROOT = Path(__file__).resolve().parents[2]
BTQ_ENQUEUE_PATH = REPO_ROOT / "scripts" / "btq-enqueue"

# id shape: <ts>__<slug>__<8 hex>
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z__[a-z0-9-]+__[0-9a-f]{8}$")


def _load_enqueue_module():
    loader = importlib.machinery.SourceFileLoader("btq_enqueue_script", str(BTQ_ENQUEUE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 6, 13, 12, 30, 45, tzinfo=tz or _dt.timezone.utc)


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch):
    """Freeze the clock in BOTH surfaces to the SAME instant and return enqueue mod."""
    enqueue_mod = _load_enqueue_module()
    monkeypatch.setattr(client.dt, "datetime", _FixedDateTime)
    monkeypatch.setattr(enqueue_mod.dt, "datetime", _FixedDateTime)
    return enqueue_mod


def _shift_report(date: str) -> dict:
    return {
        "job_type": "record_shift_report",
        "payload": {"date": date, "report": "ops note", "actor": "greg"},
    }


# ==========================================================================
# Contract 1: same-second collision-free (THE CORE FIX)
# ==========================================================================

def test_same_second_different_payload_distinct_ids(frozen):
    """Two same-type jobs, DIFFERENT payloads, SAME fixed instant -> different ids.

    Under the OLD code (timestamp + slug only) these produced IDENTICAL ids and
    the second job would 409-collide and be silently dropped.
    """
    a = _shift_report("2026-06-12")
    b = _shift_report("2026-06-13")
    id_a = client.derive_job_id(a)
    id_b = client.derive_job_id(b)
    assert id_a != id_b, "same-second distinct-payload jobs must get distinct ids"
    # Timestamp + slug segments are identical; ONLY the hash differs.
    ts_slug_a = id_a.rsplit("__", 1)[0]
    ts_slug_b = id_b.rsplit("__", 1)[0]
    assert ts_slug_a == ts_slug_b, "precondition: same clock + same slug source"
    assert id_a.rsplit("__", 1)[1] != id_b.rsplit("__", 1)[1], "hash segment must differ"
    # And the same divergence holds through build_queue_doc -> _id / job_id.
    doc_a = client.build_queue_doc(a, "greg")
    doc_b = client.build_queue_doc(b, "greg")
    assert doc_a["_id"] != doc_b["_id"]
    assert doc_a["job_id"] != doc_b["job_id"]


def test_same_second_collision_free_on_btq_enqueue_surface(frozen):
    """The script surface (build_doc) must also be collision-free same-second."""
    enqueue_mod = frozen
    a = _shift_report("2026-06-12")
    b = _shift_report("2026-06-13")
    doc_a = enqueue_mod.build_doc(a, created_by="greg")
    doc_b = enqueue_mod.build_doc(b, created_by="greg")
    assert doc_a["_id"] != doc_b["_id"]


# ==========================================================================
# Contract 2: same-payload stable (true duplicate stays idempotent)
# ==========================================================================

def test_same_payload_same_clock_stable_id(frozen):
    a = _shift_report("2026-06-13")
    b = _shift_report("2026-06-13")  # identical payload, separate dict
    assert client.derive_job_id(a) == client.derive_job_id(b)
    assert client.build_queue_doc(a, "greg")["_id"] == client.build_queue_doc(b, "greg")["_id"]


# ==========================================================================
# Contract 3: byte-parity preserved (incl. the new __{h} segment)
# ==========================================================================

def test_derive_job_id_byte_parity(frozen):
    enqueue_mod = frozen
    for date in ("2026-06-12", "2026-06-13"):
        job = _shift_report(date)
        client_id = client.derive_job_id(job)
        script_id = enqueue_mod.derive_job_id(job)
        assert client_id == script_id, (
            f"derive_job_id diverged: client={client_id} script={script_id}"
        )
        # Both must carry an 8-hex hash and the SAME hash.
        assert ID_RE.match(client_id), client_id
        assert client_id.rsplit("__", 1)[1] == script_id.rsplit("__", 1)[1]


def test_build_doc_byte_parity_includes_hash(frozen):
    enqueue_mod = frozen
    job = _shift_report("2026-06-13")
    doc_client = client.build_queue_doc(dict(job), "greg")
    doc_script = enqueue_mod.build_doc(dict(job), created_by="greg")
    assert doc_client == doc_script
    assert json.dumps(doc_client) == json.dumps(doc_script)
    # The derived _id carries the hash segment.
    assert ID_RE.match(doc_client["_id"]), doc_client["_id"]


# ==========================================================================
# Contract 4: explicit job_id wins (no hash appended)
# ==========================================================================

def test_explicit_job_id_used_verbatim(frozen):
    enqueue_mod = frozen
    job = _shift_report("2026-06-13")
    job["job_id"] = "MY-FIXED-ID"
    doc_client = client.build_queue_doc(dict(job), "greg")
    doc_script = enqueue_mod.build_doc(dict(job), created_by="greg")
    assert doc_client["_id"] == "MY-FIXED-ID"
    assert doc_client["job_id"] == "MY-FIXED-ID"
    assert "__" not in doc_client["_id"], "explicit id must NOT get a hash appended"
    assert doc_client == doc_script


# ==========================================================================
# Contract 5: robustness — non-dict / missing / empty payload hashes {}
# ==========================================================================

def _empty_dict_hash() -> str:
    return hashlib.sha256(json.dumps({}, sort_keys=True, default=str).encode()).hexdigest()[:8]


@pytest.mark.parametrize("payload", [None, {}, [], "string", 42])
def test_robust_payload_does_not_crash_and_hashes_empty(frozen, payload):
    enqueue_mod = frozen
    job = {"job_type": "record_shift_report", "payload": payload}
    cid = client.derive_job_id(job)
    sid = enqueue_mod.derive_job_id(job)
    assert cid == sid
    assert ID_RE.match(cid), cid
    # Non-dict/empty/missing payloads all hash the empty dict {}.
    assert cid.rsplit("__", 1)[1] == _empty_dict_hash()


def test_missing_payload_key_entirely(frozen):
    enqueue_mod = frozen
    job = {"job_type": "record_shift_report"}  # no payload key at all
    cid = client.derive_job_id(job)
    assert cid == enqueue_mod.derive_job_id(job)
    assert cid.rsplit("__", 1)[1] == _empty_dict_hash()
    assert ID_RE.match(cid), cid


# ==========================================================================
# Beyond-spec probes
# ==========================================================================

def test_hash_is_8_lowercase_hex(frozen):
    cid = client.derive_job_id(_shift_report("2026-06-13"))
    h = cid.rsplit("__", 1)[1]
    assert len(h) == 8
    assert re.fullmatch(r"[0-9a-f]{8}", h), h


def test_key_order_does_not_change_hash(frozen):
    """sort_keys=True: insertion order of payload keys must not move the hash."""
    p1 = {"job_type": "record_shift_report", "payload": {"a": 1, "b": 2, "c": 3}}
    p2 = {"job_type": "record_shift_report", "payload": {"c": 3, "b": 2, "a": 1}}
    assert client.derive_job_id(p1) == client.derive_job_id(p2)


def test_non_json_native_payload_still_hashes(frozen):
    """default=str: nested dicts / non-native values must not raise."""
    enqueue_mod = frozen
    job = {
        "job_type": "record_shift_report",
        "payload": {"when": _dt.date(2026, 6, 13), "nested": {"x": [1, 2, {"y": 3}]}},
    }
    cid = client.derive_job_id(job)  # must not raise
    assert cid == enqueue_mod.derive_job_id(job)
    assert ID_RE.match(cid), cid


def test_slug_source_candidate_order_unchanged(frozen):
    """A payload with site_id (and no slug/title/path) must slug on site_id."""
    job = {
        "job_type": "record_shift_report",
        "payload": {"site_id": "Dickinson-Center-789", "actor": "greg"},
    }
    cid = client.derive_job_id(job)
    # middle (slug) segment is derived from site_id
    slug = cid.split("__")[1]
    assert slug == client.slugify("Dickinson-Center-789"), slug
    assert "dickinson" in slug


def test_path_outranks_site_id_in_slug(frozen):
    """Candidate order: path comes before site_id, so path wins when both present."""
    job = {
        "job_type": "append_to_note",
        "payload": {"path": "Journal/x.md", "site_id": "S1", "content": "c", "destination": "journal"},
    }
    slug = client.derive_job_id(job).split("__")[1]
    assert slug == client.slugify("Journal/x.md")


def test_distinct_payloads_distinct_hash_across_surfaces(frozen):
    """Two different payloads must produce two different hashes on BOTH surfaces,
    and the per-payload hashes must agree across surfaces (no surface drift)."""
    enqueue_mod = frozen
    a = _shift_report("2026-06-12")
    b = _shift_report("2026-06-13")
    ha_c = client.derive_job_id(a).rsplit("__", 1)[1]
    hb_c = client.derive_job_id(b).rsplit("__", 1)[1]
    ha_s = enqueue_mod.derive_job_id(a).rsplit("__", 1)[1]
    hb_s = enqueue_mod.derive_job_id(b).rsplit("__", 1)[1]
    assert ha_c != hb_c
    assert ha_c == ha_s and hb_c == hb_s
