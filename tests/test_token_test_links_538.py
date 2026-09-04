"""538 — Tokens table links the Token ID to the public capture app.

Independent-verifier gates for the uncommitted change in
``ops_dashboard/sections/tokens.py``: ``public_capture_origin()``,
``capture_test_url()`` and the ``render_token_id_cell`` test link.

Sandbox identities only; the only origin used is ``capture.example.com``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest

from field_capture.auth import TokenStore
from ops_dashboard.sections import tokens
from tests.test_ops_dashboard import request_text


ORIGIN = "https://capture.example.com"
ENV = "BTQ_PUBLIC_CAPTURE_ORIGIN"

SANDBOX_ROSTER: list[dict[str, object]] = [
    {"_id": "employee_sandbox_alice", "type": "employee", "status": "active", "person_id": "per_alice", "first": "Sam", "last": "Sandbox", "job": "SANDBOX"},
]


@pytest.fixture(autouse=True)
def _sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_TOKEN_SYNC_DISABLED", "1")
    monkeypatch.setattr(tokens, "load_employees", lambda: [dict(doc) for doc in SANDBOX_ROSTER])
    # Deterministic person-name enrichment (no CouchDB reach-out from tests).
    monkeypatch.setattr(tokens, "person_name_map", lambda: {"per_alice": "Sam Sandbox"})
    tokens.RAW_TOKEN_FLASH.clear()


def _store(runtime_root: Path) -> TokenStore:
    return TokenStore(runtime_root / "field_capture_tokens.sqlite3")


def _create(runtime_root: Path, **kwargs: object):
    return _store(runtime_root).create_token(person_id="per_alice", label=str(kwargs.pop("label", "Sandbox phone")), **kwargs)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "token_id": "fct_synthetic_row_0001",
        "token_value": "fc_synthetic&x=1",
        "person_id": "per_alice",
        "person_name": "Sam Sandbox",
        "revoked": False,
    }
    base.update(overrides)
    return base


def _cell(body: str, token_id: str) -> str:
    """Return the <td>…</td> of the Token ID column for the given row."""
    marker = f'<code title="{token_id}">'
    idx = body.index(marker)
    start = body.rfind("<td", 0, idx)
    end = body.index("</td>", idx)
    return body[start:end]


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str | None]] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "a":
            self.anchors.append(dict(attrs))


def _parse(fragment: str) -> _Collector:
    collector = _Collector()
    collector.feed(fragment)
    return collector


def _test_links(fragment: str) -> list[dict[str, str | None]]:
    return [a for a in _parse(fragment).anchors if (a.get("class") or "") == "token-test-link"]


# --- 9. helpers -------------------------------------------------------------


def test_public_capture_origin_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert tokens.public_capture_origin() == ""
    monkeypatch.setenv(ENV, "")
    assert tokens.public_capture_origin() == ""
    monkeypatch.setenv(ENV, "   ")
    assert tokens.public_capture_origin() == ""
    monkeypatch.setenv(ENV, f"  {ORIGIN}/  ")
    assert tokens.public_capture_origin() == ORIGIN
    monkeypatch.setenv(ENV, f"{ORIGIN}///")
    assert tokens.public_capture_origin() == ORIGIN
    monkeypatch.setenv(ENV, f"{ORIGIN}/app/")
    assert tokens.public_capture_origin() == f"{ORIGIN}/app"
    # no reload needed — a second change is observed on the next call
    monkeypatch.setenv(ENV, "https://other.example.com")
    assert tokens.public_capture_origin() == "https://other.example.com"


def test_capture_test_url_empty_when_origin_or_raw_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    assert tokens.capture_test_url("fc_synthetic") == ""
    monkeypatch.setenv(ENV, ORIGIN)
    assert tokens.capture_test_url("") == ""


def test_capture_test_url_encodes_raw_with_safe_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, f"{ORIGIN}/")
    url = tokens.capture_test_url("fc_synthetic&x=1")
    assert url == f"{ORIGIN}/?token=fc_synthetic%26x%3D1"
    assert url.count("?") == 1
    # '/' and unicode are percent-encoded too (safe='')
    assert tokens.capture_test_url("fc_a/b") == f"{ORIGIN}/?token=fc_a%2Fb"
    assert tokens.capture_test_url("fc_ünï") == f"{ORIGIN}/?token=fc_%C3%BCn%C3%AF"
    # origin with a path suffix keeps the path, single '?'
    monkeypatch.setenv(ENV, f"{ORIGIN}/app/")
    url = tokens.capture_test_url("fc_synthetic")
    assert url == f"{ORIGIN}/app/?token=fc_synthetic"
    assert url.count("?") == 1


# --- 1. cell with env set ----------------------------------------------------


def test_cell_links_token_id_to_capture_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, f"{ORIGIN}/")
    row = _row()
    cell = tokens.render_token_id_cell(row["token_id"], row)

    links = _test_links(cell)
    assert len(links) == 1
    link = links[0]
    assert link["href"] == f"{ORIGIN}/?token=fc_synthetic%26x%3D1"
    assert link["target"] == "_blank"
    assert link["rel"] == "noopener noreferrer"
    for attr in ("title", "aria-label"):
        text = link.get(attr) or ""
        assert "Sam Sandbox" in text
        assert "Field Capture" in text
    # visible text is still the short id inside <code>, nested in the <a>
    short = "fct_synthetic_row_0001"[:12] + "..."
    assert short == "fct_syntheti..."
    assert re.search(r'<a [^>]*>\s*<code title="fct_synthetic_row_0001">fct_syntheti\.\.\.</code>\s*</a>', cell)
    assert short in cell
    # Copy button unchanged
    assert 'data-copy-value="fc_synthetic&amp;x=1"' in cell
    assert "test link unavailable" not in cell


def test_cell_title_falls_back_to_person_id_when_name_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    row = _row(person_name="")
    cell = tokens.render_token_id_cell(row["token_id"], row)
    link = _test_links(cell)[0]
    assert "per_alice" in (link["title"] or "")
    assert "per_alice" in (link["aria-label"] or "")


def test_cell_short_id_only_for_long_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    row = _row(token_id="fct_short")
    cell = tokens.render_token_id_cell("fct_short", row)
    assert '<code title="fct_short">fct_short</code>' in cell
    assert len(_test_links(cell)) == 1


# --- 2. env unset ------------------------------------------------------------


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_cell_without_origin_has_no_link_and_shows_unavailable_marker(monkeypatch: pytest.MonkeyPatch, env_value: str | None) -> None:
    if env_value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, env_value)
    row = _row()
    cell = tokens.render_token_id_cell(row["token_id"], row)

    assert "<a" not in cell
    assert 'data-copy-value="fc_synthetic&amp;x=1"' in cell
    assert "test link unavailable" in cell
    marker = re.search(r'<span class="muted" title="([^"]*)">test link unavailable</span>', cell)
    assert marker is not None
    assert ENV in marker.group(1)
    assert '<code title="fct_synthetic_row_0001">fct_syntheti...</code>' in cell


# --- 3. revoked --------------------------------------------------------------


def test_revoked_row_has_plain_code_no_link_no_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_value in (ORIGIN, None):
        if env_value is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, env_value)
        row = _row(revoked=True)
        cell = tokens.render_token_id_cell(row["token_id"], row)
        assert "<a" not in cell
        assert "test link unavailable" not in cell
        assert cell.startswith('<code title="fct_synthetic_row_0001">fct_syntheti...</code>')
        assert 'data-copy-value="fc_synthetic&amp;x=1"' in cell


def test_revoked_row_in_list_has_no_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    runtime_root = tmp_path / "runtime"
    created = _create(runtime_root)
    _store(runtime_root).revoke_token(created.record.token_id)

    body = request_text("GET", "/tokens?revoked=all", runtime_root)[2]
    cell = _cell(body, created.record.token_id)
    assert "token-test-link" not in cell
    assert "<a" not in cell
    assert "test link unavailable" not in cell


# --- 4. no raw value ---------------------------------------------------------


def test_row_without_raw_value_has_set_link_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_value in (ORIGIN, None):
        if env_value is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, env_value)
        for raw in ("", None):
            row = _row(token_value=raw)
            cell = tokens.render_token_id_cell(row["token_id"], row)
            assert "token-test-link" not in cell
            assert "test link unavailable" not in cell
            assert "data-copy-value" not in cell
            assert 'href="/tokens/set-raw?token_id=fct_synthetic_row_0001"' in cell
            assert "Set..." in cell
            anchors = _parse(cell).anchors
            assert len(anchors) == 1 and anchors[0]["href"] == "/tokens/set-raw?token_id=fct_synthetic_row_0001"


def test_list_row_with_null_token_value_has_set_link_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    runtime_root = tmp_path / "runtime"
    created = _create(runtime_root)
    store = _store(runtime_root)
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (created.record.token_id,))

    body = request_text("GET", "/tokens", runtime_root)[2]
    cell = _cell(body, created.record.token_id)
    assert "token-test-link" not in cell
    assert "test link unavailable" not in cell
    assert "data-copy-value" not in cell
    assert f'href="/tokens/set-raw?token_id={created.record.token_id}"' in cell
    assert "Set..." in cell


# --- 5. list, compact and all ------------------------------------------------


@pytest.mark.parametrize("columns", ["compact", "all"])
def test_list_renders_test_link_in_both_column_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, columns: str) -> None:
    monkeypatch.setenv(ENV, f"{ORIGIN}/")
    runtime_root = tmp_path / "runtime"
    created = _create(runtime_root)

    status, _ct, body = request_text("GET", f"/tokens?columns={columns}", runtime_root)
    assert status == 200
    cell = _cell(body, created.record.token_id)
    links = _test_links(cell)
    assert len(links) == 1
    assert links[0]["href"] == f"{ORIGIN}/?token={created.token_value}"
    assert links[0]["target"] == "_blank"
    assert links[0]["rel"] == "noopener noreferrer"
    assert "Sam Sandbox" in (links[0]["title"] or "")
    assert f'data-copy-value="{created.token_value}"' in cell
    assert created.record.token_id[:12] in cell
    # the rest of the table is intact
    assert 'class="data-table"' in body
    assert "col-sticky-right" in body
    assert 'action="/tokens/revoke"' in body


def test_list_without_origin_marks_unavailable_and_keeps_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    runtime_root = tmp_path / "runtime"
    created = _create(runtime_root)

    body = request_text("GET", "/tokens", runtime_root)[2]
    cell = _cell(body, created.record.token_id)
    assert "<a" not in cell
    assert "test link unavailable" in cell
    assert f'data-copy-value="{created.token_value}"' in cell
    headers = re.findall(r"<th[^>]*>([^<]+)</th>", body)
    assert headers == ["Token ID", "Person", "Role", "Site Scope", "Active", "Actions"]


# --- 6. secrets placement ----------------------------------------------------


def test_href_never_carries_token_id_or_hash_and_titles_never_carry_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    runtime_root = tmp_path / "runtime"
    created_a = _create(runtime_root, label="A")
    created_b = _create(runtime_root, label="B")
    records = {r.token_id: r for r in _store(runtime_root).list_tokens()}

    body = request_text("GET", "/tokens?columns=all", runtime_root)[2]
    links = _test_links(body)
    assert len(links) == 2
    for created in (created_a, created_b):
        record = records[created.record.token_id]
        assert record.token_hash
        for link in links:
            href = link["href"] or ""
            assert created.record.token_id not in href
            assert record.token_hash not in href
            for attr in ("title", "aria-label"):
                assert created.token_value not in (link.get(attr) or "")
        # raw value appears only in the href and the Copy data attribute
        cell = _cell(body, created.record.token_id)
        occurrences = [m.start() for m in re.finditer(re.escape(created.token_value), cell)]
        assert len(occurrences) == 2
        assert cell.count(f'href="{ORIGIN}/?token={created.token_value}"') == 1
        assert cell.count(f'data-copy-value="{created.token_value}"') == 1


# --- 7. escaping -------------------------------------------------------------


def test_raw_value_with_quote_lt_amp_cannot_break_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    raw = 'fc_"><script>alert(1)</script><&'
    row = _row(token_value=raw)
    cell = tokens.render_token_id_cell(row["token_id"], row)

    parsed = _parse(cell)
    assert "script" not in parsed.tags
    assert "<script" not in cell
    link = _test_links(cell)[0]
    # href round-trips to the exact percent-encoded URL
    assert link["href"] == f"{ORIGIN}/?token=fc_%22%3E%3Cscript%3Ealert%281%29%3C%2Fscript%3E%3C%26"
    assert '"' not in (link["href"] or "")
    # data-copy-value round-trips to the raw value (escaped in source)
    assert f'data-copy-value="{tokens.html.escape(raw, quote=True)}"' in cell
    assert 'data-copy-value="fc_">' not in cell
    # tags present are exactly what the cell is supposed to emit
    assert set(parsed.tags) == {"a", "code", "button"}


def test_origin_with_quote_and_script_cannot_break_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, f'{ORIGIN}/"><script>alert(1)</script>')
    row = _row(token_value="fc_synthetic")
    cell = tokens.render_token_id_cell(row["token_id"], row)

    parsed = _parse(cell)
    assert "script" not in parsed.tags
    assert "<script" not in cell
    assert set(parsed.tags) == {"a", "code", "button"}
    link = _test_links(cell)[0]
    # the poisoned origin lands entirely inside the href attribute value
    assert link["href"] == f'{ORIGIN}/"><script>alert(1)</script>/?token=fc_synthetic'
    assert "&quot;&gt;&lt;script&gt;" in cell


def test_person_name_with_quote_and_lt_is_escaped_in_title(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    row = _row(person_name='Sam "<b>" Sandbox')
    cell = tokens.render_token_id_cell(row["token_id"], row)
    parsed = _parse(cell)
    assert "b" not in parsed.tags
    link = _test_links(cell)[0]
    assert 'Sam "<b>" Sandbox' in (link["title"] or "")
    assert "&quot;&lt;b&gt;&quot;" in cell


# --- 8. expired but unrevoked ------------------------------------------------


def test_expired_unrevoked_row_keeps_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, ORIGIN)
    runtime_root = tmp_path / "runtime"
    created = _create(runtime_root, expires_at=datetime.now(timezone.utc) - timedelta(days=2))
    assert created.record.expires_at
    assert not created.record.revoked

    body = request_text("GET", "/tokens?columns=all", runtime_root)[2]
    cell = _cell(body, created.record.token_id)
    links = _test_links(cell)
    assert len(links) == 1
    assert links[0]["href"] == f"{ORIGIN}/?token={created.token_value}"
    assert ">Expires At</th>" in body


# --- 10. no mutation ---------------------------------------------------------


def test_rendering_list_does_not_mutate_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_root = tmp_path / "runtime"
    active = _create(runtime_root, label="Active")
    revoked = _create(runtime_root, label="Revoked")
    legacy = _create(runtime_root, label="Legacy")
    store = _store(runtime_root)
    store.revoke_token(revoked.record.token_id)
    with store.connect() as connection:
        connection.execute("UPDATE field_capture_tokens SET token_value = NULL WHERE token_id = ?", (legacy.record.token_id,))
    before = store.list_tokens()
    assert {r.token_id: r.token_value for r in before} == {
        active.record.token_id: active.token_value,
        revoked.record.token_id: revoked.token_value,
        legacy.record.token_id: None,
    }

    for env_value in (ORIGIN, None):
        if env_value is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, env_value)
        for path in ("/tokens", "/tokens?columns=all", "/tokens?revoked=all&columns=all"):
            assert request_text("GET", path, runtime_root)[0] == 200

    after = store.list_tokens()
    assert after == before  # frozen dataclasses: full field equality incl. revoked/token_value/token_hash/last_used_at
    for old, new in zip(before, after):
        assert (old.revoked, old.token_value, old.token_hash, old.last_used_at) == (new.revoked, new.token_value, new.token_hash, new.last_used_at)


# --- 11. sanitization gate ---------------------------------------------------


def test_no_real_hostname_in_test_or_source() -> None:
    forbidden = "greg" + "stoltz"
    here = Path(__file__).resolve()
    source = Path(tokens.__file__).resolve()
    assert forbidden not in here.read_text(encoding="utf-8")
    assert forbidden not in source.read_text(encoding="utf-8")
    # origin comes from env only: no default host baked into the helper
    assert tokens.PUBLIC_CAPTURE_ORIGIN_ENV == ENV
    import inspect

    helper_src = inspect.getsource(tokens.public_capture_origin)
    assert 'os.environ.get(PUBLIC_CAPTURE_ORIGIN_ENV, "")' in helper_src
    assert "http" not in helper_src
