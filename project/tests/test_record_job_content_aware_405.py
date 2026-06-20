"""Gating tests for prompt 405 — record-job corrections must reach canonical.

Independent verifier coverage for the content-aware record-job identity in
``scripts/backfill-day-records`` and ``scripts/backfill-shift-reports``.

The live bug (2026-06-19): once a date's shift_report / day_record existed in
btq_vault, corrections never reached canonical. The record job_id was date-only
(``record-<type>-<date>``), so a rerun produced an identical job_id; the RMW
idempotency marker was already present (handler skipped) and the enqueue 409'd.
The handler transform overwrites content — it just never re-ran.

The fix makes the JOB identity content-aware:
    job_id = idempotency_key = record-<type>-<date>-<sha256(content)[:12]>
while the vault doc ``_id`` stays date-keyed (one canonical doc per date). So a
changed-content rerun yields a NEW job_id (not a 409) -> handler re-applies ->
canonical updates; identical content -> same job_id -> 409 -> true no-op.

These tests drive the REAL scripts exactly like test_backfill_day_records.py
(loading the extensionless CLI, monkeypatching enqueue/diagnose/config), and
ADDITIONALLY monkeypatch ``get_doc`` so the best-effort current-doc read returns
a known state — letting us assert the honest ``updated`` / ``unchanged`` /
``enqueued`` labelling and exit codes that the contract describes.

Run from repo root:
    project/.venv/bin/python -m pytest project/tests/test_record_job_content_aware_405.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DAY_SCRIPT = REPO_ROOT / "scripts" / "backfill-day-records"
SHIFT_SCRIPT = REPO_ROOT / "scripts" / "backfill-shift-reports"


def _load_module(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def day_mod():
    return _load_module("backfill_day_records_405", DAY_SCRIPT)


@pytest.fixture(scope="module")
def shift_mod():
    return _load_module("backfill_shift_reports_405", SHIFT_SCRIPT)


class _Recorder:
    def __init__(self):
        self.enqueue_calls = []
        self.diagnose_calls = []
        self.get_doc_calls = []
        self.diagnose_result = []
        self.enqueue_result = {"status": 201}
        self.enqueue_raises = None
        # get_doc behaviour: by default raise (mirrors real env where config /
        # CouchDB is unreachable in tests -> best-effort read fails silently).
        self.get_doc_result = None
        self.get_doc_raises = RuntimeError("couch unreachable")

    def diagnose_job(self, job):
        self.diagnose_calls.append(job)
        return list(self.diagnose_result)

    def enqueue(self, job, created_by=None, dry_run=False):
        self.enqueue_calls.append({"job": job, "created_by": created_by, "dry_run": dry_run})
        if self.enqueue_raises is not None:
            raise self.enqueue_raises
        if callable(self.enqueue_result):
            return self.enqueue_result(job)
        return self.enqueue_result

    def get_doc(self, database, doc_id):
        self.get_doc_calls.append((database, doc_id))
        if self.get_doc_raises is not None:
            raise self.get_doc_raises
        if callable(self.get_doc_result):
            return self.get_doc_result(database, doc_id)
        return self.get_doc_result

    def load_reader_config(self):
        return None


def _patch(mod, monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(mod, "enqueue", r.enqueue)
    monkeypatch.setattr(mod, "diagnose_job", r.diagnose_job)
    monkeypatch.setattr(mod, "load_reader_config", r.load_reader_config)
    monkeypatch.setattr(mod, "get_doc", r.get_doc)
    return r


@pytest.fixture
def day_rec(day_mod, monkeypatch):
    return _patch(day_mod, monkeypatch)


@pytest.fixture
def shift_rec(shift_mod, monkeypatch):
    return _patch(shift_mod, monkeypatch)


@pytest.fixture
def journal(tmp_path):
    d = tmp_path / "Journal"
    d.mkdir()
    return d


# -- per-script driving helpers ------------------------------------------------

def _day_write(journal, date_text, body):
    (journal / f"{date_text}.md").write_text(f"## Day Record\n{body}\n", encoding="utf-8")


def _shift_write(journal, date_text, body):
    (journal / f"{date_text}-shift-report.md").write_text(body, encoding="utf-8")


def _run(mod, journal, date_text, *extra):
    argv = ["--journal-dir", str(journal), "--yes", "--start", date_text, "--end", date_text]
    argv.extend(extra)
    return mod.main(argv)


# ==========================================================================
# Criterion 1 — changed content re-materializes (NEW job_id, would not 409)
# ==========================================================================

def test_changed_content_different_job_id_day(day_mod, journal, day_rec):
    _day_write(journal, "2026-06-09", "original")
    _run(day_mod, journal, "2026-06-09")
    first = day_rec.enqueue_calls[-1]["job"]["job_id"]
    day_rec.enqueue_calls.clear()
    _day_write(journal, "2026-06-09", "CORRECTED")
    _run(day_mod, journal, "2026-06-09")
    second = day_rec.enqueue_calls[-1]["job"]["job_id"]
    assert first != second
    assert first.startswith("record-day-record-2026-06-09-")
    assert second.startswith("record-day-record-2026-06-09-")


def test_changed_content_different_job_id_shift(shift_mod, journal, shift_rec):
    _shift_write(journal, "2026-06-09", "original report")
    _run(shift_mod, journal, "2026-06-09")
    first = shift_rec.enqueue_calls[-1]["job"]["job_id"]
    shift_rec.enqueue_calls.clear()
    _shift_write(journal, "2026-06-09", "CORRECTED report")
    _run(shift_mod, journal, "2026-06-09")
    second = shift_rec.enqueue_calls[-1]["job"]["job_id"]
    assert first != second
    assert first.startswith("record-shift-report-2026-06-09-")
    assert second.startswith("record-shift-report-2026-06-09-")


def test_changed_content_labelled_updated_day(day_mod, journal, day_rec, capsys):
    """When the current vault doc IS readable and its content differs, the
    script labels the date `updated` (criterion 1) and exits 0."""
    _day_write(journal, "2026-06-09", "fresh body")
    day_rec.get_doc_raises = None
    day_rec.get_doc_result = {"content": "## Day Record\nstale body", "_id": "day_record_2026_06_09"}
    rc = _run(day_mod, journal, "2026-06-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-09: updated" in out
    assert len(day_rec.enqueue_calls) == 1


# ==========================================================================
# Criterion 2 — identical content is a true no-op (same job_id)
# ==========================================================================

def test_identical_content_same_job_id_day(day_mod, journal, day_rec):
    _day_write(journal, "2026-06-09", "stable body")
    _run(day_mod, journal, "2026-06-09")
    first = day_rec.enqueue_calls[-1]["job"]["job_id"]
    day_rec.enqueue_calls.clear()
    _day_write(journal, "2026-06-09", "stable body")  # byte-identical
    _run(day_mod, journal, "2026-06-09")
    second = day_rec.enqueue_calls[-1]["job"]["job_id"]
    assert first == second


def test_identical_content_unchanged_via_doc_read_day(day_mod, journal, day_rec, capsys):
    """When the current vault doc is readable AND its content matches the
    extracted section, the script short-circuits: labels `unchanged`, does NOT
    enqueue at all, exits 0 (criterion 2)."""
    _day_write(journal, "2026-06-09", "same body")
    section = day_mod.extract_day_record("## Day Record\nsame body\n")
    day_rec.get_doc_raises = None
    day_rec.get_doc_result = {"content": section, "_id": "day_record_2026_06_09"}
    rc = _run(day_mod, journal, "2026-06-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-09: unchanged" in out
    assert day_rec.enqueue_calls == []  # short-circuited, no enqueue


def test_identical_content_409_unchanged_day(day_mod, journal, day_rec, capsys):
    """When get_doc is unreadable (best-effort fails) but the server 409s on the
    duplicate, the script still treats it as `unchanged`, exits 0."""
    _day_write(journal, "2026-06-09", "dup body")
    day_rec.enqueue_result = {"status": 409}
    rc = _run(day_mod, journal, "2026-06-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-09: unchanged" in out


# ==========================================================================
# Criterion 3 — first create (no existing vault doc) -> enqueued
# ==========================================================================

def test_first_create_enqueued_day(day_mod, journal, day_rec, capsys):
    """No existing vault doc (get_doc returns None) -> `enqueued`, exit 0."""
    _day_write(journal, "2026-06-09", "brand new")
    day_rec.get_doc_raises = None
    day_rec.get_doc_result = None  # not found
    rc = _run(day_mod, journal, "2026-06-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-09: enqueued" in out
    assert len(day_rec.enqueue_calls) == 1


def test_first_create_enqueued_shift(shift_mod, journal, shift_rec, capsys):
    _shift_write(journal, "2026-06-09", "brand new report")
    shift_rec.get_doc_raises = None
    shift_rec.get_doc_result = None
    rc = _run(shift_mod, journal, "2026-06-09")
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-09: enqueued" in out


# ==========================================================================
# Criterion 4 — vault doc _id stays date-keyed; content-aware id is JOB only
# ==========================================================================

def test_vault_doc_id_is_date_keyed_day(day_mod):
    assert day_mod.vault_doc_id("2026-06-09") == "day_record_2026_06_09"
    # No content hash leaks into the vault doc id.
    job = day_mod.build_job("2026-06-09", "## Day Record\nbody")
    assert day_mod.content_hash("## Day Record\nbody") in job["job_id"]
    assert day_mod.content_hash("## Day Record\nbody") not in day_mod.vault_doc_id("2026-06-09")


def test_vault_doc_id_is_date_keyed_shift(shift_mod):
    assert shift_mod.vault_doc_id("2026-06-09") == "shift_report_journal_2026_06_09_shift_report"


def test_vault_doc_id_distinct_across_dates(day_mod, shift_mod):
    assert day_mod.vault_doc_id("2026-06-09") != day_mod.vault_doc_id("2026-06-10")
    assert shift_mod.vault_doc_id("2026-06-09") != shift_mod.vault_doc_id("2026-06-10")


# ==========================================================================
# Criterion 5 — robustness: best-effort doc read never fails the run
# ==========================================================================

def test_doc_read_error_does_not_fail_run_day(day_mod, journal, day_rec):
    """get_doc raising (CouchDB unreachable) must NOT fail the run: exit 0, the
    enqueue still happens (falls through to server-side dedup)."""
    _day_write(journal, "2026-06-09", "body")
    day_rec.get_doc_raises = RuntimeError("couch down")
    rc = _run(day_mod, journal, "2026-06-09")
    assert rc == 0
    assert len(day_rec.enqueue_calls) == 1


def test_doc_read_error_does_not_fail_run_shift(shift_mod, journal, shift_rec):
    _shift_write(journal, "2026-06-09", "body")
    shift_rec.get_doc_raises = RuntimeError("couch down")
    rc = _run(shift_mod, journal, "2026-06-09")
    assert rc == 0
    assert len(shift_rec.enqueue_calls) == 1


def test_dry_run_enqueues_nothing_day(day_mod, journal, day_rec):
    _day_write(journal, "2026-06-09", "body")
    rc = _run(day_mod, journal, "2026-06-09", "--dry-run")
    assert rc == 0
    assert day_rec.enqueue_calls == []


def test_dry_run_enqueues_nothing_shift(shift_mod, journal, shift_rec):
    _shift_write(journal, "2026-06-09", "body")
    rc = _run(shift_mod, journal, "2026-06-09", "--dry-run")
    assert rc == 0
    assert shift_rec.enqueue_calls == []


# ==========================================================================
# Sanity — a REAL enqueue error is still a failure (not swallowed by doc-read)
# ==========================================================================

def test_real_enqueue_error_still_fails_day(day_mod, journal, day_rec):
    """The best-effort get_doc swallow must NOT mask a genuine enqueue failure.
    A non-409 BTQClientError from enqueue still counts as failed -> exit 1."""
    _day_write(journal, "2026-06-09", "body")
    day_rec.enqueue_raises = day_mod.BTQClientError("network", "boom")
    rc = _run(day_mod, journal, "2026-06-09")
    assert rc == 1
    assert len(day_rec.enqueue_calls) == 1


def test_real_enqueue_error_still_fails_shift(shift_mod, journal, shift_rec):
    _shift_write(journal, "2026-06-09", "body")
    shift_rec.enqueue_raises = shift_mod.BTQClientError("network", "boom")
    rc = _run(shift_mod, journal, "2026-06-09")
    assert rc == 1
    assert len(shift_rec.enqueue_calls) == 1


# ==========================================================================
# Shift-reports parity — content-aware id, distinct across dates
# ==========================================================================

def test_shift_build_job_content_aware(shift_mod):
    content = "shift report body"
    job = shift_mod.build_job("2026-06-09", content)
    expected = f"record-shift-report-2026-06-09-{shift_mod.content_hash(content)}"
    assert job["job_id"] == expected
    assert job["idempotency_key"] == expected
    assert job["payload"]["content"] == content
    assert job["payload"]["prepared_by"] == shift_mod.DEFAULT_PREPARED_BY


def test_shift_distinct_across_dates_same_content(shift_mod):
    j1 = shift_mod.build_job("2026-06-09", "same")
    j2 = shift_mod.build_job("2026-06-10", "same")
    assert j1["job_id"] != j2["job_id"]
