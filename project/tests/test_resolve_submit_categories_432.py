"""INDEPENDENT VERIFIER coverage for the extracted module-level free function
``field_capture.server.resolve_submit_categories`` (refactor 432).

A duplicated ``resolve_submit_categories`` method was hoisted into one shared
module-level free function. These tests exercise that free function DIRECTLY
with fakes/stubs (no real registry, no real CouchDB, no handler instance) and
were authored from scratch against the behavioral contract — not copied from any
existing test.

Contract under test:
  1. Happy path: registry display categories flow through
     resolve_display_categories(...) then apply_role_category_filter(..., "site_admin").
  2. Registry raises on get_display_categories -> caught, logged WARNING, falls
     back to system defaults, does NOT raise.
  3. load_system_defaults raises/unavailable -> caught, logged, still returns,
     does NOT raise.
  4. site_registry is None -> no registry lookup; result derives from system
     defaults alone.
  5. The role filter argument is hard-coded "site_admin" (operator-only
     categories are appended) and is NOT parameterized.

The "site_admin" role filter is made observable by checking for the
operator-only canonicals (``baseline`` / ``pre_engagement``) that
``apply_role_category_filter(..., "site_admin")`` appends — these are absent for
any other role, so an assertion on their presence is a real (non-vacuous) gate.
"""

from __future__ import annotations

import pytest

from field_capture import server as fc_server
from field_capture.server import (
    BUILTIN_FALLBACK_CATEGORIES,
    OPERATOR_ONLY_CATEGORIES,
    resolve_submit_categories,
)


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #

OPERATOR_CANONICALS = {entry["canonical"] for entry in OPERATOR_ONLY_CATEGORIES}
# Sanity: the extraction's literal "site_admin" gate is what appends these.
assert OPERATOR_CANONICALS == {"baseline", "pre_engagement"}, OPERATOR_ONLY_CATEGORIES


class _LogSink:
    """Capture log calls the way BaseHTTPRequestHandler.log_message is invoked:
    a printf-style format string plus positional args."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, fmt, *args):
        self.calls.append((fmt, args))

    @property
    def messages(self) -> list[str]:
        # Render each call the way log_message would, so we can assert on text.
        out = []
        for fmt, args in self.calls:
            try:
                out.append(fmt % args if args else fmt)
            except Exception:
                out.append(repr((fmt, args)))
        return out


class _FakeRegistry:
    """Stub site registry. Either returns categories or raises on lookup."""

    def __init__(self, *, categories=None, raises: BaseException | None = None):
        self._categories = categories
        self._raises = raises
        self.calls: list[str] = []

    def get_display_categories(self, site_id):
        self.calls.append(site_id)
        if self._raises is not None:
            raise self._raises
        return self._categories


def _canonicals(result) -> list[str]:
    return [entry["canonical"] for entry in result]


SITE_CATS = [
    {"label": "Loading Dock", "canonical": "loading_dock"},
    {"label": "Stairwell", "canonical": "stairwell"},
]
DEFAULT_CATS = [
    {"label": "Lobby Default", "canonical": "lobby_default"},
    {"label": "Hallway Default", "canonical": "hallway_default"},
]


@pytest.fixture(autouse=True)
def _stub_system_defaults_empty(monkeypatch):
    """Default for every test: system defaults present but with NO display
    categories. Tests that care override load_system_defaults explicitly.

    This isolates the unit from the real CouchDB-backed loader regardless of the
    hermetic guard, so the registry branch is the controlled variable."""
    monkeypatch.setattr(
        fc_server, "load_system_defaults", lambda: {"default_display_categories": None}
    )


# --------------------------------------------------------------------------- #
# Contract 1 — happy path: registry categories win, site_admin filter applied
# --------------------------------------------------------------------------- #

def test_happy_path_uses_registry_categories_and_appends_operator_only():
    registry = _FakeRegistry(categories=SITE_CATS)
    log = _LogSink()

    result = resolve_submit_categories(registry, "site-123", log)

    canon = _canonicals(result)
    # Registry categories are present and lead the list (resolve_display_categories
    # returns site categories first when they normalize non-empty).
    assert canon[: len(SITE_CATS)] == ["loading_dock", "stairwell"]
    # site_admin filter APPENDED the operator-only categories at the end.
    assert canon[-2:] == ["baseline", "pre_engagement"]
    assert OPERATOR_CANONICALS.issubset(set(canon))
    # The default categories were NOT used (site categories took precedence).
    assert "lobby_default" not in canon
    # Lookup was performed against the right site_id; happy path logs nothing.
    assert registry.calls == ["site-123"]
    assert log.calls == []
    # Every entry is a {label, canonical} dict.
    assert all(set(e.keys()) == {"label", "canonical"} for e in result)


def test_happy_path_result_is_independent_list_not_shared_mutable_state():
    """Mutating the returned list / its operator-only entries must not corrupt
    the module-level OPERATOR_ONLY_CATEGORIES or a second call's result."""
    registry = _FakeRegistry(categories=SITE_CATS)
    log = _LogSink()

    first = resolve_submit_categories(registry, "s1", log)
    first.append({"label": "X", "canonical": "x"})
    first[0]["label"] = "MUTATED"

    second = resolve_submit_categories(_FakeRegistry(categories=SITE_CATS), "s2", log)
    assert _canonicals(second)[-2:] == ["baseline", "pre_engagement"]
    assert "x" not in _canonicals(second)
    # Module-level template untouched.
    assert {e["canonical"] for e in OPERATOR_ONLY_CATEGORIES} == OPERATOR_CANONICALS


