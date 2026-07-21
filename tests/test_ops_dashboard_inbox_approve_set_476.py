"""Gating behavioral tests for prompt 476: the dashboard /inbox groups a
capture's pending job_drafts into ONE approve-set card.

Authored by the INDEPENDENT VERIFIER (the implementer wrote no tests).

The discipline here is browser-faithful, not code-path-reading:

  * RENDER the real inbox HTML (``inbox.render``) from drafts seeded through the
    faithful in-memory job_draft double.
  * PARSE the rendered HTML with an HTML parser and serialize the grouped form
    exactly as a browser would (hidden inputs always; checkboxes only when the
    ``checked`` attribute is present; document order preserved).
  * DRIVE the resulting urlencoded body through the REAL POST route
    (``post_routes.dispatch_post_route`` -> ``candidates.handle_approve_set``)
    and assert the drafts flipped in the store.

That chain is what makes the positional ``draft_id``/``_rev`` pairing a tested
property rather than an asserted intention: ``_handle_approve_set_post`` does
``revs[index]``, so a misordered or omitted ``_rev`` would apply one draft's
expected_rev to another and surface as a conflict/already-decided here.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from ops_dashboard import post_routes
from ops_dashboard.common import SectionContext, default_actor
from ops_dashboard.sections import inbox


APPROVE_SET_ROUTE = "/field-capture/review/approve-set"


# --------------------------------------------------------------------------- #
# Browser-faithful form extraction
# --------------------------------------------------------------------------- #
class _FormCollector(HTMLParser):
    """Collect (action, [(name, value), ...]) per <form>, in document order.

    Mirrors what a browser submits: hidden/text inputs always contribute; a
    checkbox contributes its value ONLY when it carries the ``checked``
    attribute. Order is source order, which is what the endpoint's positional
    ``revs[index]`` pairing depends on.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None
        self.submit_buttons: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: (value or "") for key, value in attrs}
        if tag == "form":
            self._current = {"action": attr.get("action", ""), "fields": [], "buttons": 0}
            self.forms.append(self._current)
            return
        if self._current is None:
            return
        if tag == "input":
            input_type = attr.get("type", "text").lower()
            name = attr.get("name", "")
            if not name:
                return
            if input_type == "checkbox" and "checked" not in attr:
                return
            fields = self._current["fields"]
            assert isinstance(fields, list)
            fields.append((name, attr.get("value", "")))
        elif tag == "button" and attr.get("type", "submit").lower() == "submit":
            self._current["buttons"] = int(self._current["buttons"]) + 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def approve_set_forms(body_html: str) -> list[dict[str, object]]:
    collector = _FormCollector()
    collector.feed(body_html)
    return [form for form in collector.forms if form.get("action") == APPROVE_SET_ROUTE]


def field_order(form: dict[str, object], name: str) -> list[str]:
    fields = form["fields"]
    assert isinstance(fields, list)
    return [value for key, value in fields if key == name]


def encoded_body(form: dict[str, object], *, drop_checked: str | None = None) -> bytes:
    """Serialize the form as a browser would.

    ``drop_checked`` simulates the operator UNCHECKING one line: only that
    ``checked`` entry disappears — the paired hidden ``draft_id``/``_rev`` still
    post, which is exactly how the endpoint learns to REJECT that one draft.
    """
    fields = form["fields"]
    assert isinstance(fields, list)
    pairs = [
        (key, value)
        for key, value in fields
        if not (key == "checked" and drop_checked is not None and value == drop_checked)
    ]
    return urlencode(pairs).encode("utf-8")


# --------------------------------------------------------------------------- #
# Fixtures / seeding
# --------------------------------------------------------------------------- #
def seed_draft(
    fake,
    draft_id: str,
    *,
    group_id: str,
    capture_id: str,
    job_type: str = "log_supply_need",
    message: str = "",
    site_id: str = "7050",
    submitter_name: str = "Sandy Sandbox",
) -> None:
    payload = (
        {
            "site_id": site_id,
            "item_name": message or draft_id,
            "urgency": "normal",
            "requested_by": submitter_name,
        }
        if job_type == "log_supply_need"
        else {"path": "Accounts/7050.md", "content": "x", "destination": "site_note"}
    )
    fake.seed_draft(
        {
            "draft_id": draft_id,
            "job_type": job_type,
            "payload": payload,
            "message": message or f"Review {draft_id}.",
            "site_id": site_id,
            "submitter_name": submitter_name,
            "source_capture_id": capture_id,
            "group_id": group_id,
            "created_at": "2026-07-20T12:00:00+00:00",
        }
    )


