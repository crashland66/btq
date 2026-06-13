from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config import get_config
from io_atomic import atomic_write_text


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T")
JOURNAL_LINE_DATE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?::|\s+-|\s+—)\s*(?P<rest>.+)$")
UNKNOWN_CAPTURE_BLOCK = re.compile(
    r"---\n"
    r"type: unknown_capture\n"
    r"timestamp: (?P<timestamp>[^\n]+)\n"
    r"audio_file: (?P<audio_file>[^\n]+)\n"
    r"status: (?P<status>[^\n]+)\n"
    r"retry_count: (?P<retry_count>[^\n]+)\n"
    r"last_attempted: (?P<last_attempted>[^\n]+)\n"
    r"---\n\n"
    r"## Original Transcript\n(?P<original>.*?)\n\n"
    r"## Normalized Transcript\n(?P<normalized>.*?)\n\n"
    r"## Notes\n(?P<notes>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)
VISIT_GAP_BLOCK = re.compile(
    r"---\n"
    r"type: visit_gap\n"
    r"site: (?P<site>[^\n]+)\n"
    r"date: (?P<date>[^\n]+)\n"
    r'reason: "?(?P<reason>[^\n"]+)"?\n'
    r"---\n"
)
NON_FACT_PATTERNS = (
    re.compile(r"^(events? created today|jobs? executed today|jobs? failed today)[:\s]*$", re.IGNORECASE),
)
STATUS_CLAIM_PATTERN = re.compile(r"\b(presumed|likely|possibly)?\s*resigned\b", re.IGNORECASE)


@dataclass(frozen=True)
class DigestPaths:
    vault_root: Path
    local_root: Path
    runtime_root: Path
    logs_dir: Path


@dataclass(frozen=True)
class EventCandidate:
    source: str
    sort_group: int
    sort_value: str
    sequence: int
    text: str
    reference: str | None = None
    raw_text: str | None = None
    site: str | None = None
    employee: str | None = None
    category: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class ExcludedCandidate:
    source: str
    text: str
    reason: str


def valid_date(value: str) -> str:
    if not DATE_PATTERN.match(value):
        raise argparse.ArgumentTypeError(f"Invalid date: {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Build a nightly digest from BT Pipeline artifacts.")
    parser.add_argument("--date", type=valid_date, default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--local-root", type=Path, default=config.local_root)
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--logs-dir", type=Path, default=config.logs_dir)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def iso_date_from_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    candidate = value[:10]
    return candidate if DATE_PATTERN.match(candidate) else None


def load_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def event_files_for_date(events_dir: Path, target_date: str) -> list[dict]:
    items: list[dict] = []
    if not events_dir.exists():
        return items
    for path in sorted(events_dir.glob("*.json")):
        payload = load_json(path)
        if payload is None:
            continue
        timestamp_date = iso_date_from_timestamp(payload.get("timestamp"))
        if timestamp_date == target_date:
            payload["_source_path"] = str(path)
            items.append(payload)
    return items


def job_files_for_mtime(directory: Path, target_date: str) -> list[dict]:
    items: list[dict] = []
    if not directory.exists():
        return items
    for path in sorted(directory.glob("*.json")):
        modified_date = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()
        if modified_date != target_date:
            continue
        payload = load_json(path)
        if payload is None:
            continue
        payload["_source_path"] = str(path)
        items.append(payload)
    return items


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    remainder = text[4:]
    closing = remainder.find("\n---\n")
    if closing == -1:
        return text
    return remainder[closing + len("\n---\n"):]


def normalize_fact_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sanitize_atomic_fragment(text: str) -> str:
    cleaned = normalize_fact_text(text)
    cleaned = re.sub(r",\s*(presumed|likely|possibly)\s+resigned\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bstatus\s*:\s*(presumed|likely|possibly)\s+resigned\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpresumed resigned\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bpresumed\b.*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.rstrip(" ,;-")


def split_atomic_sentences(text: str) -> list[str]:
    collapsed = normalize_fact_text(text)
    if not collapsed:
        return []
    parts = re.split(r"(?<=[.!?])\s+|(?<=;)\s+|\s+\|\s+", collapsed)
    fragments = []
    for part in parts:
        cleaned = sanitize_atomic_fragment(part.strip(" -"))
        if cleaned:
            fragments.append(cleaned)
    return fragments


def is_fact_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#") or stripped.startswith("```"):
        return False
    if stripped.startswith("---"):
        return False
    for pattern in NON_FACT_PATTERNS:
        if pattern.match(stripped):
            return False
    return True


def is_uncertain_status_line(line: str) -> bool:
    lowered = line.lower()
    return "presumed resigned" in lowered or "likely resigned" in lowered or "possibly resigned" in lowered


def vault_doc_markdown(doc_type: str, target_date: str) -> str | None:
    """Markdown ``content`` for an entity type on a date, from CouchDB (canonical).

    Returns ``None`` when CouchDB is not configured so callers fall back to the
    Markdown projection (dev/CI only); returns "" on query failure.
    """
    import os

    if not os.environ.get("BTQ_COUCHDB_URL", "").strip():
        return None
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    payload = {"selector": {"type": doc_type, "date": target_date}, "limit": 100000}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — digest degrades to empty rather than crashing
        return ""
    parts = [
        str(doc.get("content") or "")
        for doc in response.get("docs", [])
        if str(doc.get("date") or "").strip() == target_date
    ]
    return "\n\n".join(part for part in parts if part)


def markdown_candidates(path: Path, source: str, target_date: str, sort_group: int) -> tuple[list[EventCandidate], list[ExcludedCandidate]]:
    return candidates_from_text(strip_frontmatter(load_text(path)), source, target_date, sort_group, path.name)


def candidates_from_text(text: str, source: str, target_date: str, sort_group: int, reference_name: str) -> tuple[list[EventCandidate], list[ExcludedCandidate]]:
    candidates: list[EventCandidate] = []
    excluded: list[ExcludedCandidate] = []
    sequence = 0
    in_code_block = False
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if not is_fact_line(raw_line):
            continue
        line = stripped
        if is_uncertain_status_line(line):
            excluded.append(ExcludedCandidate(source, stripped, "uncertain_status_claim"))
            continue
        match = JOURNAL_LINE_DATE_PATTERN.match(line)
        timestamp = None
        if match and match.group("date") == target_date:
            timestamp = match.group("date")
            line = match.group("rest").strip()
        fragments = split_atomic_sentences(line)
        if not fragments:
            excluded.append(ExcludedCandidate(source, stripped, "non_atomic_or_empty"))
            continue
        for fragment in fragments:
            candidates.append(
                EventCandidate(
                    source=source,
                    sort_group=sort_group,
                    sort_value=f"{sequence:06d}",
                    sequence=sequence,
                    text=fragment,
                    reference=f"{reference_name}:L{line_number}",
                    raw_text=stripped,
                    timestamp=timestamp,
                )
            )
            sequence += 1
    return candidates, excluded


def journal_candidates(journal_path: Path, target_date: str) -> tuple[list[EventCandidate], list[ExcludedCandidate]]:
    md = vault_doc_markdown("journal", target_date)
    if md is not None:
        return candidates_from_text(strip_frontmatter(md), "journal", target_date, 0, f"journal_{target_date}")
    return markdown_candidates(journal_path, "journal", target_date, 0)


def report_candidates(report_path: Path, target_date: str) -> tuple[list[EventCandidate], list[ExcludedCandidate]]:
    md = vault_doc_markdown("shift_report", target_date)
    if md is not None:
        return candidates_from_text(strip_frontmatter(md), "report", target_date, 1, f"shift_report_{target_date}")
    return markdown_candidates(report_path, "report", target_date, 1)


def infer_text_metadata(text: str) -> tuple[str | None, str | None]:
    normalized = normalize_fact_text(text)
    lowered = normalized.lower()
    category = None
    if "submitted resignation" in lowered or "resigned" in lowered:
        category = "employee_resigned"
    elif "called off" in lowered or "did not report" in lowered:
        category = "employee_callout"
    elif "open position" in lowered or "staffing pressure" in lowered:
        category = "staffing_open_positions"
    elif "slop sink" in lowered or "leak" in lowered or "badge" in lowered or "access" in lowered:
        category = "access_constraint"

    employee = None
    name_match = re.match(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", normalized)
    if name_match:
        employee = name_match.group(1)
    return category, employee


def infer_text_site(text: str) -> str | None:
    normalized = normalize_fact_text(text)
    patterns = (
        r"^([A-Z][A-Za-z0-9&' -]+?) has \d+ open position",
        r"^([A-Z][A-Za-z0-9&' -]+?) has staffing pressure",
        r"^([A-Z][A-Za-z0-9&' -]+?) critically short",
    )
    for pattern in patterns:
        match = re.match(pattern, normalized)
        if match is not None:
            return match.group(1).strip()
    return None


def collect_status_claims(path: Path, source: str) -> list[dict[str, str]]:
    text = strip_frontmatter(load_text(path))
    claims: list[dict[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        if not STATUS_CLAIM_PATTERN.search(stripped):
            continue
        name_match = re.match(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", stripped)
        if name_match is None:
            continue
        claims.append(
            {
                "source": source,
                "employee": name_match.group(1),
                "text": stripped,
                "reference": f"{path.name}:L{line_number}",
            }
        )
    return claims


def atomic_fact_from_event(event: dict) -> tuple[str | None, str | None]:
    event_type = str(event.get("type", "")).strip()
    site = event.get("site")
    employee = event.get("employee")
    details = normalize_fact_text(str(event.get("details", "")).strip())

    if event_type == "staffing_risk":
        open_positions = event.get("open_positions")
        if isinstance(site, str) and site.strip():
            if isinstance(open_positions, int):
                return f"{site.strip()} has {open_positions} open position(s)", "staffing_open_positions"
            return f"{site.strip()} has staffing pressure", "staffing_pressure"
        return None, None
    if event_type == "employee_callout":
        if details:
            return details, "employee_callout"
    if event_type == "employee_resigned":
        if details:
            return details, "employee_resigned"
        if isinstance(employee, str) and employee.strip():
            return f"{employee.strip()} resigned", "employee_resigned"
    if event_type == "employee_onboarding":
        if details:
            return details, "employee_onboarding"
    if event_type == "incident":
        if details:
            return details, "incident"
    if event_type == "access_constraint":
        if details:
            return details, "access_constraint"
    if event_type == "site_observation":
        if details:
            return details, "site_observation"
    if event_type == "employee_retention_risk":
        if details:
            return details, "retention_signal"
    return None, None


def structured_event_candidates(events: list[dict], target_date: str) -> tuple[list[EventCandidate], list[ExcludedCandidate]]:
    candidates: list[EventCandidate] = []
    excluded: list[ExcludedCandidate] = []
    for index, event in enumerate(sorted(events, key=lambda item: (str(item.get("timestamp", "")), str(item.get("event_id", ""))))):
        text, category = atomic_fact_from_event(event)
        raw_details = normalize_fact_text(str(event.get("details", "")).strip())
        if text is None or category is None:
            excluded.append(
                ExcludedCandidate(
                    "structured_event",
                    str(event.get("event_id", "unknown")),
                    "unsupported_or_empty_atomic_fact",
                )
            )
            continue
        timestamp = str(event.get("timestamp", "")).strip() or None
        sort_value = timestamp if timestamp and TIMESTAMP_PATTERN.match(timestamp) else f"{target_date}T23:59:59Z"
        candidates.append(
            EventCandidate(
                source="system",
                sort_group=2,
                sort_value=sort_value,
                sequence=index,
                text=text,
                reference=str(event.get("_source_path", event.get("event_id", "unknown"))),
                raw_text=raw_details or text,
                site=event.get("site") if isinstance(event.get("site"), str) else None,
                employee=event.get("employee") if isinstance(event.get("employee"), str) else None,
                category=category,
                timestamp=timestamp,
            )
        )
    return candidates, excluded


def summarize_unknowns(journal_path: Path) -> list[dict]:
    if not journal_path.exists():
        return []
    text = journal_path.read_text(encoding="utf-8")
    entries: list[dict] = []
    for match in UNKNOWN_CAPTURE_BLOCK.finditer(text):
        entry = {key: value.strip() for key, value in match.groupdict().items()}
        if entry["status"] == "unresolved":
            entries.append(entry)
    return entries


def collect_visit_gaps(vault_root: Path, target_date: str) -> list[dict]:
    """Visit gaps for the target date, from CouchDB (canonical) when configured.

    Reads ``type: visit_gap`` docs from ``btq_vault``; the Markdown ``about.md``
    glob is a dev/CI fallback only (removed once the projection is retired).
    """
    import os

    if os.environ.get("BTQ_COUCHDB_URL", "").strip():
        return _collect_visit_gaps_couchdb(target_date)
    return _collect_visit_gaps_filesystem(vault_root, target_date)


def _collect_visit_gaps_couchdb(target_date: str) -> list[dict]:
    import json
    from urllib import parse as urllib_parse, request as urllib_request

    from event_pipeline import couchdb_config

    cfg = couchdb_config.from_env()
    db_name = couchdb_config.vault_database()
    url = f"{cfg.base_url.rstrip('/')}/{urllib_parse.quote(db_name, safe='')}/_find"
    payload = {"selector": {"type": "visit_gap", "date": target_date}, "limit": 100000}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(cfg.auth_header())
    req = urllib_request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=cfg.timeout) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — digest degrades to no gaps rather than crashing
        return []
    gaps: list[dict] = []
    for doc in response.get("docs", []):
        if str(doc.get("date") or "").strip() != target_date:
            continue
        gaps.append(
            {
                "site": str(doc.get("site") or "").strip(),
                "date": str(doc.get("date") or "").strip(),
                "reason": str(doc.get("reason") or "").strip(),
                "path": str(doc.get("_id") or "").strip(),
            }
        )
    return gaps


def _collect_visit_gaps_filesystem(vault_root: Path, target_date: str) -> list[dict]:
    accounts_dir = vault_root / "Accounts"
    if not accounts_dir.exists():
        return []
    gaps: list[dict] = []
    for about_path in sorted(accounts_dir.glob("*/Locations/*/about.md")):
        try:
            text = about_path.read_text(encoding="utf-8")
        except Exception:
            continue
        for match in VISIT_GAP_BLOCK.finditer(text):
            entry = {key: value.strip() for key, value in match.groupdict().items()}
            if entry["date"] == target_date:
                entry["path"] = str(about_path.relative_to(vault_root))
                gaps.append(entry)
    return gaps


def candidate_precision(candidate: EventCandidate) -> tuple[int, int]:
    source_rank = {"system": 3, "report": 2, "journal": 1}.get(candidate.source, 0)
    return (len(normalize_fact_text(candidate.text)), source_rank)


def candidate_identity_key(candidate: EventCandidate) -> str:
    category = candidate.category
    employee = candidate.employee
    if category is None or employee is None:
        inferred_category, inferred_employee = infer_text_metadata(candidate.text)
        category = category or inferred_category
        employee = employee or inferred_employee
    if category and employee:
        return f"{category}|employee|{employee.lower()}"
    normalized = normalize_fact_text(candidate.text).lower()
    return f"text|{normalized}"


def dedupe_candidates(candidates: list[EventCandidate]) -> tuple[list[EventCandidate], list[ExcludedCandidate], dict[str, int]]:
    selected_by_key: dict[str, EventCandidate] = {}
    excluded: list[ExcludedCandidate] = []
    counts_by_source: Counter[str] = Counter()
    for candidate in sorted(candidates, key=lambda item: (item.sort_group, item.sort_value, item.sequence, item.text)):
        counts_by_source[candidate.source] += 1
        normalized = normalize_fact_text(candidate.text).lower()
        if not normalized:
            excluded.append(ExcludedCandidate(candidate.source, candidate.text, "empty_text"))
            continue
        key = candidate_identity_key(candidate)
        existing = selected_by_key.get(key)
        if existing is None:
            selected_by_key[key] = candidate
            continue
        if normalize_fact_text(existing.text).lower() == normalized:
            if candidate_precision(candidate) > candidate_precision(existing):
                selected_by_key[key] = candidate
                excluded.append(ExcludedCandidate(existing.source, existing.text, "duplicate_fact_less_precise"))
            else:
                excluded.append(ExcludedCandidate(candidate.source, candidate.text, "duplicate_fact"))
            continue
        if candidate_precision(candidate) > candidate_precision(existing):
            selected_by_key[key] = candidate
            excluded.append(ExcludedCandidate(existing.source, existing.text, "duplicate_fact_less_precise"))
        else:
            excluded.append(ExcludedCandidate(candidate.source, candidate.text, "duplicate_fact_less_precise"))
    included = sorted(selected_by_key.values(), key=lambda item: (item.sort_group, item.sort_value, item.sequence, item.text))
    return included, excluded, dict(sorted(counts_by_source.items()))


def target_label_for_job(job: dict) -> str:
    payload = job.get("payload")
    if not isinstance(payload, dict):
        return "unknown"
    if isinstance(payload.get("path"), str):
        return str(payload["path"])
    if isinstance(payload.get("site"), str):
        return str(payload["site"])
    if isinstance(payload.get("employee"), str):
        return str(payload["employee"])
    return "unknown"


def classify_append_target(path_value: str) -> str:
    if path_value.startswith("People/"):
        return "employee_note_append"
    if path_value.startswith("Accounts/"):
        return "site_note_append"
    if path_value.startswith("Journal/"):
        return "journal_append"
    return "other_append"


def derive_signals(events: list[EventCandidate]) -> list[str]:
    site_counts: dict[str, Counter] = defaultdict(Counter)
    unresolved_number = False
    for event in events:
        text_lower = event.text.lower()
        site = event.site.strip() if isinstance(event.site, str) and event.site.strip() and event.site.strip().lower() != "unknown" else infer_text_site(event.text)
        if site is not None:
            if "open position" in text_lower:
                match = re.search(r"(\d+)\s+open position", text_lower)
                site_counts[site]["open_positions"] += int(match.group(1)) if match else 1
            if "resign" in text_lower:
                site_counts[site]["resignations"] += 1
            if "called off" in text_lower or "did not report" in text_lower:
                site_counts[site]["calloffs"] += 1
            if "orientation" in text_lower or "onboarding" in text_lower:
                site_counts[site]["onboardings"] += 1
            if "badge" in text_lower or "key" in text_lower or "access" in text_lower:
                site_counts[site]["access_constraints"] += 1
        if "unrecognized number" in text_lower or "unknown number" in text_lower:
            unresolved_number = True

    signals: list[str] = []
    for site in sorted(site_counts):
        counts = site_counts[site]
        if counts["open_positions"] >= 1:
            signals.append(f"{site} staffing gap active (derived from {counts['open_positions']} open position signal(s))")
        if counts["resignations"] + counts["calloffs"] >= 2:
            signals.append(
                f"{site} staffing pressure elevated (derived from {counts['resignations']} resignation signal(s) and {counts['calloffs']} call-off signal(s))"
            )
        if counts["access_constraints"] >= 1:
            signals.append(f"{site} access dependency present (derived from {counts['access_constraints']} access-related event(s))")
    if unresolved_number:
        signals.append("Call-out protocol unclear (derived from unrecognized or unknown number usage)")
    return signals


def build_end_of_day_state(events: list[EventCandidate], signals: list[str]) -> dict[str, object]:
    staffing_levels: dict[str, dict[str, object]] = {}
    active_issues: list[str] = []
    resolved_issues: list[str] = []

    site_counters: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        site = event.site.strip() if isinstance(event.site, str) and event.site.strip() and event.site.strip().lower() != "unknown" else infer_text_site(event.text)
        if site is None:
            continue
        text_lower = event.text.lower()
        if "open position" in text_lower:
            match = re.search(r"(\d+)\s+open position", text_lower)
            site_counters[site]["open_positions"] += int(match.group(1)) if match else 1
        if "resign" in text_lower:
            site_counters[site]["resignations"] += 1
        if "called off" in text_lower or "did not report" in text_lower:
            site_counters[site]["calloffs"] += 1
        if "orientation" in text_lower or "onboarding" in text_lower:
            site_counters[site]["onboardings"] += 1
        if "closed on sundays" in text_lower or "do not schedule coverage on sundays" in text_lower:
            site_counters[site]["resolved_service_rule"] += 1
        if "badge" in text_lower or "key" in text_lower or "access" in text_lower:
            site_counters[site]["access_constraints"] += 1

    for site in sorted(site_counters):
        counts = site_counters[site]
        staffing_levels[site] = {
            "open_positions": counts["open_positions"],
            "resignations": counts["resignations"],
            "calloffs": counts["calloffs"],
            "onboardings": counts["onboardings"],
            "staffing_level": "unknown",
        }
        if counts["open_positions"] or counts["resignations"] or counts["calloffs"] or counts["access_constraints"]:
            active_issues.append(
                f"{site}: open_positions={counts['open_positions']}, resignations={counts['resignations']}, calloffs={counts['calloffs']}, access_constraints={counts['access_constraints']}"
            )
        if counts["resolved_service_rule"]:
            resolved_issues.append(f"{site}: Sunday closure rule clarified")

    for signal in signals:
        if signal not in active_issues:
            active_issues.append(signal)

    return {
        "staffing_levels_per_site": staffing_levels,
        "open_positions": {
            site: data["open_positions"]
            for site, data in staffing_levels.items()
            if isinstance(data, dict) and data.get("open_positions", 0)
        },
        "active_issues": sorted(active_issues),
        "resolved_issues": sorted(resolved_issues),
    }


def build_ambiguities(events: list[EventCandidate], unknown_entries: list[dict], failed_jobs: list[dict], failed_events: list[dict]) -> list[str]:
    ambiguities: list[str] = []
    for event in events:
        text_lower = event.text.lower()
        if "unknown number" in text_lower or "unrecognized number" in text_lower:
            ambiguities.append(f"Call-out number status unclear: {event.text}")
        if "presumed" in text_lower or "unclear" in text_lower or "unknown" in text_lower:
            ambiguities.append(event.text)
    for entry in unknown_entries:
        normalized = normalize_fact_text(entry.get("normalized", ""))
        ambiguities.append(f"Unknown capture unresolved: {normalized or entry.get('audio_file', 'unknown')}")
    for job in failed_jobs:
        ambiguities.append(
            f"Queue execution failed for {job.get('job_id', 'unknown')}: target or routing unclear"
        )
    for event in failed_events:
        ambiguities.append(
            f"Structured event rejected for {event.get('event_id', 'unknown')}: validation outcome unknown from digest inputs"
        )
    return sorted(dict.fromkeys(ambiguities))


def detect_conflicts(status_claims: list[dict[str, str]]) -> list[str]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for claim in status_claims:
        grouped[claim["employee"].lower()].append(claim)
    conflicts: list[str] = []
    for employee_key, items in sorted(grouped.items()):
        if len(items) < 2:
            continue
        normalized_texts = {normalize_fact_text(item["text"]).lower() for item in items}
        if len(normalized_texts) < 2:
            continue
        employee_label = items[0]["employee"] or employee_key.title()
        representations = []
        for item in sorted(items, key=lambda entry: (entry["source"], entry["reference"], entry["text"])):
            representations.append(f"{item['source']} = {item['text']}")
        conflicts.append(f"Status differs for {employee_label}: " + "; ".join(representations))
    return conflicts


def build_candidate_actions(processed_jobs: list[dict], unknown_entries: list[dict], visit_gaps: list[dict]) -> list[str]:
    signals: list[str] = []
    append_targets = Counter()
    journal_action_notes = 0
    for job in processed_jobs:
        if job.get("job_type") != "append_to_note":
            continue
        payload = job.get("payload")
        if not isinstance(payload, dict):
            continue
        path_value = payload.get("path")
        content = payload.get("content")
        if isinstance(path_value, str):
            append_targets[classify_append_target(path_value)] += 1
        if isinstance(content, str) and "ACTION NEEDED" in content:
            journal_action_notes += 1

    if append_targets["employee_note_append"] >= 2:
        signals.append(
            f"Repeated employee note appends: {append_targets['employee_note_append']} employee note writes today may indicate a missing structured employee action."
        )
    if append_targets["site_note_append"] >= 2:
        signals.append(
            f"Repeated site note appends: {append_targets['site_note_append']} site note writes today may indicate a missing structured site-level action."
        )
    if journal_action_notes >= 1:
        signals.append(
            f"Journal action notes present: {journal_action_notes} journal entries used freeform escalation language such as ACTION NEEDED."
        )
    if unknown_entries:
        signals.append(f"Unresolved unknown captures remain: {len(unknown_entries)} entries still need classification or better routing.")
    if visit_gaps:
        signals.append(f"Visit gaps present: {len(visit_gaps)} site updates were recorded without a same-day visit anchor.")
    return signals


def state_checksum(state_snapshot: dict[str, object]) -> str:
    serialized = json.dumps(state_snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def render_event(event: EventCandidate) -> str:
    prefix = f"{event.timestamp} | " if event.timestamp else ""
    reference = f" | ref={event.reference}" if event.reference else ""
    return f"- source={event.source} | {prefix}{event.text}{reference}"


def render_job(job: dict, bucket_name: str) -> str:
    result = "success" if bucket_name == "processed" else "failure"
    return (
        f"- `{job.get('job_id', 'unknown')}` | {job.get('job_type', 'unknown')} | "
        f"target={target_label_for_job(job)} | result={result}"
    )


def render_unknown(entry: dict) -> str:
    normalized = " ".join(entry.get("normalized", "").split())
    excerpt = normalized[:140] + ("..." if len(normalized) > 140 else "")
    notes = " ".join(entry.get("notes", "").split())
    return (
        f"- `{entry.get('audio_file', 'unknown')}` | timestamp={entry.get('timestamp', 'unknown')} | "
        f"retry_count={entry.get('retry_count', '0')} | last_attempted={entry.get('last_attempted', 'null')} | "
        f"excerpt={excerpt} | notes={notes}"
    )


def render_state_snapshot(state_snapshot: dict[str, object]) -> list[str]:
    staffing_levels = state_snapshot.get("staffing_levels_per_site", {})
    open_positions = state_snapshot.get("open_positions", {})
    active_issues = state_snapshot.get("active_issues", [])
    resolved_issues = state_snapshot.get("resolved_issues", [])

    lines = ["### Staffing Levels Per Site", ""]
    if isinstance(staffing_levels, dict) and staffing_levels:
        for site in sorted(staffing_levels):
            details = staffing_levels[site]
            if isinstance(details, dict):
                lines.append(
                    f"- {site}: staffing_level={details.get('staffing_level', 'unknown')}, open_positions={details.get('open_positions', 0)}, resignations={details.get('resignations', 0)}, calloffs={details.get('calloffs', 0)}, onboardings={details.get('onboardings', 0)}"
                )
    else:
        lines.append("- None")

    lines.extend(["", "### Open Positions", ""])
    if isinstance(open_positions, dict) and open_positions:
        for site in sorted(open_positions):
            lines.append(f"- {site}: {open_positions[site]}")
    else:
        lines.append("- None")

    lines.extend(["", "### Active Issues", ""])
    if isinstance(active_issues, list) and active_issues:
        lines.extend([f"- {item}" for item in active_issues])
    else:
        lines.append("- None")

    lines.extend(["", "### Resolved Issues", ""])
    if isinstance(resolved_issues, list) and resolved_issues:
        lines.extend([f"- {item}" for item in resolved_issues])
    else:
        lines.append("- None")
    return lines


def render_validation(
    events_detected: int,
    included_events: list[EventCandidate],
    excluded_events: list[ExcludedCandidate],
    checksum: str,
    total_events_by_source: dict[str, int],
    merged_event_count: int,
) -> list[str]:
    lines = [
        f"- events_detected: {events_detected}",
        f"- merged_event_count: {merged_event_count}",
        f"- events_included: {len(included_events)}",
        f"- deduplicated_event_count: {len(included_events)}",
        f"- state_checksum: {checksum}",
        "",
        "### Events By Source",
        "",
    ]
    if total_events_by_source:
        for source in sorted(total_events_by_source):
            lines.append(f"- {source}: {total_events_by_source[source]}")
    else:
        lines.append("- None")
    lines.extend([
        "",
        "### Excluded Events",
        "",
    ])
    if excluded_events:
        for item in excluded_events:
            lines.append(f"- source={item.source} | reason={item.reason} | {item.text}")
    else:
        lines.append("- None")
    return lines


def render_digest_body(
    target_date: str,
    build_time: str,
    build_id: str,
    journal_path: Path,
    state_path: Path,
    events_detected: int,
    included_events: list[EventCandidate],
    derived_signals: list[str],
    state_snapshot: dict[str, object],
    unknown_entries: list[dict],
    visit_gaps: list[dict],
    unmapped_events: list[dict],
    candidate_actions: list[str],
    processed_jobs: list[dict],
    failed_jobs: list[dict],
    excluded_events: list[ExcludedCandidate],
    status_claims: list[dict[str, str]],
    total_events_by_source: dict[str, int],
    merged_event_count: int,
    deterministic_hash_value: str,
) -> str:
    checksum = state_checksum(state_snapshot)
    lines = [
        "---",
        "digest_meta:",
        f"  date: {target_date}",
        f"  source_journal: {journal_path.relative_to(journal_path.parents[1])}",
        f"  source_state: {state_path.name}",
        f"  build_time: {build_time}",
        f"  build_id: {build_id}",
        f"  events_detected: {events_detected}",
        f"  events_included: {len(included_events)}",
        f"  deterministic_hash: {deterministic_hash_value}",
        "---",
        "",
        f"# Nightly Digest - {target_date}",
        "",
        "## Event Log (chronological)",
        "",
    ]
    lines.extend([render_event(event) for event in included_events] or ["- None"])
    lines.extend(["", "## Derived Signals", ""])
    lines.extend([f"- {signal}" for signal in derived_signals] or ["- None"])
    lines.extend(["", "## End-of-Day State", ""])
    lines.extend(render_state_snapshot(state_snapshot))
    lines.extend(["", "## Ambiguities / Unresolved", ""])
    ambiguity_items = build_ambiguities(included_events, unknown_entries, failed_jobs, unmapped_events) + detect_conflicts(status_claims)
    lines.extend([f"- {item}" for item in sorted(dict.fromkeys(ambiguity_items))] or ["- None"])
    lines.extend(["", "## Jobs Executed Today", ""])
    lines.extend([render_job(job, "processed") for job in processed_jobs] or ["- None"])
    lines.extend(["", "## Jobs Failed Today", ""])
    lines.extend([render_job(job, "failed") for job in failed_jobs] or ["- None"])
    lines.extend(["", "## Unknown Captures Still Open", ""])
    lines.extend([render_unknown(entry) for entry in unknown_entries] or ["- None"])
    lines.extend(["", "## Visit Gaps", ""])
    lines.extend(
        [f"- site={entry['site']} | path={entry['path']} | reason={entry['reason']}" for entry in visit_gaps]
        or ["- None"]
    )
    lines.extend(["", "## Potential New Structured Actions", ""])
    lines.extend([f"- {item}" for item in candidate_actions] or ["- None"])
    lines.extend(["", "## Validation", ""])
    lines.extend(render_validation(events_detected, included_events, excluded_events, checksum, total_events_by_source, merged_event_count))
    lines.append("")
    return "\n".join(lines)


def normalized_digest_for_hash(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith("  deterministic_hash: "):
            continue
        if line.startswith("  build_time: "):
            lines.append("  build_time: <normalized>")
            continue
        if line.startswith("  build_id: "):
            lines.append("  build_id: <normalized>")
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def build_digest(target_date: str, paths: DigestPaths) -> str:
    journal_path = paths.vault_root / "Journal" / f"{target_date}.md"
    report_path = paths.vault_root / "Journal" / f"{target_date}-shift-report.md"
    state_path = paths.vault_root / "state.md"
    valid_events = event_files_for_date(paths.local_root / "events_valid", target_date)
    failed_events = event_files_for_date(paths.local_root / "events_failed", target_date)
    processed_jobs = job_files_for_mtime(paths.runtime_root / "processed", target_date)
    failed_jobs = job_files_for_mtime(paths.runtime_root / "failed", target_date)
    unknown_entries = summarize_unknowns(paths.vault_root / "Journal" / f"{target_date}-unknown.md")
    visit_gaps = collect_visit_gaps(paths.vault_root, target_date)
    status_claims = collect_status_claims(journal_path, "journal") + collect_status_claims(report_path, "report")

    journal_event_candidates, journal_excluded = journal_candidates(journal_path, target_date)
    report_event_candidates, report_excluded = report_candidates(report_path, target_date)
    structured_candidates, structured_excluded = structured_event_candidates(valid_events, target_date)
    all_candidates = journal_event_candidates + report_event_candidates + structured_candidates
    included_events, dedupe_excluded, total_events_by_source = dedupe_candidates(all_candidates)
    events_detected = len(all_candidates)
    excluded_events = journal_excluded + report_excluded + structured_excluded + dedupe_excluded

    derived_signals = derive_signals(included_events)
    state_snapshot = build_end_of_day_state(included_events, derived_signals)
    candidate_actions = build_candidate_actions(processed_jobs, unknown_entries, visit_gaps)
    build_time = datetime.now(timezone.utc).isoformat()
    build_id = str(uuid4())

    first_pass = render_digest_body(
        target_date=target_date,
        build_time=build_time,
        build_id=build_id,
        journal_path=journal_path,
        state_path=state_path,
        events_detected=events_detected,
        included_events=included_events,
        derived_signals=derived_signals,
        state_snapshot=state_snapshot,
        unknown_entries=unknown_entries,
        visit_gaps=visit_gaps,
        unmapped_events=failed_events,
        candidate_actions=candidate_actions,
        processed_jobs=processed_jobs,
        failed_jobs=failed_jobs,
        excluded_events=excluded_events,
        status_claims=status_claims,
        total_events_by_source=total_events_by_source,
        merged_event_count=len(all_candidates),
        deterministic_hash_value="PENDING",
    )
    deterministic_hash_value = hashlib.sha256(normalized_digest_for_hash(first_pass).encode("utf-8")).hexdigest()
    return render_digest_body(
        target_date=target_date,
        build_time=build_time,
        build_id=build_id,
        journal_path=journal_path,
        state_path=state_path,
        events_detected=events_detected,
        included_events=included_events,
        derived_signals=derived_signals,
        state_snapshot=state_snapshot,
        unknown_entries=unknown_entries,
        visit_gaps=visit_gaps,
        unmapped_events=failed_events,
        candidate_actions=candidate_actions,
        processed_jobs=processed_jobs,
        failed_jobs=failed_jobs,
        excluded_events=excluded_events,
        status_claims=status_claims,
        total_events_by_source=total_events_by_source,
        merged_event_count=len(all_candidates),
        deterministic_hash_value=deterministic_hash_value,
    )


def output_path_for_date(vault_root: Path, target_date: str, explicit_output: Path | None) -> Path:
    if explicit_output is not None:
        return explicit_output.expanduser()
    return vault_root / "Journal" / f"{target_date}-digest.md"


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = DigestPaths(
        vault_root=args.vault_root.expanduser(),
        local_root=args.local_root.expanduser(),
        runtime_root=args.runtime_root.expanduser(),
        logs_dir=args.logs_dir.expanduser(),
    )
    output_path = output_path_for_date(paths.vault_root, args.date, args.output)
    digest_text = build_digest(args.date, paths)
    atomic_write_text(output_path, digest_text + "\n")
    print(output_path)
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