# --------------------------------------------------------------------------- #
# Contract 2 — registry.get_display_categories raises -> caught, logged, fallback
# --------------------------------------------------------------------------- #

def test_registry_lookup_raises_falls_back_to_system_defaults_and_logs(monkeypatch):
    monkeypatch.setattr(
        fc_server,
        "load_system_defaults",
        lambda: {"default_display_categories": DEFAULT_CATS},
    )
    registry = _FakeRegistry(raises=RuntimeError("boom"))
    log = _LogSink()

    result = resolve_submit_categories(registry, "site-err", log)

    canon = _canonicals(result)
    # Did NOT raise; fell back to the system-default categories.
    assert canon[: len(DEFAULT_CATS)] == ["lobby_default", "hallway_default"]
    # site_admin filter still applied on the fallback path.
    assert canon[-2:] == ["baseline", "pre_engagement"]
    # Exactly one WARNING about the failed display_categories lookup, with site_id+error.
    assert len(log.calls) == 1
    msg = log.messages[0]
    assert msg.startswith("WARNING:")
    assert "display_categories lookup failed" in msg
    assert "site-err" in msg
    assert "boom" in msg


def test_registry_lookup_raises_arbitrary_exception_does_not_propagate():
    """Even a non-RuntimeError (broad `except Exception`) is swallowed."""
    registry = _FakeRegistry(raises=KeyError("missing"))
    log = _LogSink()
    # Must not raise. With empty defaults this lands on BUILTIN fallback.
    result = resolve_submit_categories(registry, "s", log)
    canon = _canonicals(result)
    assert canon[-2:] == ["baseline", "pre_engagement"]
    assert len(log.calls) == 1
    assert "display_categories lookup failed" in log.messages[0]


# --------------------------------------------------------------------------- #
# Contract 3 — load_system_defaults raises -> caught, logged, still returns
# --------------------------------------------------------------------------- #

def test_system_defaults_raises_is_caught_logged_and_still_returns(monkeypatch):
    def _boom():
        raise RuntimeError("defaults unavailable")

    monkeypatch.setattr(fc_server, "load_system_defaults", _boom)
    registry = _FakeRegistry(categories=SITE_CATS)
    log = _LogSink()

    result = resolve_submit_categories(registry, "site-x", log)

    canon = _canonicals(result)
    # Registry categories still used; site_admin filter still applied.
    assert canon[: len(SITE_CATS)] == ["loading_dock", "stairwell"]
    assert canon[-2:] == ["baseline", "pre_engagement"]
    # The system_defaults failure was logged as a WARNING for /api/submit.
    assert any(
        m.startswith("WARNING:") and "system_defaults unavailable for /api/submit" in m
        and "defaults unavailable" in m
        for m in log.messages
    ), log.messages


def test_system_defaults_raises_with_no_registry_falls_to_builtin(monkeypatch):
    """Both sources unusable (no registry + defaults raise) -> BUILTIN fallback,
    still filtered, still no exception."""

    def _boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(fc_server, "load_system_defaults", _boom)
    log = _LogSink()

    result = resolve_submit_categories(None, "s", log)

    canon = _canonicals(result)
    builtin_canon = [c["canonical"] for c in BUILTIN_FALLBACK_CATEGORIES]
    assert canon[: len(builtin_canon)] == builtin_canon
    assert canon[-2:] == ["baseline", "pre_engagement"]
    assert any("system_defaults unavailable" in m for m in log.messages)


