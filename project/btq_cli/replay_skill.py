from __future__ import annotations

import argparse

import replay_journal
import skills
from queue_processor import replay


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    replay_journal_parser = subparsers.add_parser("replay", help="Replay a BTQ execution journal.")
    replay_journal_parser.add_argument("journal_file")
    replay_journal_parser.add_argument("--execute", action="store_true")
    replay_journal_parser.add_argument("--mode", choices=sorted({"dry-run", "approve-required", "auto-safe"}), default="dry-run")
    replay_journal_parser.add_argument("--approve", action="store_true")
    replay_journal_parser.add_argument("--diff", action="store_true")
    replay_journal_parser.set_defaults(func=handle_replay)
    skill_parser = subparsers.add_parser("skill", help="List, inspect, validate, and compose versioned skills.")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)
    skill_list_parser = skill_subparsers.add_parser("list", help="List available skills.")
    skill_list_parser.set_defaults(func=handle_skill)
    skill_show_parser = skill_subparsers.add_parser("show", help="Show skill metadata and prompt path.")
    skill_show_parser.add_argument("skill_id")
    skill_show_parser.add_argument("--version")
    skill_show_parser.set_defaults(func=handle_skill)
    skill_run_parser = skill_subparsers.add_parser("run", help="Compose a skill prompt with an input file.")
    skill_run_parser.add_argument("skill_id")
    skill_run_parser.add_argument("--version")
    skill_run_parser.add_argument("--input", required=True)
    skill_run_parser.add_argument("--out")
    skill_run_parser.add_argument("--structured", action="store_true")
    skill_run_parser.add_argument("--to-queue-dry-run", action="store_true")
    skill_run_parser.add_argument("--out-queue")
    skill_run_parser.add_argument("--execute", action="store_true")
    skill_run_parser.add_argument("--mode", choices=sorted({"dry-run", "approve-required", "auto-safe"}), default="dry-run")
    skill_run_parser.add_argument("--approve", action="store_true")
    skill_run_parser.set_defaults(func=handle_skill)
    skill_validate_parser = skill_subparsers.add_parser("validate", help="Validate all skills or one skill.")
    skill_validate_parser.add_argument("skill_id", nargs="?")
    skill_validate_parser.set_defaults(func=handle_skill)
    skill_parser.set_defaults(func=handle_skill)


def register_replay_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for command in ("replay-plan", "replay-dry-run", "replay-execute"):
        replay_parser = subparsers.add_parser(command, help=f"Run BTQ {command.replace('-', ' ')}.")
        replay_parser.add_argument("--runtime-root")
        replay_parser.add_argument("--vault-root")
        replay_parser.add_argument("--personal-vault-root")
        replay_parser.add_argument("--capture-id")
        replay_parser.add_argument("--job-id")
        replay_parser.add_argument("--since")
        replay_parser.add_argument("--failed-only", action="store_true")
        replay_parser.add_argument("--missing-index", action="store_true")
        replay_parser.add_argument("--manifest-gap", action="store_true")
        replay_parser.add_argument("--from-queue")
        replay_parser.add_argument("--plan-file")
        replay_parser.add_argument("--output")
        replay_parser.add_argument("--approve", action="store_true")
        replay_parser.add_argument("--force-dangerous-replay", action="store_true")
        replay_parser.add_argument("--json", action="store_true")
        replay_parser.set_defaults(func=handle_replay_command)


def handle_replay(args: argparse.Namespace) -> int:
    replay_args: list[str] = [args.journal_file]
    if args.execute:
        replay_args.append("--execute")
    if args.mode != "dry-run":
        replay_args.extend(["--mode", args.mode])
    if args.approve:
        replay_args.append("--approve")
    if args.diff:
        replay_args.append("--diff")
    return replay_journal.run(replay_args)


def handle_skill(args: argparse.Namespace) -> int:
    skill_args: list[str] = [args.skill_command]
    if args.skill_command in {"show", "run"}:
        skill_args.append(args.skill_id)
    if getattr(args, "version", None) is not None:
        skill_args.extend(["--version", args.version])
    if args.skill_command == "run":
        skill_args.extend(["--input", args.input])
        if args.out is not None:
            skill_args.extend(["--out", args.out])
        if args.structured:
            skill_args.append("--structured")
        if args.to_queue_dry_run:
            skill_args.append("--to-queue-dry-run")
        if args.out_queue is not None:
            skill_args.extend(["--out-queue", args.out_queue])
        if args.execute:
            skill_args.append("--execute")
        if args.mode != "dry-run":
            skill_args.extend(["--mode", args.mode])
        if args.approve:
            skill_args.append("--approve")
    if args.skill_command == "validate" and args.skill_id is not None:
        skill_args.append(args.skill_id)
    return skills.run(skill_args)


def handle_replay_command(args: argparse.Namespace) -> int:
    replay_command = {"replay-plan": "plan", "replay-dry-run": "dry-run", "replay-execute": "execute"}[args.command]
    replay_args: list[str] = [replay_command]
    common_names = ["runtime_root", "vault_root", "personal_vault_root", "capture_id", "job_id", "since", "from_queue"]
    if args.command in {"replay-dry-run", "replay-execute"}:
        common_names.append("plan_file")
    if args.command == "replay-plan":
        common_names.append("output")
    for name in common_names:
        value = getattr(args, name)
        if value is not None:
            replay_args.extend([f"--{name.replace('_', '-')}", str(value)])
    for name in ("failed_only", "missing_index", "manifest_gap", "approve", "force_dangerous_replay", "json"):
        if getattr(args, name):
            replay_args.append(f"--{name.replace('_', '-')}")
    return replay.run(replay_args)
