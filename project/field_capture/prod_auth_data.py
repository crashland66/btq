from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from config import get_config
from queue_processor.idempotency import parse_frontmatter_text, serialize_frontmatter


DEFAULT_PERSON = "Operator, Example.md"
DEFAULT_SITE_ABOUT = "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
DEFAULT_TOKEN_ID = "fct_EXAMPLE_TOKEN_REPLACE"  # placeholder; pass the real token via --token-id
DEFAULT_SITE_ID = "7050"


def read_frontmatter(path: Path) -> dict[str, object]:
    frontmatter, _body, has_frontmatter = parse_frontmatter_text(path.read_text(encoding="utf-8"))
    if not has_frontmatter:
        raise ValueError(f"Expected frontmatter in {path}")
    return frontmatter


def text_value(frontmatter: dict[str, object], key: str) -> str | None:
    value = frontmatter.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def write_note(path: Path, frontmatter: dict[str, object], heading: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{serialize_frontmatter(frontmatter)}\n\n# {heading}\n", encoding="utf-8")


def build_vault_mirror(source_vault: Path, output_root: Path, *, site_id: str) -> None:
    source_person = source_vault / "People" / DEFAULT_PERSON
    source_site = source_vault / DEFAULT_SITE_ABOUT
    person = read_frontmatter(source_person)
    site = read_frontmatter(source_site)

    person_name = text_value(person, "name") or "Example Operator"
    person_frontmatter: dict[str, object] = {
        "type": text_value(person, "type") or "person",
        "person_id": text_value(person, "person_id") or "per_EXAMPLE0000000000000000000",
        "name": person_name,
        "first": text_value(person, "first") or "Example",
        "last": text_value(person, "last") or "Operator",
        "status": text_value(person, "status") or "active",
        "job": site_id,
        "additional_jobs": [],
        "source": "field_capture_production_auth_bootstrap",
    }
    site_name = text_value(site, "location") or "Summit Wire"
    site_frontmatter: dict[str, object] = {
        "type": "location",
        "job": site_id,
        "site_id": site_id,
        "account": text_value(site, "account") or "Summitsteel",
        "location": site_name,
        "status": text_value(site, "status") or "active",
        "source": "field_capture_production_auth_bootstrap",
    }
    address = text_value(site, "address")
    if address:
        site_frontmatter["address"] = address

    write_note(output_root / "People" / DEFAULT_PERSON, person_frontmatter, person_name)
    write_note(output_root / DEFAULT_SITE_ABOUT, site_frontmatter, site_name)


def export_token_db(source_db: Path, output_db: Path, *, token_id: str) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists():
        output_db.unlink()

    with sqlite3.connect(source_db) as source, sqlite3.connect(output_db) as target:
        source.row_factory = sqlite3.Row
        target.execute(
            """
            CREATE TABLE field_capture_tokens (
              token_id TEXT PRIMARY KEY,
              token_hash TEXT NOT NULL UNIQUE,
              person_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT,
              revoked INTEGER NOT NULL DEFAULT 0,
              label TEXT NOT NULL DEFAULT '',
              last_used_at TEXT
            )
            """
        )
        row = source.execute(
            """
            SELECT token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at
            FROM field_capture_tokens
            WHERE token_id = ? AND revoked = 0
            """,
            (token_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Active token not found: {token_id}")
        target.execute(
            """
            INSERT INTO field_capture_tokens
              (token_id, token_hash, person_id, created_at, expires_at, revoked, label, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(row),
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Build minimal production auth data for field-capture.")
    parser.add_argument("--output-root", type=Path, default=Path("/private/tmp/btq-field-capture-prod-auth"))
    parser.add_argument("--source-vault", type=Path, default=config.vault_dir)
    parser.add_argument("--source-db", type=Path, default=config.runtime_root / "field_capture_tokens.sqlite3")
    parser.add_argument("--token-id", default=DEFAULT_TOKEN_ID)
    parser.add_argument("--site-id", default=DEFAULT_SITE_ID)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root.expanduser().resolve(strict=False)
    data_root = output_root / "srv" / "btq" / "data"
    vault_root = data_root / "vault-readonly"
    db_path = data_root / "field_capture_tokens.sqlite3"

    build_vault_mirror(args.source_vault.expanduser(), vault_root, site_id=args.site_id)
    export_token_db(args.source_db.expanduser(), db_path, token_id=args.token_id)

    print(f"output_root: {output_root}")
    print(f"vault_root: {vault_root}")
    print(f"token_db: {db_path}")
    print(f"token_id: {args.token_id}")
    print("raw_token: not exported")
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
