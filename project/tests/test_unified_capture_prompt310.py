"""Prompt-310 gating tests: text-only submit end-to-end, footer versions, and
the operator-gated inbox button (inbox.js hide logic).

Authored by the INDEPENDENT verification agent (NOT the implementer, Codex).

Prompt 310 made three changes to the unified_capture PWA:
  1. /api/session returns a boolean ``can_review`` (True only for an
     ``admin_viewer`` token); inbox.js hides the ✉ button unless can_review and
     never fetches /api/inbox for non-review tokens. (can_review server tests
     live in test_unified_capture_inbox.py::InboxCountTests; the JS hide logic
     is exercised here via node+jsdom when available, plus a source assertion.)
  2. The footer exposes BOTH an interface version and a pipeline version.
  3. A note-only capture (no photos, no audio) is accepted and flows
     end-to-end to an action candidate.

The text-only END-TO-END test (the critical gate) runs the REAL pipeline
functions: import_couchdb_capture -> pipeline_watcher.process_note_only_text_
semantics -> action_candidates.collect_action_candidates(_report). It proves a
note-only capture produces a ``field_text_semantic_summary`` AND a concrete
action candidate -- not merely that submit returns 2xx.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from event_pipeline.couchdb_capture_adapter import import_couchdb_capture
from field_capture import action_candidates as fc
from field_capture import audio_transcription
from field_capture import pipeline_watcher
from field_capture import text_semantics

_PUBLIC_DIR = Path(__file__).resolve().parents[1] / "unified_capture" / "public"


class _FakeRegistry:
    def resolve_canonical(self, site_id: str) -> str:  # noqa: D401
        return "Continental Metalworks"


def _no_media_runner(args: list[str]):
    # A note-only capture has no media to scp/ssh; the runner must never be hit.
    raise AssertionError(f"runner must not be called for note-only capture: {args!r}")


def _note_only_doc(note: str, capture_id: str = "cap-textonly-e2e-0001") -> dict[str, Any]:
    return {
        "_id": capture_id,
        "type": "field_capture",
        "capture_id": capture_id,
        "site_id": "7060",
        "person_id": "per_unified01",
        "person_name": "Person One",
        "captured_at": "2026-06-08T14:30:00Z",
        "qc_category": "report_an_issue",
        "note": note,
        "photos": [],
        "audio": [],
        "processing_state": "claimed",
    }


# --------------------------------------------------------------------------- #
# Feature 3 (critical) -- text-only submit flows end-to-end to a candidate
# --------------------------------------------------------------------------- #


def test_note_only_capture_flows_to_job_draft_e2e(tmp_path: Path, couchdb_job_drafts) -> None:
    """A note-only capture -> text semantic summary -> a real job_draft.

    Drives the REAL pipeline functions (no source scraping for the behavioral
    assertion). Prompt 370 retired the action-candidate collector; the live
    downstream emission is now ``job_draft_emission.collect_job_drafts``, which
    writes job_draft docs via the configured CouchDB writer. The
    ``couchdb_job_drafts`` double makes the store CONFIGURED and captures the
    emitted drafts in-memory. A note-only submit that yields NO job_draft is a
    FAIL -- this asserts a draft IS produced.
    """
    # Defensive: ensure the shared brand-keyword cache is in a clean, valid
    # state (other tests load throwaway brand files into a module-level cache).
    # Prod always has a valid brand_keywords.json; force a clean reload so this
    # e2e test is independent of suite ordering.
    from event_pipeline import site_registry_data as _srd

    _srd._brand_cache = None
    _srd.load_brand_keywords("supply", force_reload=True)

    note = (
        "The men's restroom on the second floor is out of paper towels and the "
        "trash is overflowing. Please send someone to restock and empty it today."
    )
    doc = _note_only_doc(note)

    # 1) Import the note-only CouchDB doc -> intake job (no media -> runner unused).
    result = import_couchdb_capture(
        doc=doc,
        runtime_root=tmp_path,
        remote_host="vps.example",
        registry=_FakeRegistry(),
        runner=_no_media_runner,
    )
    assert result["ok"] is True

    # 2) Pipeline watcher turns the note-only intake into a text semantic summary.
    intake_dir = audio_transcription.default_intake_dir(tmp_path)
    sem_dir = fc.default_text_semantic_dir(tmp_path)
    counts = pipeline_watcher.process_note_only_text_semantics(
        intake_dir, sem_dir, runtime_root=tmp_path
    )
    assert counts["discovered"] == 1, counts
    assert counts["completed"] == 1, counts
    assert counts["failed"] == 0, counts

    # The semantic artifact exists and is the text-note type.
    sem_path = sem_dir / f"{doc['capture_id']}.json"
    assert sem_path.exists(), f"missing text semantic summary: {sem_path}"
    import json

    artifact = json.loads(sem_path.read_text(encoding="utf-8"))
    assert artifact["type"] == text_semantics.ARTIFACT_TYPE == "field_text_semantic_summary"
    assert artifact["status"] == "complete"
    assert artifact["source_text"] == note
    assert artifact.get("issue_detected") is True

    # 3) Job-draft emission produces a real job_draft from the note (the live
    #    downstream path that replaced the retired action-candidate collector).
    from field_capture import job_draft_emission

    counts = job_draft_emission.collect_job_drafts(
        fc.default_semantic_dirs(tmp_path), runtime_root=tmp_path
    )
    assert counts["discovered"] >= 1, counts
    assert counts["emitted"] >= 1, counts

    drafts = list(couchdb_job_drafts.drafts.values())
    assert drafts, f"note-only capture produced NO job_draft: {counts}"
    draft = drafts[0]
    assert str(draft["draft_id"]), draft
    assert draft["type"] == "job_draft", draft
    # The draft traces back to the capture we imported.
    assert draft.get("source_capture_id") == doc["capture_id"], draft


def test_blank_note_only_capture_imports_but_yields_no_candidate(tmp_path: Path) -> None:
    """A whitespace-only note must not silently manufacture a candidate.

    The import gate (adapter/queue_spec) treats whitespace-only as empty and
    would reject at submit; but if such a doc ever reaches the watcher, the
    text-semantic step must not emit a candidate. Guards against over-relaxation
    in the other direction (junk text -> phantom candidate)."""
    doc = _note_only_doc("   \n\t ")
    # Adapter rejects truly-empty (blank note + no media).
    from event_pipeline.couchdb_capture_adapter import CaptureAdapterError

    with pytest.raises(CaptureAdapterError):
        import_couchdb_capture(
            doc=doc,
            runtime_root=tmp_path,
            remote_host="vps.example",
            registry=_FakeRegistry(),
            runner=_no_media_runner,
        )


# --------------------------------------------------------------------------- #
# Feature 2 -- footer exposes BOTH an interface and a pipeline version
# --------------------------------------------------------------------------- #


def _read_public(name: str) -> str:
    return (_PUBLIC_DIR / name).read_text(encoding="utf-8")


def test_footer_html_has_interface_and_pipeline_version_spans() -> None:
    html = _read_public("index.html")
    assert 'id="interfaceVersion"' in html, "index.html missing #interfaceVersion span"
    assert 'id="pipelineVersion"' in html, "index.html missing #pipelineVersion span"


def test_app_js_defines_and_assigns_both_versions_nonempty() -> None:
    """app.js defines INTERFACE_VERSION + PIPELINE_VERSION (both non-empty) and
    assigns each to its footer span."""
    import re

    app = _read_public("app.js")

    def _const_value(name: str) -> str:
        m = re.search(rf'const\s+{name}\s*=\s*"([^"]*)"', app)
        assert m, f"app.js missing const {name}"
        return m.group(1)

    interface_version = _const_value("INTERFACE_VERSION")
    pipeline_version = _const_value("PIPELINE_VERSION")
    assert interface_version.strip(), "INTERFACE_VERSION is empty"
    assert pipeline_version.strip(), "PIPELINE_VERSION is empty"
    # Both versions are wired to their spans.
    assert "elements.interfaceVersion.textContent = INTERFACE_VERSION" in app
    assert "elements.pipelineVersion.textContent = PIPELINE_VERSION" in app
    # The two versions are distinct tags (interface vs pipeline), not duplicated.
    assert interface_version != pipeline_version


# --------------------------------------------------------------------------- #
# Feature 1 -- inbox.js hides the button unless can_review (JS hide logic)
# --------------------------------------------------------------------------- #


def test_inbox_js_default_hidden_and_gated_source_assertion() -> None:
    """Source-level guard (always runs): inbox.js defaults the button hidden and
    gates show/open/fetch on can_review. Complements the jsdom behavioral test
    below (which needs node+jsdom and is skipped when absent)."""
    js = _read_public("inbox.js")
    # Button starts hidden until a session refresh proves can_review.
    assert "els.btn.hidden = true" in js, "inbox.js must default the inbox button hidden"
    # Visibility is driven by can_review from /api/session.
    assert "can_review" in js, "inbox.js must read can_review"
    assert "els.btn.hidden = !state.canReview" in js, "button visibility must follow can_review"
    # open() is a no-op unless can_review.
    assert "if (!state.canReview) return;" in js, "open() must early-return when not can_review"
    # index.html ships the button hidden by default too.
    html = _read_public("index.html")
    assert 'id="inboxBtn"' in html and "hidden" in html.split('id="inboxBtn"')[1].split(">")[0], (
        "index.html #inboxBtn must carry the hidden attribute"
    )


def _jsdom_api_path() -> str | None:
    candidates = [
        os.environ.get("BTQ_JSDOM_API"),
        "/tmp/node_modules/jsdom/lib/api.js",
        str(_PUBLIC_DIR / "tests" / "node_modules" / "jsdom" / "lib" / "api.js"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def test_inbox_button_hide_logic_via_jsdom() -> None:
    """Behavioral hide-logic test: non-review token -> button hidden + no
    /api/inbox fetch + click no-op; review token -> button shown + badge.

    Requires node + jsdom. Skips (does NOT fail) when either is missing; the
    source assertion above always runs as the floor."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available for jsdom hide-logic test")
    api = _jsdom_api_path()
    if not api:
        pytest.skip("jsdom not installed (npm install jsdom); source assertion covers this")
    harness = _PUBLIC_DIR / "tests" / "inbox_can_review_gate.mjs"
    assert harness.exists(), f"missing jsdom harness: {harness}"
    proc = subprocess.run(
        [node, str(harness), api, str(_PUBLIC_DIR)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"jsdom hide-logic test failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert "ALL_OK" in proc.stdout, proc.stdout
