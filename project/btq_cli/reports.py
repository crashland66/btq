from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import docs_export
from btq_vault import markdown_export
from btq_vault.couch_store import CouchDBEntityStore
from config import get_config
from queue_processor import governance
from queue_processor import health
from queue_processor import inspect_runtime
from queue_processor import main as queue_processor_main
from queue_processor import metrics as queue_metrics
from queue_processor import narrative
from queue_processor import reconciliation
from queue_processor import repair
from queue_processor import status


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    health_parser = subparsers.add_parser("health", help="Report runtime health.")
    health_parser.add_argument("--runtime-root")
    health_parser.add_argument("--vault-root")
    health_parser.add_argument("--queue-backlog-threshold", type=int)
    health_parser.add_argument("--failed-age-hours", type=int)
    health_parser.add_argument("--stale-claimed-hours", type=int)
    health_parser.add_argument("--emit-metrics", action="store_true", help="Append one pipeline metrics sample as part of the health check.")
    health_parser.add_argument("--metrics-retain-hours", type=int)
    health_parser.add_argument("--monitor", action="store_true", help="Scheduled mode: emit a throttled durable alert when health is critical.")
    health_parser.add_argument("--alert-throttle-hours", type=float)
    health_parser.add_argument("--quiet", action="store_true", help="Suppress non-critical scheduled output.")
    health_parser.set_defaults(func=handle_health)
    metrics_parser = subparsers.add_parser(
        "pipeline-metrics-sample",
        help="Append one pipeline metrics sample; install cron/launchd at a recommended 15-minute interval.",
        description="Append one pipeline metrics sample. Install cron/launchd at a recommended 15-minute interval; this command does not run its own timer.",
    )
    metrics_parser.add_argument("--runtime-root")
    metrics_parser.add_argument("--retain-hours", type=int, default=queue_metrics.DEFAULT_RETAIN_HOURS)
    metrics_parser.set_defaults(func=handle_pipeline_metrics_sample)
    status_parser = subparsers.add_parser("queue-status", help="Show queue transport and runtime status.")
    status_parser.add_argument("--outbox-dir")
    status_parser.add_argument("--runtime-root")
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(func=handle_queue_status)
    trigger_digest_parser = subparsers.add_parser(
        "trigger-nightly-digest",
        help="Build the nightly digest for a given date and write it to the vault.",
    )
    trigger_digest_parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    trigger_digest_parser.add_argument("--vault-root", type=Path)
    trigger_digest_parser.add_argument("--runtime-root", type=Path)
    trigger_digest_parser.add_argument("--dry-run", action="store_true")
    trigger_digest_parser.set_defaults(func=handle_trigger_nightly_digest)
    durable_parser = subparsers.add_parser("process-durable-queue", help="Process only the local durable runtime queue.")
    durable_parser.add_argument("--runtime-root")
    durable_parser.add_argument("--vault-root")
    durable_parser.add_argument("--project-root")
    durable_parser.add_argument("--personal-vault-root")
    durable_parser.add_argument("--skip-unknowns", action=argparse.BooleanOptionalAction, default=True)
    durable_parser.add_argument("--dry-run", action="store_true")
    durable_parser.add_argument("--json", action="store_true")
    durable_parser.set_defaults(func=handle_process_durable_queue)
    repair_parser = subparsers.add_parser("repair-index", help="Inspect and repair derived processed-index state.")
    repair_parser.add_argument("--runtime-root")
    repair_parser.add_argument("--vault-root")
    repair_parser.add_argument("--dry-run", action="store_true")
    repair_parser.add_argument("--force", action="store_true")
    repair_parser.add_argument("--apply", action="store_true")
    repair_parser.add_argument("--since")
    repair_parser.add_argument("--capture-id")
    repair_parser.add_argument("--mode", choices=sorted(repair.MODES))
    repair_parser.add_argument("--json", action="store_true")
    repair_parser.set_defaults(func=handle_repair_index)
    inspect_parser = subparsers.add_parser("inspect-runtime", help="Inspect stale runtime artifacts.")
    inspect_parser.add_argument("--runtime-root")
    inspect_parser.add_argument("--stale-hours", type=int)
    inspect_parser.add_argument("--emit-events", action="store_true")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(func=handle_inspect_runtime)
    reconciliation_parser = subparsers.add_parser("reconciliation-report", help="Generate operational integrity reconciliation report.")
    reconciliation_parser.add_argument("--runtime-root")
    reconciliation_parser.add_argument("--vault-root")
    reconciliation_parser.add_argument("--personal-vault-root")
    reconciliation_parser.add_argument("--capture-id")
    reconciliation_parser.add_argument("--since")
    reconciliation_parser.add_argument("--high-risk-only", action="store_true")
    reconciliation_parser.add_argument("--json", action="store_true")
    reconciliation_parser.set_defaults(func=handle_reconciliation_report)
    narrative_parser = subparsers.add_parser("narrative-report", help="Generate epistemic operational narrative timeline.")
    narrative_parser.add_argument("--runtime-root")
    narrative_parser.add_argument("--capture-id")
    narrative_parser.add_argument("--account")
    narrative_parser.add_argument("--since")
    narrative_parser.add_argument("--include-contradictions", action="store_true")
    narrative_parser.add_argument("--json", action="store_true")
    narrative_parser.set_defaults(func=handle_narrative_report)
    unresolved_parser = subparsers.add_parser("unresolved-report", help="Surface unresolved epistemic governance items.")
    unresolved_parser.add_argument("--runtime-root")
    unresolved_parser.add_argument("--vault-root")
    unresolved_parser.add_argument("--personal-vault-root")
    unresolved_parser.add_argument("--since")
    unresolved_parser.add_argument("--account")
    unresolved_parser.add_argument("--high-risk-only", action="store_true")
    unresolved_parser.add_argument("--json", action="store_true")
    unresolved_parser.set_defaults(func=handle_unresolved_report)
    export_docs_parser = subparsers.add_parser("export-docs", help="Export repo-owned bootstrap/config docs to BTDocs.")
    export_docs_parser.add_argument("--manifest")
    export_docs_parser.add_argument("--docs-dir")
    export_docs_parser.add_argument("--json", action="store_true")
    export_docs_parser.set_defaults(func=handle_export_docs)
    markdown_export_parser = subparsers.add_parser(
        "markdown-export",
        help="Regenerate the human-readable Markdown projection from canonical CouchDB entities; this is not a canonical write.",
    )
    markdown_export_parser.add_argument("--vault-root", type=Path, help="Vault root to receive exported Markdown projection files.")
    markdown_export_parser.add_argument("--type", action="append", dest="types", choices=markdown_export.EXPORTABLE_TYPES, help="Entity type to export. Repeat for multiple types.")
    markdown_export_parser.add_argument("--site", help="Restrict export to a site_id or related_site value.")
    markdown_export_parser.add_argument("--dry-run", action="store_true", help="Render and report what would change without writing Markdown files.")
    markdown_export_parser.add_argument("--json", action="store_true")
    markdown_export_parser.set_defaults(func=handle_markdown_export)


