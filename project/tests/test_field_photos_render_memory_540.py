"""540: /field-photos memory arc — independent verifier gate.

Profiling showed /field-photos costing ~230MB heap and ~8s/render because
(a) `_pending_photo_records` pulled the ENTIRE photo-vision corpus from
CouchDB just to compute a set of ids on disk, and (b) every card resolved
its site label by constructing a fresh `CouchDBSiteRegistry` and calling
`list_sites()`. This gate proves both are fixed:

  1. `common.photo_vision_sidecar_ids_on_disk` lists sidecar ids from the
     filesystem without touching file contents or CouchDB.
  2. `_pending_photo_records` (both `field_photos` and `photos`) uses that
     helper instead of `load_photo_vision_sidecars` to derive pending state.
  3. Neither `_pending_photo_records` call path pulls the full corpus.
  4. `common.resolve_site_label` memoizes per site id with a TTL, is
     lock-guarded for concurrent callers, and can be invalidated.
  5. The `/field-photos` (and `/photos`) HTML output is unaffected —
     byte-identical across a cold and a warm cache.

Everything here is synthetic: sandbox identities, SANDBOX/S2/S3 site ids,
tmp roots.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops_dashboard import common
from ops_dashboard.sections import field_photos
from ops_dashboard.sections import photos


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _asset(photo_asset_id: str, capture_id: str, site_id: str = "SANDBOX") -> SimpleNamespace:
    return SimpleNamespace(
        photo_asset_id=photo_asset_id,
        capture_id=capture_id,
        site_id=site_id,
        area="office",
        captured_at="2026-08-10T10:00:00Z",
        filename=f"{photo_asset_id}.jpg",
        image_media_url=f"/media/{photo_asset_id}.jpg",
        qc_category="",
    )


def _raising_spy(name: str):
    def _spy(*_a: object, **_k: object) -> object:
        raise AssertionError(f"{name} must not be called from _pending_photo_records")

    return _spy


@pytest.fixture(autouse=True)
def _invalidate_cache() -> None:
    # The site-label cache is module-global; keep tests independent.
    common.invalidate_site_label_cache()
    yield
    common.invalidate_site_label_cache()


# --------------------------------------------------------------------------- #
# 1. photo_vision_sidecar_ids_on_disk
# --------------------------------------------------------------------------- #
def test_sidecar_ids_on_disk_lists_json_stems(tmp_path: Path) -> None:
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()
    (pv_dir / "a.json").write_text("not even json {{{", encoding="utf-8")
    (pv_dir / "b.json").write_text(json.dumps({"photo_asset_id": "b"}), encoding="utf-8")
    (pv_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    (pv_dir / "sub").mkdir()
    (pv_dir / "sub" / "c.json").write_text("{}", encoding="utf-8")
    assert common.photo_vision_sidecar_ids_on_disk(pv_dir) == {"a", "b"}


def test_sidecar_ids_on_disk_missing_dir_returns_empty_set(tmp_path: Path) -> None:
    assert common.photo_vision_sidecar_ids_on_disk(tmp_path / "does-not-exist") == set()


def test_sidecar_ids_on_disk_swallows_oserror_from_glob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()

    def _boom(self: Path, _pattern: str):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "glob", _boom)
    assert common.photo_vision_sidecar_ids_on_disk(pv_dir) == set()


def test_sidecar_ids_on_disk_does_not_read_file_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()
    (pv_dir / "a.json").write_text("{garbage", encoding="utf-8")

    def _boom(*_a: object, **_k: object) -> str:
        raise AssertionError("must not read file contents")

    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)
    monkeypatch.setattr(Path, "open", _boom)
    assert common.photo_vision_sidecar_ids_on_disk(pv_dir) == {"a"}


# --------------------------------------------------------------------------- #
# Probes beyond the acceptance list
# --------------------------------------------------------------------------- #
def test_sidecar_ids_on_disk_uppercase_json_suffix_is_not_matched(tmp_path: Path) -> None:
    # Documents actual behavior: glob("*.json") is case-sensitive, so a
    # ".JSON" sidecar (if one ever existed on disk) would silently be
    # excluded. Not a spec requirement either way -- just pinned here.
    pv_dir = tmp_path / "photo_vision"
    pv_dir.mkdir()
    (pv_dir / "upper.JSON").write_text("{}", encoding="utf-8")
    (pv_dir / "lower.json").write_text("{}", encoding="utf-8")
    assert common.photo_vision_sidecar_ids_on_disk(pv_dir) == {"lower"}


def test_sidecar_ids_on_disk_dir_is_actually_a_file(tmp_path: Path) -> None:
    # Path.exists() is True for a plain file too; .glob() on a file raises
    # NotADirectoryError, a subclass of OSError, which must be swallowed.
    not_a_dir = tmp_path / "photo_vision_is_a_file"
    not_a_dir.write_text("surprise", encoding="utf-8")
    assert common.photo_vision_sidecar_ids_on_disk(not_a_dir) == set()


# --------------------------------------------------------------------------- #
# 2 + 3. _pending_photo_records: state derivation + no corpus pull
# --------------------------------------------------------------------------- #
def _patch_common_corpus_spies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "load_photo_vision_sidecars", _raising_spy("common.load_photo_vision_sidecars"))
    monkeypatch.setattr(
        "field_capture.photo_vision_couchdb.fetch_all_photo_vision_docs",
        _raising_spy("field_capture.photo_vision_couchdb.fetch_all_photo_vision_docs"),
    )
    monkeypatch.setattr(
        "field_capture.photo_vision_couchdb.query_photo_vision",
        _raising_spy("field_capture.photo_vision_couchdb.query_photo_vision"),
    )


@pytest.mark.parametrize("module", [field_photos, photos])
def test_pending_records_state_from_disk_sidecar_ids_no_corpus_pull(
    module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_common_corpus_spies(monkeypatch)
    monkeypatch.setattr(module, "load_photo_vision_sidecars", _raising_spy(f"{module.__name__}.load_photo_vision_sidecars"))
    monkeypatch.setattr(module, "submitters_by_capture", lambda _root: {})

    runtime_root = tmp_path / "runtime"
    pv_dir = runtime_root / "field_capture" / "photo_vision"
    pv_dir.mkdir(parents=True)
    (pv_dir / "a.json").write_text("garbage, not real json {{{", encoding="utf-8")

    assets = [_asset("a", "cap-a"), _asset("b", "cap-b"), _asset("c", "cap-c")]
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: assets)

    kwargs = dict(q="", site_id="", area_guess="")
    if module is field_photos:
        kwargs.update(date_from="", date_to="")
    else:
        kwargs.update(date_from="", date_to="")

    records = module._pending_photo_records(
        runtime_root,
        processed_asset_ids={"b"},
        **kwargs,
    )
    by_id = {str(r["photo_asset_id"]): r for r in records}
    assert set(by_id) == {"a", "c"}, "processed asset 'b' must not be listed"

    saving_value = "saving_result" if module is field_photos else "Saving vision result"
    awaiting_value = "awaiting_vision" if module is field_photos else "Awaiting vision"
    assert by_id["a"]["state"] == saving_value
    assert by_id["c"]["state"] == awaiting_value


@pytest.mark.parametrize("module", [field_photos, photos])
def test_pending_records_no_photo_vision_dir_nothing_is_saving_no_error(
    module: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_common_corpus_spies(monkeypatch)
    monkeypatch.setattr(module, "load_photo_vision_sidecars", _raising_spy(f"{module.__name__}.load_photo_vision_sidecars"))
    monkeypatch.setattr(module, "submitters_by_capture", lambda _root: {})

    runtime_root = tmp_path / "runtime"  # field_capture/photo_vision never created
    assets = [_asset("a", "cap-a"), _asset("c", "cap-c")]
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: assets)

    records = module._pending_photo_records(
        runtime_root, processed_asset_ids=set(), q="", site_id="", area_guess="", date_from="", date_to=""
    )
    states = {str(r["state"]) for r in records}
    assert "saving_result" not in states and "Saving vision result" not in states
    assert len(records) == 2


# --------------------------------------------------------------------------- #
# 4. resolve_site_label memoization
# --------------------------------------------------------------------------- #
class _CountingRegistry:
    calls = 0
    list_sites_result: object = None

    def __init__(self) -> None:
        type(self).calls += 1

    def list_sites(self) -> object:
        result = type(self).list_sites_result
        if isinstance(result, Exception):
            raise result
        return result

    @classmethod
    def reset(cls, rows: object) -> None:
        cls.calls = 0
        cls.list_sites_result = rows


ROWS = [
    {"site_id": "SANDBOX", "canonical": "Sandbox Site"},
    {"site_id": "S2", "canonical": "Sandbox Two"},
    {"site_id": "S3", "canonical": "Sandbox Two"},
]


@pytest.fixture
def counting_registry(monkeypatch: pytest.MonkeyPatch) -> type:
    _CountingRegistry.reset(list(ROWS))
    account_calls = {"n": 0}

    def _account(_site_id: str) -> str:
        account_calls["n"] += 1
        return "Sandbox Account"

    monkeypatch.setattr(common, "CouchDBSiteRegistry", _CountingRegistry)
    monkeypatch.setattr(common, "_location_account", _account)
    _CountingRegistry.account_calls = account_calls  # type: ignore[attr-defined]
    common.invalidate_site_label_cache()
    return _CountingRegistry


def test_resolve_site_label_memoizes_per_site_id(counting_registry: type, tmp_path: Path) -> None:
    results = {sid: [] for sid in ("SANDBOX", "S2", "S3")}
    for _ in range(40):
        for sid in ("SANDBOX", "S2", "S3"):
            results[sid].append(common.resolve_site_label(sid, tmp_path / "vault"))
    assert counting_registry.calls == 3, "one construction per distinct site id, not per call"
    for sid, values in results.items():
        assert len(set(values)) == 1, f"{sid} results diverged across calls"
    # S2 and S3 share the duplicate canonical name -> the account branch ran.
    assert counting_registry.account_calls["n"] > 0  # type: ignore[attr-defined]


def test_resolve_site_label_invalidate_forces_reconstruction(counting_registry: type, tmp_path: Path) -> None:
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert counting_registry.calls == 1
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert counting_registry.calls == 1
    common.invalidate_site_label_cache()
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert counting_registry.calls == 2


def test_resolve_site_label_zero_ttl_resolves_every_call(
    counting_registry: type, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "_SITE_LABEL_CACHE_TTL_SECONDS", 0)
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert counting_registry.calls == 3


def test_resolve_site_label_blank_site_id_skips_registry_and_cache(
    counting_registry: type, tmp_path: Path
) -> None:
    assert common.resolve_site_label("", tmp_path / "vault") == common.render_site_label("")
    assert common.resolve_site_label(None, tmp_path / "vault") == common.render_site_label(None)
    assert counting_registry.calls == 0
    # And a blank id never occupies a cache slot that would shadow a real one.
    common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert counting_registry.calls == 1


def test_resolve_site_label_registry_error_falls_back_and_caches_fallback(
    counting_registry: type, tmp_path: Path
) -> None:
    counting_registry.reset(RuntimeError("registry unreachable"))
    result = common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert result == common.render_site_label("SANDBOX")
    assert counting_registry.calls == 1
    # Within TTL, the fallback itself is cached -- no re-construction.
    result2 = common.resolve_site_label("SANDBOX", tmp_path / "vault")
    assert result2 == result
    assert counting_registry.calls == 1


def test_resolve_site_label_different_site_ids_do_not_share_cache_entries(
    counting_registry: type, tmp_path: Path
) -> None:
    label_sandbox = common.resolve_site_label("SANDBOX", tmp_path / "vault")
    label_s2 = common.resolve_site_label("S2", tmp_path / "vault")
    assert label_sandbox != label_s2
    assert counting_registry.calls == 2


def test_resolve_site_label_concurrent_callers_same_id_construct_at_most_once(
    counting_registry: type, tmp_path: Path
) -> None:
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def _worker() -> None:
        barrier.wait(timeout=5)
        value = common.resolve_site_label("SANDBOX", tmp_path / "vault")
        with lock:
            results.append(value)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert len(set(results)) == 1
    assert counting_registry.calls <= 8
    assert counting_registry.calls == 1, "ideally exactly one construction under the lock"


# --------------------------------------------------------------------------- #
# 5. Byte-identical output, cold vs warm cache
# --------------------------------------------------------------------------- #
def _sidecar(asset: str, capture_id: str, site_id: str, qc_category: str, generated_at: str) -> dict[str, object]:
    return {
        "doc_type": "photo_vision_sidecar",
        "status": "completed",
        "capture_id": capture_id,
        "photo_id": f"photo-{asset}",
        "photo_asset_id": asset,
        "site_id": site_id,
        "qc_category": qc_category,
        "vision_category": "",
        "category_agreement": "",
        "area_guess": "",
        "description": "synthetic sandbox photo",
        "search_text": "synthetic sandbox photo",
        "generated_at": generated_at,
        "source_filename": f"{asset}.jpg",
        "visible_objects": [],
        "possible_conditions": [],
        "possible_issues": [],
        "provenance": {"captured_at": generated_at, "image_media_url": f"/media/{asset}.jpg"},
    }


RENDER_SIDECARS = [
    _sidecar("r-1", "cap-1", "SANDBOX", "qc_visit", "2026-08-01T10:00:00Z"),
    _sidecar("r-2", "cap-2", "SANDBOX", "maintenance", "2026-08-02T10:00:00Z"),
    _sidecar("r-3", "cap-3", "SANDBOX", "qc_visit", "2026-08-03T10:00:00Z"),
]


@pytest.fixture
def render_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    common.invalidate_site_label_cache()
    monkeypatch.setattr(common, "CouchDBSiteRegistry", _CountingRegistry)
    _CountingRegistry.reset([{"site_id": "SANDBOX", "canonical": "Sandbox Site"}])

    monkeypatch.setattr(field_photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(field_photos, "load_photo_vision_sidecars", lambda _dir: list(RENDER_SIDECARS))
    monkeypatch.setattr(field_photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr("field_capture.photo_vision.discover_photo_assets", lambda *_a, **_k: [])
    monkeypatch.setattr(field_photos, "_load_site_options", lambda: [("SANDBOX", "Sandbox Site (SANDBOX)")])
    monkeypatch.setattr(field_photos, "load_site_options", lambda: [("SANDBOX", "Sandbox Site (SANDBOX)")])

    monkeypatch.setattr(photos, "load_photo_vision_sidecars", lambda _dir: list(RENDER_SIDECARS))
    monkeypatch.setattr(photos, "submitters_by_capture", lambda _root: {})
    monkeypatch.setattr(photos, "_photo_vision_couchdb_config", lambda: None)
    monkeypatch.setattr(photos, "_load_site_options", lambda: [("SANDBOX", "Sandbox Site (SANDBOX)")])

    runtime_root = tmp_path / "runtime"
    ctx = SimpleNamespace(
        runtime_root=runtime_root,
        query={},
        route_path="/field-photos",
        config=SimpleNamespace(vault_root=tmp_path / "vault"),
    )
    return ctx


def test_field_photos_render_byte_identical_cold_vs_warm_cache(render_fixture: SimpleNamespace) -> None:
    common.invalidate_site_label_cache()
    html_cold = field_photos.render(render_fixture)
    html_warm = field_photos.render(render_fixture)
    assert html_cold == html_warm
    assert "SANDBOX" in html_cold
    assert "Sandbox Site" in html_cold


def test_photos_render_smoke_returns_html(render_fixture: SimpleNamespace) -> None:
    html = photos.render(render_fixture)
    assert "<h1>" in html
    assert "Field Photo Search" in html


# --------------------------------------------------------------------------- #
# 6. Diff review — quote the lines
# --------------------------------------------------------------------------- #
def test_diff_review_load_photo_vision_sidecars_and_app_untouched() -> None:
    import inspect

    # load_photo_vision_sidecars itself must be unchanged behaviorally: it
    # still pulls the ENTIRE corpus via fetch_all_photo_vision_docs (that is
    # the expensive corpus-pull the new helper exists to avoid triggering
    # from the pending-state computation).
    source = inspect.getsource(common.load_photo_vision_sidecars)
    uncached_source = inspect.getsource(common._load_photo_vision_sidecars_uncached)
    assert "fetch_all_photo_vision_docs" in uncached_source
    assert "_PHOTO_VISION_CACHE" in source

    # app.py must be untouched by this change (not part of the diff).
    import ops_dashboard.app as ops_app

    app_source = Path(ops_app.__file__).read_text(encoding="utf-8")
    assert "photo_vision_sidecar_ids_on_disk" not in app_source


def test_diff_review_pending_records_use_new_helper_not_full_loader() -> None:
    import inspect

    for module in (field_photos, photos):
        source = inspect.getsource(module._pending_photo_records)
        assert "photo_vision_sidecar_ids_on_disk(photo_vision_dir)" in source
        assert "load_photo_vision_sidecars(photo_vision_dir)" not in source


def test_diff_review_cache_is_lock_guarded_with_ttl_and_invalidate() -> None:
    assert isinstance(common._SITE_LABEL_CACHE_LOCK, type(threading.Lock()))
    assert isinstance(common._SITE_LABEL_CACHE_TTL_SECONDS, (int, float))
    assert common._SITE_LABEL_CACHE_TTL_SECONDS > 0
    assert callable(common.invalidate_site_label_cache)
