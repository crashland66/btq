"""KEYSTONE behavioral gate for prompt 337b: the ops-dashboard draft-review
EDIT + multi-job CHECKLIST (approve-set) interactions.

Authored by the INDEPENDENT VERIFIER.

337a proved approve/reject. 337b adds two NEW write interactions that the green
suite does NOT exercise:

  (A) Working Edit -- ``candidates.handle_edit`` -> ``_handle_edit_post`` ->
      ``job_draft_review.edit_job_draft_payload`` (the 335 contract:
      re-validate via queue_spec, persist iff valid, persist NOTHING on an
      invalid payload, surface the reason).

  (B) Multi-job checklist / approve-set -- ``candidates.handle_approve_set`` ->
      ``_handle_approve_set_post``: for each *checked* draft_id ``approve``, for
      each *unchecked* ``reject``; every draft is its own decision and a
      mid-set already_decided must NOT abort the rest.

These ``@pytest.mark.real_couchdb`` tests stand up a throwaway CouchDB, push the
real 333 design doc (so the server-side ``validate_doc_update`` runs), seed real
``job_draft`` docs through the REAL writer, point ALL surface/handler config
resolution at that database, drive the REAL POST handlers (building the urlencoded
body exactly as the browser would), and re-read CouchDB to assert outcomes.

They also RENDER (not code-read) the swipe surface to prove a multi-draft group
carries one checklist entry per draft and a single-draft group carries no
checklist chrome.

If no live CouchDB is reachable they skip LOUDLY -- they must not silently pass.
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
    """Stand up a throwaway CouchDB + design doc and point ALL surface/handler
    config resolution at it; yield ``(cfg, db, runtime_root)``.

    Edit + approve-set handlers resolve CouchDB via ``couchdb_config.from_env`` +
    ``field_captures_database``; the swipe render path resolves via
    ``job_draft_review.couchdb_candidate_config_or_none`` + ``field_captures_database``.
    Override every resolver so reads + writes target the throwaway db -- the REAL
    handler + render logic runs end to end against a real CouchDB.
    """
    cfg = _live_config()
    if cfg is None:
        pytest.skip(
            "no reachable CouchDB (set BTQ_COUCHDB_URL/USER/PASSWORD or "
            "populate ~/.config/btq/config.json)"
        )
    db = f"btq_verify_337b_{uuid.uuid4().hex[:12]}"
    _http(cfg, "PUT", db)
    design = _design_doc()
    _http(
        cfg,
        "PUT",
        f"{db}/_design/btq_field_captures",
        {k: v for k, v in design.items() if k != "_rev"},
    )
    from field_capture import job_draft_review as review_mod

    monkeypatch.setattr(review_mod, "couchdb_candidate_config_or_none", lambda: cfg)
    monkeypatch.setattr(couchdb_config, "from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(couchdb_config, "field_captures_database", lambda *a, **k: db)
    monkeypatch.setattr(
        review_mod.couchdb_config, "field_captures_database", lambda *a, **k: db, raising=False
    )
    # candidates.py imports couchdb_config as a module symbol; the from_env /
    # field_captures_database monkeypatches above are on that same module object,
    # so the handler's couchdb_config.from_env()/field_captures_database() resolve
    # to the throwaway db. Assert that wiring rather than trust it.
    from ops_dashboard.sections import candidates as candidates_mod

    assert candidates_mod.couchdb_config.from_env() is cfg
    assert candidates_mod.couchdb_config.field_captures_database() == db
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


# A valid append_to_note payload (passes queue_spec.validate_job).
def _valid_payload(content="Staffing risk: Bruce no-showed.", path="Accounts/7050.md"):
    return {"path": path, "content": content, "destination": "site_note"}


def _seed_draft(cfg, db, draft_id="draft-edit-1", *, payload=None, **extra):
    draft = {
        "draft_id": draft_id,
        "job_type": "append_to_note",
        "payload": payload if payload is not None else _valid_payload(),
        "message": f"Bruce no-showed again ({draft_id}).",
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


def _ctx(runtime_root):
    from ops_dashboard.common import SectionContext

    return SectionContext(runtime_root, lambda: SimpleNamespace(vault_dir=runtime_root / "vault"))


def _location(headers: dict[str, str]) -> str:
    return headers.get("Location", "") or headers.get("location", "")


# --------------------------------------------------------------------------- #
# EDIT
# --------------------------------------------------------------------------- #
def test_edit_valid_payload_persists_revalidates_and_stays_pending(live_surfaces):
    """A valid edited payload is persisted, validation_error cleared, status
    stays pending_approval, _rev advances, response is a redirect success."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db, "draft-edit-1")
    before_rev = doc["_rev"]
    before_payload = doc["payload"]
    assert doc["review_status"] == "pending_approval"

    new_payload = _valid_payload(content="EDITED: Bruce no-showed twice this week.")
    assert new_payload != before_payload
    body = urllib.parse.urlencode(
        {
            "draft_id": "draft-edit-1",
            "_rev": before_rev,
            "reviewer": "Greg",
            "payload": json.dumps(new_payload),
        }
    ).encode()
    status, _ctype, _out, headers = candidates.handle_edit(_ctx(runtime_root), body)
    assert int(status) in (200, 303), status

    after = get_job_draft(cfg, db, "draft-edit-1")
    assert after is not None
    assert after["payload"] == new_payload, after["payload"]
    assert after["review_status"] == "pending_approval", after
    assert not after.get("validation_error"), after.get("validation_error")
    assert after["_rev"] != before_rev, "edit must advance _rev"
    loc = _location(headers)
    assert "error=" not in loc, loc  # success flash, not error


