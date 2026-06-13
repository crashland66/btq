"""Independent behavioral gating tests for scripts/backfill-day-records.

Authored by a verifier who did NOT write the implementation (prompt 366). The
goal is to PROVE the contract — especially (a) the ``## Day Record`` section
extraction stops at the next ``## `` heading, and (b) the job_id is date-keyed
so a same-second batch cannot collide — and to break it where possible.

All I/O is stubbed: ``enqueue`` / ``diagnose_job`` / ``load_reader_config`` are
monkeypatched on the loaded module so no real CouchDB or network is touched.
Journal files are written to a pytest tmp_path that is passed via --journal-dir.

Run ONLY this file from repo root:
    project/.venv/bin/python -m pytest project/tests/test_backfill_day_records.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "backfill-day-records"


# --------------------------------------------------------------------------
# Load the extensionless CLI exactly like test_btq_client.py loads btq-enqueue.
# --------------------------------------------------------------------------

def _load_module():
    loader = importlib.machinery.SourceFileLoader("backfill_day_records_script", str(SCRIPT_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


@pytest.fixture
def journal(tmp_path):
    d = tmp_path / "Journal"
    d.mkdir()
    return d


class _Recorder:
    """Records enqueue/diagnose calls so tests can assert without network."""

    def __init__(self):
        self.enqueue_calls = []
        self.diagnose_calls = []
        self.reader_config_loaded = 0
        # diagnose returns this (empty list = valid). Override per-test.
        self.diagnose_result = []
        # enqueue returns this. Override per-test (e.g. {"status": 409}).
        self.enqueue_result = {"status": 201}
        # If set, enqueue raises this BTQClientError-like exception.
        self.enqueue_raises = None

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

    def load_reader_config(self):
        self.reader_config_loaded += 1
        return None


@pytest.fixture
def rec(mod, monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(mod, "enqueue", r.enqueue)
    monkeypatch.setattr(mod, "diagnose_job", r.diagnose_job)
    monkeypatch.setattr(mod, "load_reader_config", r.load_reader_config)
    return r


def _write(journal, date_text, content):
    (journal / f"{date_text}.md").write_text(content, encoding="utf-8")


def _run(mod, journal, *extra, yes=True):
    argv = ["--journal-dir", str(journal)]
    if yes:
        argv.append("--yes")
    argv.extend(extra)
    return mod.main(argv)


# ==========================================================================
# Contract 1 — Section extraction boundaries
# ==========================================================================

def test_extract_stops_before_next_heading(mod):
    content = (
        "---\ntitle: 2026-06-01\n---\n\n"
        "## Day Record — 2026-06-01\n"
        "did the thing\nmore body\n\n"
        "## Something Else\n"
        "other content that must NOT bleed in\n"
    )
    section = mod.extract_day_record(content)
    assert section is not None
    assert section.startswith("## Day Record")
    assert "did the thing" in section
    assert "more body" in section
    # The boundary is the crux: nothing from the next section may appear.
    assert "Something Else" not in section
    assert "must NOT bleed in" not in section


def test_extract_runs_to_eof(mod):
    content = (
        "## Day Record — 2026-06-02\n"
        "line one\nline two\nlast line\n"
    )
    section = mod.extract_day_record(content)
    assert section is not None
    assert "line one" in section
    assert "last line" in section


def test_extract_no_section_returns_none(mod):
    content = "## Some Other Heading\nbody\n## And Another\nmore\n"
    assert mod.extract_day_record(content) is None


# ==========================================================================
# Contract 2 — Job shape (date-keyed job_id is critical)
# ==========================================================================

def test_build_job_shape(mod):
    job = mod.build_job("2026-06-03", "## Day Record\nbody")
    assert job["job_id"] == "record-day-record-2026-06-03"
    assert job["job_type"] == "record_day_record"
    assert job["payload"]["date"] == "2026-06-03"
    assert job["payload"]["content"] == "## Day Record\nbody"
    assert job["idempotency_key"] == "day-record-2026-06-03"


def test_job_id_is_date_keyed_distinct_across_dates(mod):
    j1 = mod.build_job("2026-06-03", "x")
    j2 = mod.build_job("2026-06-04", "y")
    # Two dates in a same-second batch must NOT share a job_id.
    assert j1["job_id"] != j2["job_id"]
    assert j1["job_id"] == "record-day-record-2026-06-03"
    assert j2["job_id"] == "record-day-record-2026-06-04"


def test_job_validates_against_real_queue_spec(mod):
    """Build a job from a real extracted section and run it through the REAL
    queue_spec validator (not the stub) to prove the shape is genuinely valid
    for record_day_record. queue_spec exposes validate_job; the script's own
    diagnose_job is re-exported from event_pipeline.btq_client."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "project"))
    import queue_spec  # type: ignore
    from event_pipeline import btq_client as real_client  # type: ignore

    content = "## Day Record — 2026-06-05\nthe day went fine\n"
    section = mod.extract_day_record(content)
    job = mod.build_job("2026-06-05", section)
    assert queue_spec.validate_job(job) is True
    # The REAL diagnose_job (not the stub) must report zero problems.
    assert real_client.diagnose_job(job) == []


def test_enqueued_content_equals_extracted_section(mod, journal, rec):
    content = (
        "## Day Record — 2026-06-06\n"
        "body of the record\n"
        "## Trailing\nignored\n"
    )
    _write(journal, "2026-06-06", content)
    rc = _run(mod, journal, "--start", "2026-06-06", "--end", "2026-06-06")
    assert rc == 0
    assert len(rec.enqueue_calls) == 1
    enq = rec.enqueue_calls[0]["job"]
    expected = mod.extract_day_record(content)
    assert enq["payload"]["content"] == expected
    assert enq["job_id"] == "record-day-record-2026-06-06"
    assert "Trailing" not in enq["payload"]["content"]


