"""INDEPENDENT VERIFIER structural gate for the /candidates header fix.

Contract under test (multi-label-precision, uncommitted on top of f6d6b71):
a large thumbnail batch must no longer collapse the worker's message column.
The header becomes a stacking layout (message block first, thumbnail strip
below), thumbnails are lazy-loaded, and the strip stays a wrapping flex row.

pytest cannot execute browser layout, so this gate is deliberately STRUCTURAL:
it parses the HTML produced by the real render functions and asserts on the
raw CSS text of ``admin.css``. Real-browser measurement was done separately by
the planner.

SANDBOX identity only: site "SANDBOX", submitter "Sandy Sandbox",
capture ids "cap-534-*". No real names, no real sites.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import ops_dashboard.sections.candidates as candidates
from ops_dashboard.sections.candidates import _render_thumb_strip, render_candidate_groups

ADMIN_CSS = Path(__file__).resolve().parents[1] / "ops_dashboard" / "static" / "admin.css"


# ---------------------------------------------------------------------------
# Structural DOM walker (order + nesting + attributes, not string offsets)
# ---------------------------------------------------------------------------

_VOID_TAGS = {"img", "input", "br", "hr", "meta", "link", "source"}


class DomWalker(HTMLParser):
    """Index every element with a document-order sequence number.

    Records, per element: tag, classes, attrs, open/close sequence numbers,
    and the open records of its ancestors at the time it was opened. That is
    enough to assert containment ("the strip is inside the header") and order
    ("the message opens before the strip opens") structurally.
    """

    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True: attr values are unescaped
        self.seq = 0
        self.stack: list[dict] = []
        self.elements: list[dict] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        self.seq += 1
        attr_map: dict[str, str | None] = {}
        for key, value in attrs:
            attr_map.setdefault(key, value)
        record = {
            "tag": tag,
            "classes": (attr_map.get("class") or "").split(),
            "attrs": attr_map,
            "open": self.seq,
            "close": None,
            "ancestors": list(self.stack),
        }
        self.elements.append(record)
        if tag not in _VOID_TAGS:
            self.stack.append(record)

    def handle_startendtag(self, tag, attrs):  # noqa: ANN001
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):  # noqa: ANN001
        self.seq += 1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                for record in self.stack[i:]:
                    if record["close"] is None:
                        record["close"] = self.seq
                del self.stack[i:]
                return


def walk(html_text: str) -> DomWalker:
    walker = DomWalker()
    walker.feed(html_text)
    walker.close()
    return walker


def find_all(walker: DomWalker, *, tag: str | None = None, cls: str | None = None,
             within: dict | None = None) -> list[dict]:
    out = []
    for el in walker.elements:
        if tag is not None and el["tag"] != tag:
            continue
        if cls is not None and cls not in el["classes"]:
            continue
        if within is not None:
            close = within["close"] if within["close"] is not None else float("inf")
            if not (within["open"] < el["open"] < close):
                continue
        out.append(el)
    return out


def find_one(walker: DomWalker, **kwargs) -> dict:
    matches = find_all(walker, **kwargs)
    assert len(matches) == 1, f"expected exactly one match for {kwargs}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Synthetic SANDBOX candidates
# ---------------------------------------------------------------------------


class SyntheticCandidate(dict):
    """Candidate doc whose incidental fields default to empty string.

    ``render_candidate_card`` indexes many fields directly (``candidate[...]``);
    only the fields the contract cares about are set explicitly.
    """

    def __missing__(self, key):  # noqa: ANN001
        return ""


def make_candidate(capture_id: str, *, n: int = 1, source_text: str | None = None,
                   vision_items: list | None = None) -> SyntheticCandidate:
    candidate = SyntheticCandidate()
    candidate.update(
        {
            "capture_id": capture_id,
            "source_text": (
                source_text
                if source_text is not None
                else f"Deep-scrubbed the SANDBOX entry mats, batch {n}."
            ),
            "submitter_name": "Sandy Sandbox",
            "site_id": "SANDBOX",
            "captured_at": "2026-08-18T09:15:00Z",
            "capture_candidate_count": 3,
            "vision_items": vision_items if vision_items is not None else [],
            "candidate_id": f"{capture_id}-cand-{n}",
            "status": "pending_approval",
            "summary": f"Log supply need for SANDBOX (cand {n})",
            "rationale": "Worker reported supplies running low.",
            "draft_id": f"{capture_id}-draft-{n}",
            "_rev": "1-sandboxrev",
            "job_type": "log_supply_need",
        }
    )
    return candidate


def urls_for(capture_id: str, count: int) -> list[str]:
    return [f"/media/{capture_id}/photo-{i:03d}.jpg" for i in range(count)]


def patch_thumbnails(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    monkeypatch.setattr(
        candidates,
        "capture_thumbnails",
        lambda runtime_root, capture_id, **kwargs: mapping.get(capture_id, []),
    )


def render_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, photo_count: int,
               **candidate_kwargs) -> str:
    capture_id = f"cap-534-batch{photo_count:03d}"
    patch_thumbnails(monkeypatch, {capture_id: urls_for(capture_id, photo_count)})
    candidate = make_candidate(capture_id, **candidate_kwargs)
    return render_candidate_groups([candidate], tmp_path)


# ---------------------------------------------------------------------------
# Contract 1 + 6: DOM order inside the header, cards after the header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("photo_count", [1, 13, 32])
def test_message_block_precedes_thumb_strip_in_header(monkeypatch, tmp_path, photo_count):
    """Message, meta, and pending signal all open before .thumb-strip, inside the header."""
    html_text = render_one(monkeypatch, tmp_path, photo_count)
    walker = walk(html_text)
    header = find_one(walker, cls="candidate-group-header")
    message = find_one(walker, cls="candidate-message", within=header)
    meta = find_one(walker, cls="candidate-meta", within=header)
    signal = find_one(walker, cls="capture-candidate-count", within=header)
    strip = find_one(walker, cls="thumb-strip", within=header)
    assert message["open"] < strip["open"], "worker message must precede the thumbnail strip"
    assert meta["open"] < strip["open"], "submitter/site/time meta must precede the strip"
    assert signal["open"] < strip["open"], "pending-count signal must precede the strip"
    assert strip["close"] is not None and strip["close"] < header["close"], (
        "the strip must be fully contained in the header"
    )


@pytest.mark.parametrize("photo_count", [0, 13])
def test_candidate_cards_render_after_header_inside_section(monkeypatch, tmp_path, photo_count):
    """The section keeps id capture-<id> and the action cards follow the closed header."""
    capture_id = f"cap-534-batch{photo_count:03d}"
    patch_thumbnails(monkeypatch, {capture_id: urls_for(capture_id, photo_count)})
    group = [make_candidate(capture_id, n=1), make_candidate(capture_id, n=2)]
    walker = walk(render_candidate_groups(group, tmp_path))
    section = find_one(walker, tag="section", cls="candidate-group")
    assert section["attrs"].get("id") == f"capture-{capture_id}"
    header = find_one(walker, cls="candidate-group-header", within=section)
    articles = find_all(walker, tag="article", within=section)
    assert len(articles) == 2, "one card per candidate in the capture group"
    for article in articles:
        assert article["open"] > header["close"], "cards must come after the closed header"


# ---------------------------------------------------------------------------
# Contract 4 + 5: thumbnail attributes, lightbox anchor, empty batch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("photo_count", [1, 13, 32])
def test_every_thumb_img_is_lazy_async_and_lightbox_wired(monkeypatch, tmp_path, photo_count):
    """Every img: loading=lazy, decoding=async, class candidate-thumb, inside an openLb anchor."""
    html_text = render_one(monkeypatch, tmp_path, photo_count)
    walker = walk(html_text)
    strip = find_one(walker, cls="thumb-strip")
    imgs = find_all(walker, tag="img", within=strip)
    assert len(imgs) == photo_count
    for img in imgs:
        assert img["attrs"].get("loading") == "lazy", f"missing loading=lazy on {img['attrs']}"
        assert img["attrs"].get("decoding") == "async", f"missing decoding=async on {img['attrs']}"
        assert "candidate-thumb" in img["classes"]
        anchor_onclicks = [
            (a["attrs"].get("onclick") or "")
            for a in img["ancestors"]
            if a["tag"] == "a"
        ]
        assert any(oc.startswith("openLb(") for oc in anchor_onclicks), (
            "thumbnail must stay wrapped in the openLb lightbox anchor"
        )


def test_empty_url_list_renders_empty_string():
    assert _render_thumb_strip([]) == ""


def test_zero_photo_capture_renders_header_without_strip(monkeypatch, tmp_path):
    html_text = render_one(monkeypatch, tmp_path, 0)
    walker = walk(html_text)
    header = find_one(walker, cls="candidate-group-header")
    assert find_all(walker, cls="thumb-strip") == [], "no strip div for a photo-less capture"
    find_one(walker, cls="candidate-message", within=header)  # message still renders


# ---------------------------------------------------------------------------
# Contract 2 + 3 + 7: CSS structure (raw text of admin.css)
# ---------------------------------------------------------------------------


def css_rule_body(selector_pattern: str) -> str:
    """Return the declaration body of the single top-level rule matching the pattern."""
    text = ADMIN_CSS.read_text(encoding="utf-8")
    matches = re.findall(rf"(?m)^{selector_pattern}\s*\{{([^}}]*)\}}", text)
    assert len(matches) == 1, f"expected exactly one rule for {selector_pattern!r}, got {len(matches)}"
    return matches[0]


def declarations(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" in chunk:
            prop, _, value = chunk.partition(":")
            out[prop.strip().lower()] = value.strip().lower()
    return out


def test_css_header_is_stacking_not_two_track_grid():
    """.candidate-group-header must not place an auto track beside the message."""
    body = css_rule_body(r"\.candidate-group-header")
    decls = declarations(body)
    assert "minmax" not in body, f"two-track grid survived: {body!r}"
    assert "grid-template-columns" not in decls or " " not in decls.get(
        "grid-template-columns", ""
    ), f"multi-column grid template survived: {body!r}"
    display = decls.get("display", "")
    is_flex_column = display == "flex" and decls.get("flex-direction") == "column"
    is_block = display in {"", "block"} and "grid" not in display
    is_single_col_grid = display == "grid" and " " not in decls.get("grid-template-columns", "1fr")
    assert is_flex_column or is_block or is_single_col_grid, (
        f"header must stack (flex-column, block, or 1-col grid); got {body!r}"
    )


def test_css_thumb_strip_wraps_and_is_bounded():
    body = css_rule_body(r"\.thumb-strip")
    decls = declarations(body)
    assert decls.get("display") == "flex"
    assert decls.get("flex-wrap") == "wrap"
    width = decls.get("width", "")
    assert not re.match(r"^\d+(\.\d+)?(px|rem|em|ch|vw)$", width), (
        f"strip must not carry a fixed width: {width!r}"
    )
    max_width = decls.get("max-width", "")
    if max_width:
        assert max_width in {"100%", "none"}, f"strip max-width must not exceed container: {max_width!r}"


@pytest.mark.parametrize(
    "selector_pattern",
    [
        r"\.candidate-group-header",
        r"\.candidate-group-header\s*>\s*\*",
        r"\.candidate-thumb",
        r"\.thumb-strip",
    ],
)
def test_css_changed_rules_have_no_hardcoded_colors(selector_pattern):
    body = css_rule_body(selector_pattern)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), f"hex color in changed rule: {body!r}"
    assert not re.search(r"\b(rgb|rgba|hsl|hsla)\(", body), f"literal color fn in changed rule: {body!r}"


def test_css_thumb_rule_keeps_theme_tokens():
    body = css_rule_body(r"\.candidate-thumb")
    assert "var(--line)" in body
    assert "var(--pill-bg)" in body


# ---------------------------------------------------------------------------
# Probes beyond the contract
# ---------------------------------------------------------------------------


def test_probe_narrow_media_query_no_longer_targets_header():
    """PROBE: with a flex-column header, the 760px block must not re-grid it.

    Correct current state: .candidate-group-header appears in admin.css only in
    the two stacking rules (the rule itself and its > * child rule) and in no
    media-query grid override.
    """
    text = ADMIN_CSS.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if "candidate-group-header" in line]
    assert len(lines) == 2, f"unexpected extra references: {lines!r}"
    for line in lines:
        assert "grid-template-columns" not in line, f"media-query grid override survived: {line!r}"
    # And no media block re-grids it under another spelling:
    for match in re.finditer(r"@media[^{]*\{", text):
        block_start = match.end()
        depth = 1
        i = block_start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        assert "candidate-group-header" not in text[block_start:i], (
            "a media query still restyles .candidate-group-header"
        )


def test_probe_escapable_message_still_escapes_and_precedes_strip(monkeypatch, tmp_path):
    """PROBE: HTML-hostile worker message stays escaped and stays first."""
    hostile = 'Spill near <b>dock & "bay 3"</b> — do not mix bleach & ammonia'
    html_text = render_one(monkeypatch, tmp_path, 13, source_text=hostile)
    assert "<b>dock" not in html_text, "raw markup leaked from the worker message"
    assert "&lt;b&gt;dock &amp; &quot;bay 3&quot;&lt;/b&gt;" in html_text
    walker = walk(html_text)
    header = find_one(walker, cls="candidate-group-header")
    message = find_one(walker, cls="candidate-message", within=header)
    strip = find_one(walker, cls="thumb-strip", within=header)
    assert message["open"] < strip["open"]


def test_probe_vision_note_stays_before_strip(monkeypatch, tmp_path):
    """PROBE: a capture with a vision summary keeps the note in the message block, pre-strip."""
    vision_items = [
        {
            "status": "described",
            "area_guess": "SANDBOX lobby",
            "description": "Scuffed floor near the entry mats.",
            "warnings": [],
        }
    ]
    html_text = render_one(monkeypatch, tmp_path, 13, vision_items=vision_items)
    walker = walk(html_text)
    header = find_one(walker, cls="candidate-group-header")
    note = find_one(walker, cls="candidate-vision-note", within=header)
    strip = find_one(walker, cls="thumb-strip", within=header)
    assert note["open"] < strip["open"], "vision note must precede the thumbnail strip"


def test_probe_multiple_groups_keep_distinct_ids_and_own_strips(monkeypatch, tmp_path):
    """PROBE: two captures render two sections, each with its own correctly-sized strip."""
    mapping = {
        "cap-534-alpha": urls_for("cap-534-alpha", 13),
        "cap-534-beta": urls_for("cap-534-beta", 2),
    }
    patch_thumbnails(monkeypatch, mapping)
    group = [make_candidate("cap-534-alpha"), make_candidate("cap-534-beta", n=2)]
    walker = walk(render_candidate_groups(group, tmp_path))
    sections = find_all(walker, tag="section", cls="candidate-group")
    ids = [s["attrs"].get("id") for s in sections]
    assert ids == ["capture-cap-534-alpha", "capture-cap-534-beta"]
    for section, capture_id in zip(sections, ["cap-534-alpha", "cap-534-beta"]):
        header = find_one(walker, cls="candidate-group-header", within=section)
        strip = find_one(walker, cls="thumb-strip", within=header)
        imgs = find_all(walker, tag="img", within=strip)
        assert len(imgs) == len(mapping[capture_id])
        assert all(img["attrs"].get("src", "").startswith(f"/media/{capture_id}/") for img in imgs)


def test_probe_hostile_thumbnail_url_round_trips_escaped(monkeypatch, tmp_path):
    """PROBE: a URL with quotes/ampersands is attribute-escaped, not truncated."""
    hostile_url = '/media/cap-534-q/photo "one".jpg?size=big&x=1'
    html_text = _render_thumb_strip([hostile_url])
    assert 'src="/media/cap-534-q/photo "one"' not in html_text  # raw quote must not split the attr
    walker = walk(html_text)
    img = find_one(walker, tag="img")
    # convert_charrefs unescapes attribute values: a clean round trip proves escaping
    assert img["attrs"].get("src") == hostile_url
    assert img["attrs"].get("loading") == "lazy"
    assert img["attrs"].get("decoding") == "async"


def test_probe_swipe_details_path_gets_same_thumb_markup():
    """PROBE (contract 8): render_candidate_vision's strip is lazy + lightbox-wired too."""
    vision_items = [
        {
            "status": "described",
            "photo_asset_id": "asset-534-001",
            "area_guess": "SANDBOX break room",
            "description": "Counter wiped, trash out.",
            "image_media_url": "/media/cap-534-swipe/photo-000.jpg",
            "visible_objects": ["counter"],
            "possible_conditions": [],
            "possible_issues": [],
            "warnings": [],
            "model_name": "sandbox-model",
            "confidence": "0.9",
        }
    ]
    html_text = candidates.render_candidate_vision(vision_items)
    walker = walk(html_text)
    strip = find_one(walker, cls="thumb-strip")
    img = find_one(walker, tag="img", within=strip)
    assert img["attrs"].get("loading") == "lazy"
    assert img["attrs"].get("decoding") == "async"
    assert "candidate-thumb" in img["classes"]
    anchor_onclicks = [
        (a["attrs"].get("onclick") or "") for a in img["ancestors"] if a["tag"] == "a"
    ]
    assert any(oc.startswith("openLb(") for oc in anchor_onclicks)
