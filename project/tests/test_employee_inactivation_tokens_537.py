"""Gating tests for employee inactivation vs. active field-capture tokens (537).

Verifier-authored. Contract under test (identity save with status=inactive on
an employee that is not already inactive):

  * 0 active tokens  -> plain PUT + redirect, no token calls.
  * N active tokens  -> 200 HTML confirmation counting ONLY unrevoked, unexpired
    tokens under the employee's identity keys (computed from the doc AS LOADED),
    no PUT / revoke / sync.
  * _token_decision=revoke_tokens -> PUT FIRST, then store.revoke_token +
    sync_token_to_vps("revoke", ...) per token; redirect ?tokens_deactivated=N.
  * keep_tokens -> PUT, no token calls, redirect ?tokens_kept=N.
  * cancel -> no PUT; identity edit form re-rendered with SUBMITTED values.
  * inventory drift between confirm and decision -> re-confirm + audit, no PUT.
  * PUT failure -> zero revocations.
  * partial gateway failure -> token_deactivation_pending + retry route.
  * raw token secrets never appear in audits, redirects, or HTML.

Sandbox identities only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from ops_dashboard import post_routes
from ops_dashboard.sections import employee_detail as ed
from ops_dashboard.sections import tokens

EMPLOYEE_ID = "sandbox_user"
DOC_ID = "employee_sandbox_user"
LEGACY_PERSON_ID = "sandbox-user"  # dash-form legacy person_id, distinct from the doc slug
OTHER_PERSON_ID = "sandbox_other"
NOW = datetime.now(timezone.utc)


class DummyContext(SimpleNamespace):
    def __init__(self, tmp_path: Path, query: dict[str, list[str]] | None = None) -> None:
        super().__init__(runtime_root=tmp_path / "runtime", query=query or {})
        self.audit_entries: list[tuple[str, dict[str, object], str]] = []

    def redirect(self, location: str):
        return 303, "text/html; charset=utf-8", f'<a href="{location}">Return</a>'.encode(), {"Location": location}

    def audit(self, route: str, payload: dict[str, object], result: str) -> None:
        self.audit_entries.append((route, payload, result))


def _doc(**overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": DOC_ID,
        "_rev": "1-abc",
        "type": "employee",
        "operator": "op_greg",
        "first": "Sandy",
        "last": "Sandbox",
        "name": "Sandy Sandbox",
        "status": "active",
        "job": "SANDBOX",
        "site_ids": ["SANDBOX"],
    }
    doc.update(overrides)
    return doc


def _install_doc(monkeypatch: pytest.MonkeyPatch, doc: dict[str, object]) -> None:
    monkeypatch.setattr(ed, "_load_vault_doc", lambda _doc_id: dict(doc))


def _install_render_loaders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ed, "_load_location_name", lambda s: f"Site {s}")
    monkeypatch.setattr(ed, "_field_captures", lambda _p: [])
    monkeypatch.setattr(ed, "_personnel_events", lambda _d: [])


def _install_put(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    raise_exc: Exception | None = None,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_request_json(method: str, path: str, payload: dict[str, object]):
        calls.append({"method": method, "path": path, "payload": payload})
        events.append(f"{method}:{path}")
        if raise_exc is not None:
            raise raise_exc
        return {"ok": True}

    monkeypatch.setattr(ed.sites, "request_json", fake_request_json)
    return calls


def _install_sync(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    results: list[tuple[bool, str]] | None = None,
    *,
    raise_exc: Exception | None = None,
) -> list[tuple[str, dict[str, object]]]:
    calls: list[tuple[str, dict[str, object]]] = []
    queue = list(results or [])

    def fake_sync(action: str, payload: dict[str, object]) -> tuple[bool, str]:
        calls.append((action, dict(payload)))
        events.append(f"sync:{action}:{payload.get('token_id')}")
        if raise_exc is not None:
            raise raise_exc
        return queue.pop(0) if queue else (True, "")

    monkeypatch.setattr(tokens, "sync_token_to_vps", fake_sync)
    return calls


def _identity_form(**overrides: object) -> dict[str, object]:
    form: dict[str, object] = {
        "_section": "identity",
        "_rev": "1-abc",
        "_entity_id": EMPLOYEE_ID,
        "first": "Sandy",
        "last": "Sandbox",
        "preferred_name": "",
        "person_id": "",
        "status": "inactive",
        "phone": "",
        "email": "",
    }
    form.update(overrides)
    return form


def _body(form: dict[str, object]) -> bytes:
    return urlencode(form).encode("utf-8")


def _seed_tokens(store) -> dict[str, object]:
    """Two active + one expired + one revoked under the legacy person_id, plus one for another employee."""
    active_a = store.create_token(person_id=LEGACY_PERSON_ID, label="phone", expires_at=None)
    active_b = store.create_token(person_id=LEGACY_PERSON_ID, label="tablet", expires_at=NOW + timedelta(days=30))
    expired = store.create_token(person_id=LEGACY_PERSON_ID, label="old", expires_at=NOW - timedelta(days=1))
    revoked = store.create_token(person_id=LEGACY_PERSON_ID, label="lost", expires_at=None)
    assert store.revoke_token(revoked.record.token_id)
    other = store.create_token(person_id=OTHER_PERSON_ID, label="other-phone", expires_at=None)
    return {
        "active_ids": {active_a.record.token_id, active_b.record.token_id},
        "active_a": active_a,
        "active_b": active_b,
        "expired": expired,
        "revoked": revoked,
        "other": other,
        "raw_values": [c.token_value for c in (active_a, active_b, expired, revoked, other)],
    }


def _revoked_ids(store) -> set[str]:
    return {r.token_id for r in store.list_tokens() if r.revoked}


def _redirect_query(headers: dict[str, str]) -> dict[str, list[str]]:
    return parse_qs(urlsplit(headers["Location"]).query, keep_blank_values=True)


def _assert_no_raw_secret(ctx: DummyContext, raw_values: list[str], *blobs: object) -> None:
    for raw in raw_values:
        assert raw.startswith("fc_")
        for route, payload, result in ctx.audit_entries:
            assert raw not in route and raw not in repr(payload) and raw not in result
        for blob in blobs:
            text = blob.decode("utf-8") if isinstance(blob, (bytes, bytearray)) else str(blob)
            assert raw not in text


@pytest.fixture(autouse=True)
def _sync_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real sync short-circuits when this is set; the recorder must be reached instead.
    monkeypatch.delenv("BTQ_TOKEN_SYNC_DISABLED", raising=False)


@pytest.fixture
def ctx(tmp_path: Path) -> DummyContext:
    return DummyContext(tmp_path)


@pytest.fixture
def store(ctx: DummyContext):
    return tokens.token_store(ctx.runtime_root)


# ---------------------------------------------------------------------------
# 1. No active tokens -> save as before
# ---------------------------------------------------------------------------


def test_inactivation_with_no_active_tokens_saves_directly(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    # Only non-active tokens exist for this identity.
    expired = store.create_token(person_id=LEGACY_PERSON_ID, label="old", expires_at=NOW - timedelta(hours=1))
    revoked = store.create_token(person_id=LEGACY_PERSON_ID, label="lost", expires_at=None)
    store.revoke_token(revoked.record.token_id)

    status, _ct, body, headers = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(_identity_form()))

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}"
    assert len(puts) == 1 and puts[0]["method"] == "PUT"
    assert puts[0]["payload"]["status"] == "inactive"
    assert syncs == []
    assert _revoked_ids(store) == {revoked.record.token_id}
    assert not any(route.startswith("/tokens/") for route, _p, _r in ctx.audit_entries)
    _assert_no_raw_secret(ctx, [expired.token_value, revoked.token_value], body, headers["Location"])


# ---------------------------------------------------------------------------
# 2. Active tokens -> confirmation, counting only truly active ones
# ---------------------------------------------------------------------------


def test_inactivation_with_active_tokens_renders_confirmation_counting_only_active(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)
    submitted = _identity_form(first="Renamed", phone="814-555-0100")

    status, content_type, body, _headers = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(submitted))

    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    html = body.decode("utf-8")
    assert "This employee has 2 active tokens. Deactivate those tokens too?" in html
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}

    # Hidden inputs carry the submitted identity fields + bookkeeping.
    for key in ("first", "last", "preferred_name", "person_id", "status", "phone", "email", "_rev", "_entity_id", "_section"):
        assert f'name="{key}"' in html, key
    assert 'name="first" value="Renamed"' in html
    assert 'name="phone" value="814-555-0100"' in html
    assert 'name="status" value="inactive"' in html
    assert 'name="_section" value="identity"' in html
    assert 'name="_rev" value="1-abc"' in html
    assert f'name="_entity_id" value="{EMPLOYEE_ID}"' in html
    assert f'action="/employees/{EMPLOYEE_ID}/save-section"' in html

    # _token_ids lists exactly the two active ids.
    import re

    match = re.search(r'name="_token_ids" value="([^"]*)"', html)
    assert match, "missing _token_ids hidden input"
    assert set(match.group(1).split(",")) == seeded["active_ids"]
    assert seeded["expired"].record.token_id not in match.group(1)
    assert seeded["revoked"].record.token_id not in match.group(1)
    assert seeded["other"].record.token_id not in html

    # Three decision buttons.
    for value in ("revoke_tokens", "keep_tokens", "cancel"):
        assert f'name="_token_decision" value="{value}"' in html, value
    _assert_no_raw_secret(ctx, seeded["raw_values"], body)


def test_confirmation_uses_singular_wording_for_one_token(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    _install_put(monkeypatch, events)
    _install_sync(monkeypatch, events)
    store.create_token(person_id=LEGACY_PERSON_ID, label="only", expires_at=None)

    status, _ct, body, _h = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(_identity_form()))

    assert status == 200
    assert "This employee has 1 active token. Deactivate those tokens too?" in body.decode("utf-8")


# ---------------------------------------------------------------------------
# 3. revoke_tokens: PUT first, then revoke + sync exactly those ids
# ---------------------------------------------------------------------------


def test_revoke_decision_puts_first_then_revokes_and_syncs_exactly_the_listed_tokens(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)
    ids = sorted(seeded["active_ids"])

    status, _ct, body, headers = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(ids))),
    )

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}?tokens_deactivated=2"
    assert "token_deactivation_pending" not in headers["Location"]
    # Ordering: the PUT is the first event, both syncs follow it.
    assert len(events) == 3
    assert events[0].startswith("PUT:")
    assert all(e.startswith("sync:revoke:") for e in events[1:])
    assert len(puts) == 1 and puts[0]["payload"]["status"] == "inactive"
    assert sorted(payload["token_id"] for _a, payload in syncs) == ids
    assert all(action == "revoke" and payload["action"] == "revoke" for action, payload in syncs)
    # Exactly the two active tokens revoked; the other employee's and the expired one untouched.
    assert _revoked_ids(store) == seeded["active_ids"] | {seeded["revoked"].record.token_id}
    assert store.get_token(seeded["other"].record.token_id).revoked is False
    assert store.get_token(seeded["expired"].record.token_id).revoked is False
    _assert_no_raw_secret(ctx, seeded["raw_values"], body, headers["Location"])


def test_revoke_decision_generic_put_failure_prevents_any_revocation(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    _install_put(monkeypatch, events, raise_exc=RuntimeError("couch down"))
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, _ct, _resp, headers = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(seeded["active_ids"]))),
    )

    assert status == 303
    assert headers["Location"].startswith(f"/employees/{EMPLOYEE_ID}?error=")
    assert "couch" in headers["Location"]
    assert syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}
    assert all(not e.startswith("sync:") for e in events)


def test_revoke_decision_put_conflict_rerenders_without_revocation(monkeypatch, ctx, store) -> None:
    class ConflictError(Exception):
        code = 409

    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    _install_put(monkeypatch, events, raise_exc=ConflictError())
    syncs = _install_sync(monkeypatch, events)
    monkeypatch.setattr(ed, "render", lambda ctx, employee_id, edit_values=None: "<html>rerendered</html>")
    seeded = _seed_tokens(store)

    status, content_type, body, _h = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(seeded["active_ids"]))),
    )

    assert status == 200 and content_type == "text/html; charset=utf-8"
    assert b"rerendered" in body
    assert syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}
    assert any(result == "failed: conflict" for _r, _p, result in ctx.audit_entries)


# ---------------------------------------------------------------------------
# 4. keep_tokens
# ---------------------------------------------------------------------------


def test_keep_decision_saves_and_leaves_tokens_alone(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, _ct, body, headers = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="keep_tokens", _token_ids=",".join(seeded["active_ids"]))),
    )

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}?tokens_kept=2"
    assert len(puts) == 1 and puts[0]["payload"]["status"] == "inactive"
    assert syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}
    _assert_no_raw_secret(ctx, seeded["raw_values"], body, headers["Location"])


# ---------------------------------------------------------------------------
# 5. cancel -> identity edit form with the SUBMITTED values, doc untouched
# ---------------------------------------------------------------------------


def test_cancel_decision_rerenders_identity_form_with_submitted_values_and_no_put(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    _install_render_loaders(monkeypatch)
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, content_type, body, _h = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(first="Changed", phone="814-555-0199", _token_decision="cancel", _token_ids=",".join(seeded["active_ids"]))),
    )

    assert status == 200 and content_type == "text/html; charset=utf-8"
    html = body.decode("utf-8")
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}
    # The identity edit form is open...
    assert f'<form method="post" action="/employees/{EMPLOYEE_ID}/save-section"' in html
    assert '<input type="hidden" name="_section" value="identity">' in html
    # ...and its controls carry the submitted, unsaved values.
    assert '<input type="text" name="first" value="Changed">' in html
    assert '<input type="text" name="phone" value="814-555-0199">' in html
    assert '<option value="inactive" selected>' in html
    # The stored doc is unchanged: the header still shows the persisted name.
    assert "<h1>Sandy Sandbox</h1>" in html
    assert "Deactivate those tokens too?" not in html
    _assert_no_raw_secret(ctx, seeded["raw_values"], body)


# ---------------------------------------------------------------------------
# 6. Inventory drift between confirmation and decision
# ---------------------------------------------------------------------------


def test_revoke_decision_with_new_token_since_confirmation_rerenders_and_audits(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)
    confirmed_ids = ",".join(sorted(seeded["active_ids"]))
    # A new token is issued after the operator saw the confirmation.
    newer = store.create_token(person_id=DOC_ID, label="new phone", expires_at=None)

    status, content_type, body, _h = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=confirmed_ids)),
    )

    assert status == 200 and content_type == "text/html; charset=utf-8"
    html = body.decode("utf-8")
    assert "This employee has 3 active tokens." in html
    assert "token inventory changed" in html.lower()
    assert newer.record.token_id in html
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}
    mismatch = [e for e in ctx.audit_entries if "inventory changed" in e[2]]
    assert len(mismatch) == 1
    route, payload, _result = mismatch[0]
    assert route == f"/employees/{EMPLOYEE_ID}/save-section"
    assert payload["decision"] == "revoke_tokens"
    assert set(payload["token_ids"]) == seeded["active_ids"]
    _assert_no_raw_secret(ctx, seeded["raw_values"] + [newer.token_value], body)


def test_revoke_decision_when_listed_token_was_revoked_meanwhile_rerenders_as_changed(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)
    # One of the two listed tokens was revoked (e.g. from /tokens) before the decision arrived.
    store.revoke_token(seeded["active_b"].record.token_id)

    status, _ct, body, _h = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(seeded["active_ids"]))),
    )

    assert status == 200
    html = body.decode("utf-8")
    assert "This employee has 1 active token." in html
    assert "token inventory changed" in html.lower()
    assert puts == [] and syncs == []
    assert store.get_token(seeded["active_a"].record.token_id).revoked is False


# ---------------------------------------------------------------------------
# 7/8. Partial gateway failure -> pending + retry route
# ---------------------------------------------------------------------------


def test_partial_sync_failure_marks_pending_and_retry_route_resolves_it(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    _install_render_loaders(monkeypatch)
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    seeded = _seed_tokens(store)
    ids = sorted(seeded["active_ids"])
    syncs = _install_sync(monkeypatch, events, results=[(False, "boom"), (True, "")])

    status, _ct, body, headers = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(ids))),
    )

    assert status == 303
    assert len(puts) == 1
    assert len(syncs) == 2
    failed_id = syncs[0][1]["token_id"]
    # Both revoked locally regardless of gateway outcome.
    assert _revoked_ids(store) == seeded["active_ids"] | {seeded["revoked"].record.token_id}
    query = _redirect_query(headers)
    assert query["tokens_deactivated"] == ["1"]
    assert query["token_deactivation_pending"] == [failed_id]
    _assert_no_raw_secret(ctx, seeded["raw_values"], body, headers["Location"])

    # GET render with that query shows the unresolved warning + retry form.
    get_ctx = DummyContext(ctx.runtime_root.parent, query=query)
    page = ed.render(get_ctx, EMPLOYEE_ID)
    assert "gateway revoke is unresolved" in page
    assert f'<form method="post" action="/employees/{EMPLOYEE_ID}/retry-token-deactivation">' in page
    assert f'<input type="hidden" name="token_ids" value="{failed_id}">' in page
    assert "deactivated locally and at the gateway" not in page
    _assert_no_raw_secret(get_ctx, seeded["raw_values"], page)

    # Retry through the POST dispatcher with a now-succeeding gateway.
    retry_ctx = DummyContext(ctx.runtime_root.parent)
    retry_events: list[str] = []
    retry_syncs = _install_sync(monkeypatch, retry_events)
    response = post_routes.dispatch_post_route(
        f"/employees/{EMPLOYEE_ID}/retry-token-deactivation",
        retry_ctx,
        _body({"token_ids": failed_id}),
        "application/x-www-form-urlencoded",
        {},
    )
    assert response is not None
    r_status, _r_ct, r_body, r_headers = response
    assert r_status == 303
    assert r_headers["Location"] == f"/employees/{EMPLOYEE_ID}?tokens_deactivated=1"
    assert "token_deactivation_pending" not in r_headers["Location"]
    assert retry_syncs == [("revoke", {"action": "revoke", "token_id": failed_id})]
    # Local revoke of an already-revoked row is not an error.
    revoke_audits = [e for e in retry_ctx.audit_entries if e[0] == "/tokens/revoke"]
    assert len(revoke_audits) == 1
    assert revoke_audits[0][2].startswith("success:")
    assert "already revoked" in revoke_audits[0][2]
    assert revoke_audits[0][1]["via"] == "employee_inactivation_retry"
    _assert_no_raw_secret(retry_ctx, seeded["raw_values"], r_body, r_headers["Location"])


def test_retry_with_unknown_token_id_reports_it_still_pending(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    syncs = _install_sync(monkeypatch, events)

    status, _ct, _resp, headers = ed.handle_retry_token_deactivation(
        ctx, EMPLOYEE_ID, _body({"token_ids": "fct_does_not_exist"})
    )

    assert status == 303
    query = _redirect_query(headers)
    assert query["tokens_deactivated"] == ["0"]
    assert query["token_deactivation_pending"] == ["fct_does_not_exist"]
    assert syncs == []


def test_retry_with_empty_token_ids_redirects_cleanly(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    syncs = _install_sync(monkeypatch, events)

    status, _ct, _resp, headers = ed.handle_retry_token_deactivation(ctx, EMPLOYEE_ID, b"token_ids=")

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}?tokens_deactivated=0"
    assert syncs == []


# ---------------------------------------------------------------------------
# 9. Identity association from the doc AS LOADED
# ---------------------------------------------------------------------------


def test_person_id_edit_still_finds_tokens_under_original_identity_and_never_other_employee(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    # Confirmation still counts the two original-identity tokens.
    status, _ct, body, _h = ed.handle_save_section(
        ctx, EMPLOYEE_ID, _body(_identity_form(person_id="someone_else"))
    )
    assert status == 200
    html = body.decode("utf-8")
    assert "This employee has 2 active tokens." in html
    assert seeded["other"].record.token_id not in html
    assert 'name="person_id" value="someone_else"' in html

    # And the revoke decision revokes those two only.
    status, _ct, _resp, headers = ed.handle_save_section(
        ctx,
        EMPLOYEE_ID,
        _body(_identity_form(person_id="someone_else", _token_decision="revoke_tokens", _token_ids=",".join(seeded["active_ids"]))),
    )
    assert status == 303
    assert headers["Location"].endswith("tokens_deactivated=2")
    assert puts[-1]["payload"]["person_id"] == "someone_else"
    assert sorted(p["token_id"] for _a, p in syncs) == sorted(seeded["active_ids"])
    assert store.get_token(seeded["other"].record.token_id).revoked is False


def test_display_name_match_alone_never_associates_tokens(monkeypatch, ctx, store) -> None:
    # A token whose person_id is the display name (not an identity key) is not this employee's.
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    _install_sync(monkeypatch, events)
    store.create_token(person_id="Sandy Sandbox", label="name-keyed", expires_at=None)

    status, _ct, _resp, headers = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(_identity_form()))

    assert status == 303 and headers["Location"] == f"/employees/{EMPLOYEE_ID}"
    assert len(puts) == 1


# ---------------------------------------------------------------------------
# 10. Non-inactivation saves never touch tokens; stray decisions are 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_status", "form"),
    [
        ("active", {"_section": "assignment", "_rev": "1-abc", "_entity_id": EMPLOYEE_ID, "job": "SANDBOX", "role": "Cleaner"}),
        ("active", _identity_form(status="active")),
        ("active", _identity_form(status="")),
        ("inactive", _identity_form(status="inactive")),
        ("Inactive", _identity_form(status="inactive")),
        ("inactive", _identity_form(status="active")),
    ],
    ids=["assignment", "identity-unchanged-active", "identity-blank-status", "already-inactive", "already-Inactive-legacy-case", "reactivation"],
)
def test_non_inactivation_saves_never_render_confirmation_or_touch_tokens(monkeypatch, ctx, store, existing_status, form) -> None:
    _install_doc(monkeypatch, _doc(status=existing_status))
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    _seed_tokens(store)  # active tokens exist; they must be ignored

    def _never(*_a, **_k):
        raise AssertionError("token store must not be consulted for a non-inactivation save")

    monkeypatch.setattr(tokens, "active_tokens_for_identity", _never)
    monkeypatch.setattr(tokens, "deactivate_token", _never)

    status, _ct, _resp, headers = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(form))

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}"
    assert len(puts) == 1
    assert syncs == []


@pytest.mark.parametrize(
    "form",
    [
        {"_section": "assignment", "_rev": "1-abc", "_entity_id": EMPLOYEE_ID, "job": "SANDBOX", "_token_decision": "revoke_tokens"},
        _identity_form(status="active", _token_decision="revoke_tokens"),
        _identity_form(status="active", _token_decision="keep_tokens"),
        _identity_form(status="active", _token_decision="cancel"),
    ],
    ids=["assignment", "identity-active-revoke", "identity-active-keep", "identity-active-cancel"],
)
def test_token_decision_on_non_inactivation_is_400(monkeypatch, ctx, store, form) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, content_type, body, _h = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(form))

    assert status == 400
    assert content_type.startswith("text/plain")
    assert b"token decision" in body.lower()
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}


def test_token_decision_on_already_inactive_employee_is_400(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc(status="inactive"))
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, _ct, _resp, _h = ed.handle_save_section(
        ctx, EMPLOYEE_ID, _body(_identity_form(_token_decision="revoke_tokens", _token_ids=",".join(seeded["active_ids"])))
    )

    assert status == 400
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}


def test_garbage_token_decision_is_400_and_touches_nothing(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, _ct, _resp, _h = ed.handle_save_section(
        ctx, EMPLOYEE_ID, _body(_identity_form(_token_decision="nuke_everything", _token_ids=",".join(seeded["active_ids"])))
    )

    assert status == 400
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}


def test_token_ids_with_whitespace_and_duplicates_still_match_inventory(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)
    a, b = sorted(seeded["active_ids"])
    messy = f" {a} , {b},{a}, ,{b} "

    status, _ct, _resp, headers = ed.handle_save_section(
        ctx, EMPLOYEE_ID, _body(_identity_form(_token_decision="revoke_tokens", _token_ids=messy))
    )

    assert status == 303
    assert headers["Location"] == f"/employees/{EMPLOYEE_ID}?tokens_deactivated=2"
    assert len(puts) == 1
    assert len(syncs) == 2  # never once per duplicate
    assert _revoked_ids(store) == seeded["active_ids"] | {seeded["revoked"].record.token_id}


def test_status_with_surrounding_whitespace_still_triggers_confirmation(monkeypatch, ctx, store) -> None:
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    _install_sync(monkeypatch, events)
    _seed_tokens(store)

    status, _ct, body, _h = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(_identity_form(status="  inactive  ")))

    assert status == 200
    assert "This employee has 2 active tokens." in body.decode("utf-8")
    assert puts == []


def test_status_with_wrong_case_is_rejected_before_any_token_work(monkeypatch, ctx, store) -> None:
    # ENTITY_STATUSES is lower-case; "Inactive" is not a valid vocabulary value for an
    # active employee, so the pre-existing validator rejects it and no token lookup runs.
    _install_doc(monkeypatch, _doc())
    events: list[str] = []
    puts = _install_put(monkeypatch, events)
    syncs = _install_sync(monkeypatch, events)
    seeded = _seed_tokens(store)

    status, _ct, body, _h = ed.handle_save_section(ctx, EMPLOYEE_ID, _body(_identity_form(status=" Inactive ")))

    assert status == 400
    assert b"Invalid status" in body
    assert puts == [] and syncs == []
    assert _revoked_ids(store) == {seeded["revoked"].record.token_id}


# ---------------------------------------------------------------------------
# 12. tokens.active_tokens_for_identity
# ---------------------------------------------------------------------------


def test_active_tokens_for_identity_filters_by_revoked_and_expiry_without_authenticating(monkeypatch, store) -> None:
    keys = tokens.employee_identity_keys(_doc())
    assert {DOC_ID, EMPLOYEE_ID, LEGACY_PERSON_ID} <= set(keys)
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    never = store.create_token(person_id=DOC_ID, label="never", expires_at=None)
    future = store.create_token(person_id=EMPLOYEE_ID, label="future", expires_at=now + timedelta(minutes=1))
    legacy = store.create_token(person_id=LEGACY_PERSON_ID, label="legacy", expires_at=None)
    boundary = store.create_token(person_id=DOC_ID, label="boundary", expires_at=now)
    past = store.create_token(person_id=DOC_ID, label="past", expires_at=now - timedelta(seconds=1))
    revoked = store.create_token(person_id=DOC_ID, label="revoked", expires_at=None)
    store.revoke_token(revoked.record.token_id)
    other = store.create_token(person_id=OTHER_PERSON_ID, label="other", expires_at=None)

    auth_calls: list[object] = []
    monkeypatch.setattr(store, "authenticate", lambda *a, **k: auth_calls.append((a, k)))

    active = tokens.active_tokens_for_identity(store, keys, now=now)

    assert {r.token_id for r in active} == {never.record.token_id, future.record.token_id, legacy.record.token_id}
    assert boundary.record.token_id not in {r.token_id for r in active}  # expires_at <= now is expired
    assert past.record.token_id not in {r.token_id for r in active}
    assert other.record.token_id not in {r.token_id for r in active}
    assert auth_calls == []
    assert all(r.last_used_at is None for r in store.list_tokens())


def test_active_tokens_for_identity_treats_empty_expires_as_never(monkeypatch, store) -> None:
    created = store.create_token(person_id=DOC_ID, label="blank", expires_at=None)
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET expires_at = '' WHERE token_id = ?", (created.record.token_id,))
    active = tokens.active_tokens_for_identity(store, frozenset({DOC_ID}), now=NOW)
    assert [r.token_id for r in active] == [created.record.token_id]


# ---------------------------------------------------------------------------
# 13. tokens.deactivate_token
# ---------------------------------------------------------------------------


def _audits_for(ctx: DummyContext, route: str) -> list[tuple[str, dict[str, object], str]]:
    return [e for e in ctx.audit_entries if e[0] == route]


def test_deactivate_token_revoked_path_audits_with_via(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    syncs = _install_sync(monkeypatch, events)
    created = store.create_token(person_id=DOC_ID, label="x", expires_at=None)

    outcome = tokens.deactivate_token(ctx, store, created.record.token_id, via="employee_inactivation")

    assert outcome == {"token_id": created.record.token_id, "local": "revoked", "sync_ok": True, "sync_detail": ""}
    assert store.get_token(created.record.token_id).revoked is True
    assert syncs == [("revoke", {"action": "revoke", "token_id": created.record.token_id})]
    for route in ("/tokens/revoke", "/tokens/sync"):
        entries = _audits_for(ctx, route)
        assert len(entries) == 1, route
        assert entries[0][1]["via"] == "employee_inactivation"
        assert entries[0][1]["token_id"] == created.record.token_id
        assert entries[0][2].startswith("success:")
    _assert_no_raw_secret(ctx, [created.token_value])


def test_deactivate_token_already_revoked_still_syncs(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    syncs = _install_sync(monkeypatch, events)
    created = store.create_token(person_id=DOC_ID, label="x", expires_at=None)
    store.revoke_token(created.record.token_id)

    outcome = tokens.deactivate_token(ctx, store, created.record.token_id, via="employee_inactivation_retry")

    assert outcome["local"] == "already_revoked"
    assert outcome["sync_ok"] is True
    assert len(syncs) == 1
    assert _audits_for(ctx, "/tokens/revoke")[0][1]["via"] == "employee_inactivation_retry"


def test_deactivate_token_missing_does_not_sync(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    syncs = _install_sync(monkeypatch, events)

    outcome = tokens.deactivate_token(ctx, store, "fct_missing", via="employee_inactivation")

    assert outcome["token_id"] == "fct_missing"
    assert outcome["local"] == "missing"
    assert outcome["sync_ok"] is False
    assert isinstance(outcome["sync_detail"], str) and outcome["sync_detail"]
    assert syncs == []
    assert _audits_for(ctx, "/tokens/revoke") and _audits_for(ctx, "/tokens/revoke")[0][1]["via"] == "employee_inactivation"


def test_deactivate_token_sync_exception_becomes_sync_false(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    _install_sync(monkeypatch, events, raise_exc=OSError("ssh exploded"))
    created = store.create_token(person_id=DOC_ID, label="x", expires_at=None)

    outcome = tokens.deactivate_token(ctx, store, created.record.token_id, via="employee_inactivation")

    assert outcome["local"] == "revoked"
    assert outcome["sync_ok"] is False
    assert "ssh exploded" in str(outcome["sync_detail"])
    assert store.get_token(created.record.token_id).revoked is True
    sync_audit = _audits_for(ctx, "/tokens/sync")
    assert len(sync_audit) == 1 and sync_audit[0][2].startswith("failed:")


def test_deactivate_token_sync_false_propagates_detail(monkeypatch, ctx, store) -> None:
    events: list[str] = []
    _install_sync(monkeypatch, events, results=[(False, "boom")])
    created = store.create_token(person_id=DOC_ID, label="x", expires_at=None)

    outcome = tokens.deactivate_token(ctx, store, created.record.token_id, via="employee_inactivation")

    assert outcome["sync_ok"] is False and outcome["sync_detail"] == "boom"
    assert store.get_token(created.record.token_id).revoked is True