# ==========================================================================
# Contract 3 — Skips (missing file / missing section), never empty enqueue
# ==========================================================================

def test_missing_file_skipped_not_failure(mod, journal, rec):
    rc = _run(mod, journal, "--start", "2026-06-07", "--end", "2026-06-07")
    assert rc == 0  # not a failure
    assert rec.enqueue_calls == []  # nothing enqueued


def test_file_without_section_skipped(mod, journal, rec):
    _write(journal, "2026-06-08", "## Notes\njust notes\n## Other\nx\n")
    rc = _run(mod, journal, "--start", "2026-06-08", "--end", "2026-06-08")
    assert rc == 0
    assert rec.enqueue_calls == []


def test_mixed_range_enqueues_only_present_sections(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nday nine\n")
    # 2026-06-10 missing entirely
    _write(journal, "2026-06-11", "## Notes\nno day record here\n")
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-11")
    assert rc == 0
    assert len(rec.enqueue_calls) == 1
    assert rec.enqueue_calls[0]["job"]["payload"]["date"] == "2026-06-09"


# ==========================================================================
# Contract 4 — Dry-run enqueues nothing
# ==========================================================================

def test_dry_run_never_enqueues(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nday nine body\n")
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09", "--dry-run")
    assert rc == 0
    assert rec.enqueue_calls == []  # stub never called
    # diagnose IS run during collection even in dry-run (validation is fine).


# ==========================================================================
# Contract 5 — Idempotency: 409 is success, re-run same job_id
# ==========================================================================

def test_409_treated_as_success(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    rec.enqueue_result = {"status": 409}
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    assert rc == 0  # 409 is NOT a failure
    assert len(rec.enqueue_calls) == 1


def test_duplicate_flag_treated_as_success(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    rec.enqueue_result = {"duplicate": True}
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    assert rc == 0
    assert len(rec.enqueue_calls) == 1


def test_rerun_yields_same_date_keyed_job_id(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    first_id = rec.enqueue_calls[0]["job"]["job_id"]
    rec.enqueue_calls.clear()
    _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    second_id = rec.enqueue_calls[0]["job"]["job_id"]
    assert first_id == second_id == "record-day-record-2026-06-09"


# ==========================================================================
# Beyond-spec probes
# ==========================================================================

def test_section_immediately_followed_by_heading_no_blank_line(mod):
    content = (
        "## Day Record — 2026-06-01\n"
        "body line\n"
        "## Next\n"  # immediately, no blank line between
        "leak\n"
    )
    section = mod.extract_day_record(content)
    assert "body line" in section
    assert "Next" not in section
    assert "leak" not in section


def test_frontmatter_before_section(mod):
    content = (
        "---\ntags: [journal]\ndate: 2026-06-01\n---\n"
        "intro prose\n\n"
        "## Day Record — 2026-06-01\n"
        "real body\n"
    )
    section = mod.extract_day_record(content)
    assert section.startswith("## Day Record")
    assert "real body" in section
    assert "intro prose" not in section  # frontmatter/prose excluded
    assert "tags:" not in section


def test_empty_section_heading_only_is_skipped(mod, journal, rec):
    """A '## Day Record' heading with NO body is skipped (not enqueued).

    A heading-only Day Record has no real content to capture; extract_day_record
    returns None so the date is skipped, honoring the "don't enqueue an empty
    day-record section" contract."""
    # Heading then immediately another heading -> body is empty -> None.
    assert mod.extract_day_record("## Day Record — 2026-06-01\n## Next\nx\n") is None

    _write(journal, "2026-06-09", "## Day Record\n## Next\nother\n")
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    assert rc == 0
    assert len(rec.enqueue_calls) == 0  # heading-only -> skipped, no enqueue


def test_empty_section_heading_only_to_eof_is_skipped(mod):
    # Heading is the only line, runs to EOF with no body -> empty section -> None.
    assert mod.extract_day_record("## Day Record\n") is None


def test_crlf_line_endings(mod):
    content = (
        "## Day Record — 2026-06-01\r\n"
        "crlf body\r\n"
        "## Next\r\n"
        "crlf leak\r\n"
    )
    section = mod.extract_day_record(content)
    assert section is not None
    assert "crlf body" in section
    assert "crlf leak" not in section
    assert "Next" not in section


def test_crlf_empty_section_is_skipped(mod):
    # CRLF heading-only-to-next-heading: body empty -> skipped (None), mirroring LF.
    assert mod.extract_day_record("## Day Record — x\r\n## Next\r\nbody\r\n") is None


def test_diagnose_failure_raises_and_aborts(mod, journal, rec):
    """If diagnose_job reports problems, collection raises -> main returns 2 and
    NOTHING is enqueued (validation gate is upstream of enqueue)."""
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    rec.diagnose_result = ["payload.date: bad"]
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    assert rc == 2
    assert rec.enqueue_calls == []


def test_created_by_passed_through(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09",
         "--created-by", "verifier")
    assert rec.enqueue_calls[0]["created_by"] == "verifier"
    assert rec.enqueue_calls[0]["dry_run"] is False


def test_enqueue_error_counts_as_failure_returns_1(mod, journal, rec):
    _write(journal, "2026-06-09", "## Day Record\nbody\n")
    rec.enqueue_raises = mod.BTQClientError("network", "boom")
    rc = _run(mod, journal, "--start", "2026-06-09", "--end", "2026-06-09")
    assert rc == 1  # failed > 0
    assert len(rec.enqueue_calls) == 1
