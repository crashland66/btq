"""Independent behavioral gating tests for the btq_client queue-authoring module.

Authored by a verifier who did NOT write the implementation. The goal is to
PROVE the contract (especially the SOLE-MUTATOR guarantee) and to break it where
possible. Network is fully stubbed; no real CouchDB is contacted.

Run ONLY this file from repo root:
    project/.venv/bin/python -m pytest project/tests/test_btq_client.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

from event_pipeline import btq_client as client
from event_pipeline import couchdb_config
from event_pipeline.couchdb_queue_reader import QueueReaderConfig
from queue_spec import (
    ALLOWED_JOB_TYPES,
    ALLOWED_TOP_LEVEL_FIELDS,
    JOB_SCHEMAS,
    validate_job,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BTQ_ENQUEUE_PATH = REPO_ROOT / "scripts" / "btq-enqueue"

MUTATING_METHODS = {"PUT", "DELETE"}


# --------------------------------------------------------------------------
# Test config + network stub
# --------------------------------------------------------------------------

def _config(queue_db: str = "btq_queue") -> QueueReaderConfig:
    return QueueReaderConfig(
        couchdb=couchdb_config.CouchDBConfig(
            base_url="http://couchdb.test",
            username="verifier",
            password="secret",
            timeout=10.0,
            heartbeat_ms=10000,
        ),
        queue_database=queue_db,
    )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _Recorder:
    """Captures every urllib request and returns scripted responses.

    `router(method, db, doc_id, path, body)` returns either:
      - a dict  -> 200 OK with that JSON payload
      - an int  -> raise HTTPError with that status code
    Defaults to {} (200 empty) when no router is supplied.
    """

    def __init__(self, router=None):
        self.calls: list[dict] = []
        self._router = router

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        body = None
        if req.data is not None:
            body = json.loads(req.data.decode("utf-8"))
        # url shape: http://couchdb.test/<db>/<doc>?...  or /<db>/_find etc.
        rest = url.split("http://couchdb.test/", 1)[-1]
        path_part = rest.split("?", 1)[0]
        segments = path_part.split("/")
        db = urllib_unquote(segments[0]) if segments else ""
        tail = segments[1] if len(segments) > 1 else ""
        self.calls.append({"method": method, "url": url, "db": db, "tail": tail, "body": body})

        result = {}
        if self._router is not None:
            result = self._router(method=method, db=db, tail=tail, body=body, url=url)
        if isinstance(result, int):
            raise urllib.error.HTTPError(url, result, f"HTTP {result}", {}, io.BytesIO(b"{}"))
        return _FakeResponse(result if result is not None else {})

    def mutating_calls(self) -> list[dict]:
        return [c for c in self.calls if c["method"] in MUTATING_METHODS]


def urllib_unquote(s: str) -> str:
    from urllib.parse import unquote
    return unquote(s)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Patch config loading + urllib. Returns a helper to install a recorder."""
    monkeypatch.setattr(client, "load_reader_config", lambda *a, **k: _config())

    def install(router=None) -> _Recorder:
        rec = _Recorder(router)
        monkeypatch.setattr(client.urllib.request, "urlopen", rec)
        return rec

    return install


# --------------------------------------------------------------------------
# Load btq-enqueue (extensionless) for byte-compat parity checks
# --------------------------------------------------------------------------

