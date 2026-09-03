"""Verifier-authored gate for the active-employees-only token picker (536).

Drives the real code paths: ``route_response_with_headers`` for POST
``/tokens/new`` and ``tokens.render_new_form`` / the GET route for the picker.
Sandbox identities only.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from field_capture.auth import TokenStore
from ops_dashboard.app import route_response_with_headers
from ops_dashboard.sections import employees, tokens


ACTIVE_DOC = {
    "_id": "employee_sandbox_user",
    "type": "employee",
    "status": "active",
    "person_id": "per_sandbox",
    "first": "Sandy",
    "last": "Sandbox",
    "job": "SANDBOX",
}
INACTIVE_DOC = {
    "_id": "employee_sandbox_former",
    "type": "employee",
    "status": "inactive",
    "person_id": "per_former",
    "first": "Former",
    "last": "Sandboxer",
    "job": "SANDBOX",
}
ACTIVE_KEYS = frozenset({"employee_sandbox_user", "sandbox_user", "sandbox-user", "per_sandbox"})

OPTION_RE = re.compile(r'<option value="([^"]*)"([^>]*)>([^<]*)</option>')
SELECT_RE = re.compile(r'<select name="person_id"[^>]*>(.*?)</select>', re.S)
NO_ACTIVE_TEXT = "No active employees"
UNAVAILABLE_TEXT = "roster unavailable"


def _doc(**overrides: object) -> dict[str, object]:
    doc = dict(ACTIVE_DOC)
    doc.update(overrides)
    return doc


def _roster(monkeypatch: pytest.MonkeyPatch, docs: list) -> None:
    monkeypatch.setattr(tokens, "load_employees", lambda: list(docs))


def _roster_boom(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list:
        raise RuntimeError("sandbox couchdb unreachable")

    monkeypatch.setattr(tokens, "load_employees", boom)


def _options(page: str) -> list[tuple[str, str, str]]:
    selects = SELECT_RE.findall(page)
    assert len(selects) == 1, f"expected exactly one person_id select, found {len(selects)}"
    return OPTION_RE.findall(selects[0])


def _value_options(page: str) -> list[tuple[str, str, str]]:
    return [opt for opt in _options(page) if opt[0]]


def _placeholder(page: str) -> tuple[str, str, str]:
    empties = [opt for opt in _options(page) if not opt[0]]
    assert len(empties) == 1
    return empties[0]


def _store(runtime_root: Path) -> TokenStore:
    return TokenStore(runtime_root / "field_capture_tokens.sqlite3")


def _audit_entries(runtime_root: Path, route: str) -> list[dict]:
    path = runtime_root / "logs" / "admin_audit.log"
    if not path.exists():
        return []
    return [
        entry
        for entry in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        if entry.get("route") == route
    ]


def _post_new(runtime_root: Path, body: str):
    return route_response_with_headers("POST", "/tokens/new", runtime_root, body.encode("utf-8"))


@pytest.fixture(autouse=True)
def _clear_flash() -> None:
    tokens.RAW_TOKEN_FLASH.clear()


@pytest.fixture
def sync_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Recorder for sync_token_to_vps with the env kill-switch removed so the
    recorder is the only thing standing between the handler and ssh."""
    monkeypatch.delenv("BTQ_TOKEN_SYNC_DISABLED", raising=False)
    calls: list[tuple[str, dict]] = []

    def fake_sync(action: str, payload: dict) -> tuple[bool, str]:
        calls.append((action, payload))
        return True, ""

    monkeypatch.setattr(tokens, "sync_token_to_vps", fake_sync)
    return calls


# --- 1. picker lists active employees only -------------------------------------


def test_picker_lists_only_the_active_employee(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])

    page = tokens.render_new_form({})

    valued = _value_options(page)
    assert [opt[0] for opt in valued] == ["per_sandbox"]
    assert "Sandbox, Sandy" in valued[0][2]
    select_html = SELECT_RE.findall(page)[0]
    for needle in ("per_former", "employee_sandbox_former", "sandbox_former", "Former", "Sandboxer"):
        assert needle not in select_html


