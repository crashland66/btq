from __future__ import annotations

BUILTIN_FALLBACK_CATEGORIES = [
    {"label": "Report an Issue", "canonical": "report_an_issue"},
    {"label": "Entryways / Lobby / Doorways", "canonical": "Entryways / Lobby / Doorways"},
    {"label": "Windows / Glass / Sills / Ledges", "canonical": "Windows / Glass / Sills / Ledges"},
    {"label": "Hallways", "canonical": "Hallways"},
    {"label": "Common / Open Areas", "canonical": "Common / Open Areas"},
    {"label": "Restrooms", "canonical": "Restrooms"},
    {"label": "Offices / Classrooms / Exam Rooms", "canonical": "Offices / Classrooms / Exam Rooms"},
    {"label": "Break Rooms / Kitchens / Cafes", "canonical": "Break Rooms / Kitchens / Cafes"},
    {"label": "Supply Levels", "canonical": "Supply Levels"},
    {"label": "Trash", "canonical": "Trash"},
    {"label": "Touch Points", "canonical": "Touch Points"},
    {"label": "Janitorial Closet", "canonical": "Janitorial Closet"},
    {"label": "Chemicals / Safety / PPE / SDS", "canonical": "Chemicals / Safety / PPE / SDS"},
    {"label": "Other", "canonical": "Other"},
]
OPERATOR_ONLY_CATEGORIES = [
    # Native operator clients default QC walks to canonical "qc"; keep the
    # public category contract to exactly {label, canonical}.
    {"label": "QC", "canonical": "qc"},
    {"label": "Baseline", "canonical": "baseline"},
    {"label": "Pre-Engagement", "canonical": "pre_engagement"},
]
OPERATOR_ONLY_CANONICALS = frozenset(entry["canonical"] for entry in OPERATOR_ONLY_CATEGORIES)
OPERATOR_ONLY_LABELS = frozenset(entry["label"] for entry in OPERATOR_ONLY_CATEGORIES)


def normalize_display_categories(categories: object) -> list[dict[str, str]]:
    if not isinstance(categories, list) or not categories:
        return []
    normalized: list[dict[str, str]] = []
    for item in categories:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        canonical = str(item.get("canonical") or "").strip()
        if label and canonical:
            normalized.append({"label": label, "canonical": canonical})
    return normalized


def resolve_display_categories(site_categories: object, default_categories: object) -> list[dict[str, str]]:
    for categories in (site_categories, default_categories):
        normalized = normalize_display_categories(categories)
        if normalized:
            return normalized
    return [dict(category) for category in BUILTIN_FALLBACK_CATEGORIES]


def apply_role_category_filter(
    categories: list[dict[str, str]],
    role: str,
) -> list[dict[str, str]]:
    """Append operator-only categories when role grants access."""
    base = list(categories)
    if role != "site_admin":
        return base
    existing_canonicals = {entry.get("canonical") for entry in base}
    additions = [entry for entry in OPERATOR_ONLY_CATEGORIES if entry.get("canonical") not in existing_canonicals]
    return base + additions


def canonicalize_qc_category(raw_value: str, categories: list[dict[str, str]]) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    for entry in categories:
        canonical = str(entry.get("canonical") or "").strip()
        label = str(entry.get("label") or "").strip()
        if value == canonical or value == label:
            return canonical
    if value in OPERATOR_ONLY_CANONICALS or value in OPERATOR_ONLY_LABELS:
        return ""
    return value