def test_edit_invalid_payload_not_persisted_and_flashes_reason(live_surfaces):
    """An edit whose payload fails validate_job must NOT overwrite the stored
    payload; the flash carries the reason; no 500."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db, "draft-edit-bad")
    before_rev = doc["_rev"]
    before_payload = doc["payload"]

    # Invalid: drop the required "destination" field for append_to_note.
    bad_payload = {"path": "Accounts/7050.md", "content": "missing destination -> invalid"}
    body = urllib.parse.urlencode(
        {
            "draft_id": "draft-edit-bad",
            "_rev": before_rev,
            "reviewer": "Greg",
            "payload": json.dumps(bad_payload),
        }
    ).encode()
    status, _ctype, _out, headers = candidates.handle_edit(_ctx(runtime_root), body)
    assert int(status) in (200, 303), status  # NOT a 500

    after = get_job_draft(cfg, db, "draft-edit-bad")
    assert after["payload"] == before_payload, ("invalid edit must not overwrite", after["payload"])
    assert after["_rev"] == before_rev, "invalid edit must not advance _rev"
    loc = _location(headers)
    assert "error=" in loc, loc  # flashes the failure


def test_edit_malformed_json_flashes_error_payload_unchanged(live_surfaces):
    """Malformed JSON in the payload field -> flash error, no 500, payload
    untouched."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db, "draft-edit-malformed")
    before_payload = doc["payload"]
    before_rev = doc["_rev"]

    body = urllib.parse.urlencode(
        {
            "draft_id": "draft-edit-malformed",
            "_rev": before_rev,
            "reviewer": "Greg",
            "payload": "{not valid json,,,",
        }
    ).encode()
    status, _ctype, _out, headers = candidates.handle_edit(_ctx(runtime_root), body)
    assert int(status) in (200, 303), status

    after = get_job_draft(cfg, db, "draft-edit-malformed")
    assert after["payload"] == before_payload, after["payload"]
    assert after["_rev"] == before_rev
    loc = _location(headers)
    assert "error=" in loc, loc
    assert "malformed" in urllib.parse.unquote(loc).lower(), loc