def test_picker_via_get_route_lists_only_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])

    status, _ct, body, _headers = route_response_with_headers("GET", "/tokens/new", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    page = body.decode("utf-8")
    assert [opt[0] for opt in _value_options(page)] == ["per_sandbox"]
    assert "per_former" not in page


def test_picker_uses_deprefixed_id_when_doc_has_no_person_id(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    del doc["person_id"]
    _roster(monkeypatch, [doc])

    page = tokens.render_new_form({})

    assert [opt[0] for opt in _value_options(page)] == ["sandbox_user"]


# --- 2. status normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("status", "eligible"),
    [
        ("active", True),
        (" Active ", True),
        ("ACTIVE", True),
        ("\tactive\n", True),
        ("", False),
        (None, False),
        ("inactive", False),
        ("on_leave", False),
        ("activated", False),
        ("in active", False),
        (True, False),
        (1, False),
    ],
)
def test_employee_is_active_normalizes_status(status: object, eligible: bool) -> None:
    doc = _doc()
    if status is None:
        del doc["status"]
    else:
        doc["status"] = status
    assert employees.employee_is_active(doc) is eligible


def test_employee_is_active_missing_key_is_false() -> None:
    assert employees.employee_is_active({}) is False


@pytest.mark.parametrize("status", [" Active ", "ACTIVE"])
def test_picker_accepts_normalized_active_statuses(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _roster(monkeypatch, [_doc(status=status)])

    page = tokens.render_new_form({})

    assert [opt[0] for opt in _value_options(page)] == ["per_sandbox"]


@pytest.mark.parametrize("status", ["", "inactive", "on_leave"])
def test_picker_rejects_non_active_statuses(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _roster(monkeypatch, [_doc(status=status)])

    page = tokens.render_new_form({})

    assert NO_ACTIVE_TEXT in page
    assert 'name="person_id"' not in page


# --- 3. empty states, no person_id control -------------------------------------


def test_all_inactive_roster_renders_no_active_state_without_control(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, _doc(_id="employee_sandbox_two", person_id="per_two", status="on_leave")])

    page = tokens.render_new_form({})

    assert NO_ACTIVE_TEXT in page
    assert UNAVAILABLE_TEXT not in page
    assert 'name="person_id"' not in page
    assert "<select" not in page
    assert '<input name="person_id"' not in page


def test_all_missing_status_roster_renders_no_active_state(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    del doc["status"]
    _roster(monkeypatch, [doc])

    page = tokens.render_new_form({})

    assert NO_ACTIVE_TEXT in page
    assert 'name="person_id"' not in page


def test_empty_roster_renders_no_active_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [])

    page = tokens.render_new_form({})

    assert NO_ACTIVE_TEXT in page
    assert 'name="person_id"' not in page


def test_roster_loader_raising_renders_unavailable_state_without_control(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster_boom(monkeypatch)

    page = tokens.render_new_form({})

    assert UNAVAILABLE_TEXT in page
    assert NO_ACTIVE_TEXT not in page
    assert 'name="person_id"' not in page
    assert "<select" not in page
    assert '<input name="person_id"' not in page
    assert 'action="/tokens/new"' in page  # the rest of the form still renders


def test_roster_unavailable_via_get_route_is_200_not_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _roster_boom(monkeypatch)

    status, _ct, body, _headers = route_response_with_headers("GET", "/tokens/new", tmp_path / "runtime")

    assert status == HTTPStatus.OK
    assert UNAVAILABLE_TEXT in body.decode("utf-8")


def test_two_empty_states_are_textually_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [INACTIVE_DOC])
    no_active = tokens.render_person_id_field()
    _roster_boom(monkeypatch)
    unavailable = tokens.render_person_id_field()

    assert no_active != unavailable
    assert NO_ACTIVE_TEXT in no_active and NO_ACTIVE_TEXT not in unavailable
    assert UNAVAILABLE_TEXT in unavailable and UNAVAILABLE_TEXT not in no_active


# --- 4. preselection only when eligible ----------------------------------------


@pytest.mark.parametrize("selected", ["per_sandbox", "sandbox_user", "sandbox-user", "employee_sandbox_user"])
def test_eligible_selected_value_preselects_that_option(monkeypatch: pytest.MonkeyPatch, selected: str) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])

    page = tokens.render_new_form({"person_id": [selected]})

    valued = _value_options(page)
    assert len(valued) == 1
    assert "selected" in valued[0][1]
    assert "selected" not in _placeholder(page)[1]