def _load_enqueue_module():
    loader = importlib.machinery.SourceFileLoader("btq_enqueue_script", str(BTQ_ENQUEUE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Sample well-formed jobs (accepted by validate_job)
# --------------------------------------------------------------------------

def _valid_append_job() -> dict:
    return {
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-06-13.md",
            "content": "An operational note.",
            "destination": "journal",
        },
    }


def _valid_mark_issue_job() -> dict:
    return {
        "job_type": "mark_issue_resolved",
        "payload": {"issue_id": "ISSUE-1", "actor": "greg"},
    }


# ==========================================================================
# Contract 1: vocabulary helpers
# ==========================================================================

def test_job_types_keys_match_allowed_exactly():
    jt = client.job_types()
    assert set(jt.keys()) == set(ALLOWED_JOB_TYPES)


def test_job_types_values_are_required_fields_per_schema():
    jt = client.job_types()
    for job_type, fields in jt.items():
        assert fields == list(JOB_SCHEMAS[job_type]), job_type


def test_job_schema_returns_required_fields():
    for job_type in ALLOWED_JOB_TYPES:
        assert client.job_schema(job_type) == list(JOB_SCHEMAS[job_type])


def test_job_schema_unknown_raises():
    with pytest.raises(client.BTQClientError):
        client.job_schema("not_a_type")


def test_client_does_not_redeclare_vocabulary():
    # The client must MIRROR queue_spec, never re-declare it. Identity check.
    assert client.ALLOWED_JOB_TYPES is ALLOWED_JOB_TYPES
    assert client.JOB_SCHEMAS is JOB_SCHEMAS
    assert client.ALLOWED_TOP_LEVEL_FIELDS is ALLOWED_TOP_LEVEL_FIELDS


# ==========================================================================
# Contract 2: diagnose_job
# ==========================================================================

def test_diagnose_unknown_job_type_nonempty():
    reasons = client.diagnose_job({"job_type": "frobnicate", "payload": {}})
    assert reasons
    assert any("frobnicate" in r for r in reasons)


def test_diagnose_missing_required_payload_field_nonempty():
    job = {"job_type": "append_to_note", "payload": {"path": "x", "content": "y"}}  # no destination
    reasons = client.diagnose_job(job)
    assert reasons
    assert any("destination" in r for r in reasons)


def test_diagnose_stray_top_level_key_nonempty():
    job = _valid_append_job()
    job["bogus_top"] = "nope"
    reasons = client.diagnose_job(job)
    assert reasons
    assert any("bogus_top" in r for r in reasons)


def test_diagnose_valid_job_returns_empty():
    job = _valid_append_job()
    assert validate_job(job) is True  # sanity: our sample really is valid
    assert client.diagnose_job(job) == []


def test_diagnose_deep_validation_failure_caught_even_when_shape_ok():
    # Shape is fine (all required payload keys present, known type, no stray keys)
    # but a deep rule fails: append_to_note with an illegal destination.
    job = {
        "job_type": "append_to_note",
        "payload": {"path": "p", "content": "c", "destination": "ILLEGAL_DEST"},
    }
    assert validate_job(job) is False
    reasons = client.diagnose_job(job)
    assert reasons, "deep validate_job failure must be reported by diagnose_job"


# ==========================================================================
# Contract 3: build_queue_doc parity with btq-enqueue build_doc
# ==========================================================================

def test_build_queue_doc_field_set_and_values():
    job = _valid_append_job()
    job["job_id"] = "FIXED-ID"
    job["created_at"] = "2026-06-13T00:00:00+00:00"
    doc = client.build_queue_doc(job, "greg")
    assert set(doc.keys()) == {
        "_id", "btq_state", "created_at", "created_by", "job_id", "job_type", "payload",
    }
    assert doc["_id"] == "FIXED-ID"
    assert doc["job_id"] == "FIXED-ID"
    assert doc["btq_state"] == "pending"
    assert doc["created_by"] == "greg"
    assert doc["created_at"] == "2026-06-13T00:00:00+00:00"
    assert doc["job_type"] == "append_to_note"
    assert doc["payload"] == job["payload"]


def test_build_queue_doc_allowed_extras_passed_through():
    job = _valid_append_job()
    job["job_id"] = "X"
    job["metadata"] = {"source": "verifier"}
    job["intent"] = "test-intent"
    job["idempotency_key"] = "abc-123"
    doc = client.build_queue_doc(job, "greg")
    assert doc["metadata"] == {"source": "verifier"}
    assert doc["intent"] == "test-intent"
    assert doc["idempotency_key"] == "abc-123"


def test_build_queue_doc_drops_disallowed_top_level_keys():
    job = _valid_append_job()
    job["job_id"] = "X"
    job["type"] = "should_be_dropped"
    job["whatever"] = 1
    doc = client.build_queue_doc(job, "greg")
    assert "type" not in doc
    assert "whatever" not in doc


def test_build_queue_doc_byte_identical_to_btq_enqueue(monkeypatch: pytest.MonkeyPatch):
    enqueue_mod = _load_enqueue_module()
    # Fix the clock in BOTH modules so derive_job_id + created_at match.
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 13, 12, 30, 45, tzinfo=tz or _dt.timezone.utc)

    monkeypatch.setattr(client.dt, "datetime", _FixedDateTime)
    monkeypatch.setattr(enqueue_mod.dt, "datetime", _FixedDateTime)

    for sample in (_valid_append_job(), _valid_mark_issue_job()):
        # also exercise extras + a disallowed key in one go
        job_client = dict(sample)
        job_client["metadata"] = {"source": "s"}
        job_client["type"] = "DROP_ME"
        job_script = dict(job_client)

        doc_client = client.build_queue_doc(job_client, "greg")
        doc_script = enqueue_mod.build_doc(job_script, created_by="greg")
        assert doc_client == doc_script, (
            f"build_queue_doc diverged from btq-enqueue for {sample['job_type']}:\n"
            f"client={doc_client}\nscript={doc_script}"
        )
        # byte-identical JSON (key order is insertion order in both)
        assert json.dumps(doc_client) == json.dumps(doc_script)


