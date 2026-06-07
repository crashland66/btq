from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from config import get_config
from io_atomic import atomic_write_text
from supply_orders import load_site_metadata


SITE_ID_PATTERN = re.compile(r"#(?P<site_id>\d{3,})")
AMOUNT_PATTERN = r"(?P<amount>[0-9][0-9,]*(?:\.\d{1,2})?)"
BUDGETED_PATTERN = re.compile(rf"basic supply budget:\s*\$?{AMOUNT_PATTERN}", re.IGNORECASE)
INCLUDED_PATTERN = re.compile(rf"supplies included:\s*\$?{AMOUNT_PATTERN}", re.IGNORECASE)
SUPPLIES_PATTERN = re.compile(rf"supplies:\s*\$?{AMOUNT_PATTERN}", re.IGNORECASE)
EXCLUDED_PATTERNS = (
    re.compile(r"\bno supplies\b", re.IGNORECASE),
    re.compile(r"\blabor only\b", re.IGNORECASE),
)
RESTRICTION_PATTERNS = (
    re.compile(r"\bno consumables included\b", re.IGNORECASE),
    re.compile(r"\bno liners or paper products included\b", re.IGNORECASE),
    re.compile(r"\bno liners or paper towels included\b", re.IGNORECASE),
    re.compile(r"\bno pt,\s*tp,\s*liners or soap\b", re.IGNORECASE),
    re.compile(r"\bno pt,\s*tp,\s*liners,\s*or soap\b", re.IGNORECASE),
    re.compile(r"\bexcept disposables\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class SiteSupplyBudgetRecord:
    site_id: str
    site_name: str
    supply_budget_type: str
    monthly_supply_budget: float | None
    supply_budget_notes: str | None
    notes: str
    about_path: Path | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "supply_budget_type": self.supply_budget_type,
            "monthly_supply_budget": self.monthly_supply_budget,
            "supply_budget_notes": self.supply_budget_notes,
            "notes": self.notes,
        }


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_frontmatter_fields(path: Path) -> tuple[list[tuple[str, str]], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return [], text
    remainder = text[4:]
    closing = remainder.find("\n---\n")
    if closing == -1:
        return [], text
    frontmatter_text = remainder[:closing]
    body = remainder[closing + len("\n---\n") :]
    fields: list[tuple[str, str]] = []
    for line in frontmatter_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields.append((key.strip(), value.strip()))
    return fields, body


def upsert_frontmatter_field(fields: list[tuple[str, str]], key: str, value: str) -> list[tuple[str, str]]:
    updated: list[tuple[str, str]] = []
    found = False
    for existing_key, existing_value in fields:
        if existing_key == key:
            updated.append((key, value))
            found = True
        else:
            updated.append((existing_key, existing_value))
    if not found:
        updated.append((key, value))
    return updated


def render_frontmatter(fields: list[tuple[str, str]], body: str) -> str:
    lines = ["---", *[f"{key}: {value}" for key, value in fields], "---"]
    if body:
        return "\n".join(lines) + "\n" + body
    return "\n".join(lines) + "\n"


def parse_site_name_prefix(line: str, site_id: str) -> str:
    prefix = line.split(f"#{site_id}", 1)[0]
    prefix = re.sub(r"\.\.\..*$", "", prefix).strip(" -,.")
    return normalize_whitespace(prefix)


def parse_budget_amount(match: re.Match[str]) -> float:
    return float(match.group("amount").replace(",", ""))


def normalize_restriction_text(value: str) -> str:
    normalized = normalize_whitespace(value).lower().strip(" ,.;:-")
    normalized = re.sub(r"\s*\(provided by customer\)\s*$", "", normalized, flags=re.IGNORECASE)
    return normalized.strip(" ,.;:-") or None


def extract_restriction_note(line: str, match: re.Match[str]) -> str | None:
    trailing = line[match.end() :]
    trailing = trailing.lstrip(" -,:;")
    for pattern in RESTRICTION_PATTERNS:
        note_match = pattern.search(trailing)
        if note_match is not None:
            return normalize_restriction_text(note_match.group(0))
    return None


def classify_budget(line: str) -> tuple[str, float | None, str | None, str]:
    budgeted_match = BUDGETED_PATTERN.search(line)
    if budgeted_match is not None:
        return "budgeted", parse_budget_amount(budgeted_match), extract_restriction_note(line, budgeted_match), normalize_whitespace(line)

    included_match = INCLUDED_PATTERN.search(line)
    if included_match is not None:
        return "included", parse_budget_amount(included_match), extract_restriction_note(line, included_match), normalize_whitespace(line)

    supplies_match = SUPPLIES_PATTERN.search(line)
    if supplies_match is not None:
        return "budgeted", parse_budget_amount(supplies_match), extract_restriction_note(line, supplies_match), normalize_whitespace(line)

    for pattern in EXCLUDED_PATTERNS:
        if pattern.search(line):
            return "excluded", None, None, normalize_whitespace(line)
    return "unknown", None, None, normalize_whitespace(line)


def site_metadata_index(vault_root: Path) -> dict[str, tuple[str, Path]]:
    index: dict[str, tuple[str, Path]] = {}
    for site in load_site_metadata(vault_root):
        if site.about_path is not None:
            index[site.site_id] = (site.canonical_name, site.about_path)
    return index


def parse_supply_budget_lines(lines: list[str], vault_root: Path) -> tuple[list[SiteSupplyBudgetRecord], list[SiteSupplyBudgetRecord]]:
    known_sites = site_metadata_index(vault_root)
    records: list[SiteSupplyBudgetRecord] = []
    manual_review: list[SiteSupplyBudgetRecord] = []

    for raw_line in lines:
        line = normalize_whitespace(raw_line)
        if not line:
            continue
        site_id_match = SITE_ID_PATTERN.search(line)
        if site_id_match is None:
            record = SiteSupplyBudgetRecord(
                site_id="",
                site_name="",
                supply_budget_type="unknown",
                monthly_supply_budget=None,
                supply_budget_notes=None,
                notes=f"Missing site_id: {line}",
                about_path=None,
            )
            records.append(record)
            manual_review.append(record)
            continue

        site_id = site_id_match.group("site_id")
        parsed_site_name = parse_site_name_prefix(line, site_id)
        supply_budget_type, monthly_supply_budget, supply_budget_notes, notes = classify_budget(line)
        canonical_name, about_path = known_sites.get(site_id, (parsed_site_name, None))
        record = SiteSupplyBudgetRecord(
            site_id=site_id,
            site_name=canonical_name or parsed_site_name,
            supply_budget_type=supply_budget_type,
            monthly_supply_budget=monthly_supply_budget,
            supply_budget_notes=supply_budget_notes,
            notes=notes,
            about_path=about_path,
        )
        records.append(record)
        if supply_budget_type == "unknown" or about_path is None:
            manual_review.append(record)
    return records, manual_review


def update_site_about_frontmatter(record: SiteSupplyBudgetRecord, as_of_date: date) -> None:
    if record.about_path is None:
        return
    fields, body = parse_frontmatter_fields(record.about_path)
    fields = upsert_frontmatter_field(fields, "supply_budget_type", record.supply_budget_type)
    if record.monthly_supply_budget is None:
        fields = upsert_frontmatter_field(fields, "monthly_supply_budget", "null")
    else:
        fields = upsert_frontmatter_field(fields, "monthly_supply_budget", f"{record.monthly_supply_budget:.2f}")
    if record.supply_budget_notes is None:
        fields = upsert_frontmatter_field(fields, "supply_budget_notes", "null")
    else:
        fields = upsert_frontmatter_field(fields, "supply_budget_notes", record.supply_budget_notes)
    fields = upsert_frontmatter_field(fields, "budget_last_verified", as_of_date.isoformat())
    atomic_write_text(record.about_path, render_frontmatter(fields, body))


def apply_supply_budget_updates(lines: list[str], vault_root: Path, as_of_date: date | None = None) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    verified_on = date.today() if as_of_date is None else as_of_date
    records, manual_review = parse_supply_budget_lines(lines, vault_root)
    for record in records:
        update_site_about_frontmatter(record, verified_on)
    return [record.to_record() for record in records], [record.to_record() for record in manual_review]


def records_to_json(records: list[dict[str, object]]) -> str:
    return json.dumps(records, indent=2, sort_keys=True) + "\n"


def load_lines_from_csv(csv_path: Path) -> list[str]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        fieldnames = {name.strip().lower(): name for name in reader.fieldnames}
        site_key = fieldnames.get("site")
        notes_key = fieldnames.get("notes")
        if notes_key is None:
            raise ValueError("CSV must include a notes column")

        lines: list[str] = []
        for row in reader:
            notes = (row.get(notes_key) or "").strip()
            site = (row.get(site_key) or "").strip() if site_key is not None else ""
            if not notes and not site:
                continue
            combined = f"{site} {notes}".strip()
            if combined:
                lines.append(combined)
        return lines


def importer_output_dir(project_root: Path, as_of_date: date) -> Path:
    return project_root.parent / "local" / "site_supply_budgets" / as_of_date.isoformat()


def import_supply_budget_csv(
    csv_path: Path,
    vault_root: Path,
    project_root: Path,
    as_of_date: date | None = None,
) -> tuple[Path, Path, list[dict[str, object]], list[dict[str, object]]]:
    verified_on = date.today() if as_of_date is None else as_of_date
    lines = load_lines_from_csv(csv_path)
    records, manual_review = apply_supply_budget_updates(lines, vault_root, verified_on)
    output_dir = importer_output_dir(project_root, verified_on)
    parsed_path = output_dir / "parsed_supply_budgets.json"
    manual_review_path = output_dir / "manual_review_supply_budgets.json"
    atomic_write_text(parsed_path, records_to_json(records))
    atomic_write_text(manual_review_path, records_to_json(manual_review))
    return parsed_path, manual_review_path, records, manual_review


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Import site supply budget contract notes from CSV.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--project-root", type=Path, default=config.project_dir)
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    parsed_path, manual_review_path, records, manual_review = import_supply_budget_csv(
        csv_path=args.csv_path.expanduser(),
        vault_root=args.vault_root.expanduser(),
        project_root=args.project_root.expanduser(),
        as_of_date=args.date,
    )
    print(parsed_path)
    print(manual_review_path)
    print(f"records={len(records)}")
    print(f"manual_review={len(manual_review)}")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
