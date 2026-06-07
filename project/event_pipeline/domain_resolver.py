from __future__ import annotations

import re

_CONTEXT_KEYWORDS = {"tile", "tiles", "floor", "floors", "flooring"}


def _append_correction(corrections: list[dict[str, str]], raw_value: str, replacement: str) -> None:
    normalized_raw = raw_value.strip()
    if not normalized_raw or normalized_raw == replacement:
        return
    candidate = {"from": normalized_raw, "to": replacement}
    if candidate not in corrections:
        corrections.append(candidate)


def _normalize_bct_context(text: str, corrections: list[dict[str, str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        start, end = match.span()
        window_start = max(0, start - 30)
        window_end = min(len(text), end + 30)
        context = text[window_start:window_end].lower()
        if any(keyword in context for keyword in _CONTEXT_KEYWORDS):
            _append_correction(corrections, match.group(0), "VCT")
            return "VCT"
        return match.group(0)

    return re.sub(r"\bbct\b", replace, text, flags=re.IGNORECASE)


def normalize_text(text: str) -> tuple[str, list[dict[str, str]]]:
    normalized = text
    corrections: list[dict[str, str]] = []
    normalized = _normalize_bct_context(normalized, corrections)

    replacements = [
        (r"\bvinyl composition tile\b", "VCT"),
        (r"\bv\s*c\s*t\b", "VCT"),
        (r"\bvct\b", "VCT"),
        (r"\bvinyl tile\b", "VCT"),
        (r"\bluxury vinyl plank\b", "LVP"),
        (r"\bvinyl plank\b", "LVP"),
        (r"\blvp\b", "LVP"),
    ]

    for pattern, replacement in replacements:
        def replace(match: re.Match[str], canonical: str = replacement) -> str:
            _append_correction(corrections, match.group(0), canonical)
            return canonical

        normalized = re.sub(pattern, replace, normalized, flags=re.IGNORECASE)

    return normalized, corrections