def test_derive_job_id_matches_enqueue(monkeypatch: pytest.MonkeyPatch):
    enqueue_mod = _load_enqueue_module()
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 13, 12, 30, 45, tzinfo=tz or _dt.timezone.utc)

    monkeypatch.setattr(client.dt, "datetime", _FixedDateTime)
    monkeypatch.setattr(enqueue_mod.dt, "datetime", _FixedDateTime)
    job = {"job_type": "append_to_note", "payload": {"path": "Journal/x.md", "content": "c", "destination": "journal"}}
    assert client.derive_job_id(job) == enqueue_mod.derive_job_id(job)


# ==========================================================================
# Contract 4: enqueue gating + dry_run + PUT + 409
# ==========================================================================

def test_enqueue_invalid_raises_before_network(patched):
    rec = patched()  # any call would be recorded
    bad = {"job_type": "append_to_note", "payload": {"path": "p"}}  # missing fields
    with pytest.raises(client.BTQClientError):
        client.enqueue(bad, "greg")
    assert rec.calls == [], "enqueue must NOT touch the network for an invalid job"


def test_enqueue_dry_run_no_io_returns_doc(patched):
    rec = patched()
    job = _valid_append_job()
    doc = client.enqueue(job, "greg", dry_run=True)
    assert rec.calls == [], "dry_run must perform NO I/O"
    assert doc["job_type"] == "append_to_note"
    assert doc["btq_state"] == "pending"
    assert doc["created_by"] == "greg"


def test_enqueue_valid_puts_to_queue_db(patched):
    def router(method, db, tail, body, url):
        return {"ok": True, "id": tail, "rev": "1-abc"}

    rec = patched(router)
    job = _valid_append_job()
    result = client.enqueue(job, "greg")
    puts = [c for c in rec.calls if c["method"] == "PUT"]
    assert len(puts) == 1
    assert puts[0]["db"] == "btq_queue"
    assert result["ok"] is True


def test_enqueue_409_is_idempotent_success(patched):
    def router(method, db, tail, body, url):
        if method == "PUT":
            return 409
        return {}

    rec = patched(router)
    job = _valid_append_job()
    result = client.enqueue(job, "greg")  # must NOT raise
    assert result.get("duplicate") is True
    assert result.get("ok") is True
    assert result.get("status") == 409


def test_enqueue_non_409_http_error_raises(patched):
    def router(method, db, tail, body, url):
        if method == "PUT":
            return 500
        return {}

    patched(router)
    with pytest.raises(client.BTQClientError):
        client.enqueue(_valid_append_job(), "greg")


# ==========================================================================
# Contract 6: read helpers
# ==========================================================================

def test_get_doc_returns_none_on_404(patched):
    def router(method, db, tail, body, url):
        return 404

    patched(router)
    assert client.get_doc("sites", "missing") is None


def test_get_doc_returns_doc(patched):
    def router(method, db, tail, body, url):
        return {"_id": "site-1", "name": "Hillcrest"}

    rec = patched(router)
    doc = client.get_doc("sites", "site-1")
    assert doc == {"_id": "site-1", "name": "Hillcrest"}
    assert rec.calls[0]["method"] == "GET"
    assert rec.calls[0]["db"] == couchdb_config.DEFAULT_SITES_DB


def test_all_docs_omits_design_rows(patched):
    rows = {
        "rows": [
            {"id": "_design/jobs", "doc": {"_id": "_design/jobs"}},
            {"id": "a", "doc": {"_id": "a", "v": 1}},
            {"id": "b", "doc": {"_id": "b", "v": 2}},
        ]
    }

    def router(method, db, tail, body, url):
        return rows

    patched(router)
    docs = client.all_docs("vault")
    ids = [d["_id"] for d in docs]
    assert ids == ["a", "b"]
    assert all(not i.startswith("_design/") for i in ids)


def test_find_posts_selector_and_returns_docs(patched):
    captured = {}

    def router(method, db, tail, body, url):
        captured["method"] = method
        captured["body"] = body
        captured["tail"] = tail
        return {"docs": [{"_id": "p1"}, {"_id": "p2"}]}

    patched(router)
    docs = client.find("vault", {"role": "cleaner"}, fields=["_id"], limit=5)
    assert [d["_id"] for d in docs] == ["p1", "p2"]
    assert captured["method"] == "POST"
    assert captured["tail"] == "_find"
    assert captured["body"]["selector"] == {"role": "cleaner"}
    assert captured["body"]["limit"] == 5
    assert captured["body"]["fields"] == ["_id"]