@pytest.mark.parametrize("selected", ["per_former", "sandbox_former", "employee_sandbox_former", "nobody_here", "PER_SANDBOX"])
def test_ineligible_or_unknown_selected_value_selects_placeholder(monkeypatch: pytest.MonkeyPatch, selected: str) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])

    page = tokens.render_new_form({"person_id": [selected]})

    assert "selected" in _placeholder(page)[1]
    assert all("selected" not in opt[1] for opt in _value_options(page))
    assert [opt[0] for opt in _value_options(page)] == ["per_sandbox"]


def test_no_selected_value_selects_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [ACTIVE_DOC])

    page = tokens.render_new_form({})

    assert "selected" in _placeholder(page)[1]
    assert all("selected" not in opt[1] for opt in _value_options(page))


# --- 5./6. POST fails closed: inactive, unknown, roster unavailable -------------


def _assert_failed_closed(runtime_root: Path, response, sync_calls: list, error: str, person_id: str) -> None:
    status, _ct, _body, headers = response
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == f"/tokens/new?error={error}"
    assert _store(runtime_root).list_tokens() == []
    assert sync_calls == []
    failed = [e for e in _audit_entries(runtime_root, "/tokens/new") if e["result_summary"].startswith("failed:")]
    assert failed, "failure path must still audit"
    assert failed[-1]["result_summary"] == f"failed: {error}"
    assert failed[-1]["payload"]["person_id"] == person_id
    assert _audit_entries(runtime_root, "/tokens/sync") == []


@pytest.mark.parametrize("person_id", ["per_former", "sandbox_former", "sandbox-former", "employee_sandbox_former"])
def test_post_inactive_employee_any_identity_form_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list, person_id: str
) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [ACTIVE_DOC, INACTIVE_DOC])

    response = _post_new(runtime_root, f"person_id={person_id}&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "employee_not_active", person_id)


@pytest.mark.parametrize("status", ["", "on_leave", "Inactive "])
def test_post_non_active_status_variants_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list, status: str
) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [_doc(status=status)])

    response = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "employee_not_active", "per_sandbox")


def test_post_missing_status_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    doc = _doc()
    del doc["status"]
    _roster(monkeypatch, [doc])

    response = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "employee_not_active", "per_sandbox")


@pytest.mark.parametrize("person_id", ["nobody_here", "PER_SANDBOX", "employee_", "per_sandboxx"])
def test_post_unknown_person_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list, person_id: str
) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [ACTIVE_DOC, INACTIVE_DOC])

    response = _post_new(runtime_root, f"person_id={person_id}&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "unknown_employee", person_id)


def test_post_empty_roster_is_unknown_employee(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [])

    response = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "unknown_employee", "per_sandbox")


def test_post_roster_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    _roster_boom(monkeypatch)

    response = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    _assert_failed_closed(runtime_root, response, sync_calls, "roster_unavailable", "per_sandbox")


def test_post_roster_unavailable_beats_unknown_and_inactive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    # With no roster we cannot know whether the id is unknown or inactive; the
    # error must say so rather than guess.
    runtime_root = tmp_path / "runtime"
    _roster_boom(monkeypatch)

    _status, _ct, _body, headers = _post_new(runtime_root, "person_id=nobody_here&label=Phone&token_type=capture&site_ids=SANDBOX")

    assert headers["Location"] == "/tokens/new?error=roster_unavailable"


def test_post_validation_errors_still_precede_roster_lookup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    calls = {"n": 0}

    def counting() -> list:
        calls["n"] += 1
        return [ACTIVE_DOC]

    monkeypatch.setattr(tokens, "load_employees", counting)

    _s1, _c1, _b1, h1 = _post_new(runtime_root, "person_id=&label=Phone&token_type=capture&site_ids=SANDBOX")
    _s2, _c2, _b2, h2 = _post_new(runtime_root, "person_id=per_sandbox&label=&token_type=capture&site_ids=SANDBOX")

    assert h1["Location"] == "/tokens/new?error=person_id_required"
    assert h2["Location"] == "/tokens/new?error=label_required"
    assert calls["n"] == 0
    assert _store(runtime_root).list_tokens() == []
    assert sync_calls == []


# --- 7. POST succeeds for an active employee via every identity form -----------


