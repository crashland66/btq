from __future__ import annotations

import re


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not normalized:
        return []
    parts = [part.strip(" \t\r\n,;:-") for part in re.split(r"[.!?]+", normalized)]
    return [re.sub(r"\s+", " ", part).strip() for part in parts if part.strip()]
