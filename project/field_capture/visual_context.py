from __future__ import annotations


def safe_visual_context(value: object, *, limit: int = 520) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    lowered = text.lower()
    unsafe_markers = (
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
    if any(marker in lowered for marker in unsafe_markers):
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
