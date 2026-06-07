from __future__ import annotations

import argparse
import json
from pathlib import Path

from btq_vault.couch_store import CouchDBEntityStore
from config import get_config
from field_capture import backfill_employees as field_backfill_employees
from field_capture import dedupe_employee_docs as field_dedupe_employee_docs
from queue_processor import backfill_unknown_captures


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backfill_unknowns_parser = subparsers.add_parser(
        "backfill-unknown-captures",
        help="Backfill historical Journal/*-unknown.md blocks into canonical unknown_capture CouchDB docs.",
    )
    backfill_unknowns_parser.add_argument("--vault-root", type=Path, help="Vault root containing Journal/*-unknown.md files.")
    backfill_unknowns_parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing CouchDB docs.")
    backfill_unknowns_parser.add_argument("--json", action="store_true")
    backfill_unknowns_parser.set_defaults(func=handle_backfill_unknown_captures)
    backfill_field_employees_parser = subparsers.add_parser(
        "backfill-field-capture-employees",
        help="ADD-only canonical employee backfill for active field-capture token people or all People notes.",
    )
    backfill_field_employees_parser.add_argument("--token-db", "--db", dest="token_db", type=Path, help="field_capture_tokens.sqlite3 path.")
    backfill_field_employees_parser.add_argument("--vault-root", type=Path, help="Vault root containing People/*.md files.")
    backfill_field_employees_parser.add_argument("--all-people", action="store_true", help="Backfill missing canonical employee docs for every People/*.md note.")
    backfill_field_employees_parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing CouchDB docs.")
    backfill_field_employees_parser.add_argument("--json", action="store_true")
    backfill_field_employees_parser.set_defaults(func=handle_backfill_field_capture_employees)
    dedupe_employee_docs_parser = subparsers.add_parser(
        "dedupe-employee-docs",
        help="Merge duplicate canonical employee docs into the active field-capture-token survivor.",
    )
    dedupe_employee_docs_parser.add_argument("--token-db", "--db", dest="token_db", type=Path, help="field_capture_tokens.sqlite3 path.")
    dedupe_employee_docs_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Report the dedupe plan without writing. Use --no-dry-run to apply.",
    )
    dedupe_employee_docs_parser.add_argument("--json", action="store_true")
    dedupe_employee_docs_parser.set_defaults(func=handle_dedupe_employee_docs)


def handle_backfill_unknown_captures(args: argparse.Namespace) -> int:
    config = get_config()
    vault_root = args.vault_root.expanduser() if args.vault_root is not None else config.vault_dir
    store = CouchDBEntityStore.from_env()
    report = backfill_unknown_captures.backfill(store, vault_root, dry_run=bool(args.dry_run))
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "write"
        print(f"BTQ unknown_capture Markdown backfill ({mode})")
        print(
            "files_scanned={files_scanned} blocks_found={blocks_found} created={created} would_create={would_create} "
            "skipped_existing={skipped_existing} note_journal_reconciled={note_journal_reconciled} errors={errors}".format(
                **{**report.as_dict(), "errors": len(report.errors)}
            )
        )
        for doc_id, message in report.errors:
            print(f"error: {doc_id}: {message}")
    return 1 if report.errors else 0


def handle_backfill_field_capture_employees(args: argparse.Namespace) -> int:
    config = get_config()
    vault_root = args.vault_root.expanduser() if args.vault_root is not None else config.vault_dir
    store = CouchDBEntityStore.from_env()
    if args.all_people:
        report = field_backfill_employees.backfill_all_people_employees(
            store,
            vault_root,
            dry_run=bool(args.dry_run),
        )
    else:
        token_db = args.token_db.expanduser() if args.token_db is not None else config.runtime_root / "field_capture_tokens.sqlite3"
        token_store = field_backfill_employees.TokenStore(token_db)
        report = field_backfill_employees.backfill_field_capture_employees(
            store,
            token_store,
            vault_root,
            dry_run=bool(args.dry_run),
        )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "write"
        scope = "all People" if args.all_people else "field-capture"
        print(f"BTQ {scope} canonical employee backfill ({mode})")
        print(
            "active_tokens={active_tokens} distinct_people={distinct_people} created={created} "
            "would_create={would_create} skipped_existing={skipped_existing} missing_people={missing_people} "
            "unkeyable_people={unkeyable_people} errors={errors}".format(
                **{
                    **report.as_dict(),
                    "missing_people": len(report.missing_people),
                    "unkeyable_people": len(report.unkeyable_people),
                    "errors": len(report.errors),
                }
            )
        )
        for item in report.would_create_docs:
            print(f"would_create: {item['_id']} {item['name']} sites={','.join(item['site_ids'])} path={item['vault_path']}")
        for item in report.created_docs:
            print(f"created: {item['_id']} {item['name']} sites={','.join(item['site_ids'])} path={item['vault_path']}")
        for item in report.missing_people:
            print(f"missing_people: {item['person_id']} labels={','.join(item['labels'])} site_ids={','.join(item['site_ids'])}")
        for item in report.unkeyable_people:
            print(f"unkeyable_people: {item['path']}: {item['message']}")
        for error_item in report.errors:
            print(f"error: {error_item['id']}: {error_item['message']}")
    return 1 if report.errors else 0


def handle_dedupe_employee_docs(args: argparse.Namespace) -> int:
    config = get_config()
    store = CouchDBEntityStore.from_env()
    token_db = args.token_db.expanduser() if args.token_db is not None else config.runtime_root / "field_capture_tokens.sqlite3"
    token_store = field_dedupe_employee_docs.TokenStore(token_db)
    report = field_dedupe_employee_docs.dedupe_employee_docs(
        store,
        token_store,
        dry_run=bool(args.dry_run),
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "write"
        print(f"BTQ canonical employee dedupe ({mode})")
        print(
            "employee_docs={employee_docs} candidate_groups={candidate_groups} planned_merges={planned_merges} "
            "applied_merges={applied_merges} deleted_docs={deleted_docs} skipped_groups={skipped_groups} errors={errors}".format(
                **{**report.as_dict(), "errors": len(report.errors)}
            )
        )
        for item in report.items:
            if item.status in {"planned", "applied"}:
                before = "; ".join(
                    f"{doc_id}=[{','.join(job_ids)}]" for doc_id, job_ids in sorted(item.job_ids_before.items())
                )
                actions = "; ".join(
                    f"{doc_id}:{action}" for doc_id, action in sorted(item.content_actions.items())
                )
                print(
                    f"{item.status}: survivor={item.survivor} merged_from={','.join(item.merged_from)} "
                    f"job_ids_before={before} job_ids_after={','.join(item.job_ids_after)} "
                    f"content_actions={actions}"
                )
            else:
                print(f"{item.status}: {item.group_key}: {item.message}")
        for error_item in report.errors:
            print(f"error: {error_item['group_key']}: {error_item['message']}")
    return 1 if report.errors else 0
