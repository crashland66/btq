from __future__ import annotations


def first_name_from_canonical(canonical_name: str) -> str:
    name = canonical_name.strip()
    if not name:
        return ""
    if "," in name:
        parts = name.split(",", 1)
        remainder = parts[1].strip()
        if remainder:
            return remainder.split()[0]
        return ""
    tokens = name.split()
    return tokens[0] if tokens else ""
