"""Gating tests for prompt 370: the dead LEGACY action-candidate review path is
retired, while the live ``job_draft`` review path stays intact.

Authored by the INDEPENDENT VERIFIER for 370. The PLANNER verified the
PRODUCTION change (live ``job_draft`` path imports cleanly; scope is exactly the
intended 4 modified + 3 deleted files). These gates lock the retire in place:

  * ``field_capture`` no longer exposes ``action_candidate_staging_watcher``.
  * ``field_capture.action_candidates`` no longer exposes the retired collector
    (``collect_action_candidates``) -- the shared helpers the live path uses are
    KEPT (asserted positively so the gate isn't vacuously over-broad).
  * ``btq_cli/field.py`` no longer registers the 3 retired CLI commands; a
    known-good command still parses.
  * ``post_routes`` no longer dispatches ``/field-capture/review/{fix,resubmit}``
    (a POST to either is NOT routed).
  * The LIVE ``job_draft`` review path still works end-to-end in-process: the
    ``/candidates`` (+ ``/swipe``) surface renders a seeded draft and the shared
    approve handler flips it to ``approved`` via the 335 write path.

If any retire assertion here ever FAILS because a removed symbol/route was
re-added, that is the intended mutation-check tripwire.
"""
from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- #
# RETIRE 1: the staging watcher module is gone from the field_capture package.
# --------------------------------------------------------------------------- #
def test_action_candidate_staging_watcher_module_is_removed() -> None:
    import field_capture

    assert not hasattr(field_capture, "action_candidate_staging_watcher")

    with pytest.raises(ImportError):
        from field_capture import action_candidate_staging_watcher  # noqa: F401


# --------------------------------------------------------------------------- #
# RETIRE 2: the collector is gone; the shared helpers the live path uses stay.
# --------------------------------------------------------------------------- #
def test_collect_action_candidates_is_removed() -> None:
    from field_capture import action_candidates

    for retired in (
        "collect_action_candidates",
        "collect_action_candidates_report",
        "run",
        "run_list",
        "run_review",
    ):
        assert not hasattr(action_candidates, retired), f"{retired} should be retired"


def test_shared_live_helpers_are_kept() -> None:
    # Positive companion so RETIRE 2 cannot pass vacuously: the helpers the live
    # job_draft path imports from action_candidates MUST remain.
    from field_capture import action_candidates

    for kept in (
        "default_candidate_dir",
        "default_semantic_dirs",
        "ACCEPTED_SEMANTIC_TYPES",
        "iter_semantic_artifacts",
        "payloads_from_semantic",
        "couchdb_candidate_config_or_none",
        "couchdb_patch_action_candidate_fields",
        "couchdb_set_action_candidate_archived",
    ):
        assert hasattr(action_candidates, kept), f"{kept} must stay (live path uses it)"

    # The live emission module imports its helpers from action_candidates.
    from field_capture import job_draft_emission

    assert hasattr(job_draft_emission, "collect_job_drafts")


# --------------------------------------------------------------------------- #
# RETIRE 3: the 3 retired CLI commands are unregistered; a known-good one parses.
# --------------------------------------------------------------------------- #
def _subcommands() -> list[str]:
    import argparse

    import btq

    holder: dict[str, argparse.ArgumentParser] = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):  # type: ignore[no-untyped-def]
        holder["parser"] = self
        return argparse.Namespace()

    argparse.ArgumentParser.parse_args = capture  # type: ignore[assignment]
    try:
        btq.parse_args([])
    finally:
        argparse.ArgumentParser.parse_args = original
    parser = holder["parser"]
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return list(action.choices)


def test_retired_cli_commands_are_unregistered() -> None:
    choices = _subcommands()
    for retired in ("collect-action-candidates", "list-action-candidates", "generate-approved-drafts"):
        assert retired not in choices, f"{retired} should be retired from the CLI"
    # A known-good live command is still registered + parses.
    assert "watch-field-capture-pipeline" in choices

    import btq

    parsed = btq.parse_args(["watch-field-capture-pipeline", "--poll-seconds", "60"])
    assert getattr(parsed, "command", None) == "watch-field-capture-pipeline"


# --------------------------------------------------------------------------- #
# RETIRE 4: the fix/resubmit routes are gone (a POST to them is NOT dispatched).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route", ["/field-capture/review/fix", "/field-capture/review/resubmit"])
def test_fix_resubmit_routes_are_not_dispatched(tmp_path: Path, route: str) -> None:
    from ops_dashboard import post_routes
    from ops_dashboard.common import SectionContext

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    body = urllib.parse.urlencode({"candidate_id": "x", "_rev": "1-x", "reviewer": "Greg"}).encode()

    result = post_routes.dispatch_post_route(route, ctx, body, "application/x-www-form-urlencoded", {})

    # An unrouted POST returns None (the server then 404s); it is NOT handled.
    assert result is None

    # The retired handlers are gone from the section module too.
    from ops_dashboard.sections import candidates

    assert not hasattr(candidates, "handle_fix")
    assert not hasattr(candidates, "handle_resubmit")


# --------------------------------------------------------------------------- #
# LIVE: the job_draft review path still works in-process (render + approve).
# --------------------------------------------------------------------------- #
def _seed_review_draft(couchdb_job_draft_review, draft_id: str) -> None:
    couchdb_job_draft_review.seed_draft(
        {
            "draft_id": draft_id,
            "job_type": "append_to_note",
            "payload": {
                "path": "Accounts/7050.md",
                "content": "Staffing risk: Bruce no-showed.",
                "destination": "site_note",
            },
            "message": "Bruce no-showed again.",
            "site_id": "7050",
            "submitter_name": "Greg",
            "source_capture_id": f"cap-{draft_id}",
            "confidence": "high",
            "created_at": "2026-06-11T00:00:00Z",
        }
    )


def test_live_job_draft_swipe_surface_renders_seeded_draft(tmp_path: Path, couchdb_job_draft_review) -> None:
    from ops_dashboard.sections import swipe

    _seed_review_draft(couchdb_job_draft_review, "draft_live_1")

    cards = swipe.collect_cards(tmp_path)
    assert [c["draft_id"] for c in cards] == ["draft_live_1"]
    assert cards[0]["approvable"] is True


def test_live_job_draft_candidates_page_renders_seeded_draft(tmp_path: Path, couchdb_job_draft_review) -> None:
    from ops_dashboard.sections import candidates

    _seed_review_draft(couchdb_job_draft_review, "draft_live_2")

    body = candidates.render_field_capture_review(tmp_path, {})
    assert "draft_live_2" in body


def test_live_job_draft_approve_flips_review_status(tmp_path: Path, couchdb_job_draft_review) -> None:
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    _seed_review_draft(couchdb_job_draft_review, "draft_live_3")
    rev = couchdb_job_draft_review.rev_of("draft_live_3")

    ctx = SectionContext(tmp_path, lambda: SimpleNamespace(vault_dir=tmp_path / "vault"))
    ctx.action = "approve"
    post_body = urllib.parse.urlencode({"draft_id": "draft_live_3", "_rev": rev, "reviewer": "Greg"}).encode()

    status, _ctype, _out, _headers = candidates.handle_review_post(ctx, post_body)

    assert int(status) in (int(HTTPStatus.OK), int(HTTPStatus.SEE_OTHER))
    assert couchdb_job_draft_review.review_status_of("draft_live_3") == "approved"