@pytest.mark.parametrize("person_id", sorted(ACTIVE_KEYS))
def test_post_active_employee_any_identity_form_issues_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list, person_id: str
) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])

    status, _ct, _body, headers = _post_new(runtime_root, f"person_id={person_id}&label=Phone&token_type=capture&site_ids=SANDBOX")

    assert status == HTTPStatus.SEE_OTHER
    location = headers["Location"]
    assert location.startswith("/tokens?issued=1")
    query = parse_qs(urlsplit(location).query)
    assert query["message"] == ["created"]
    rows = _store(runtime_root).list_tokens()
    assert len(rows) == 1
    assert rows[0].person_id == person_id
    assert rows[0].label == "Phone"
    assert query["token_id"] == [rows[0].token_id]
    assert len(sync_calls) == 1
    action, payload = sync_calls[0]
    assert action == "upsert"
    assert payload["action"] == "upsert"
    assert payload["row"]["person_id"] == person_id
    assert payload["row"]["token_id"] == rows[0].token_id
    success = [e for e in _audit_entries(runtime_root, "/tokens/new") if e["result_summary"].startswith("success:")]
    assert len(success) == 1


@pytest.mark.parametrize("status", [" Active ", "ACTIVE"])
def test_post_accepts_normalized_active_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list, status: str
) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [_doc(status=status)])

    status_code, _ct, _body, headers = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    assert status_code == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/tokens?issued=1")
    assert len(_store(runtime_root).list_tokens()) == 1
    assert len(sync_calls) == 1


def test_post_strips_whitespace_around_person_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [ACTIVE_DOC])

    _status, _ct, _body, headers = _post_new(runtime_root, "person_id=%20%20per_sandbox%20%20&label=Phone&token_type=capture&site_ids=SANDBOX")

    assert headers["Location"].startswith("/tokens?issued=1")
    rows = _store(runtime_root).list_tokens()
    assert [row.person_id for row in rows] == ["per_sandbox"]
    assert len(sync_calls) == 1


def test_post_ignores_non_dict_roster_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sync_calls: list) -> None:
    runtime_root = tmp_path / "runtime"
    _roster(monkeypatch, [None, "employee_sandbox_user", 42, ["per_sandbox"], ACTIVE_DOC])

    _status, _ct, _body, headers = _post_new(runtime_root, "person_id=per_sandbox&label=Phone&token_type=capture&site_ids=SANDBOX")

    assert headers["Location"].startswith("/tokens?issued=1")
    assert len(_store(runtime_root).list_tokens()) == 1


@pytest.mark.xfail(
    strict=True,
    raises=AttributeError,
    reason=(
        "FINDING (low): render_person_id_field filters with employee_is_active(doc) "
        "without the isinstance(doc, dict) guard handle_new_post uses; a non-dict "
        "roster entry raises AttributeError. Unreachable via load_employees(), which "
        "only yields dicts. Remove this marker when the picker guards like the handler."
    ),
)
def test_picker_tolerates_non_dict_roster_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_employees() only ever yields dicts, but the POST path guards
    # isinstance(doc, dict) and the picker should not be the weaker of the two.
    _roster(monkeypatch, [None, "employee_sandbox_user", ACTIVE_DOC])

    page = tokens.render_new_form({})

    assert [opt[0] for opt in _value_options(page)] == ["per_sandbox"]


# --- 8. identity helper and name map -------------------------------------------


def test_employee_identity_keys_exact_set_for_employee_doc() -> None:
    assert tokens.employee_identity_keys(ACTIVE_DOC) == ACTIVE_KEYS


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ({}, frozenset()),
        ({"_id": "", "person_id": ""}, frozenset()),
        ({"person_id": "per_only"}, frozenset({"per_only"})),
        ({"_id": "employee_"}, frozenset({"employee_"})),
        ({"_id": "employee_sandbox_user"}, frozenset({"employee_sandbox_user", "sandbox_user", "sandbox-user"})),
        ({"_id": "person_sandbox_user"}, frozenset({"person_sandbox_user", "sandbox_user", "sandbox-user"})),
        ({"_id": "operator_sandbox_user"}, frozenset({"operator_sandbox_user", "sandbox_user", "sandbox-user"})),
        ({"_id": "sandbox_user"}, frozenset({"sandbox_user"})),
        ({"_id": " employee_sandbox_user ", "person_id": " per_sandbox "}, ACTIVE_KEYS),
        ({"_id": "employee_sandbox", "person_id": "per_sandbox"}, frozenset({"employee_sandbox", "sandbox", "per_sandbox"})),
    ],
)
def test_employee_identity_keys_edge_cases(doc: dict, expected: frozenset) -> None:
    assert tokens.employee_identity_keys(doc) == expected


