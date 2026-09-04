"""546: /field-photos Submitter filter — grouped by employee status.

Verifies the new grouping layer on top of 539's flat submitter options:
`All submitters` -> optgroup "Active" -> optgroup "Inactive" -> `Unknown
submitter`, with status/identity sourced from
`ops_dashboard.common.employee_status_index()`, which itself is built from
`employee_is_active()` + `employee_identity_keys()` (also now in
`ops_dashboard.common`, re-exported from `ops_dashboard.sections.tokens` /
`ops_dashboard.sections.employees` for their own callers).

`field_photos.py` does not import any `ops_dashboard.sections` module — see
`_group_submitter_options()`'s bare `employee_status_index()` call, resolved
against `ops_dashboard.common` (imported at module load). Grouping tests here
patch `field_photos.employee_status_index` directly (the seam
`_group_submitter_options` actually calls); the roster-fixture helper builds
that patched return value by driving the real `common.employee_status_index()`
logic through a patched `common._selector_couch_find`, so the identity/status/
display-name algorithm under test is the production one, not a test-side
reimplementation.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import ops_dashboard.app as ops_app  # noqa: F401
from ops_dashboard import common as ops_common
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos

UNKNOWN = field_photos.UNKNOWN_SUBMITTER_ID
FC_DB = "btq_field_captures_synthetic_546"


# --------------------------------------------------------------------------- #
# 0. Architecture gate: ops_dashboard.app must import cleanly on its own.
# --------------------------------------------------------------------------- #
def test_ops_dashboard_app_imports_cleanly_in_a_fresh_subprocess() -> None:
    """Regression gate for the import cycle the first verifier pass caught
    (field_photos importing a sibling `ops_dashboard.sections` module could
    make `import ops_dashboard.app` fail with a partially-initialized-module
    ImportError, depending on which module a caller happens to import
    first). An in-process import can't be trusted here: this test file (and
    others) already import `ops_dashboard.app` and `field_photos` earlier in
    the same interpreter, which caches fully-initialized modules in
    `sys.modules` and would hide the cycle. A fresh subprocess has no such
    cache, so it actually exercises the ordering that broke before."""
    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", "import ops_dashboard.app"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


# --------------------------------------------------------------------------- #
# Fixture plumbing (styled after test_field_photos_submitter_filter_539.py).
# --------------------------------------------------------------------------- #
def _capture(person_id: object = None, person_name: str = "") -> dict[str, object]:
    doc: dict[str, object] = {"type": "field_capture"}
    if person_id is not None:
        doc["person_id"] = person_id
    if person_name:
        doc["person_name"] = person_name
    return doc


def _disk_record(submitter_id: object = None, submitter_name: str = "") -> dict[str, object]:
    doc: dict[str, object] = {}
    if submitter_id is not None:
        doc["submitter_id"] = submitter_id
    if submitter_name:
        doc["submitter_name"] = submitter_name
    return doc


def _employee(
    *,
    _id: str | None = None,
    first: str = "",
    last: str = "",
    status: str = "",
    person_id: object = None,
) -> dict[str, object]:
    doc: dict[str, object] = {"type": "employee"}
    if _id is not None:
        doc["_id"] = _id
    if first:
        doc["first"] = first
    if last:
        doc["last"] = last
    if status:
        doc["status"] = status
    if person_id is not None:
        doc["person_id"] = person_id
    return doc


class FakeCaptureCouch:
    """Answers the field_capture Mango query used by the options loader."""

    def __init__(self, captures: list[dict[str, object]]) -> None:
        self.captures = captures
        self.queries: list[dict[str, object]] = []

    def find(self, _config: object, database: str, mango: dict[str, object]) -> dict[str, object]:
        assert database == FC_DB
        self.queries.append(mango)
        assert mango["selector"] == {"type": "field_capture"}
        return {"docs": list(self.captures), "bookmark": None}


@pytest.fixture(autouse=True)
def _surroundings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "_load_site_options", lambda: [])
    monkeypatch.setattr("event_pipeline.couchdb_config.field_captures_database", lambda override=None: FC_DB)
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: [])
    monkeypatch.setattr(
        "field_capture.photo_vision_couchdb.query_photo_vision",
        lambda _config, _mango: {"docs": [], "bookmark": None},
    )


def _ctx(tmp_path: Path) -> SectionContext:
    runtime_root = tmp_path / "runtime"
    ctx = SectionContext(
        runtime_root,
        lambda: SimpleNamespace(vault_root=runtime_root / "vault", vault_dir=runtime_root / "vault"),
    )
    ctx.query = {}
    ctx.route_path = "/field-photos"
    return ctx


def _couch(monkeypatch: pytest.MonkeyPatch, captures: list[dict[str, object]]) -> FakeCaptureCouch:
    fake = FakeCaptureCouch(captures)
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", fake.find)
    return fake


def _disk(monkeypatch: pytest.MonkeyPatch, records: dict[str, dict[str, object]]) -> None:
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: records)


def _roster(monkeypatch: pytest.MonkeyPatch, employees: list[dict[str, object]]) -> None:
    """Patch the seam `_group_submitter_options` actually calls
    (`field_photos.employee_status_index`), computing its return value via
    the REAL `common.employee_status_index()` algorithm against a patched
    CouchDB selector — so these tests exercise production identity/status/
    display-name logic, not a reimplementation of it."""
    monkeypatch.setattr(ops_common, "_selector_couch_find", lambda *_a, **_k: list(employees))
    monkeypatch.setattr(field_photos, "employee_status_index", ops_common.employee_status_index)


def _roster_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> dict[str, tuple[bool, str]]:
        raise RuntimeError("employee roster unavailable in test")

    monkeypatch.setattr(field_photos, "employee_status_index", _boom)


# --------------------------------------------------------------------------- #
# <select> structure parser: order-preserving, group-aware.
# --------------------------------------------------------------------------- #
def _parse_select(select_html: str) -> dict[str, object]:
    """Parse the submitter <select> body into a structure preserving order.

    Returns {"root": [(value, label, selected), ...] (the "All submitters"
    option and, if present, the trailing Unknown option — anything NOT
    inside an <optgroup>), "groups": [(group_label, [(value, label,
    selected), ...]), ...]} in document order.
    """
    option_re = re.compile(r'<option value="([^"]*)"( selected)?>([^<]*)</option>')
    group_re = re.compile(r'<optgroup label="([^"]+)">(.*?)</optgroup>', re.S)

    # Replace optgroup blocks with a placeholder so root-level options
    # (All submitters / Unknown submitter) are easy to pull out separately.
    groups: list[tuple[str, list[tuple[str, str, bool]]]] = []

    def _extract_group(match: re.Match[str]) -> str:
        label = match.group(1)
        body = match.group(2)
        options = [(v, lbl, bool(sel)) for v, sel, lbl in option_re.findall(body)]
        groups.append((label, options))
        return ""

    root_html = group_re.sub(_extract_group, select_html)
    root_options = [(v, lbl, bool(sel)) for v, sel, lbl in option_re.findall(root_html)]
    return {"root": root_options, "groups": groups}


def _select_html_for(tmp_path: Path, **filter_kwargs: object) -> str:
    full_html = field_photos.render_filter_form(runtime_root=tmp_path / "runtime", **filter_kwargs)
    match = re.search(r'<select name="submitter_id"[^>]*>(.*?)</select>', full_html, re.S)
    assert match, "submitter <select> not found in rendered filter form"
    return match.group(1)


# --------------------------------------------------------------------------- #
# 1. Full grouped order via CouchDB corpus + roster.
# --------------------------------------------------------------------------- #
CORPUS_1 = [
    _capture("sandbox_b", "Zed Sandbox"),
    _capture("sandbox_a", "Amy Sandbox"),
    _capture("sandbox_c", "Kim Sandbox"),
    _capture("sandbox_d"),  # no name
    _capture(None),  # unattributed capture -> Unknown submitter option
]

ROSTER_1 = [
    _employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active"),
    _employee(_id="employee_sandbox_c", first="Kim", last="Sandbox", status="active"),
    _employee(_id="employee_sandbox_b", first="Zed", last="Sandbox", status="inactive"),
    _employee(_id="employee_sandbox_d", first="Dee", last="Sandbox", status="inactive"),
]


def test_grouped_select_order_active_inactive_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, CORPUS_1)
    _roster(monkeypatch, ROSTER_1)
    parsed = _parse_select(_select_html_for(tmp_path))

    # Root: "All submitters" first, "Unknown submitter" last; values unchanged.
    assert parsed["root"][0] == ("", "All submitters", False)
    assert parsed["root"][-1] == (UNKNOWN, "Unknown submitter", False)

    group_labels = [label for label, _opts in parsed["groups"]]
    assert group_labels == ["Active", "Inactive"]

    active_group = dict(parsed["groups"])["Active"]
    assert [(v, lbl) for v, lbl, _sel in active_group] == [
        ("sandbox_a", "Amy Sandbox (sandbox_a)"),
        ("sandbox_c", "Kim Sandbox (sandbox_c)"),
    ]

    inactive_group = dict(parsed["groups"])["Inactive"]
    inactive_values = [(v, lbl) for v, lbl, _sel in inactive_group]
    # sandbox_b's label is corpus-derived and unambiguous.
    assert inactive_values[1] == ("sandbox_b", "Zed Sandbox (sandbox_b)")
    # sandbox_d has no corpus name, so the label falls back to the index's
    # display name. common.employee_status_index() builds that name as
    # "First Last" directly (f"{first} {last}"), not through
    # employees._display_name()'s "Last, First" table format, so this comes
    # out "Dee Sandbox (sandbox_d)" — consistent with the corpus-derived
    # label style used everywhere else in this select.
    assert inactive_values[0] == ("sandbox_d", "Dee Sandbox (sandbox_d)")


# --------------------------------------------------------------------------- #
# 2. Identity matching: no-employee-doc, dash-form corpus id, person_id-only doc.
# --------------------------------------------------------------------------- #
def test_submitter_with_no_employee_doc_lands_in_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    options = [("sandbox_e", "Some Person (sandbox_e)")]
    monkeypatch.setattr(field_photos, "employee_status_index", lambda: {})
    grouped, active_ids = field_photos._group_submitter_options(options, frozenset())
    assert grouped == [("sandbox_e", "Some Person (sandbox_e)")]
    assert active_ids == frozenset()


def test_dash_form_corpus_id_matches_underscore_employee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, [_capture("sandbox-a", "Amy Sandbox")])  # dash form from the capture doc
    _roster(monkeypatch, [_employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active")])
    options, active_ids = field_photos._load_submitter_option_state(object(), tmp_path / "runtime")
    assert active_ids == frozenset({"sandbox_a"})
    assert ("sandbox_a", "Amy Sandbox (sandbox_a)") in options


def test_person_id_only_employee_doc_matches_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # Minimal roster doc: no `_id`, only `person_id` — exercises the
    # person_id fallback branch of employee_identity_keys().
    _roster(monkeypatch, [_employee(person_id="sandbox_a", status="active")])
    options = [("sandbox_a", "Amy Sandbox (sandbox_a)")]
    grouped, active_ids = field_photos._group_submitter_options(options, frozenset())
    assert active_ids == frozenset({"sandbox_a"})
    assert grouped == [("sandbox_a", "Amy Sandbox (sandbox_a)")]


# --------------------------------------------------------------------------- #
# 2b. common.employee_status_index() itself, against a patched selector.
# --------------------------------------------------------------------------- #
def test_employee_status_index_builds_keys_and_display_names_from_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {"_id": "employee_sandbox_a", "status": "active", "first": "Amy", "last": "Sandbox"},
        {"_id": "employee_sandbox_b", "status": "active", "preferred_name": "Bex", "last": "Sandbox"},
        {"_id": "employee_sandbox_c", "status": "active", "name": "Cam Sandbox"},
        {"_id": "employee_sandbox_d", "status": "inactive", "first": "Dee", "last": "Sandbox"},
        {"person_id": "sandbox_e", "status": "active", "first": "Eve", "last": "Sandbox"},
    ]
    monkeypatch.setattr(ops_common, "_selector_couch_find", lambda *_a, **_k: docs)

    index = ops_common.employee_status_index()

    # (a) raw `_id` is a key in its own right.
    assert index["employee_sandbox_a"] == (True, "Amy Sandbox")
    # (b) the de-prefixed underscore slug ("employee_" stripped) is a key...
    assert index["sandbox_a"] == (True, "Amy Sandbox")
    # (c) ...and the dash form of that slug normalizes onto the SAME key
    # (employee_identity_keys emits both "sandbox_a" and "sandbox-a"; the
    # index normalizes "-" -> "_" before storing, so no separate "sandbox-a"
    # key survives).
    assert "sandbox-a" not in index

    # preferred_name wins over first; name-only doc; inactive; person_id-only.
    assert index["sandbox_b"] == (True, "Bex Sandbox")
    assert index["sandbox_c"] == (True, "Cam Sandbox")
    assert index["sandbox_d"] == (False, "Dee Sandbox")
    # (d) `person_id`-only doc (no `_id` at all) keys by its person_id verbatim.
    assert index["sandbox_e"] == (True, "Eve Sandbox")


def test_employee_status_index_propagates_selector_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> list[dict[str, object]]:
        raise RuntimeError("couch unreachable")

    monkeypatch.setattr(ops_common, "_selector_couch_find", _boom)
    with pytest.raises(RuntimeError, match="couch unreachable"):
        ops_common.employee_status_index()


# --------------------------------------------------------------------------- #
# 3. employee_status_index() raising -> flat, no optgroup, page still renders.
# --------------------------------------------------------------------------- #
def test_roster_failure_degrades_to_flat_alphabetical_no_optgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disk(monkeypatch, {
        "cap-b": {"submitter_id": "sandbox_b", "submitter_name": "Zed Sandbox"},
        "cap-a": {"submitter_id": "sandbox_a", "submitter_name": "Amy Sandbox"},
        "cap-u": {"submitter_id": "", "submitter_name": ""},
    })
    _roster_raises(monkeypatch)

    select_html = _select_html_for(tmp_path)
    assert "<optgroup" not in select_html
    assert select_html.index('value="sandbox_a"') < select_html.index('value="sandbox_b"') < select_html.index(f'value="{UNKNOWN}"')
    assert '<option value="sandbox_a">Amy Sandbox (sandbox_a)</option>' in select_html
    assert '<option value="sandbox_b">Zed Sandbox (sandbox_b)</option>' in select_html

    # page still renders end-to-end on the disk path
    html = field_photos.render(_ctx(tmp_path))
    assert "field-photos" in html.lower() or "Submitter" in html
    assert "<optgroup" not in html


# --------------------------------------------------------------------------- #
# 4. Selecting a value marks it selected inside its group; filter semantics unaffected.
# --------------------------------------------------------------------------- #
def test_selected_value_marked_inside_its_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, CORPUS_1)
    _roster(monkeypatch, ROSTER_1)
    parsed = _parse_select(_select_html_for(tmp_path, submitter_id="sandbox_a"))
    active_group = dict(parsed["groups"])["Active"]
    selected = {(v, sel) for v, _lbl, sel in active_group}
    assert ("sandbox_a", True) in selected
    assert ("sandbox_c", False) in selected


class _FilterFakeCouch:
    """Minimal fake honoring only the clauses load_filtered_photo_sidecars needs."""

    def __init__(self) -> None:
        self.sidecar_queries: list[dict[str, object]] = []

    def find_captures(self, _config: object, database: str, mango: dict[str, object]) -> dict[str, object]:
        assert database == FC_DB
        assert mango["selector"]["type"] == "field_capture"
        assert set(mango["selector"]["person_id"]["$in"]) == {"sandbox_a", "sandbox-a"}
        return {"docs": [{"capture_id": "cap-a1"}], "bookmark": None}

    def find_sidecars(self, _config: object, mango: dict[str, object]) -> dict[str, object]:
        self.sidecar_queries.append(mango)
        return {"docs": [], "bookmark": None}


def test_filter_clause_unaffected_by_grouping_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FilterFakeCouch()
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", fake.find_captures)
    monkeypatch.setattr("field_capture.photo_vision_couchdb.query_photo_vision", fake.find_sidecars)

    ctx = _ctx(tmp_path)
    field_photos.load_filtered_photo_sidecars(ctx, submitter_id="sandbox_a")
    assert fake.sidecar_queries, "gallery query must still run"
    mango = fake.sidecar_queries[-1]
    clauses = mango["selector"]["$and"]
    assert {"capture_id": {"$in": ["cap-a1"]}} in clauses


# --------------------------------------------------------------------------- #
# 5. Disk fallback path groups identically to the CouchDB path.
# --------------------------------------------------------------------------- #
def test_disk_fallback_path_groups_same_as_couch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _disk(monkeypatch, {
        "cap-b": {"submitter_id": "sandbox_b", "submitter_name": "Zed Sandbox"},
        "cap-a": {"submitter_id": "sandbox_a", "submitter_name": "Amy Sandbox"},
        "cap-c": {"submitter_id": "sandbox_c", "submitter_name": "Kim Sandbox"},
        "cap-u": {"submitter_id": "", "submitter_name": ""},
    })
    _roster(monkeypatch, [
        _employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active"),
        _employee(_id="employee_sandbox_c", first="Kim", last="Sandbox", status="active"),
        _employee(_id="employee_sandbox_b", first="Zed", last="Sandbox", status="inactive"),
    ])
    parsed = _parse_select(_select_html_for(tmp_path))
    group_labels = [label for label, _opts in parsed["groups"]]
    assert group_labels == ["Active", "Inactive"]
    active_group = dict(parsed["groups"])["Active"]
    assert [(v, lbl) for v, lbl, _sel in active_group] == [
        ("sandbox_a", "Amy Sandbox (sandbox_a)"),
        ("sandbox_c", "Kim Sandbox (sandbox_c)"),
    ]
    inactive_group = dict(parsed["groups"])["Inactive"]
    assert [(v, lbl) for v, lbl, _sel in inactive_group] == [("sandbox_b", "Zed Sandbox (sandbox_b)")]
    assert parsed["root"][-1] == (UNKNOWN, "Unknown submitter", False)


# --------------------------------------------------------------------------- #
# 6. Empty groups are omitted entirely.
# --------------------------------------------------------------------------- #
def test_all_active_roster_omits_inactive_optgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, [_capture("sandbox_a", "Amy Sandbox"), _capture("sandbox_c", "Kim Sandbox")])
    _roster(monkeypatch, [
        _employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active"),
        _employee(_id="employee_sandbox_c", first="Kim", last="Sandbox", status="active"),
    ])
    select_html = _select_html_for(tmp_path)
    assert 'label="Inactive"' not in select_html
    assert 'label="Active"' in select_html


def test_no_unattributed_capture_omits_unknown_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, [_capture("sandbox_a", "Amy Sandbox")])
    _roster(monkeypatch, [_employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active")])
    select_html = _select_html_for(tmp_path)
    assert UNKNOWN not in select_html
    assert "Unknown submitter" not in select_html


def test_all_inactive_roster_omits_active_optgroup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, [_capture("sandbox_b", "Zed Sandbox")])
    _roster(monkeypatch, [_employee(_id="employee_sandbox_b", first="Zed", last="Sandbox", status="inactive")])
    select_html = _select_html_for(tmp_path)
    assert 'label="Active"' not in select_html
    assert 'label="Inactive"' in select_html


# --------------------------------------------------------------------------- #
# 7. Casefold sorting within a group + stable tie-breaks.
# --------------------------------------------------------------------------- #
def test_casefold_sort_within_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _couch(monkeypatch, [_capture("sandbox_z", "Zed Sandbox"), _capture("sandbox_a", "amy sandbox")])
    _roster(monkeypatch, [
        _employee(_id="employee_sandbox_z", first="Zed", last="Sandbox", status="active"),
        _employee(_id="employee_sandbox_a", first="Amy", last="Sandbox", status="active"),
    ])
    parsed = _parse_select(_select_html_for(tmp_path))
    active_group = dict(parsed["groups"])["Active"]
    assert [v for v, _lbl, _sel in active_group] == ["sandbox_a", "sandbox_z"]


def test_casefold_tie_break_is_stable_by_label_then_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two options with the identical label text differing only in case must
    # tie-break deterministically (label, then person_id) rather than by
    # insertion order.
    _roster(monkeypatch, [
        _employee(_id="employee_sandbox_b", first="Sam", last="Sandbox", status="active"),
        _employee(_id="employee_sandbox_a", first="Sam", last="Sandbox", status="active"),
    ])
    options = [
        ("sandbox_b", "Sam Sandbox (sandbox_b)"),
        ("sandbox_a", "Sam Sandbox (sandbox_a)"),
    ]
    grouped, _active_ids = field_photos._group_submitter_options(options, frozenset())
    assert grouped == [
        ("sandbox_a", "Sam Sandbox (sandbox_a)"),
        ("sandbox_b", "Sam Sandbox (sandbox_b)"),
    ]
