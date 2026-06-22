from __future__ import annotations

import ast
import pathlib

import pytest

from field_capture import site_status_export, site_viewer, visual_context
from field_capture.visual_context import safe_visual_context


# --- Security: every unsafe marker blanks the output -------------------------

UNSAFE_MARKERS = (
    "bearer ",
    "field_capture_token",
    "fct_",
    "auth",
    "/users/",
    "/srv/",
    "/var/",
    "\\users\\",
    "source_image_path",
    "queue",
)


@pytest.mark.parametrize("marker", UNSAFE_MARKERS)
def test_unsafe_marker_blanks_output(marker):
    # Marker embedded in otherwise-clean text must produce "".
    text = f"A clean looking sentence with {marker} hidden inside it."
    assert safe_visual_context(text) == ""


@pytest.mark.parametrize("marker", UNSAFE_MARKERS)
def test_unsafe_marker_alone_blanks_output(marker):
    # The marker followed by token-like content must blank. Note: the matching
    # happens AFTER whitespace-collapse, so "bearer " (trailing space) only
    # triggers when followed by another word -- which is exactly how it appears
    # in real auth strings ("Bearer eyJ..."). We append a token so every marker,
    # including the space-suffixed ones, is exercised in realistic form.
    assert safe_visual_context(marker + "x") == ""


@pytest.mark.parametrize(
    "text",
    [
        "AUTH failure detected",
        "Bearer eyJhbGciOiJ",
        "FIELD_CAPTURE_TOKEN=abc",
        "FCT_secrettoken",
        "/USERS/greg/photo.jpg",
        "SOURCE_IMAGE_PATH=/x",
        "QUEUE depth high",
    ],
)
def test_case_insensitive_markers_blank(text):
    # Function lowercases before matching, so uppercase markers must also blank.
    assert safe_visual_context(text) == ""


# --- Empty / None / whitespace ----------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n  ", []])
def test_empty_like_values_return_empty_string(value):
    assert safe_visual_context(value) == ""


def test_internal_whitespace_collapsed():
    assert safe_visual_context("a   b") == "a b"
    assert safe_visual_context("  hello \t world \n again  ") == "hello world again"


# --- Clean passthrough -------------------------------------------------------


def test_clean_string_passthrough_unchanged():
    text = "A spotless lobby floor freshly mopped near the entrance."
    assert safe_visual_context(text) == text


# --- Truncation boundary -----------------------------------------------------


def test_exactly_limit_returned_unchanged():
    text = "x" * 520
    assert safe_visual_context(text) == text
    assert len(safe_visual_context(text)) == 520


def test_limit_plus_one_truncated():
    text = "x" * 521
    result = safe_visual_context(text)
    # limit-3 = 517 chars + "..." = 520 chars total
    assert result == "x" * 517 + "..."
    assert len(result) == 520


def test_truncation_rstrips_before_ellipsis():
    # Char at index limit-3 boundary is a space -> rstripped before "...".
    text = "y" * 516 + " " + "z" * 50  # length 567, well over 520
    result = safe_visual_context(text)
    # First 517 chars = 516 y's + one space; rstrip removes the trailing space.
    assert result == "y" * 516 + "..."


def test_custom_limit_honored():
    text = "abcdefghij"  # 10 chars
    # limit=5: 5 < 10, so truncate to limit-3=2 chars + "..."
    assert safe_visual_context(text, limit=5) == "ab..."
    # limit exactly equal to length -> unchanged
    assert safe_visual_context(text, limit=10) == text
    # generous limit -> unchanged
    assert safe_visual_context(text, limit=520) == text


def test_custom_small_limit_clamps_negative():
    # limit < 3 -> max(0, limit-3) == 0, so just "..."
    assert safe_visual_context("abcdef", limit=2) == "..."


# --- Single-definition guard -------------------------------------------------


def test_single_shared_definition_identity():
    # All three module-level names must be the exact same function object.
    assert site_viewer.safe_visual_context is visual_context.safe_visual_context
    assert site_status_export.safe_visual_context is visual_context.safe_visual_context
    assert safe_visual_context is visual_context.safe_visual_context


@pytest.mark.parametrize(
    "module_filename",
    ["site_viewer.py", "site_status_export.py"],
)
def test_no_local_def_in_section_files(module_filename):
    # AST-confirm neither consumer module still defines safe_visual_context locally.
    src_dir = pathlib.Path(visual_context.__file__).parent
    tree = ast.parse((src_dir / module_filename).read_text())
    local_defs = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "safe_visual_context"
    ]
    assert local_defs == [], f"{module_filename} still defines safe_visual_context locally"


def test_visual_context_module_is_a_leaf():
    # The shared module must not import the consumer modules (no cycle).
    src = pathlib.Path(visual_context.__file__).read_text()
    assert "site_viewer" not in src
    assert "site_status_export" not in src