def test_system_defaults_non_dict_is_tolerated(monkeypatch):
    """isinstance(system_defaults, dict) guard: a non-dict return must not raise
    (default_categories stays None) and must not be logged as an error."""
    monkeypatch.setattr(fc_server, "load_system_defaults", lambda: ["not", "a", "dict"])
    registry = _FakeRegistry(categories=SITE_CATS)
    log = _LogSink()

    result = resolve_submit_categories(registry, "s", log)

    assert _canonicals(result)[: len(SITE_CATS)] == ["loading_dock", "stairwell"]
    assert _canonicals(result)[-2:] == ["baseline", "pre_engagement"]
    # Non-dict is not an exception path -> no WARNING logged.
    assert log.calls == []


# --------------------------------------------------------------------------- #
# Contract 4 — site_registry is None -> no lookup; result from defaults alone
# --------------------------------------------------------------------------- #

def test_none_registry_uses_system_defaults_only(monkeypatch):
    monkeypatch.setattr(
        fc_server,
        "load_system_defaults",
        lambda: {"default_display_categories": DEFAULT_CATS},
    )
    log = _LogSink()

    result = resolve_submit_categories(None, "ignored-site", log)

    canon = _canonicals(result)
    assert canon[: len(DEFAULT_CATS)] == ["lobby_default", "hallway_default"]
    assert canon[-2:] == ["baseline", "pre_engagement"]
    # No registry => no registry-failure WARNING.
    assert all("display_categories lookup failed" not in m for m in log.messages)


def test_none_registry_with_empty_defaults_uses_builtin(monkeypatch):
    monkeypatch.setattr(
        fc_server, "load_system_defaults", lambda: {"default_display_categories": []}
    )
    log = _LogSink()

    result = resolve_submit_categories(None, "s", log)

    canon = _canonicals(result)
    builtin_canon = [c["canonical"] for c in BUILTIN_FALLBACK_CATEGORIES]
    assert canon[: len(builtin_canon)] == builtin_canon
    assert canon[-2:] == ["baseline", "pre_engagement"]
    assert log.calls == []


# --------------------------------------------------------------------------- #
# Contract 5 — the role filter is hard-coded "site_admin" (operator-only added)
# --------------------------------------------------------------------------- #

def test_role_filter_is_site_admin_operator_only_categories_always_present(monkeypatch):
    """The strongest gate: regardless of the source branch, the operator-only
    categories (which ONLY appear for role == "site_admin") must be present.

    If the literal "site_admin" is changed to any other role, or the
    apply_role_category_filter wrapper is dropped, these disappear and this
    fails. We assert across all three source branches to make that robust."""
    log = _LogSink()

    # (a) registry-sourced
    monkeypatch.setattr(
        fc_server, "load_system_defaults", lambda: {"default_display_categories": None}
    )
    r_registry = resolve_submit_categories(_FakeRegistry(categories=SITE_CATS), "s", log)

    # (b) defaults-sourced
    monkeypatch.setattr(
        fc_server,
        "load_system_defaults",
        lambda: {"default_display_categories": DEFAULT_CATS},
    )
    r_defaults = resolve_submit_categories(None, "s", log)

    # (c) builtin-sourced
    monkeypatch.setattr(
        fc_server, "load_system_defaults", lambda: {"default_display_categories": None}
    )
    r_builtin = resolve_submit_categories(None, "s", log)

    for result in (r_registry, r_defaults, r_builtin):
        canon = _canonicals(result)
        assert "baseline" in canon, canon
        assert "pre_engagement" in canon, canon
        # And appended (not prepended) at the tail.
        assert canon[-2:] == ["baseline", "pre_engagement"], canon


def test_operator_only_not_double_appended_when_already_present(monkeypatch):
    """If the chosen category source already contains an operator-only canonical,
    apply_role_category_filter must not duplicate it. Confirms the dedupe in the
    filter is reached through the free function."""
    cats_with_baseline = [
        {"label": "Baseline (custom)", "canonical": "baseline"},
        {"label": "Loading Dock", "canonical": "loading_dock"},
    ]
    monkeypatch.setattr(
        fc_server,
        "load_system_defaults",
        lambda: {"default_display_categories": None},
    )
    log = _LogSink()

    result = resolve_submit_categories(_FakeRegistry(categories=cats_with_baseline), "s", log)
    canon = _canonicals(result)

    assert canon.count("baseline") == 1, canon
    assert canon.count("pre_engagement") == 1, canon
    # pre_engagement still appended (was absent), baseline kept its original spot.
    assert canon[0] == "baseline"
    assert canon[-1] == "pre_engagement"
