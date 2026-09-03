"""539: /field-photos Submitter filter — independent verifier gate.

Ownership comes from capture attribution only (field_capture.person_id on
CouchDB, submitters_by_capture on disk) joined to sidecars by capture_id.
Everything here is synthetic: sandbox identities, SANDBOX sites, tmp roots.
The CouchDB transports are replaced with selector-honoring fakes so that a
clause missing from the Mango query is observable as a wrong result set, not
just a wrong dict.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import ops_dashboard.app as ops_app
from field_capture.photo_vision import FieldPhotoAsset
from field_capture.photo_vision_categories import CATEGORY_AGREEMENT_MISMATCH
from ops_dashboard.common import SectionContext
from ops_dashboard.sections import field_photos

PAGE_LIMIT = field_photos.PAGE_LIMIT
UNKNOWN = field_photos.UNKNOWN_SUBMITTER_ID
FC_DB = "btq_field_captures_synthetic"


# --------------------------------------------------------------------------- #
# Minimal Mango evaluator so the fakes honor whatever selector they are sent.
# --------------------------------------------------------------------------- #
def _lookup(doc: object, dotted: str) -> tuple[bool, object]:
    current = doc
    for part in dotted.split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _match_condition(doc: object, field: str, condition: object) -> bool:
    exists, value = _lookup(doc, field)
    if isinstance(condition, dict) and any(str(k).startswith("$") for k in condition):
        for op, operand in condition.items():
            if op == "$in":
                if not exists or value not in operand:
                    return False
            elif op == "$exists":
                if bool(operand) != exists:
                    return False
            elif op == "$regex":
                if not exists or re.search(str(operand), str(value)) is None:
                    return False
            elif op == "$gte":
                if not exists or not (str(value) >= str(operand)):
                    return False
            elif op == "$lte":
                if not exists or not (str(value) <= str(operand)):
                    return False
            elif op == "$ne":
                if exists and value == operand:
                    return False
            else:
                raise AssertionError(f"fake mango: unsupported operator {op}")
        return True
    return exists and value == condition


def _matches(doc: dict[str, object], selector: dict[str, object]) -> bool:
    for key, condition in selector.items():
        if key == "$and":
            if not all(_matches(doc, sub) for sub in condition):
                return False
        elif key == "$or":
            if not any(_matches(doc, sub) for sub in condition):
                return False
        elif not _match_condition(doc, key, condition):
            return False
    return True


def _run_find(docs: list[dict[str, object]], mango: dict[str, object]) -> dict[str, object]:
    """Apply selector, sort, bookmark/limit like a (very) small CouchDB."""
    selected = [d for d in docs if _matches(d, mango.get("selector") or {})]
    sort = mango.get("sort")
    if sort:
        spec = sort[0]
        field, direction = next(iter(spec.items()))
        selected.sort(key=lambda d: str(d.get(field) or ""), reverse=(direction == "desc"))
    limit = int(mango.get("limit") or len(selected) or 1)
    start = int(mango.get("bookmark") or 0)
    page = selected[start : start + limit]
    fields = mango.get("fields")
    if fields:
        page = [{k: d[k] for k in fields if k in d} for d in page]
    next_bookmark = start + limit if start + limit < len(selected) else None
    return {"docs": page, "bookmark": next_bookmark}


# --------------------------------------------------------------------------- #
# Synthetic fixture: captures (attribution) + sidecars (photos).
# --------------------------------------------------------------------------- #
def _capture(capture_id: str, person_id: object = None, person_name: str = "", **extra: object) -> dict[str, object]:
    doc: dict[str, object] = {"_id": f"field_capture:{capture_id}", "type": "field_capture", "capture_id": capture_id}
    if person_id is not None:
        doc["person_id"] = person_id
    if person_name:
        doc["person_name"] = person_name
    doc.update(extra)
    return doc


CAPTURES = [
    _capture("cap-a1", "sandbox_a", "Sandy Sandbox"),
    _capture("cap-a2", "sandbox-a", "Sandy Sandbox"),  # dash form of the SAME person
    _capture("cap-b1", "sandbox_b", "Sandy Sandbox"),  # duplicate display name, different person
    _capture("cap-u1", None, "", field_capture_token_label="Token 1"),  # person_id missing
    _capture("cap-u2", "", ""),  # person_id empty
]


def _disk_submitters() -> dict[str, dict[str, str]]:
    return {
        "cap-a1": {"submitter_name": "Sandy Sandbox", "submitter_id": "sandbox_a", "token_label": ""},
        "cap-a2": {"submitter_name": "Sandy Sandbox", "submitter_id": "sandbox-a", "token_label": ""},
        "cap-b1": {"submitter_name": "Sandy Sandbox", "submitter_id": "sandbox_b", "token_label": ""},
        "cap-u1": {"submitter_name": "Token 1", "submitter_id": "", "token_label": "Token 1"},
        "cap-u2": {"submitter_name": "Unknown", "submitter_id": "", "token_label": ""},
    }


def _sidecar(
    asset: str,
    capture_id: str,
    site_id: str,
    qc_category: str,
    generated_at: str,
    *,
    area_guess: str = "office",
    vision_category: str = "Offices",
    agreement: str = "match",
    deep: bool = False,
    description: str = "synthetic office photo",
) -> dict[str, object]:
    doc: dict[str, object] = {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": capture_id,
        "photo_id": f"photo-{asset}",
        "photo_asset_id": asset,
        "site_id": site_id,
        "qc_category": qc_category,
        "vision_category": vision_category,
        "category_agreement": agreement,
        "area_guess": area_guess,
        "description": description,
        "search_text": f"{description} {qc_category} {vision_category} {area_guess}".lower(),
        "generated_at": generated_at,
        "source_filename": f"{asset}.jpg",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "provenance": {"captured_at": generated_at, "image_media_url": f"/media/{asset}.jpg"},
    }
    if deep:
        doc["deep_analysis"] = [{"prompt_id": "custom", "result": "synthetic"}]
    return doc


SIDECARS = [
    _sidecar("s-a1-sb-qc", "cap-a1", "SANDBOX", "qc_visit", "2026-08-01T10:00:00Z",
             area_guess="restroom", vision_category="Restrooms", deep=True, description="urinal tile floor"),
    _sidecar("s-a1-sb2-mt", "cap-a1", "SANDBOX2", "maintenance", "2026-08-02T10:00:00Z",
             area_guess="kitchen", vision_category="Kitchens", agreement=CATEGORY_AGREEMENT_MISMATCH,
             description="dark kitchen"),
    _sidecar("s-a2-sb-mt", "cap-a2", "SANDBOX", "maintenance", "2026-08-03T10:00:00Z"),
    _sidecar("s-a2-sb2-qc", "cap-a2", "SANDBOX2", "qc_visit", "2026-08-04T10:00:00Z"),
    # B mirrors A's attributes (and straddles A's dates) so every non-submitter
    # filter alone admits B too.
    _sidecar("s-b1-sb-qc", "cap-b1", "SANDBOX", "qc_visit", "2026-08-01T12:00:00Z",
             area_guess="restroom", vision_category="Restrooms", deep=True, description="urinal tile floor"),
    _sidecar("s-b1-sb2-mt", "cap-b1", "SANDBOX2", "maintenance", "2026-08-02T12:00:00Z",
             area_guess="kitchen", vision_category="Kitchens", agreement=CATEGORY_AGREEMENT_MISMATCH,
             description="dark kitchen"),
    _sidecar("s-u1-sb-qc", "cap-u1", "SANDBOX", "qc_visit", "2026-08-07T10:00:00Z",
             area_guess="restroom", vision_category="Restrooms", deep=True, description="urinal tile floor"),
    _sidecar("s-u2-sb2-mt", "cap-u2", "SANDBOX2", "maintenance", "2026-08-08T10:00:00Z"),
    # Sidecar whose capture has no field_capture doc at all.
    _sidecar("s-orphan", "cap-orphan", "SANDBOX", "qc_visit", "2026-08-09T10:00:00Z",
             area_guess="restroom", vision_category="Restrooms", deep=True, description="urinal tile floor"),
]

A_ALL = {"s-a1-sb-qc", "s-a1-sb2-mt", "s-a2-sb-mt", "s-a2-sb2-qc"}
B_ALL = {"s-b1-sb-qc", "s-b1-sb2-mt"}
UNKNOWN_ALL = {"s-u1-sb-qc", "s-u2-sb2-mt"}
SITE_SANDBOX = {"s-a1-sb-qc", "s-a2-sb-mt", "s-b1-sb-qc", "s-u1-sb-qc", "s-orphan"}
QC_VISIT = {"s-a1-sb-qc", "s-a2-sb2-qc", "s-b1-sb-qc", "s-u1-sb-qc", "s-orphan"}


def _ids(rows: list[dict[str, object]]) -> set[str]:
    return {str(r.get("photo_asset_id")) for r in rows}


def _ctx(tmp_path: Path, query: dict[str, list[str]] | None = None, route_path: str = "/field-photos") -> SectionContext:
    runtime_root = tmp_path / "runtime"
    ctx = SectionContext(
        runtime_root,
        lambda: SimpleNamespace(vault_root=runtime_root / "vault", vault_dir=runtime_root / "vault"),
    )
    ctx.query = query or {}
    ctx.route_path = route_path
    return ctx


class FakeCouch:
    """Records every Mango query sent and answers from the fixture docs."""

    def __init__(self, captures: list[dict[str, object]], sidecars: list[dict[str, object]]) -> None:
        self.captures = captures
        self.sidecars = sidecars
        self.capture_queries: list[dict[str, object]] = []
        self.sidecar_queries: list[dict[str, object]] = []

    def find(self, _config: object, database: str, mango: dict[str, object]) -> dict[str, object]:
        assert database == FC_DB
        self.capture_queries.append(mango)
        return _run_find(self.captures, mango)

    def photo_vision(self, _config: object, mango: dict[str, object]) -> dict[str, object]:
        self.sidecar_queries.append(mango)
        return _run_find(self.sidecars, mango)

    def gallery_selectors(self) -> list[dict[str, object]]:
        return [m for m in self.sidecar_queries if m.get("sort")]


@pytest.fixture(autouse=True)
def _synthetic_surroundings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "resolve_site_label", lambda site_id, _vault_root=None: str(site_id))
    monkeypatch.setattr(field_photos, "_load_site_options", lambda: [("SANDBOX", "Sandbox (SANDBOX)"), ("SANDBOX2", "Sandbox Two (SANDBOX2)")])
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("SANDBOX", "Sandbox (SANDBOX)")])
    monkeypatch.setattr("event_pipeline.couchdb_config.field_captures_database", lambda override=None: FC_DB)
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: [])


@pytest.fixture
def couch(monkeypatch: pytest.MonkeyPatch) -> FakeCouch:
    fake = FakeCouch(list(CAPTURES), list(SIDECARS))
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", fake.find)
    monkeypatch.setattr("field_capture.photo_vision_couchdb.query_photo_vision", fake.photo_vision)
    # render() legitimately reads disk attribution for card names and disk
    # sidecars for pending state; the FILTER must not. Spy, don't fail.
    fake.disk_reads: list[str] = []

    def _spy_submitters(_root: Path) -> dict[str, dict[str, str]]:
        fake.disk_reads.append("submitters_by_capture")
        return _disk_submitters()

    def _spy_sidecars(_dir: Path) -> list[dict[str, object]]:
        fake.disk_reads.append("load_photo_vision_sidecars")
        return []

    monkeypatch.setattr(field_photos, "submitters_by_capture", _spy_submitters)
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", _spy_sidecars)
    return fake


@pytest.fixture
def disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", lambda *_a, **_k: pytest.fail("CouchDB find on disk path"))
    monkeypatch.setattr("field_capture.photo_vision_couchdb.query_photo_vision", lambda *_a, **_k: pytest.fail("CouchDB photo-vision on disk path"))
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: _disk_submitters())
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: list(SIDECARS))


def _load(tmp_path: Path, **filters: object) -> tuple[set[str], bool, bool]:
    rows, fallback, has_more = field_photos.load_filtered_photo_sidecars(_ctx(tmp_path), **filters)
    return _ids(rows), fallback, has_more


# --------------------------------------------------------------------------- #
# B. identity normalization
# --------------------------------------------------------------------------- #
def test_normalize_person_id_maps_dash_to_underscore_and_strips() -> None:
    assert field_photos._normalize_person_id(" sandbox-a ") == "sandbox_a"
    assert field_photos._normalize_person_id("sandbox_a") == "sandbox_a"
    assert field_photos._normalize_person_id(None) == ""
    assert field_photos._normalize_person_id("   ") == ""


# --------------------------------------------------------------------------- #
# C. AND semantics — CouchDB path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("submitter", ["sandbox_a", "sandbox-a", " sandbox_a "])
def test_couch_submitter_alone_returns_exactly_a_in_both_id_forms(tmp_path: Path, couch: FakeCouch, submitter: str) -> None:
    ids, fallback, has_more = _load(tmp_path, submitter_id=submitter)
    assert ids == A_ALL
    assert fallback is False and has_more is False
    assert not ids & B_ALL and not ids & UNKNOWN_ALL and "s-orphan" not in ids
    # Attribution came from field_capture docs, not disk / token / name.
    assert couch.disk_reads == []
    assert couch.capture_queries and all(m["selector"]["type"] == "field_capture" for m in couch.capture_queries)


def test_couch_site_alone_and_category_alone_admit_different_sets(tmp_path: Path, couch: FakeCouch) -> None:
    assert _load(tmp_path, site_id="SANDBOX")[0] == SITE_SANDBOX
    assert _load(tmp_path, qc_category="qc_visit")[0] == QC_VISIT
    assert SITE_SANDBOX != A_ALL != QC_VISIT


def test_couch_submitter_and_site_is_exact_intersection_not_union(tmp_path: Path, couch: FakeCouch) -> None:
    ids, _fb, _more = _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX")
    assert ids == A_ALL & SITE_SANDBOX == {"s-a1-sb-qc", "s-a2-sb-mt"}
    assert ids != A_ALL | SITE_SANDBOX  # OR would yield 7 rows


def test_couch_triple_intersection(tmp_path: Path, couch: FakeCouch) -> None:
    ids, _fb, _more = _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX", qc_category="qc_visit")
    assert ids == A_ALL & SITE_SANDBOX & QC_VISIT == {"s-a1-sb-qc"}


def test_couch_b_and_unknown_are_separate_from_a(tmp_path: Path, couch: FakeCouch) -> None:
    assert _load(tmp_path, submitter_id="sandbox_b")[0] == B_ALL
    assert _load(tmp_path, submitter_id=UNKNOWN)[0] == UNKNOWN_ALL
    # A sidecar whose capture has no attribution doc is neither A's nor "unknown".
    for who in ("sandbox_a", "sandbox_b", UNKNOWN):
        assert "s-orphan" not in _load(tmp_path, submitter_id=who)[0]


def test_couch_unknown_selector_targets_missing_or_empty_person_id(tmp_path: Path, couch: FakeCouch) -> None:
    _load(tmp_path, submitter_id=UNKNOWN)
    capture_selectors = [m["selector"] for m in couch.capture_queries]
    assert any("$or" in sel for sel in capture_selectors)
    unknown_sel = next(sel for sel in capture_selectors if "$or" in sel)
    assert unknown_sel["type"] == "field_capture"
    assert {"person_id": {"$exists": False}} in unknown_sel["$or"]
    assert {"person_id": ""} in unknown_sel["$or"]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"q": "urinal"}, {"s-a1-sb-qc"}),
        ({"vision_category": "Kitchens"}, {"s-a1-sb2-mt"}),
        ({"area_guess": "restroom"}, {"s-a1-sb-qc"}),
        ({"capture_id": "cap-b1"}, set()),  # someone else's capture: AND -> nothing, OR -> B's rows
        ({"date_from": "2026-08-03"}, {"s-a2-sb-mt", "s-a2-sb2-qc"}),
        ({"date_to": "2026-08-02"}, {"s-a1-sb-qc", "s-a1-sb2-mt"}),
        ({"date_from": "2026-08-02", "date_to": "2026-08-03"}, {"s-a1-sb2-mt", "s-a2-sb-mt"}),
        ({"vision_disagrees": True}, {"s-a1-sb2-mt"}),
        ({"has_deep_analysis": True}, {"s-a1-sb-qc"}),
    ],
)
def test_couch_every_other_filter_ands_with_submitter(tmp_path: Path, couch: FakeCouch, extra: dict[str, object], expected: set[str]) -> None:
    alone = _load(tmp_path, **extra)[0]
    assert alone - A_ALL, f"fixture bug: {extra} alone must admit non-A rows"
    assert _load(tmp_path, submitter_id="sandbox_a", **extra)[0] == expected == alone & A_ALL


def test_couch_capture_id_of_own_capture_narrows_within_submitter(tmp_path: Path, couch: FakeCouch) -> None:
    assert _load(tmp_path, submitter_id="sandbox_a", capture_id="cap-a2")[0] == {"s-a2-sb-mt", "s-a2-sb2-qc"}


def test_couch_clearing_submitter_leaves_other_filters(tmp_path: Path, couch: FakeCouch) -> None:
    assert _load(tmp_path, submitter_id="", site_id="SANDBOX")[0] == SITE_SANDBOX
    assert all("$in" not in str(m["selector"]) for m in couch.gallery_selectors())


def test_couch_selector_carries_capture_id_in_inside_and_with_limit_and_sort(tmp_path: Path, couch: FakeCouch) -> None:
    _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX")
    mango = couch.gallery_selectors()[-1]
    clauses = mango["selector"]["$and"]
    assert {"doc_type": "photo_vision_sidecar"} in clauses
    assert {"site_id": "SANDBOX"} in clauses
    assert {"capture_id": {"$in": ["cap-a1", "cap-a2"]}} in clauses
    assert mango["limit"] == PAGE_LIMIT + 1 == 121
    assert mango["sort"] == [{"generated_at": "desc"}]


def test_couch_match_beyond_first_page_is_found_by_the_query(tmp_path: Path, couch: FakeCouch) -> None:
    newer_b = [
        _sidecar(f"s-b-{i:03d}", "cap-b1", "SANDBOX", "qc_visit", f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00Z")
        for i in range(PAGE_LIMIT + 5)
    ]
    couch.sidecars = newer_b + [_sidecar("s-a-oldest", "cap-a1", "SANDBOX", "qc_visit", "2026-01-01T00:00:00Z")]
    unfiltered, _fb, more = _load(tmp_path)
    assert "s-a-oldest" not in unfiltered and more is True and len(unfiltered) == PAGE_LIMIT
    ids, fallback, has_more = _load(tmp_path, submitter_id="sandbox_a")
    assert ids == {"s-a-oldest"}
    assert (fallback, has_more) == (False, False)


def test_couch_has_more_true_when_submitter_exceeds_page(tmp_path: Path, couch: FakeCouch) -> None:
    couch.sidecars = [
        _sidecar(f"s-a-{i:03d}", "cap-a1", "SANDBOX", "qc_visit", f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00Z")
        for i in range(PAGE_LIMIT + 1)
    ] + list(SIDECARS)
    ids, _fb, has_more = _load(tmp_path, submitter_id="sandbox_a")
    assert len(ids) == PAGE_LIMIT and has_more is True


def test_couch_person_with_zero_captures_returns_empty_without_in_empty_query(tmp_path: Path, couch: FakeCouch) -> None:
    result = field_photos.load_filtered_photo_sidecars(_ctx(tmp_path), submitter_id="sandbox_zed")
    assert result == ([], False, False)
    assert couch.gallery_selectors() == []
    assert all("$in\": []" not in str(m) and "'$in': []" not in str(m) for m in couch.sidecar_queries)


def test_couch_capture_query_uses_person_id_forms_and_fields(tmp_path: Path, couch: FakeCouch) -> None:
    _load(tmp_path, submitter_id="sandbox-a")
    mango = couch.capture_queries[0]
    assert mango["selector"] == {"type": "field_capture", "person_id": {"$in": ["sandbox-a", "sandbox_a"]}}
    assert "capture_id" in mango["fields"]


def test_couch_capture_ids_paginate_by_bookmark(tmp_path: Path, couch: FakeCouch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "_FIELD_CAPTURE_PAGE_SIZE", 2)
    couch.captures = [_capture(f"cap-a-{i}", "sandbox_a", "Sandy Sandbox") for i in range(5)]
    ids = field_photos._capture_ids_for_submitter(object(), tmp_path, "sandbox_a")
    assert ids == {f"cap-a-{i}" for i in range(5)}
    assert len(couch.capture_queries) == 3


# --------------------------------------------------------------------------- #
# C. AND semantics — disk path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("submitter", ["sandbox_a", "sandbox-a"])
def test_disk_submitter_alone_returns_exactly_a(tmp_path: Path, disk: None, submitter: str) -> None:
    ids, fallback, has_more = _load(tmp_path, submitter_id=submitter)
    assert ids == A_ALL and fallback is False and has_more is False


def test_disk_intersections_match_couch(tmp_path: Path, disk: None) -> None:
    assert _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX")[0] == {"s-a1-sb-qc", "s-a2-sb-mt"}
    assert _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX", qc_category="qc_visit")[0] == {"s-a1-sb-qc"}
    assert _load(tmp_path, submitter_id="sandbox_b")[0] == B_ALL
    assert _load(tmp_path, submitter_id=UNKNOWN)[0] == UNKNOWN_ALL
    assert _load(tmp_path, submitter_id="sandbox_zed")[0] == set()
    assert _load(tmp_path, site_id="SANDBOX")[0] == SITE_SANDBOX


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"q": "urinal"}, {"s-a1-sb-qc"}),
        ({"vision_category": "Kitchens"}, {"s-a1-sb2-mt"}),
        ({"area_guess": "restroom"}, {"s-a1-sb-qc"}),
        ({"capture_id": "cap-b1"}, set()),
        ({"date_from": "2026-08-03"}, {"s-a2-sb-mt", "s-a2-sb2-qc"}),
        ({"date_to": "2026-08-02"}, {"s-a1-sb-qc", "s-a1-sb2-mt"}),
        ({"vision_disagrees": True}, {"s-a1-sb2-mt"}),
        ({"has_deep_analysis": True}, {"s-a1-sb-qc"}),
    ],
)
def test_disk_every_other_filter_ands_with_submitter(tmp_path: Path, disk: None, extra: dict[str, object], expected: set[str]) -> None:
    alone = _load(tmp_path, **extra)[0]
    assert alone - A_ALL
    assert _load(tmp_path, submitter_id="sandbox_a", **extra)[0] == expected == alone & A_ALL


def test_disk_match_beyond_first_page_is_found_before_the_slice(tmp_path: Path, disk: None, monkeypatch: pytest.MonkeyPatch) -> None:
    newer_b = [
        _sidecar(f"s-b-{i:03d}", "cap-b1", "SANDBOX", "qc_visit", f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00Z")
        for i in range(PAGE_LIMIT + 5)
    ]
    corpus = newer_b + [_sidecar("s-a-oldest", "cap-a1", "SANDBOX", "qc_visit", "2026-01-01T00:00:00Z")]
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: list(corpus))
    unfiltered, _fb, more = _load(tmp_path)
    assert "s-a-oldest" not in unfiltered and more is True
    ids, fallback, has_more = _load(tmp_path, submitter_id="sandbox_a")
    assert ids == {"s-a-oldest"} and (fallback, has_more) == (False, False)


def test_disk_real_sidecar_files_are_filtered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: _disk_submitters())
    runtime_root = tmp_path / "runtime"
    pv_dir = runtime_root / "field_capture" / "photo_vision"
    pv_dir.mkdir(parents=True)
    for doc in SIDECARS:
        (pv_dir / f"{doc['photo_asset_id']}.json").write_text(json.dumps(doc), encoding="utf-8")
    ids, _fb, _more = _load(tmp_path, submitter_id="sandbox_a", site_id="SANDBOX")
    assert ids == {"s-a1-sb-qc", "s-a2-sb-mt"}


def test_in_memory_filter_applies_capture_ids_before_sort_and_slice() -> None:
    rows = [_sidecar(f"s-{i}", "cap-b1", "SANDBOX", "qc_visit", f"2026-09-01T00:{i:02d}:00Z") for i in range(5)]
    rows.append(_sidecar("s-target", "cap-a1", "SANDBOX", "qc_visit", "2026-01-01T00:00:00Z"))
    out = field_photos._in_memory_filter(rows, "", "", "", "", "", capture_ids={"cap-a1"}, limit=2)
    assert _ids(out) == {"s-target"}
    # None means "no submitter filter"; an empty set means "no captures" -> nothing.
    assert len(field_photos._in_memory_filter(rows, "", "", "", "", "", capture_ids=None, limit=10)) == 6
    assert field_photos._in_memory_filter(rows, "", "", "", "", "", capture_ids=set(), limit=10) == []


# --------------------------------------------------------------------------- #
# C. render() — CouchDB path, late fallback, count text
# --------------------------------------------------------------------------- #
def test_render_couch_path_applies_submitter_and_site(tmp_path: Path, couch: FakeCouch) -> None:
    ctx = _ctx(tmp_path, {"submitter_id": ["sandbox-a"], "site_id": ["SANDBOX"]})
    html = field_photos.render(ctx)
    assert "2 photos" in html
    assert "s-a1-sb-qc.jpg" in html and "s-a2-sb-mt.jpg" in html
    assert "s-b1-sb-qc.jpg" not in html and "s-orphan.jpg" not in html and "s-a1-sb2-mt.jpg" not in html
    assert "CouchDB unavailable" not in html


def test_render_late_fallback_to_disk_keeps_submitter_condition_and_truthful_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeCouch(list(CAPTURES), list(SIDECARS))
    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: object())
    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", fake.find)

    def _boom(_config: object, _mango: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("photo-vision db unreachable")

    monkeypatch.setattr("field_capture.photo_vision_couchdb.query_photo_vision", _boom)
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: _disk_submitters())
    newer_b = [
        _sidecar(f"s-b-{i:03d}", "cap-b1", "SANDBOX", "qc_visit", f"2026-09-01T{i // 60:02d}:{i % 60:02d}:00Z")
        for i in range(PAGE_LIMIT + 5)
    ]
    corpus = newer_b + list(SIDECARS)
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: list(corpus))

    html = field_photos.render(_ctx(tmp_path, {"submitter_id": ["sandbox_a"], "site_id": ["SANDBOX"]}))
    assert "CouchDB unavailable" in html
    assert "2 photos" in html
    assert "s-a1-sb-qc.jpg" in html and "s-a2-sb-mt.jpg" in html
    assert "s-b-000.jpg" not in html and "s-b1-sb-qc.jpg" not in html

    html_b = field_photos.render(_ctx(tmp_path, {"submitter_id": ["sandbox_b"]}))
    assert f"{PAGE_LIMIT}+ photos (showing first {PAGE_LIMIT})" in html_b


def test_render_disk_path_count_text_matches_filtered_set(tmp_path: Path, disk: None) -> None:
    html = field_photos.render(_ctx(tmp_path, {"submitter_id": [UNKNOWN]}))
    assert "2 photos" in html
    assert "s-u1-sb-qc.jpg" in html and "s-u2-sb2-mt.jpg" in html and "s-orphan.jpg" not in html
    html_none = field_photos.render(_ctx(tmp_path, {"submitter_id": ["sandbox_zed"]}))
    assert "0 photos" in html_none


# --------------------------------------------------------------------------- #
# C. pending photos
# --------------------------------------------------------------------------- #
def _asset(capture_id: str, asset_id: str, site_id: str = "SANDBOX") -> FieldPhotoAsset:
    return FieldPhotoAsset(
        capture_id=capture_id,
        site_id=site_id,
        area="office",
        phase="during",
        photo_asset_id=asset_id,
        photo_id=f"photo-{asset_id}",
        filename=f"{asset_id}.jpg",
        image_path=Path(f"/synthetic/{asset_id}.jpg"),
        image_media_url=f"/media/{asset_id}.jpg",
        mime_type="image/jpeg",
        size_bytes=1,
        intake_json_path=Path(f"/synthetic/{asset_id}.json"),
        captured_at="2026-08-10T10:00:00Z",
    )


PENDING_ASSETS = [
    _asset("cap-a1", "pa-a1"),
    _asset("cap-a2", "pa-a2", site_id="SANDBOX2"),
    _asset("cap-b1", "pa-b1"),
    _asset("cap-u1", "pa-u1"),
    _asset("cap-orphan", "pa-orphan"),
]


def _pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **filters: object) -> set[str]:
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: list(PENDING_ASSETS))
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: _disk_submitters())
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: [])
    records = field_photos._pending_photo_records(
        tmp_path / "runtime",
        processed_asset_ids=set(),
        q=filters.pop("q", ""),
        site_id=filters.pop("site_id", ""),
        area_guess=filters.pop("area_guess", ""),
        **filters,
    )
    return {str(r["photo_asset_id"]) for r in records}


def test_pending_records_obey_submitter_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _pending(tmp_path, monkeypatch) == {"pa-a1", "pa-a2", "pa-b1", "pa-u1", "pa-orphan"}
    assert _pending(tmp_path, monkeypatch, submitter_id="sandbox_a") == {"pa-a1", "pa-a2"}
    assert _pending(tmp_path, monkeypatch, submitter_id="sandbox-a") == {"pa-a1", "pa-a2"}
    assert _pending(tmp_path, monkeypatch, submitter_id="sandbox_a", site_id="SANDBOX") == {"pa-a1"}
    assert _pending(tmp_path, monkeypatch, submitter_id="sandbox_b") == {"pa-b1"}
    assert _pending(tmp_path, monkeypatch, submitter_id="sandbox_zed") == set()


def test_pending_unknown_matches_only_unattributed_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _pending(tmp_path, monkeypatch, submitter_id=UNKNOWN) == {"pa-u1", "pa-orphan"}


def test_asset_matches_filters_submitter_semantics() -> None:
    asset = _asset("cap-a1", "pa-a1")
    common = {"q": "", "site_id": "", "area_guess": ""}
    assert field_photos._asset_matches_filters(asset, submitter_id="sandbox_a", asset_submitter_id="sandbox-a", **common)
    assert field_photos._asset_matches_filters(asset, submitter_id="sandbox-a", asset_submitter_id="sandbox_a", **common)
    assert not field_photos._asset_matches_filters(asset, submitter_id="sandbox_b", asset_submitter_id="sandbox_a", **common)
    assert not field_photos._asset_matches_filters(asset, submitter_id=UNKNOWN, asset_submitter_id="sandbox_a", **common)
    assert field_photos._asset_matches_filters(asset, submitter_id=UNKNOWN, asset_submitter_id="", **common)
    # Token label / name never stand in for the id.
    assert not field_photos._asset_matches_filters(
        asset, submitter_id="sandbox_a", asset_submitter_id="", submitter_name="sandbox_a", **common
    )


# --------------------------------------------------------------------------- #
# D. options
# --------------------------------------------------------------------------- #
def test_submitter_options_from_couch_corpus(tmp_path: Path, couch: FakeCouch) -> None:
    couch.captures = list(CAPTURES) + [
        _capture("cap-c1", "sandbox_c", "amy Sandbox"),
        _capture("cap-c2", "sandbox-c", "amy Sandbox"),
    ]
    options = field_photos._load_submitter_options(object(), tmp_path)
    assert options == [
        ("sandbox_c", "amy Sandbox (sandbox_c)"),
        ("sandbox_a", "Sandy Sandbox (sandbox_a)"),
        ("sandbox_b", "Sandy Sandbox (sandbox_b)"),
        (UNKNOWN, "Unknown submitter"),
    ]
    mango = couch.capture_queries[0]
    assert mango["selector"] == {"type": "field_capture"}
    assert set(mango["fields"]) >= {"person_id", "person_name"}


def test_submitter_options_paginate_whole_corpus(tmp_path: Path, couch: FakeCouch, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "_FIELD_CAPTURE_PAGE_SIZE", 2)
    couch.captures = [_capture(f"cap-{i}", f"sandbox_{i}", f"Person {i}") for i in range(5)]
    options = field_photos._load_submitter_options(object(), tmp_path)
    assert [value for value, _label in options] == [f"sandbox_{i}" for i in range(5)]
    assert len(couch.capture_queries) == 3


def test_submitter_options_omit_unknown_when_all_attributed(tmp_path: Path, couch: FakeCouch) -> None:
    couch.captures = [_capture("cap-a1", "sandbox_a", "Sandy Sandbox")]
    assert field_photos._load_submitter_options(object(), tmp_path) == [("sandbox_a", "Sandy Sandbox (sandbox_a)")]


def test_submitter_options_disk_fallback_when_couch_absent_or_failing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: _disk_submitters())
    expected = [
        ("sandbox_a", "Sandy Sandbox (sandbox_a)"),
        ("sandbox_b", "Sandy Sandbox (sandbox_b)"),
        (UNKNOWN, "Unknown submitter"),
    ]
    assert field_photos._load_submitter_options(None, tmp_path) == expected

    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise RuntimeError("field captures db unreachable")

    monkeypatch.setattr("voice_memo.couchdb.query_couchdb_find", _boom)
    assert field_photos._load_submitter_options(object(), tmp_path) == expected


def test_options_do_not_consult_employee_status() -> None:
    source = Path(field_photos.__file__).read_text(encoding="utf-8")
    assert "employee_selector_options" not in source
    assert '"status": "active"' not in source and "status\": \"inactive\"" not in source


# --------------------------------------------------------------------------- #
# E. UI / retention
# --------------------------------------------------------------------------- #
def test_filter_form_renders_submitter_select_with_neutral_and_selected(tmp_path: Path, disk: None) -> None:
    html = field_photos.render_filter_form(submitter_id="sandbox-a", site_id="SANDBOX", runtime_root=tmp_path / "runtime")
    assert '<select name="submitter_id"' in html
    assert '<option value="">All submitters</option>' in html
    assert '<option value="sandbox_a" selected>Sandy Sandbox (sandbox_a)</option>' in html
    assert '<option value="sandbox_b">Sandy Sandbox (sandbox_b)</option>' in html
    assert f'<option value="{UNKNOWN}">Unknown submitter</option>' in html
    assert '<option value="SANDBOX" selected>' in html


def test_route_keeps_submitter_and_site_selected_and_return_link(tmp_path: Path, disk: None) -> None:
    status, content_type, body = ops_app.route_response(
        "GET", "/field-photos?submitter_id=sandbox_a&site_id=SANDBOX", tmp_path / "runtime"
    )
    html = body.decode("utf-8")
    assert int(status) == 200 and content_type.startswith("text/html")
    assert '<option value="sandbox_a" selected>' in html
    assert '<option value="SANDBOX" selected>' in html
    assert "2 photos" in html
    return_to = field_photos._field_photos_return_to({"submitter_id": ["sandbox_a"], "site_id": ["SANDBOX"]})
    assert return_to == "/field-photos?submitter_id=sandbox_a&site_id=SANDBOX"
    assert "submitter_id=sandbox_a" in html and "site_id=SANDBOX" in html


# --------------------------------------------------------------------------- #
# F. no collateral: QC handoff
# --------------------------------------------------------------------------- #
def test_qc_handoff_unchanged_no_capture_in_clause(tmp_path: Path, couch: FakeCouch) -> None:
    ctx = _ctx(tmp_path, {"site_id": ["SANDBOX"]}, route_path="/qc-handoff")
    html = field_photos.render(ctx)
    assert "QC Handoff" in html
    assert couch.gallery_selectors(), "handoff must still query the gallery path"
    for mango in couch.sidecar_queries:
        assert "$in" not in str(mango["selector"])
    assert couch.capture_queries == []