def test_edit_stale_rev_is_already_decided_no_overwrite(live_surfaces):
    """A stale _rev edit must be reported as already-decided / error and must
    NOT overwrite the payload."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db, "draft-edit-stale")
    stale_rev = doc["_rev"]

    # Advance the doc out of band with a first VALID edit, so stale_rev is no
    # longer current.
    first_payload = _valid_payload(content="first valid edit advances the rev")
    candidates.handle_edit(
        _ctx(runtime_root),
        urllib.parse.urlencode(
            {
                "draft_id": "draft-edit-stale",
                "_rev": stale_rev,
                "reviewer": "Greg",
                "payload": json.dumps(first_payload),
            }
        ).encode(),
    )
    mid = get_job_draft(cfg, db, "draft-edit-stale")
    assert mid["payload"] == first_payload
    current_rev = mid["_rev"]
    assert current_rev != stale_rev

    # Now a SECOND edit with the STALE rev must not overwrite.
    clobber = _valid_payload(content="STALE clobber attempt must not land")
    status, _ctype, _out, headers = candidates.handle_edit(
        _ctx(runtime_root),
        urllib.parse.urlencode(
            {
                "draft_id": "draft-edit-stale",
                "_rev": stale_rev,
                "reviewer": "Mallory",
                "payload": json.dumps(clobber),
            }
        ).encode(),
    )
    assert int(status) in (200, 303), status
    after = get_job_draft(cfg, db, "draft-edit-stale")
    assert after["payload"] == first_payload, ("stale edit must not clobber", after["payload"])
    assert after["_rev"] == current_rev
    loc = _location(headers)
    assert "error=" in loc, loc


# --------------------------------------------------------------------------- #
# CHECKLIST / APPROVE-SET
# --------------------------------------------------------------------------- #
def _seed_group(cfg, db, group_id="grp-1", n=3):
    """Seed N valid drafts sharing one group_id; return their live docs by id."""
    docs = {}
    for i in range(n):
        did = f"{group_id}-d{i}"
        docs[did] = _seed_draft(
            cfg,
            db,
            did,
            group_id=group_id,
            payload=_valid_payload(content=f"group {group_id} draft {i}"),
        )
    return docs


def test_approve_set_splits_checked_approved_unchecked_rejected(live_surfaces):
    """N=3 group, 2 checked + 1 unchecked -> the 2 checked become approved, the
    1 unchecked becomes rejected; summary reflects per-draft outcomes."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    docs = _seed_group(cfg, db, "grp-split", n=3)
    ids = sorted(docs)  # grp-split-d0, d1, d2
    checked = {ids[0], ids[1]}  # approve these two
    unchecked = ids[2]  # reject this one

    fields = [("reviewer", "Greg"), ("rationale", "")]
    for did in ids:
        fields.append(("draft_id", did))
        fields.append(("_rev", docs[did]["_rev"]))
    for did in checked:
        fields.append(("checked", did))
    body = urllib.parse.urlencode(fields).encode()

    status, _ctype, _out, headers = candidates.handle_approve_set(_ctx(runtime_root), body)
    assert int(status) in (200, 303), status

    final = {did: get_job_draft(cfg, db, did)["review_status"] for did in ids}
    assert final[ids[0]] == "approved", final
    assert final[ids[1]] == "approved", final
    assert final[unchecked] == "rejected", final
    loc = urllib.parse.unquote_plus(_location(headers))
    assert "approved 2" in loc and "rejected 1" in loc, loc


def test_approve_set_partial_already_decided_does_not_abort_rest(live_surfaces):
    """If one group draft is already decided (approved out of band), approve-set
    reports it already_decided but STILL decides the rest -- proof the others
    moved."""
    from ops_dashboard.sections import candidates

    cfg, db, runtime_root = live_surfaces
    docs = _seed_group(cfg, db, "grp-partial", n=3)
    ids = sorted(docs)

    # Pre-decide ids[0] out of band (approve it via the 335 path directly).
    from field_capture import job_draft_review

    pre = job_draft_review.apply_job_draft_review(
        cfg, db, ids[0], "approve", "OutOfBand", expected_rev=docs[ids[0]]["_rev"]
    )
    assert pre.error is None, pre
    assert get_job_draft(cfg, db, ids[0])["review_status"] == "approved"

    # Now approve-set the whole group: check ids[1] (approve), leave ids[2]
    # unchecked (reject). ids[0] is already-decided -- it must not abort the rest.
    fields = [("reviewer", "Greg"), ("rationale", "")]
    for did in ids:
        fields.append(("draft_id", did))
        fields.append(("_rev", docs[did]["_rev"]))
    fields.append(("checked", ids[1]))
    body = urllib.parse.urlencode(fields).encode()

    status, _ctype, _out, headers = candidates.handle_approve_set(_ctx(runtime_root), body)
    assert int(status) in (200, 303), status

    final = {did: get_job_draft(cfg, db, did)["review_status"] for did in ids}
    # ids[0] stays approved (already decided, untouched).
    assert final[ids[0]] == "approved", final
    # The REST still moved: ids[1] approved, ids[2] rejected.
    assert final[ids[1]] == "approved", ("rest must still decide after a mid-set already_decided", final)
    assert final[ids[2]] == "rejected", final
    loc = urllib.parse.unquote_plus(_location(headers))
    assert "1 already-decided" in loc, loc