def section_ctx(tmp_path: Path) -> SectionContext:
    vault = tmp_path / "vault"
    return SectionContext(
        tmp_path / "runtime",
        lambda: SimpleNamespace(vault_dir=vault, vault_root=vault),
    )


def post_approve_set(ctx: SectionContext, body: bytes):
    return post_routes.dispatch_post_route(APPROVE_SET_ROUTE, ctx, body, "application/x-www-form-urlencoded", {})


# --------------------------------------------------------------------------- #
# (1) Two drafts from one capture -> ONE grouped card, 2 checklist lines,
#     ONE approve-set submit.
# --------------------------------------------------------------------------- #
def test_two_drafts_from_one_capture_render_one_grouped_approve_set_card(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    seed_draft(couchdb_job_draft_review, "sup-1", group_id="grp-a", capture_id="cap-a", message="Toilet paper for the 2nd floor")
    seed_draft(couchdb_job_draft_review, "sup-2", group_id="grp-a", capture_id="cap-a", message="Blue shop towels")

    body_html = inbox.render(section_ctx(tmp_path))
    forms = approve_set_forms(body_html)

    # ONE card for the capture, not one per draft.
    assert len(forms) == 1, body_html
    form = forms[0]
    # ONE submit for the whole set.
    assert form["buttons"] == 1
    # Two checklist lines, both checked by default.
    assert field_order(form, "draft_id") == ["sup-1", "sup-2"]
    assert field_order(form, "checked") == ["sup-1", "sup-2"]
    # Each line stays individually openable.
    assert "/candidates?draft_id=sup-1" in body_html
    assert "/candidates?draft_id=sup-2" in body_html
    # The grouped drafts are NOT ALSO listed as standalone table rows.
    assert body_html.count("/candidates?draft_id=sup-1") == 1


# --------------------------------------------------------------------------- #
# (2) The rendered form satisfies _handle_approve_set_post's contract and the
#     REAL handler approves both drafts through the real review write path.
# --------------------------------------------------------------------------- #
def test_grouped_form_pairs_revs_positionally_and_approves_the_whole_set(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    seed_draft(couchdb_job_draft_review, "sup-1", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "sup-2", group_id="grp-a", capture_id="cap-a")

    ctx = section_ctx(tmp_path)
    form = approve_set_forms(inbox.render(ctx))[0]

    draft_ids = field_order(form, "draft_id")
    revs = field_order(form, "_rev")
    reviewers = field_order(form, "reviewer")

    # Positional pairing: same arity, and rev[i] is draft_id[i]'s CURRENT rev.
    assert len(revs) == len(draft_ids) == 2
    assert revs == [couchdb_job_draft_review.rev_of(did) for did in draft_ids]
    # reviewer is required by the endpoint and must be the dashboard default.
    assert reviewers == [default_actor()]
    assert reviewers[0].strip()

    status, _location, _payload, _headers = post_approve_set(ctx, encoded_body(form))

    assert couchdb_job_draft_review.review_status_of("sup-1") == "approved"
    assert couchdb_job_draft_review.review_status_of("sup-2") == "approved"
    assert couchdb_job_draft_review.reviewer_of("sup-1") == default_actor()
    assert couchdb_job_draft_review.reviewer_of("sup-2") == default_actor()
    assert int(status) in {200, 302, 303}


def test_swapped_revs_would_be_caught_as_already_decided(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    """Negative control for test (2): proves the rev pairing is LOAD-BEARING.

    If the renderer ever emitted _revs out of order (or omitted one, shifting
    the rest), the endpoint's revs[index] pairing sends a stale expected_rev and
    the drafts do NOT approve. This test deliberately mis-pairs and asserts the
    failure mode, so a green test (2) means real alignment, not a no-op check.
    """
    seed_draft(couchdb_job_draft_review, "sup-1", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "sup-2", group_id="grp-a", capture_id="cap-a")

    ctx = section_ctx(tmp_path)
    form = approve_set_forms(inbox.render(ctx))[0]
    draft_ids = field_order(form, "draft_id")
    revs = field_order(form, "_rev")

    swapped = urlencode(
        [("reviewer", default_actor())]
        + [("draft_id", draft_ids[0]), ("_rev", revs[1])]
        + [("draft_id", draft_ids[1]), ("_rev", revs[0])]
        + [("checked", did) for did in draft_ids]
    ).encode("utf-8")
    post_approve_set(ctx, swapped)

    assert couchdb_job_draft_review.review_status_of("sup-1") == "pending_approval"
    assert couchdb_job_draft_review.review_status_of("sup-2") == "pending_approval"


# --------------------------------------------------------------------------- #
# (3) Unchecking one line rejects EXACTLY that draft, approves the rest.
# --------------------------------------------------------------------------- #
def test_unchecking_one_line_rejects_only_that_draft(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    seed_draft(couchdb_job_draft_review, "sup-1", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "sup-2", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "sup-3", group_id="grp-a", capture_id="cap-a")

    ctx = section_ctx(tmp_path)
    form = approve_set_forms(inbox.render(ctx))[0]

    body = encoded_body(form, drop_checked="sup-2")
    # The unchecked draft still posts its draft_id + _rev (that is how it gets
    # rejected rather than silently skipped) -- only `checked` loses it.
    assert b"sup-2" in body
    from urllib.parse import parse_qs

    parsed = parse_qs(body.decode(), keep_blank_values=True)
    assert parsed["draft_id"] == ["sup-1", "sup-2", "sup-3"]
    assert parsed["checked"] == ["sup-1", "sup-3"]
    assert len(parsed["_rev"]) == 3

    post_approve_set(ctx, body)

    assert couchdb_job_draft_review.review_status_of("sup-1") == "approved"
    assert couchdb_job_draft_review.review_status_of("sup-2") == "rejected"
    assert couchdb_job_draft_review.review_status_of("sup-3") == "approved"


# --------------------------------------------------------------------------- #
# (4) Single-draft capture regression guard: renders as the existing row.
# --------------------------------------------------------------------------- #
def test_single_draft_capture_still_renders_as_a_plain_row(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    seed_draft(
        couchdb_job_draft_review,
        "solo-1",
        group_id="grp-solo",
        capture_id="cap-solo",
        job_type="append_to_note",
        message="Single ask from one capture.",
    )

    ctx = section_ctx(tmp_path)
    body_html = inbox.render(ctx)

    assert approve_set_forms(body_html) == []
    assert "inbox-group" not in body_html
    # The pre-476 row shape survives: deep link + summary in the table.
    assert "/candidates?draft_id=solo-1" in body_html
    assert "Single ask from one capture." in body_html

    card = next(c for c in inbox.inbox_cards(ctx) if c.get("id") == "captures_with_note")
    assert [str(row.get("draft_id")) for row in card["top"]] == ["solo-1"]
    assert card["groups"] == []
    assert card["count"] == 1


def test_mixed_single_and_grouped_captures_both_surface(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    """The suppression of the empty table must never hide pending work."""
    seed_draft(couchdb_job_draft_review, "grp-1", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "grp-2", group_id="grp-a", capture_id="cap-a")
    seed_draft(couchdb_job_draft_review, "solo-1", group_id="grp-solo", capture_id="cap-solo")

    ctx = section_ctx(tmp_path)
    body_html = inbox.render(ctx)

    assert len(approve_set_forms(body_html)) == 1
    assert "/candidates?draft_id=solo-1" in body_html
    card = next(c for c in inbox.inbox_cards(ctx) if c.get("id") == "captures_with_note")
    assert card["count"] == 3


# --------------------------------------------------------------------------- #
# (5) Escaping: hostile draft content renders inert and round-trips intact.
# --------------------------------------------------------------------------- #
def test_grouped_card_escapes_hostile_draft_content(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    hostile_message = '<script>alert("xss")</script> "quoted" supply'
    seed_draft(
        couchdb_job_draft_review,
        'ev"il-1',
        group_id="grp-x",
        capture_id="cap-x",
        message=hostile_message,
        submitter_name='<img src=x onerror="alert(1)">',
    )
    seed_draft(couchdb_job_draft_review, "sup-2", group_id="grp-x", capture_id="cap-x")

    ctx = section_ctx(tmp_path)
    body_html = inbox.render(ctx)

    # No raw markup from any rendered DRAFT value escapes into the page. (The
    # page layout has its own legitimate <script> block, so assert on the
    # hostile payload itself rather than on the tag in the abstract.)
    assert "<script>alert" not in body_html
    assert "<img src=x" not in body_html
    assert "&lt;script&gt;alert" in body_html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body_html
    # The quote in the draft_id is entity-escaped inside the hidden input, so it
    # cannot terminate the value attribute and inject new attributes.
    assert 'value="ev&quot;il-1"' in body_html
    assert 'value="ev"il-1"' not in body_html

    form = approve_set_forms(body_html)[0]
    # The quote in the draft_id did NOT break out of the hidden input value:
    # the parser recovers the ORIGINAL id, so the handler acts on the right doc.
    assert field_order(form, "draft_id") == ['ev"il-1', "sup-2"]
    assert field_order(form, "checked") == ['ev"il-1', "sup-2"]

    post_approve_set(ctx, encoded_body(form))
    assert couchdb_job_draft_review.review_status_of('ev"il-1') == "approved"
    assert couchdb_job_draft_review.review_status_of("sup-2") == "approved"


# --------------------------------------------------------------------------- #
# (6) A draft missing its _rev must emit an EMPTY _rev, never omit the field --
#     omission would shift every later (draft_id, _rev) pair by one.
# --------------------------------------------------------------------------- #
def test_missing_rev_emits_blank_field_and_keeps_pairs_aligned() -> None:
    candidates = [
        {"draft_id": "a", "candidate_id": "a", "_rev": "1-aaa", "group_id": "g", "capture_id": "cap", "job_type": "log_supply_need", "summary": "A", "site_id": "7050", "submitter_name": "Sandy"},
        {"draft_id": "b", "candidate_id": "b", "_rev": "", "group_id": "g", "capture_id": "cap", "job_type": "log_supply_need", "summary": "B", "site_id": "7050", "submitter_name": "Sandy"},
        {"draft_id": "c", "candidate_id": "c", "_rev": "3-ccc", "group_id": "g", "capture_id": "cap", "job_type": "log_supply_need", "summary": "C", "site_id": "7050", "submitter_name": "Sandy"},
    ]
    _rows, groups = inbox.group_pending_candidates(candidates)
    card_html = inbox.render_capture_group_card(groups[0], Path("/nonexistent-vault"))

    form = approve_set_forms(card_html)[0]
    assert field_order(form, "draft_id") == ["a", "b", "c"]
    # Blank, not absent: index alignment for revs[index] survives.
    assert field_order(form, "_rev") == ["1-aaa", "", "3-ccc"]


def test_group_key_prefers_group_id_then_capture_id() -> None:
    by_group = [
        {"draft_id": "a", "group_id": "g1", "capture_id": "capA"},
        {"draft_id": "b", "group_id": "g1", "capture_id": "capB"},
    ]
    assert len({inbox.candidate_group_key(c) for c in by_group}) == 1

    by_capture = [
        {"draft_id": "a", "group_id": "", "capture_id": "capA"},
        {"draft_id": "b", "group_id": "", "capture_id": "capA"},
    ]
    assert len({inbox.candidate_group_key(c) for c in by_capture}) == 1

    # No group/capture context at all -> each draft stands alone (never merged
    # into one bogus "" bucket that would approve unrelated drafts together).
    orphans = [
        {"draft_id": "a", "group_id": "", "capture_id": ""},
        {"draft_id": "b", "group_id": "", "capture_id": ""},
    ]
    assert len({inbox.candidate_group_key(c) for c in orphans}) == 2


# --------------------------------------------------------------------------- #
# (7) The 5-entry display cap must not make a pending draft unreachable.
# --------------------------------------------------------------------------- #
def test_display_cap_still_offers_a_path_to_every_pending_draft(
    tmp_path: Path, couchdb_job_draft_review
) -> None:
    for index in range(7):
        seed_draft(couchdb_job_draft_review, f"solo-{index}", group_id=f"grp-{index}", capture_id=f"cap-{index}")

    ctx = section_ctx(tmp_path)
    card = next(c for c in inbox.inbox_cards(ctx) if c.get("id") == "captures_with_note")

    assert card["count"] == 7
    assert len(card["top"]) + len(card["groups"]) <= 5
    # The overflow is reachable: the card links out to the full review list.
    assert card["see_all"] == "/candidates?status=pending_approval"
    assert "/candidates?status=pending_approval" in inbox.render(ctx)
