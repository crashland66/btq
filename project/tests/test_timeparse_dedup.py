from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from queue_processor import couchdb_queue_watcher, metrics, timeparse
from queue_processor.timeparse import parse_timestamp


# --- Behavior parity with the original byte-copied _parse_timestamp ---


def test_z_suffix_parsed_as_aware_utc() -> None:
    result = parse_timestamp("2026-06-22T12:00:00Z")
    assert result == datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo == timezone.utc


def test_naive_iso_assumed_utc() -> None:
    result = parse_timestamp("2026-06-22T12:00:00")
    assert result == datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    assert result.tzinfo == timezone.utc


def test_tz_aware_offset_converted_to_utc() -> None:
    result = parse_timestamp("2026-06-22T12:00:00+05:00")
    # 12:00 at +05:00 is 07:00 UTC
    assert result == datetime(2026, 6, 22, 7, 0, 0, tzinfo=timezone.utc)
    assert result.utcoffset() == timedelta(0)


def test_invalid_string_returns_none() -> None:
    assert parse_timestamp("not-a-timestamp") is None
    assert parse_timestamp("2026-13-99T99:99:99") is None


@pytest.mark.parametrize("value", [None, 123, 0, 3.14, object(), b"2026-06-22T12:00:00Z"])
def test_non_str_returns_none(value: object) -> None:
    assert parse_timestamp(value) is None


@pytest.mark.parametrize("value", ["", "   ", "\t\n  "])
def test_empty_or_whitespace_returns_none(value: str) -> None:
    assert parse_timestamp(value) is None


# --- Single-definition guard: one function object shared by all sites ---


def test_single_shared_definition_identity() -> None:
    # both modules import the shared function under the private alias used at call
    # sites (`parse_timestamp as _parse_timestamp`), so the bound attribute is the
    # one identical function object from the leaf module.
    assert metrics._parse_timestamp is timeparse.parse_timestamp
    assert couchdb_queue_watcher._parse_timestamp is timeparse.parse_timestamp
    assert metrics._parse_timestamp is couchdb_queue_watcher._parse_timestamp


def _module_path(module) -> Path:
    return Path(module.__file__)


def _defines_function_locally(path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found.add(node.name)
    return found


def test_no_local_function_definition_in_import_sites() -> None:
    names = {"_parse_timestamp", "parse_timestamp"}
    for module in (metrics, couchdb_queue_watcher):
        defined = _defines_function_locally(_module_path(module), names)
        assert defined == set(), f"{module.__name__} still defines {defined} locally"


def test_leaf_module_defines_exactly_one() -> None:
    defined = _defines_function_locally(_module_path(timeparse), {"parse_timestamp"})
    assert defined == {"parse_timestamp"}


def test_leaf_module_imports_only_datetime() -> None:
    tree = ast.parse(_module_path(timeparse).read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            imported_modules.add(node.module or "")
    # only the datetime stdlib module — no project imports (no cycle risk)
    assert imported_modules == {"datetime"}, imported_modules