# ==========================================================================
# Beyond-spec probes
# ==========================================================================

def test_read_helper_accepts_alias_and_literal_db_name(patched):
    seen = {}

    def router(method, db, tail, body, url):
        seen.setdefault("dbs", []).append(db)
        return 404  # get_doc -> None

    patched(router)
    # alias resolves...
    client.get_doc("captures", "x")
    # ...literal passes through unchanged
    client.get_doc("btq_field_captures", "x")
    assert seen["dbs"][0] == couchdb_config.DEFAULT_FIELD_CAPTURES_DB
    assert seen["dbs"][1] == "btq_field_captures"
    assert seen["dbs"][0] == seen["dbs"][1]  # alias == literal target


def test_enqueue_stray_top_level_key_rejected_before_network(patched):
    rec = patched()
    job = _valid_append_job()
    job["evil"] = "extra"
    with pytest.raises(client.BTQClientError):
        client.enqueue(job, "greg")
    assert rec.calls == [], "stray top-level key must be rejected before any network call"


def test_enqueue_force_bypasses_validation_but_only_puts_to_queue(patched):
    # force=True is documented to bypass validation. Probe: it must STILL only
    # ever PUT to the queue db. An invalid job that lacks payload cannot build a
    # doc, so use a job that is shape-buildable but validation-rejected (stray key
    # + missing deep rule). build_queue_doc only requires job_type + payload.
    def router(method, db, tail, body, url):
        return {"ok": True}

    rec = patched(router)
    job = {"job_type": "append_to_note", "payload": {"path": "p", "content": "c", "destination": "journal"}, "evil": "x"}
    assert client.diagnose_job(job), "precondition: job is invalid"
    result = client.enqueue(job, "greg", force=True)
    assert result == {"ok": True}
    muts = rec.mutating_calls()
    assert muts, "force enqueue should still PUT"
    assert all(c["db"] == "btq_queue" for c in muts)
    # force path still drops the disallowed key from the built doc
    put = [c for c in rec.calls if c["method"] == "PUT"][0]
    assert "evil" not in (put["body"] or {})


# ==========================================================================
# Contract 5: SOLE-MUTATOR (critical)
# ==========================================================================

def test_sole_mutator_no_mutation_outside_queue_db(patched):
    """Drive EVERY public path and assert the only PUT/DELETE targets btq_queue.

    POST is allowed as a READ (_find) against canonical DBs; a mutating POST or
    any PUT/DELETE to a non-queue db is a FAILURE.
    """
    def router(method, db, tail, body, url):
        # get_doc on a 404 path returns None; all_docs needs rows; find needs docs
        if tail == "_all_docs":
            return {"rows": [{"id": "a", "doc": {"_id": "a"}}]}
        if tail == "_find":
            return {"docs": [{"_id": "x"}]}
        if method == "PUT":
            return {"ok": True}
        if method == "GET":
            return {"_id": db}
        return {}

    rec = patched(router)

    # Exercise reads against several canonical (non-queue) DBs.
    for db in ("sites", "vault", "captures", "photo_vision", "journal", "voice_memos"):
        client.get_doc(db, "some-id")
        client.all_docs(db)
        client.find(db, {"any": "selector"})

    # Exercise the write path (PUT to queue).
    client.enqueue(_valid_append_job(), "greg")
    # 409 idempotent path too.
    # (separate recorder for clarity)

    mutating = rec.mutating_calls()
    # Every mutating call must target ONLY the queue db.
    offenders = [c for c in mutating if c["db"] != "btq_queue"]
    assert offenders == [], f"MUTATION outside queue db detected: {offenders}"

    # And there must actually have been a PUT to the queue (so the assertion isn't vacuous).
    assert any(c["method"] == "PUT" and c["db"] == "btq_queue" for c in mutating)

    # No POST against a canonical db may carry a mutating intent: confirm every
    # POST we issued was a _find read.
    posts = [c for c in rec.calls if c["method"] == "POST"]
    assert posts, "precondition: find() should have issued POSTs"
    assert all(c["tail"] == "_find" for c in posts), "a POST that is not _find would be a write"


def test_sole_mutator_find_post_against_queue_is_still_read(patched):
    # Even targeting the queue db, find() must remain a READ (POST _find), never
    # a mutation. This guards against a refactor that turns find into a write.
    def router(method, db, tail, body, url):
        return {"docs": []}

    rec = patched(router)
    client.find("queue", {"btq_state": "pending"})
    assert rec.mutating_calls() == []
    assert rec.calls[0]["method"] == "POST"
    assert rec.calls[0]["tail"] == "_find"
