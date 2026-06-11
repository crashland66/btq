"""KEYSTONE behavioral gate for prompt 337a: the ops-dashboard review surfaces
RENDER + ACT on job_draft docs (not action_candidate docs).

Authored by the INDEPENDENT VERIFIER.

The methodology demands rendering, not code-path reading, for UI. These
``@pytest.mark.real_couchdb`` tests stand up a throwaway CouchDB, push the real
333 design doc (so the server-side ``validate_doc_update`` runs), seed
``job_draft`` docs through the REAL writer, point the surfaces' config
resolution at that database, and then:

  (1) RENDER the swipe page (``swipe.swipe_payload`` + ``swipe.render_body``) and
      the table review page (``candidates.render_field_capture_review``) and
      assert the rendered HTML carries the draft's message/site/job_type and the
      approve/reject form carries ``draft_id`` + ``_rev``.
  (2) DRIVE the approve POST handler (``candidates.handle_review_post``) with a
      real draft_id + _rev and assert the draft flips pending_approval ->
      approved via the 335 path (re-read the doc from CouchDB).
  (3) A STALE ``_rev`` approve is rejected as "already decided" and does NOT
      overwrite.
  (4) Reject flips the draft to ``rejected`` (kept, not deleted).
  (5) READS-ZERO-action_candidate: an ``action_candidate`` doc co-resident in
      the SAME database must NOT appear on any draft surface.

These run against a live CouchDB (creds from env or ~/.config/btq/config.json,
same precedent as test_couchdb_job_draft_gate.py). If none is reachable they
skip loudly -- they must not silently pass.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib import error, request

import pytest

from event_pipeline import couchdb_config
from event_pipeline.couchdb_candidate_writer import build_action_candidate_document
from event_pipeline.couchdb_job_draft_writer import (
    get_job_draft,
    job_draft_doc_id,
    upsert_job_draft,
)


pytestmark = pytest.mark.real_couchdb


DESIGN_DOC_PATH = (
    Path(__file__).resolve().parents[1]
    / "event_pipeline"
    / "couchdb"
    / "design_btq_field_captures.json"
)


def _design_doc() -> dict:
    return json.loads(DESIGN_DOC_PATH.read_text(encoding="utf-8"))


def _config_file_creds() -> tuple[str, str, str] | None:
    path = Path(os.path.expanduser("~/.config/btq/config.json"))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("couchdb_url")
    user = data.get("couchdb_user")
    pwd = data.get("couchdb_password")
    if url and user and pwd:
        return url, user, pwd
    return None


def _live_config() -> couchdb_config.CouchDBConfig | None:
    url = os.environ.get("BTQ_TEST_COUCHDB_URL") or os.environ.get("BTQ_COUCHDB_URL")
    user = os.environ.get("BTQ_TEST_COUCHDB_USER") or os.environ.get("BTQ_COUCHDB_USER")
    pwd = os.environ.get("BTQ_TEST_COUCHDB_PASSWORD") or os.environ.get("BTQ_COUCHDB_PASSWORD")
    if not (url and user and pwd):
        creds = _config_file_creds()
        if creds is None:
            return None
        url, user, pwd = creds
    cfg = couchdb_config.from_env(
        base_url_override=url,
        username_override=user,
        password_override=pwd,
        timeout_override=10.0,
    )
    req = request.Request(
        f"{cfg.base_url}/_session",
        headers={**cfg.auth_header(), "Accept": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=5) as resp:
            if not 200 <= getattr(resp, "status", 200) < 300:
                return None
    except (error.URLError, OSError):
        return None
    return cfg


def _http(cfg, method, path, body=None):
    url = f"{cfg.base_url}/{path}"
    headers = {"Accept": "application/json", **cfg.auth_header()}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=10) as resp:
        return int(getattr(resp, "status", 200)), json.loads(resp.read() or b"{}")


@pytest.fixture()
def live_surfaces(monkeypatch, tmp_path):
    """Stand up a throwaway CouchDB + design doc, point ALL surface config
    resolution at it, and yield ``(cfg, db, runtime_root)``.

    The surfaces resolve CouchDB three ways: ``couchdb_job_draft_payloads`` via
    ``job_draft_review.couchdb_candidate_config_or_none`` + ``field_captures_database``;
    the review POST handler via ``couchdb_config.from_env`` + ``field_captures_database``.
    Override all of them so reads + writes target the throwaway db -- the REAL
    list/render/review logic runs end to end against a real CouchDB.
    """
    cfg = _live_config()
    if cfg is None:
        pytest.skip(
            "no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD or "
            "populate ~/.config/btq/config.json)"
        )
    db = f"btq_verify_337a_{uuid.uuid4().hex[:12]}"
    _http(cfg, "PUT", db)
    design = _design_doc()
    _http(
        cfg,
        "PUT",
        f"{db}/_design/btq_field_captures",
        {k: v for k, v in design.items() if k != "_rev"},
    )
    # Provision the type/review_status _find behavior: CouchDB _find works
    # without a declared index (warns, uses a full scan) -- fine for a tiny
    # throwaway db. Point every config resolver at this db.
    from field_capture import job_draft_review as review_mod

    monkeypatch.setattr(review_mod, "couchdb_candidate_config_or_none", lambda: cfg)
    monkeypatch.setattr(couchdb_config, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(couchdb_config, "field_captures_database", lambda *a, **k: db)
    monkeypatch.setattr(
        review_mod.couchdb_config, "field_captures_database", lambda *a, **k: db, raising=False
    )
    try:
        yield cfg, db, tmp_path
    finally:
        try:
            request.urlopen(
                request.Request(
                    f"{cfg.base_url}/{db}",
                    headers={**cfg.auth_header(), "Accept": "application/json"},
                    method="DELETE",
                ),
                timeout=10,
            )
        except (error.URLError, OSError):
            pass


def _seed_draft(cfg, db, draft_id="draft-surf-1", **extra):
    draft = {
        "draft_id": draft_id,
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/7050.md",
            "content": "Staffing risk: Bruce no-showed.",
            "destination": "site_note",
        },
        "message": "Bruce no-showed again at site 7050.",
        "site_id": "7050",
        "submitter_name": "Sandy",
        "source_capture_id": "cap-7050-1",
        "confidence": "high",
        "created_at": "2026-06-11T00:00:00Z",
    }
    draft.update(extra)
    upsert_job_draft(cfg, db, draft)
    _, doc = _http(cfg, "GET", f"{db}/{job_draft_doc_id(draft_id)}")
    return doc


def _seed_action_candidate(cfg, db, candidate_id="cand-leak-1"):
    """Seed a co-resident action_candidate in the SAME db, so reads-zero tests
    prove the draft surfaces do NOT pick it up."""
    payload = {
        "candidate_id": candidate_id,
        "candidate_type": "field_capture_follow_up",
        "status": "pending_review",
        "summary": "LEAK-CANARY action_candidate summary",
        "source_text": "this is an action_candidate, not a job_draft",
        "channel_metadata": {"site_id": "9999", "upload_id": "cap-leak"},
    }
    doc = build_action_candidate_document(payload)
    _http(cfg, "PUT", f"{db}/{doc['_id']}", {k: v for k, v in doc.items() if k != "_rev"})
    return candidate_id


# --------------------------------------------------------------------------- #
# (1) RENDER: the page shows the draft's message/site/job_type + draft_id/_rev.
# --------------------------------------------------------------------------- #
def test_swipe_page_renders_draft_and_form_carries_draft_id_and_rev(live_surfaces):
    from ops_dashboard.sections import swipe

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db)

    payload = swipe.swipe_payload(runtime_root)
    assert payload["counts"]["pending_approval"] == 1, payload["counts"]
    cards = payload["cards"]
    assert [c["draft_id"] for c in cards] == ["draft-surf-1"], cards
    card = cards[0]
    assert card["message"] == "Bruce no-showed again at site 7050."
    assert card["site_id"] == "7050"
    assert card["proposed_job_type"] == "append_to_note"
    assert card["approvable"] is True
    # _rev must be carried so the browser form can POST optimistic concurrency.
    assert card["_rev"] == doc["_rev"]

    body = swipe.render_body(
        SimpleNamespace(runtime_root=runtime_root, config=SimpleNamespace(vault_dir=runtime_root / "vault")),
        payload=payload,
    )
    # The bootstrap JSON the browser parses carries the draft_id + _rev + message.
    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap script not found"
    data = json.loads(m.group(1))
    boot = {c["draft_id"]: c for c in data["cards"]}
    assert "draft-surf-1" in boot
    assert boot["draft-surf-1"]["_rev"] == doc["_rev"]
    assert boot["draft-surf-1"]["proposed_job_type"] == "append_to_note"
    # The hidden POST form template carries draft_id + _rev inputs.
    assert 'name="draft_id"' in body
    assert 'name="_rev"' in body
    assert 'name="candidate_id"' not in body


def test_table_review_page_renders_draft_with_draft_id_and_rev(live_surfaces):
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db)

    html_out = candidates.render_field_capture_review(runtime_root, {"status": ["pending_approval"]})
    assert "Bruce no-showed again at site 7050." in html_out
    assert "draft-surf-1" in html_out
    assert "Drafts" in html_out  # heading retargeted from "Candidates"
    # Approve + reject forms POST draft_id + the live _rev.
    assert 'name="draft_id" value="draft-surf-1"' in html_out
    assert f'name="_rev" value="{doc["_rev"]}"' in html_out
    assert 'action="/field-capture/review/approve"' in html_out
    assert 'action="/field-capture/review/reject"' in html_out


# --------------------------------------------------------------------------- #
# (2) APPROVE POST flips pending_approval -> approved via the 335 path.
# --------------------------------------------------------------------------- #
def test_approve_post_flips_draft_to_approved(live_surfaces):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db)
    assert doc["review_status"] == "pending_approval"

    ctx = SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=runtime_root / "vault"))
    ctx.action = "approve"
    body = urllib.parse.urlencode(
        {"draft_id": "draft-surf-1", "_rev": doc["_rev"], "reviewer": "Greg"}
    ).encode()
    status, _ctype, _out, _headers = candidates.handle_review_post(ctx, body)
    assert int(status) in (200, 303)

    after = get_job_draft(cfg, db, "draft-surf-1")
    assert after is not None
    assert after["review_status"] == "approved", after
    assert after.get("reviewed_by") == "Greg"


# --------------------------------------------------------------------------- #
# (3) STALE _rev approve -> "already decided", no overwrite.
# --------------------------------------------------------------------------- #
def test_stale_rev_approve_is_already_decided_no_overwrite(live_surfaces):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db)
    stale_rev = doc["_rev"]

    # Advance the doc out of band so stale_rev is no longer current AND it is
    # already decided (rejected). A second approve with the stale rev must be a
    # no-op, leaving the rejected status intact.
    ctx = SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=runtime_root / "vault"))
    ctx.action = "reject"
    reject_body = urllib.parse.urlencode(
        {"draft_id": "draft-surf-1", "_rev": stale_rev, "reviewer": "Greg"}
    ).encode()
    candidates.handle_review_post(ctx, reject_body)
    assert get_job_draft(cfg, db, "draft-surf-1")["review_status"] == "rejected"

    # Now approve with the STALE rev -> already decided; must not overwrite.
    ctx.action = "approve"
    approve_body = urllib.parse.urlencode(
        {"draft_id": "draft-surf-1", "_rev": stale_rev, "reviewer": "Mallory"}
    ).encode()
    status, _ctype, _out, headers = candidates.handle_review_post(ctx, approve_body)
    # Handler redirects with an error (does not 500); the doc stays rejected.
    after = get_job_draft(cfg, db, "draft-surf-1")
    assert after["review_status"] == "rejected", after
    location = headers.get("Location", "")
    assert "error=" in location, location


# --------------------------------------------------------------------------- #
# (4) REJECT POST flips the draft to rejected (kept, not deleted).
# --------------------------------------------------------------------------- #
def test_reject_post_flips_draft_to_rejected_and_keeps_it(live_surfaces):
    from ops_dashboard.sections import candidates
    from ops_dashboard.common import SectionContext

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db)

    ctx = SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=runtime_root / "vault"))
    ctx.action = "reject"
    body = urllib.parse.urlencode(
        {"draft_id": "draft-surf-1", "_rev": doc["_rev"], "reviewer": "Greg"}
    ).encode()
    candidates.handle_review_post(ctx, body)

    after = get_job_draft(cfg, db, "draft-surf-1")
    assert after is not None, "rejected draft must be KEPT, not deleted"
    assert after["review_status"] == "rejected", after


# --------------------------------------------------------------------------- #
# (5) READS-ZERO-action_candidate: a co-resident action_candidate must NOT
#     appear on ANY draft surface (swipe, table review, inbox).
# --------------------------------------------------------------------------- #
def test_draft_surfaces_read_zero_action_candidates(live_surfaces):
    from ops_dashboard.sections import swipe
    from ops_dashboard.sections import candidates
    from ops_dashboard.sections import inbox

    cfg, db, runtime_root = live_surfaces
    _seed_draft(cfg, db)  # one real draft
    _seed_action_candidate(cfg, db)  # one co-resident action_candidate (canary)

    # swipe: only the draft, never the candidate canary.
    cards = swipe.collect_cards(runtime_root)
    assert [c["draft_id"] for c in cards] == ["draft-surf-1"], cards
    swipe_body = swipe.render_body(
        SimpleNamespace(runtime_root=runtime_root, config=SimpleNamespace(vault_dir=runtime_root / "vault")),
        payload=swipe.swipe_payload(runtime_root),
    )
    assert "LEAK-CANARY" not in swipe_body
    assert "cand-leak-1" not in swipe_body

    # table review page: shows the draft, not the candidate canary.
    review_html = candidates.render_field_capture_review(runtime_root, {"status": ["all"]})
    assert "draft-surf-1" in review_html
    assert "LEAK-CANARY" not in review_html
    assert "cand-leak-1" not in review_html

    # inbox projection: drafts only.
    candidate_dir = Path(runtime_root)
    rows = inbox.review_candidates(candidate_dir, None, runtime_root)
    ids = [str(r.get("draft_id") or "") for r in rows]
    assert ids == ["draft-surf-1"], rows
    assert all("cand-leak-1" not in str(r) for r in rows)
