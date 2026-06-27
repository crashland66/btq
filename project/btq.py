from __future__ import annotations

import argparse

from btq_cli import backfill, field, replay_skill, reports, shift_report_to_sc, vault


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTQ operational commands.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reports.register(subparsers)
    backfill.register(subparsers)
    shift_report_to_sc.register(subparsers)
    replay_skill.register(subparsers)
    field.register(subparsers)
    vault.register(subparsers)
    replay_skill.register_replay_commands(subparsers)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