def handle_health(args: argparse.Namespace) -> int:
    health_args: list[str] = []
    for name in ("runtime_root", "vault_root", "queue_backlog_threshold", "failed_age_hours", "stale_claimed_hours", "metrics_retain_hours", "alert_throttle_hours"):
        value = getattr(args, name)
        if value is None:
            continue
        health_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.emit_metrics:
        health_args.append("--emit-metrics")
    if args.monitor:
        health_args.append("--monitor")
    if args.quiet:
        health_args.append("--quiet")
    return health.run(health_args)


def handle_pipeline_metrics_sample(args: argparse.Namespace) -> int:
    config = get_config()
    runtime_root = Path(args.runtime_root).expanduser() if args.runtime_root is not None else config.project_runtime_root
    metrics_path = queue_metrics.metrics_path_for(runtime_root)
    sample = queue_metrics.sample_metrics(runtime_root)
    queue_metrics.append_sample(metrics_path, sample)
    pruned = queue_metrics.prune(metrics_path, args.retain_hours)
    print(f"Wrote pipeline metrics sample: {metrics_path}")
    print(f"Pruned old samples: {pruned}")
    return 0


def handle_queue_status(args: argparse.Namespace) -> int:
    status_args: list[str] = []
    for name in ("outbox_dir", "runtime_root"):
        value = getattr(args, name)
        if value is not None:
            status_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        status_args.append("--json")
    return status.run(status_args)


