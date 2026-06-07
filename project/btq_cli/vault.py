from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from config import get_config
from ops_dashboard import app as ops_dashboard_app
from vps import ssh as vps_ssh


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    refresh_vault_parser = subparsers.add_parser(
        "refresh-vault",
        help="Sync Clearpath vault sites and people into CouchDB.",
    )
    refresh_vault_parser.add_argument("--vault-root", type=Path)
    refresh_vault_parser.add_argument("--sites-only", action="store_true")
    refresh_vault_parser.add_argument("--people-only", action="store_true")
    refresh_vault_parser.add_argument("--no-prune", action="store_true")
    refresh_vault_parser.add_argument("--dry-run", action="store_true")
    refresh_vault_parser.add_argument("--json", action="store_true")
    refresh_vault_parser.add_argument("--log-path", type=Path)
    refresh_vault_parser.set_defaults(func=handle_refresh_vault)
    setup_couchdb_parser = subparsers.add_parser(
        "setup-couchdb",
        help="Provision CouchDB databases, design docs, site data, and optional replication.",
    )
    setup_couchdb_parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify CouchDB connectivity. Do not create or modify anything.",
    )
    setup_couchdb_parser.add_argument(
        "--with-replication",
        action="store_true",
        help="Also configure continuous replication to the Dell target.",
    )
    setup_couchdb_parser.add_argument(
        "--skip-migrate",
        action="store_true",
        help="Skip site data migration (useful when re-running after a partial failure).",
    )
    setup_couchdb_parser.set_defaults(func=handle_setup_couchdb)
    migrate_vault_parser = subparsers.add_parser("migrate-vault", help="Migrate typed vault markdown into btq_vault.")
    migrate_vault_parser.set_defaults(func=handle_migrate_vault)
    audit_site_coverage_parser = subparsers.add_parser("audit-site-coverage", help="Report active vault locations missing from the site registry.")
    audit_site_coverage_parser.set_defaults(func=handle_audit_site_coverage)
    project_vault_parser = subparsers.add_parser("project-vault", help="Project btq_vault CouchDB data into static HTML.")
    project_vault_parser.set_defaults(func=handle_project_vault)
    backup_vault_parser = subparsers.add_parser("backup-vault", help="Watch btq_vault changes and write iCloud backups.")
    backup_vault_parser.set_defaults(func=handle_backup_vault)
    export_today_parser = subparsers.add_parser("export-vault-today", help="Print today's btq_vault entity activity as markdown text.")
    export_today_parser.add_argument("--date", default=None, help="Date (YYYY-MM-DD). Defaults to today UTC.")
    export_today_parser.set_defaults(func=handle_export_vault_today)
    open_positions_parser = subparsers.add_parser(
        "open-positions",
        help="List sites with net-open recruiting triggers (derived from vault about.md).",
    )
    open_positions_parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root (defaults to effective_config().vault_dir).",
    )
    open_positions_parser.add_argument("--json", action="store_true", help="Output JSON instead of markdown.")
    open_positions_parser.set_defaults(func=handle_open_positions)
    freeze_vault_parser = subparsers.add_parser("freeze-vault", help="Write a FROZEN marker to the vault root and print the git-tag command.")
    freeze_vault_parser.add_argument("--vault-root", required=True, help="Absolute path to the vault directory to freeze.")
    freeze_vault_parser.set_defaults(func=handle_freeze_vault)
    vps_parser = subparsers.add_parser("vps", help="Manage the remote VPS.")
    vps_subparsers = vps_parser.add_subparsers(dest="vps_command", required=True)
    vps_status_parser = vps_subparsers.add_parser("status", help="Show VPS CouchDB service and disk status.")
    vps_status_parser.add_argument("--remote-host", default=vps_ssh.DEFAULT_REMOTE_HOST)
    vps_status_parser.set_defaults(func=handle_vps)
    vps_provision_parser = vps_subparsers.add_parser("provision-couchdb", help="Install CouchDB on the VPS and run full setup.")
    vps_provision_parser.add_argument("--remote-host", default=vps_ssh.DEFAULT_REMOTE_HOST)
    vps_provision_parser.set_defaults(func=handle_vps)
    vps_setup_parser = vps_subparsers.add_parser("setup-couchdb", help="Run CouchDB database setup through an SSH tunnel (skip apt install).")
    vps_setup_parser.add_argument("--remote-host", default=vps_ssh.DEFAULT_REMOTE_HOST)
    vps_setup_parser.add_argument("--with-replication", action="store_true")
    vps_setup_parser.add_argument("--skip-migrate", action="store_true")
    vps_setup_parser.set_defaults(func=handle_vps)
    vps_parser.set_defaults(func=handle_vps)
    ops_dashboard_parser = subparsers.add_parser(
        "ops-dashboard",
        help="Run the read-only local BTQ ops dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ops_dashboard_parser.add_argument("--host", default="127.0.0.1", help="Interface to bind. Use localhost by default.")
    ops_dashboard_parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    ops_dashboard_parser.add_argument("--runtime-root", help="BTQ runtime root to inspect.")
    ops_dashboard_parser.set_defaults(func=handle_ops_dashboard)


def handle_refresh_vault(args: argparse.Namespace) -> int:
    from vault_sync.refresh import run as refresh_run

    return refresh_run(args)


def handle_setup_couchdb(args: argparse.Namespace) -> int:
    from event_pipeline.couchdb.setup_command import run_setup

    return run_setup(
        verify_only=args.verify_only,
        with_replication=args.with_replication,
        skip_migrate=args.skip_migrate,
    )


def handle_migrate_vault(args: argparse.Namespace) -> int:
    from event_pipeline.couchdb import migrate_vault

    return migrate_vault.main()


def handle_audit_site_coverage(args: argparse.Namespace) -> int:
    from event_pipeline.couchdb import audit_site_coverage

    return audit_site_coverage.main()


def handle_project_vault(args: argparse.Namespace) -> int:
    from btq_vault import projection_watcher

    return projection_watcher.main()


def handle_backup_vault(args: argparse.Namespace) -> int:
    from btq_vault.backup_worker import main as backup_main

    return backup_main()


def handle_export_vault_today(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from btq_vault.today_export import export_today
    from event_pipeline import couchdb_config

    target_date = args.date or datetime.now(timezone.utc).date().isoformat()
    try:
        config = couchdb_config.from_env()
        result = export_today(config.base_url, dict(config.auth_header()), couchdb_config.vault_database(), target_date, timeout=config.timeout)
        print(result)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def handle_open_positions(args: argparse.Namespace) -> int:
    from btq_vault.open_positions import compute_open_positions

    root = args.vault_root or get_config().vault_dir
    positions = compute_open_positions(root)
    if args.json:
        import json as _json

        print(_json.dumps([p._asdict() for p in positions], indent=2))
    elif not positions:
        print("(no open positions)")
    else:
        for position in positions:
            date_part = f" — oldest {position.oldest_trigger_date}" if position.oldest_trigger_date else ""
            print(f"- {position.site_name} (#{position.site_id}) — {position.net_open} open position(s){date_part}")
    return 0


def handle_freeze_vault(args: argparse.Namespace) -> int:
    import datetime as _dt

    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    frozen_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag_date = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    marker = vault_root / "FROZEN.md"
    content = (
        f"---\nfrozen_at: {frozen_at}\n---\n\n"
        "This vault is frozen. No new entity data is written here.\n"
        "CouchDB (`btq_vault`) is the entity source of truth from this date forward.\n"
        "Run `btq project-vault` to view live data via the HTML projection.\n"
    )
    marker.write_text(content, encoding="utf-8")
    print(f"Wrote {marker}")
    print("Next step — run this git command to tag the archive:")
    print(f"  git -C {vault_root} tag vault-archive-{tag_date}")
    return 0


def handle_vps(args: argparse.Namespace) -> int:
    from vps import couchdb_ops

    if args.vps_command == "status":
        couchdb_status = couchdb_ops.vps_couchdb_status(args.remote_host)
        print(f"service : {couchdb_status['service']}")
        print(f"version : {couchdb_status['version']}")
        print(f"disk    : {couchdb_status['disk']}")
        return 0

    if args.vps_command == "provision-couchdb":
        admin_user = os.environ.get("BTQ_COUCHDB_USER", "")
        admin_password = os.environ.get("BTQ_COUCHDB_PASSWORD", "")
        if not admin_user or not admin_password:
            print("error: BTQ_COUCHDB_USER and BTQ_COUCHDB_PASSWORD must be set")
            return 1
        steps = couchdb_ops.install_couchdb(args.remote_host)
        for label, code, output in steps:
            status_str = "ok" if code == 0 else f"FAILED (exit {code})"
            print(f"  {label}: {status_str}")
            if output:
                print(f"    {output}")
            if code != 0:
                return 1
        rc, out = couchdb_ops.create_couchdb_admin(args.remote_host, admin_user, admin_password)
        print(f"  create admin: {'ok' if rc == 0 else f'FAILED (exit {rc})'}")
        if out:
            print(f"    {out}")
        if rc != 0:
            return 1
        return couchdb_ops.run_remote_setup(args.remote_host)

    if args.vps_command == "setup-couchdb":
        return couchdb_ops.run_remote_setup(
            args.remote_host,
            with_replication=args.with_replication,
            skip_migrate=args.skip_migrate,
        )
    raise SystemExit(f"Unknown command: {args.command}")


def handle_ops_dashboard(args: argparse.Namespace) -> int:
    dashboard_args: list[str] = ["--host", args.host, "--port", str(args.port)]
    if args.runtime_root is not None:
        dashboard_args.extend(["--runtime-root", str(args.runtime_root)])
    return ops_dashboard_app.run(dashboard_args)
