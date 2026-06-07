from __future__ import annotations

import hashlib
import json
from pathlib import Path


BTQ_JOB_IDS_KEY = "btq_job_ids"
LEGACY_BTQ_JOB_IDS_TOKEN = "btq_job_ids:"


def compute_job_id(job: dict) -> str:
    canonical = {
        "job_type": job.get("job_type"),
        "payload": job.get("payload"),
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_frontmatter_text(file_content: str) -> tuple[dict[str, object], str, bool]:
    if not file_content.startswith("---\n"):
        return {}, file_content, False

    remainder = file_content[4:]
    closing_marker = "\n---\n"
    closing_index = remainder.find(closing_marker)
    if closing_index == -1:
        return {}, file_content, False

    frontmatter_text = remainder[:closing_index]
    body = remainder[closing_index + len(closing_marker):]
    fields: dict[str, object] = {}
    current_list_key: str | None = None

    for raw_line in frontmatter_text.splitlines():
        if not raw_line.strip():
            continue
        if current_list_key is not None and raw_line.startswith("  - "):
            existing = fields.setdefault(current_list_key, [])
            if isinstance(existing, list):
                existing.append(raw_line[4:])
            continue
        if current_list_key is not None and raw_line.startswith("    "):
            continue

        current_list_key = None
        if ":" not in raw_line:
            return {}, file_content, False

        key, raw_value = raw_line.split(":", 1)
        value = raw_value.lstrip(" ")
        if not key:
            return {}, file_content, False
        if value == "":
            fields[key] = []
            current_list_key = key
            continue
        fields[key] = value

    return fields, body, True


def serialize_frontmatter(frontmatter: dict[str, object]) -> str:
    lines: list[str] = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
            continue
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def has_job_been_applied(file_content: str, job_id: str) -> bool:
    frontmatter, _body, has_frontmatter = parse_frontmatter_text(file_content)
    if has_frontmatter:
        existing = frontmatter.get(BTQ_JOB_IDS_KEY)
        if isinstance(existing, list) and job_id in existing:
            return True
    legacy_marker = f"{LEGACY_BTQ_JOB_IDS_TOKEN}\n- {job_id}\n"
    return legacy_marker in file_content


def record_job_id(frontmatter: dict, job_id: str) -> dict:
    updated = dict(frontmatter)
    existing = updated.get(BTQ_JOB_IDS_KEY)
    if isinstance(existing, list):
        job_ids = list(existing)
    elif existing is None:
        job_ids = []
    else:
        job_ids = [str(existing)]
    if job_id not in job_ids:
        job_ids.append(job_id)
    updated[BTQ_JOB_IDS_KEY] = job_ids
    return updated


def upsert_job_id_frontmatter(file_content: str, job_id: str) -> str:
    frontmatter, body, has_frontmatter = parse_frontmatter_text(file_content)
    if has_frontmatter and frontmatter.get("type") == "unknown_capture":
        updated_frontmatter = record_job_id({}, job_id)
        serialized = serialize_frontmatter(updated_frontmatter)
        return f"{serialized}\n{file_content}"
    updated_frontmatter = record_job_id(frontmatter, job_id)
    serialized = serialize_frontmatter(updated_frontmatter)
    if has_frontmatter:
        return f"{serialized}\n{body}"
    if body:
        return f"{serialized}\n{body}"
    return f"{serialized}\n"


def file_has_job_marker(path: Path, job_id: str) -> bool:
    if not path.exists():
        return False
    return has_job_been_applied(path.read_text(encoding="utf-8"), job_id)
