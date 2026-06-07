from __future__ import annotations

import hashlib


def deterministic_artifact_id(prefix: str, *parts: object, digest_chars: int = 24) -> str:
    digest = hashlib.sha256("\n".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:digest_chars]}"