def test_person_name_map_resolves_every_identity_key() -> None:
    names = tokens._build_person_name_map([ACTIVE_DOC, INACTIVE_DOC])

    for key in ACTIVE_KEYS:
        assert names[key] == "Sandy Sandbox"
    for key in tokens.employee_identity_keys(INACTIVE_DOC):
        assert names[key] == "Former Sandboxer"
    assert "sandbox" not in names


def test_person_name_map_keys_match_identity_helper_exactly() -> None:
    names = tokens._build_person_name_map([ACTIVE_DOC])
    assert frozenset(names) == tokens.employee_identity_keys(ACTIVE_DOC)


# --- 9. employees directory status filters use the shared helper ---------------


def _ctx(query: dict[str, list[str]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(query=query or {})


def _dir_doc(slug: str, last: str, **overrides: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "_id": f"employee_{slug}",
        "type": "employee",
        "first": "Sandy",
        "last": last,
        "status": "active",
        "job": "SANDBOX",
        "phone": "",
        "email": "",
    }
    doc.update(overrides)
    return doc


DIRECTORY = [
    _dir_doc("sb_a", "Alpha", status="active"),
    _dir_doc("sb_b", "Bravo", status=" Active "),
    _dir_doc("sb_c", "Charlie", status="ACTIVE"),
    _dir_doc("sb_d", "Delta", status="inactive"),
    _dir_doc("sb_e", "Echo", status="on_leave"),
    _dir_doc("sb_f", "Foxtrot", status=""),
]
DIRECTORY.append(_dir_doc("sb_g", "Golf"))
del DIRECTORY[-1]["status"]
ACTIVE_LAST = {"Alpha", "Bravo", "Charlie"}
INACTIVE_LAST = {"Delta", "Echo", "Foxtrot", "Golf"}


def _names_in(body: str) -> set[str]:
    return {last for last in ACTIVE_LAST | INACTIVE_LAST if f"{last}, Sandy" in body}


def test_employees_directory_active_filter_uses_normalized_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employees, "load_employees", lambda: list(DIRECTORY))

    assert _names_in(employees.render(_ctx({"status": ["active"]}))) == ACTIVE_LAST


def test_employees_directory_inactive_filter_is_complement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employees, "load_employees", lambda: list(DIRECTORY))

    assert _names_in(employees.render(_ctx({"status": ["inactive"]}))) == INACTIVE_LAST


def test_employees_directory_all_filter_lists_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(employees, "load_employees", lambda: list(DIRECTORY))

    assert _names_in(employees.render(_ctx())) == ACTIVE_LAST | INACTIVE_LAST
    assert _names_in(employees.render(_ctx({"status": ["all"]}))) == ACTIVE_LAST | INACTIVE_LAST


def test_employees_directory_filters_route_through_employee_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    # Invariant: the filter is the shared helper, not a re-derived predicate.
    monkeypatch.setattr(employees, "load_employees", lambda: list(DIRECTORY))
    seen: list[str] = []
    real = employees.employee_is_active

    def spy(doc: dict) -> bool:
        seen.append(str(doc.get("last")))
        return real(doc)

    monkeypatch.setattr(employees, "employee_is_active", spy)
    employees.render(_ctx({"status": ["active"]}))
    assert set(seen) >= ACTIVE_LAST | INACTIVE_LAST
    seen.clear()
    employees.render(_ctx({"status": ["inactive"]}))
    assert set(seen) >= ACTIVE_LAST | INACTIVE_LAST


def test_token_picker_routes_through_employee_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    _roster(monkeypatch, [INACTIVE_DOC, ACTIVE_DOC])
    seen: list[str] = []
    real = employees.employee_is_active

    def spy(doc: dict) -> bool:
        seen.append(str(doc.get("_id")))
        return real(doc)

    monkeypatch.setattr(tokens, "employee_is_active", spy)
    tokens.render_new_form({})
    assert set(seen) == {"employee_sandbox_user", "employee_sandbox_former"}