def test_approve_set_single_draft_group_renders_no_checklist_and_approve_works(live_surfaces):
    """A single-draft group yields a card with one draft and NO checklist chrome
    (the rendered bootstrap card carries exactly one draft), and the normal
    approve-set path still flips it."""
    from ops_dashboard.sections import candidates, swipe

    cfg, db, runtime_root = live_surfaces
    doc = _seed_draft(cfg, db, "solo-1", group_id="grp-solo")

    cards = swipe.collect_cards(runtime_root)
    assert len(cards) == 1, cards
    card = cards[0]
    assert isinstance(card.get("drafts"), list)
    assert len(card["drafts"]) == 1, card["drafts"]  # single draft -> no checklist
    assert card["drafts"][0]["draft_id"] == "solo-1"

    # Render: the bootstrap card carries exactly one draft (client renders the
    # single-draft form, not the checklist), and no per-draft checkbox markup
    # appears for this card's data.
    body = swipe.render_body(
        SimpleNamespace(
            runtime_root=runtime_root,
            config=SimpleNamespace(vault_dir=runtime_root / "vault"),
        ),
        payload=swipe.swipe_payload(runtime_root),
    )
    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap script not found"
    data = json.loads(m.group(1).replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">"))
    boot_cards = data["cards"]
    assert len(boot_cards) == 1, boot_cards
    assert len(boot_cards[0]["drafts"]) == 1, boot_cards[0]["drafts"]

    # And the normal (single) approve-set still works.
    fields = [
        ("reviewer", "Greg"),
        ("rationale", ""),
        ("draft_id", "solo-1"),
        ("_rev", doc["_rev"]),
        ("checked", "solo-1"),
    ]
    status, _ctype, _out, _headers = candidates.handle_approve_set(
        _ctx(runtime_root), urllib.parse.urlencode(fields).encode()
    )
    assert int(status) in (200, 303), status
    assert get_job_draft(cfg, db, "solo-1")["review_status"] == "approved"


def test_multi_draft_group_render_has_checkbox_per_draft_with_job_type(live_surfaces):
    """A group with >1 draft -> the rendered swipe surface carries one checklist
    entry per draft (the bootstrap card holds N drafts each with its job_type,
    and the client-side checklist template emits one checkbox per draft)."""
    from ops_dashboard.sections import swipe

    cfg, db, runtime_root = live_surfaces
    # Three drafts of distinct job_types sharing a group_id. visit_create +
    # close_recruiting give visibly different job_types alongside append_to_note.
    _seed_draft(cfg, db, "mg-d0", group_id="mg", payload=_valid_payload(content="a"))
    _seed_draft(
        cfg,
        db,
        "mg-d1",
        group_id="mg",
        job_type="visit_create",
        payload={"site": "7050", "confidence": "high", "source": "field", "evidence": "saw it"},
    )
    _seed_draft(
        cfg,
        db,
        "mg-d2",
        group_id="mg",
        job_type="flag_access_constraint",
        payload={"site": "7050", "details": "gate code changed"},
    )

    cards = swipe.collect_cards(runtime_root)
    assert len(cards) == 1, ("group must collapse to one card", cards)
    card = cards[0]
    drafts = card["drafts"]
    assert len(drafts) == 3, drafts
    job_types = {d["draft_id"]: d["job_type"] for d in drafts}
    assert job_types == {
        "mg-d0": "append_to_note",
        "mg-d1": "visit_create",
        "mg-d2": "flag_access_constraint",
    }, job_types

    body = swipe.render_body(
        SimpleNamespace(
            runtime_root=runtime_root,
            config=SimpleNamespace(vault_dir=runtime_root / "vault"),
        ),
        payload=swipe.swipe_payload(runtime_root),
    )
    m = re.search(r'id="swipe-bootstrap" type="application/json">(.*?)</script>', body, re.S)
    assert m, "bootstrap script not found"
    data = json.loads(m.group(1).replace("\\u0026", "&").replace("\\u003c", "<").replace("\\u003e", ">"))
    boot = data["cards"][0]
    boot_drafts = boot["drafts"]
    assert len(boot_drafts) == 3, boot_drafts
    # Each draft's job_type survives into the bootstrap the checklist renders from.
    assert {d["job_type"] for d in boot_drafts} == {
        "append_to_note",
        "visit_create",
        "flag_access_constraint",
    }, boot_drafts
    # The client-side checklist template that emits one checkbox per draft is
    # present (renderChecklist maps drafts -> input[type=checkbox name=checked]).
    assert "renderChecklist" in body
    assert 'name="checked"' in body
    assert "function renderChecklist" in body
