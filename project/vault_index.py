from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from canonical_domain import Person, PersonId, Site, SiteId
from vault_markdown import (
    frontmatter_float,
    frontmatter_list,
    frontmatter_str,
    normalize_lookup,
    read_markdown_note,
    slugify_identifier,
)


@dataclass(frozen=True)
class VaultIndex:
    vault_root: Path
    sites_by_id: dict[str, Site]
    people_by_id: dict[str, Person]
    site_aliases: dict[str, tuple[str, ...]]
    person_aliases: dict[str, tuple[str, ...]]
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def site_ids(self) -> set[str]:
        return set(self.sites_by_id)


def _add_alias(mapping: dict[str, list[str]], alias: str, entity_id: str) -> None:
    normalized = normalize_lookup(alias)
    if not normalized:
        return
    values = mapping.setdefault(normalized, [])
    if entity_id not in values:
        values.append(entity_id)


def _parse_site(path: Path, vault_root: Path) -> Site | None:
    frontmatter, _body, has_frontmatter = read_markdown_note(path)
    if not has_frontmatter or frontmatter_str(frontmatter, "type") != "location":
        return None
    raw_site_id = frontmatter_str(frontmatter, "site_id") or frontmatter_str(frontmatter, "job")
    if raw_site_id is None:
        return None
    site_id = str(raw_site_id)
    canonical_name = frontmatter_str(frontmatter, "location") or path.parent.name.split(" - ", 1)[-1]
    account = frontmatter_str(frontmatter, "account")
    if account is None:
        try:
            account = path.relative_to(vault_root).parts[1]
        except IndexError:
            account = None
    aliases = {
        site_id,
        canonical_name,
        path.parent.name,
        path.parent.name.split(" - ", 1)[-1],
        *frontmatter_list(frontmatter.get("site_aliases")),
        *frontmatter_list(frontmatter.get("aliases")),
    }
    return Site(
        site_id=SiteId(site_id),
        canonical_name=canonical_name,
        account=account,
        aliases=tuple(sorted({alias for alias in aliases if alias}, key=str.lower)),
        address=frontmatter_str(frontmatter, "address"),
        monthly_supply_budget=frontmatter_float(frontmatter, "monthly_supply_budget"),
        budget_basis=frontmatter_str(frontmatter, "budget_basis"),
        about_path=path,
    )


def _person_aliases(path: Path, frontmatter: dict[str, object], canonical_name: str) -> set[str]:
    aliases = {canonical_name, path.stem, *frontmatter_list(frontmatter.get("aliases"))}
    first = frontmatter_str(frontmatter, "first")
    last = frontmatter_str(frontmatter, "last")
    preferred = frontmatter_str(frontmatter, "preferred") or frontmatter_str(frontmatter, "preferred_name")
    if first and last:
        aliases.add(f"{first} {last}")
        aliases.add(f"{last}, {first}")
    if preferred and last:
        aliases.add(f"{preferred} {last}")
        aliases.add(f"{last}, {preferred}")
    if "," in path.stem:
        last_part, first_part = [part.strip() for part in path.stem.split(",", 1)]
        if first_part and last_part:
            aliases.add(f"{first_part} {last_part}")
    return {alias for alias in aliases if alias}


def _parse_site_ids(value: object) -> tuple[SiteId, ...]:
    site_ids: list[SiteId] = []
    for item in frontmatter_list(value):
        stripped = item.strip()
        if stripped:
            site_ids.append(SiteId(stripped))
    return tuple(site_ids)


def _parse_person_site_ids(frontmatter: dict[str, object]) -> tuple[SiteId, ...]:
    site_ids: list[SiteId] = []
    primary_job = frontmatter_str(frontmatter, "job")
    if primary_job is not None:
        site_ids.append(SiteId(primary_job))
    site_ids.extend(_parse_site_ids(frontmatter.get("additional_jobs")))
    if not site_ids:
        site_ids.extend(_parse_site_ids(frontmatter.get("sites")))
    deduped: list[SiteId] = []
    seen: set[str] = set()
    for site_id in site_ids:
        key = str(site_id)
        if key not in seen:
            deduped.append(site_id)
            seen.add(key)
    return tuple(deduped)


def _parse_person(path: Path) -> Person | None:
    frontmatter, _body, has_frontmatter = read_markdown_note(path)
    if not has_frontmatter:
        frontmatter = {}
    canonical_name = frontmatter_str(frontmatter, "name") or path.stem
    person_id = frontmatter_str(frontmatter, "person_id") or frontmatter_str(frontmatter, "id") or slugify_identifier(canonical_name)
    aliases = _person_aliases(path, frontmatter, canonical_name)
    return Person(
        person_id=PersonId(person_id),
        canonical_name=canonical_name,
        aliases=tuple(sorted(aliases, key=str.lower)),
        status=frontmatter_str(frontmatter, "status"),
        site_ids=_parse_person_site_ids(frontmatter),
        note_path=path,
    )


def build_vault_index(vault_root: Path) -> VaultIndex:
    resolved_root = vault_root.expanduser().resolve()
    site_aliases: dict[str, list[str]] = {}
    person_aliases: dict[str, list[str]] = {}
    sites_by_id: dict[str, Site] = {}
    people_by_id: dict[str, Person] = {}
    diagnostics: list[str] = []

    accounts_dir = resolved_root / "Accounts"
    if accounts_dir.exists():
        for path in sorted(accounts_dir.glob("*/Locations/*/about.md")):
            site = _parse_site(path.resolve(), resolved_root)
            if site is None:
                continue
            site_id = str(site.site_id)
            if site_id in sites_by_id:
                diagnostics.append(f"duplicate site_id {site_id}: {sites_by_id[site_id].about_path} and {path}")
            sites_by_id[site_id] = site
            for alias in site.aliases:
                _add_alias(site_aliases, alias, site_id)

    people_dir = resolved_root / "People"
    if people_dir.exists():
        for path in sorted(item for item in people_dir.iterdir() if item.is_file() and item.suffix == ".md"):
            person = _parse_person(path.resolve())
            if person is None:
                continue
            person_id = str(person.person_id)
            if person_id in people_by_id:
                diagnostics.append(f"duplicate person_id {person_id}: {people_by_id[person_id].note_path} and {path}")
            people_by_id[person_id] = person
            for alias in person.aliases:
                _add_alias(person_aliases, alias, person_id)

    return VaultIndex(
        vault_root=resolved_root,
        sites_by_id=sites_by_id,
        people_by_id=people_by_id,
        site_aliases={key: tuple(values) for key, values in site_aliases.items()},
        person_aliases={key: tuple(values) for key, values in person_aliases.items()},
        diagnostics=tuple(diagnostics),
    )
