from __future__ import annotations

import re


REVIEW_H2   = re.compile(r'^## Field Capture Reviews?\s*$')          # plural AND singular
REVIEW_H3   = re.compile(r'^### Field Capture Review - ')
# the ONLY lines that belong to a review block body — keep this precise so non-review
# content interleaved between review blocks is NOT swallowed:
REVIEW_BODY = re.compile(
    r'^(- (field_capture_timestamp|site_id|area|phase|capture_id|upload_id|audio_asset_id|reviewer)\s*:'
    r'|Summary\s*:|Review rationale\s*:|Reviewed context\s*:|Candidate label\s*:'
    r'|Source semantic artifact\s*:|Source transcript artifact\s*:)')
PATH_LINE   = re.compile(r'^\s*(Source semantic artifact|Source transcript artifact|raw_transcript)\s*:\s*/')
def _is_review_head(l): return bool(REVIEW_H2.match(l) or REVIEW_H3.match(l))


def strip_review_blocks(content: str) -> str:
    lines = content.split("\n"); out = []; i = 0; n = len(lines)
    while i < n:
        if _is_review_head(lines[i]):
            # drop the separator we already emitted for this block
            while out and out[-1].strip() in ("", "---"): out.pop()
            i += 1
            # consume the review-block body ONLY: blank lines, review headings, and the
            # known review-body field lines. STOP at the first line that is none of those
            # (e.g. a `---` frontmatter open, a visit_gap block, operator prose) — do NOT
            # consume it. This is what prevents swallowing interleaved operator content.
            while i < n and (lines[i].strip() == "" or _is_review_head(lines[i]) or REVIEW_BODY.match(lines[i])):
                i += 1
            continue
        out.append(lines[i]); i += 1
    # strip any path-leak lines that survive inside PRESERVED entries (voice-memo raw_transcript, etc.)
    out = [l for l in out if not PATH_LINE.match(l)]
    return re.sub(r'\n{3,}', '\n\n', "\n".join(out)).rstrip() + "\n"
