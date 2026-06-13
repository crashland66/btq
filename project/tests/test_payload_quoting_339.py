"""Independent verifier tests for BTQ prompt 339 (job-first review polish).

Two review surfaces (the unified-capture PWA approval inbox and the ops-dashboard
swipe card) render a job's payload as a small key/value table via a client-side
JS ``payloadRows`` helper. Before 339, a list-valued payload field (e.g.
``related_capture_ids: ["cap-…", …]``) was passed through ``JSON.stringify`` and
then HTML-escaped, so the operator saw the ugly raw-JSON literal
``[&quot;cap-…&quot;]``. 339 special-cases arrays to a human ``", "``-join.

The render is **client-side JavaScript**, so these tests do not assert on the JS
*source text* — they EXECUTE the real ``payloadRows`` (plus its ``esc`` helper)
with ``node`` against representative payloads and assert on the rendered ``<td>``
HTML. The two helpers are extracted verbatim from the shipped sources:

  * ``inbox.js``           — read from disk, double-quote JS style.
  * ``swipe.py`` ``_SWIPE_SCRIPT`` — the embedded JS string, single-quote style.

This proves both surfaces actually produce ``cap-a, cap-b`` (no ``[``/``]``/quote
characters around the list) for the SAME input, and that the scalar / non-array
object fallbacks are intact.

If ``node`` is not on PATH the JS-execution tests skip LOUDLY (pytest.skip) — they
never silently pass. The version-bump assertions are pure-Python string checks and
always run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PROJECT_DIR = Path(__file__).resolve().parents[1]
_PUBLIC_DIR = _PROJECT_DIR / "unified_capture" / "public"
_INBOX_JS = _PUBLIC_DIR / "inbox.js"
_APP_JS = _PUBLIC_DIR / "app.js"
_INDEX_HTML = _PUBLIC_DIR / "index.html"

_NODE = shutil.which("node")


# --------------------------------------------------------------------------- #
# JS extraction + execution helpers
# --------------------------------------------------------------------------- #
def _extract_fn(source: str, name: str) -> str:
    """Return the source text of ``function <name>(...) { ... }`` from ``source``.

    Brace-matched so it survives the nested ``map(function (k) { ... })`` body.
    """
    anchor = re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)
    assert anchor, f"function {name} not found in source"
    i = source.index("{", anchor.end() - 1)
    depth = 0
    for j in range(i, len(source)):
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[anchor.start() : j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _swipe_script() -> str:
    """The embedded JS string the swipe surface ships to the browser."""
    from ops_dashboard.sections import swipe

    return swipe._SWIPE_SCRIPT


def _run_payload_rows(esc_src: str, rows_src: str, rows_fn: str, payload: dict) -> str:
    """Execute the real ``esc`` + ``payloadRows`` against ``payload`` via node."""
    harness = (
        esc_src
        + "\n"
        + rows_src
        + "\n"
        + "const __p = JSON.parse(process.argv[1]);\n"
        + f"process.stdout.write({rows_fn}(__p));\n"
    )
    proc = subprocess.run(
        [_NODE, "-e", harness, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout


def _td_for(html: str, key: str) -> str:
    """Pull the rendered ``<td>…</td>`` body for the row whose ``<th>`` is ``key``."""
    m = re.search(
        r"<th>" + re.escape(key) + r"</th><td>(.*?)</td>", html, re.S
    )
    assert m, f"row for key {key!r} not found in: {html!r}"
    return m.group(1)


# --------------------------------------------------------------------------- #
# inbox.js payloadRows (double-quote JS)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def inbox_helpers() -> tuple[str, str]:
    src = _INBOX_JS.read_text(encoding="utf-8")
    return _extract_fn(src, "esc"), _extract_fn(src, "payloadRows")


def _inbox_render(inbox_helpers, payload: dict) -> str:
    if not _NODE:
        pytest.skip("node not on PATH — cannot execute client-side payloadRows")
    esc_src, rows_src = inbox_helpers
    return _run_payload_rows(esc_src, rows_src, "payloadRows", payload)


def test_inbox_array_field_renders_comma_joined_no_brackets(inbox_helpers):
    payload = {"related_capture_ids": ["cap-unified-abc", "cap-xyz"]}
    html = _inbox_render(inbox_helpers, payload)
    td = _td_for(html, "related_capture_ids")
    assert td == "cap-unified-abc, cap-xyz"
    # No raw-JSON list syntax anywhere in the rendered row.
    for bad in ("[", "]", '"', "&quot;"):
        assert bad not in td, f"unexpected {bad!r} in inbox array render: {td!r}"


def test_inbox_single_element_array_renders_bare_value(inbox_helpers):
    html = _inbox_render(inbox_helpers, {"ids": ["only-one"]})
    assert _td_for(html, "ids") == "only-one"


def test_inbox_scalar_string_unchanged(inbox_helpers):
    html = _inbox_render(inbox_helpers, {"path": "Accounts/7050.md"})
    assert _td_for(html, "path") == "Accounts/7050.md"


def test_inbox_non_array_object_still_json_stringified(inbox_helpers):
    # Fallback intact: a plain (non-array) object must still JSON.stringify,
    # so the brace/quote literal IS expected here (escaped by esc()).
    html = _inbox_render(inbox_helpers, {"meta": {"k": "v"}})
    td = _td_for(html, "meta")
    assert td == "{&quot;k&quot;:&quot;v&quot;}"


# --------------------------------------------------------------------------- #
# swipe.py embedded payloadRows (single-quote JS)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def swipe_helpers() -> tuple[str, str]:
    # The swipe ``payloadRows`` now humanizes the row key via a ``humanizeKey``
    # helper (prompt 364). ``payloadRows`` references it at call time, so the
    # extracted execution snippet must include it or node throws a ReferenceError.
    # ``humanizeKey`` only changes the <th> label text; the <td> *value*-rendering
    # contract this file protects (arrays comma-joined, no brackets) is unchanged.
    src = _swipe_script()
    esc_src = _extract_fn(src, "esc")
    rows_src = _extract_fn(src, "payloadRows") + "\n" + _extract_fn(src, "humanizeKey")
    return esc_src, rows_src


def _swipe_render(swipe_helpers, payload: dict) -> str:
    if not _NODE:
        pytest.skip("node not on PATH — cannot execute client-side payloadRows")
    esc_src, rows_src = swipe_helpers
    return _run_payload_rows(esc_src, rows_src, "payloadRows", payload)


def test_swipe_array_field_renders_comma_joined_no_brackets(swipe_helpers):
    payload = {"related_capture_ids": ["cap-unified-abc", "cap-xyz"]}
    html = _swipe_render(swipe_helpers, payload)
    # The <th> is now the humanized key ("Related capture ids"); the value
    # rendering is what this test protects and must be unchanged.
    td = _td_for(html, "Related capture ids")
    assert td == "cap-unified-abc, cap-xyz"
    for bad in ("[", "]", '"', "&quot;"):
        assert bad not in td, f"unexpected {bad!r} in swipe array render: {td!r}"


def test_swipe_single_element_array_renders_bare_value(swipe_helpers):
    html = _swipe_render(swipe_helpers, {"ids": ["only-one"]})
    assert _td_for(html, "Ids") == "only-one"


def test_swipe_scalar_string_unchanged(swipe_helpers):
    html = _swipe_render(swipe_helpers, {"path": "Accounts/7050.md"})
    assert _td_for(html, "Path") == "Accounts/7050.md"


def test_swipe_non_array_object_still_json_stringified(swipe_helpers):
    html = _swipe_render(swipe_helpers, {"meta": {"k": "v"}})
    assert _td_for(html, "Meta") == "{&quot;k&quot;:&quot;v&quot;}"


# --------------------------------------------------------------------------- #
# Version bump (pure-Python string assertions — always run)
# --------------------------------------------------------------------------- #
def test_app_js_interface_version_bumped_and_history_preserved():
    src = _APP_JS.read_text(encoding="utf-8")
    assert 'INTERFACE_VERSION = "2026.06.12-offline"' in src
    # Old value retained in the history comment (not silently dropped).
    assert '"2026.06.12-blob-persist"' in src
    # The current constant is exactly the new string (no stale active value).
    m = re.search(r'INTERFACE_VERSION\s*=\s*"([^"]+)"', src)
    assert m and m.group(1) == "2026.06.12-offline"
    # Pipeline version is unchanged.
    assert 'PIPELINE_VERSION = "unified-capture-intake-v2"' in src


def test_index_html_interface_version_matches():
    src = _INDEX_HTML.read_text(encoding="utf-8")
    assert (
        '<span id="interfaceVersion">2026.06.12-offline</span>' in src
    )
    assert '<span id="pipelineVersion">unified-capture-intake-v2</span>' in src