def handle_trigger_nightly_digest(args: argparse.Namespace) -> int:
    from config import get_config as _get_config
    from nightly_digest_builder import DigestPaths, build_digest, output_path_for_date

    cfg = _get_config()
    vault_root = (args.vault_root or cfg.vault_dir).expanduser()
    runtime_root = (args.runtime_root or cfg.runtime_root).expanduser()
    target_date = args.date
    digest_text = build_digest(
        target_date,
        DigestPaths(
            vault_root=vault_root,
            local_root=cfg.local_root,
            runtime_root=runtime_root,
            logs_dir=cfg.logs_dir,
        ),
    )
    if args.dry_run:
        print(digest_text)
        return 0
    output_path = output_path_for_date(vault_root, target_date, None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    from io_atomic import atomic_write_text

    atomic_write_text(output_path, digest_text + "\n")
    print(f"Written: {output_path}")
    return 0


def handle_process_durable_queue(args: argparse.Namespace) -> int:
    config = get_config()
    project_root = Path(args.project_root).expanduser() if args.project_root is not None else config.project_dir
    vault_root = Path(args.vault_root).expanduser() if args.vault_root is not None else config.vault_dir
    runtime_root = Path(args.runtime_root).expanduser() if args.runtime_root is not None else config.project_runtime_root
    personal_vault_root = Path(args.personal_vault_root).expanduser() if args.personal_vault_root is not None else config.personal_vault_dir
    try:
        if args.json:
            processor_stdout = io.StringIO()
            with redirect_stdout(processor_stdout):
                report = queue_processor_main.process_all(
                    project_root=project_root,
                    vault_root=vault_root,
                    personal_vault_root=personal_vault_root,
                    runtime_root=runtime_root,
                    dry_run=bool(args.dry_run),
                    skip_unknowns=bool(args.skip_unknowns),
                )
            report["processor_output"] = processor_stdout.getvalue().splitlines()
        else:
            report = queue_processor_main.process_all(
                project_root=project_root,
                vault_root=vault_root,
                personal_vault_root=personal_vault_root,
                runtime_root=runtime_root,
                dry_run=bool(args.dry_run),
                skip_unknowns=bool(args.skip_unknowns),
            )
    except queue_processor_main.QueueProcessorError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(str(exc))
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("BTQ durable queue process")
        print(f"discovered={report['discovered']} processed={report['processed']} failed={report['failed']} skipped={report['skipped']}")
        print(f"queue_before={report['queue_before']} queue_after={report['queue_after']}")
        print(f"unknown_reclassification_skipped={report['unknown_reclassification_skipped']}")
        if report["failed_paths"]:
            print("failed_paths:")
            for path in report["failed_paths"]:
                print(f"- {path}")
    return 0


def handle_repair_index(args: argparse.Namespace) -> int:
    repair_args: list[str] = []
    for name in ("runtime_root", "vault_root", "since", "capture_id", "mode"):
        value = getattr(args, name)
        if value is not None:
            repair_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.force:
        repair_args.append("--force")
    if args.apply:
        repair_args.append("--apply")
    elif args.dry_run:
        repair_args.append("--dry-run")
    if args.json:
        repair_args.append("--json")
    return repair.run(repair_args)


def handle_inspect_runtime(args: argparse.Namespace) -> int:
    inspect_args: list[str] = []
    for name in ("runtime_root", "stale_hours"):
        value = getattr(args, name)
        if value is not None:
            inspect_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.emit_events:
        inspect_args.append("--emit-events")
    if args.json:
        inspect_args.append("--json")
    return inspect_runtime.run(inspect_args)


def handle_reconciliation_report(args: argparse.Namespace) -> int:
    report_args: list[str] = []
    for name in ("runtime_root", "vault_root", "personal_vault_root", "capture_id", "since"):
        value = getattr(args, name)
        if value is not None:
            report_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.high_risk_only:
        report_args.append("--high-risk-only")
    if args.json:
        report_args.append("--json")
    return reconciliation.run(report_args)


def handle_narrative_report(args: argparse.Namespace) -> int:
    narrative_args: list[str] = []
    for name in ("runtime_root", "capture_id", "account", "since"):
        value = getattr(args, name)
        if value is not None:
            narrative_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_contradictions:
        narrative_args.append("--include-contradictions")
    if args.json:
        narrative_args.append("--json")
    return narrative.run(narrative_args)


def handle_unresolved_report(args: argparse.Namespace) -> int:
    governance_args: list[str] = []
    for name in ("runtime_root", "vault_root", "personal_vault_root", "since", "account"):
        value = getattr(args, name)
        if value is not None:
            governance_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.high_risk_only:
        governance_args.append("--high-risk-only")
    if args.json:
        governance_args.append("--json")
    return governance.run(governance_args)


def handle_export_docs(args: argparse.Namespace) -> int:
    export_args: list[str] = []
    for name in ("manifest", "docs_dir"):
        value = getattr(args, name)
        if value is not None:
            export_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        export_args.append("--json")
    return docs_export.run(export_args)


def handle_markdown_export(args: argparse.Namespace) -> int:
    config = get_config()
    vault_root = args.vault_root.expanduser() if args.vault_root is not None else config.vault_dir
    store = CouchDBEntityStore.from_env()
    report = markdown_export.export_all(
        store,
        vault_root,
        types=args.types,
        site=args.site,
        dry_run=bool(args.dry_run),
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        mode = "dry-run" if args.dry_run else "write"
        print(f"BTQ Markdown projection export ({mode})")
        print(
            "seen={seen} rendered={rendered} written={written} would_write={would_write} unchanged={unchanged} skipped={skipped} errors={errors}".format(
                **{**report.as_dict(), "errors": len(report.errors)}
            )
        )
        for error in report.errors:
            print(f"error: {error.doc_id}: {error.message}")
    return 1 if report.errors else 0
