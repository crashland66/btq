from __future__ import annotations

import argparse

import pytest

import btq
from btq_cli import backfill, field, replay_skill, reports, vault


EXPECTED_SUBCOMMANDS = [
    "health",
    "pipeline-metrics-sample",
    "queue-status",
    "trigger-nightly-digest",
    "process-durable-queue",
    "repair-index",
    "inspect-runtime",
    "reconciliation-report",
    "narrative-report",
    "unresolved-report",
    "export-docs",
    "backfill-unknown-captures",
    "backfill-field-capture-employees",
    "dedupe-employee-docs",
    "backfill-media-to-r2",
    "audit-media-coverage",
    "shift-report-to-sc",
    "replay",
    "skill",
    "transcribe-field-audio",
    "process-field-audio-semantics",
    "describe-field-photos",
    "audit-field-capture-pilot",
    "list-approved-drafts",
    "mark-client-informed",
    "show-review-item",
    "stage-approved-drafts",
    "review-status",
    "review-maintenance-status",
    "review-dashboard",
    "export-field-capture-site-status",
    "pull-field-capture",
    "watch-field-capture-pipeline",
    "watch-couchdb-field-captures",
    "watch-couchdb-queue",
    "watch-couchdb-job-drafts",
    "watch-couchdb-voice-memos",
    "setup-couchdb",
    "audit-site-coverage",
    "backup-vault",
    "export-vault-today",
    "vps",
    "ops-dashboard",
    "replay-plan",
    "replay-dry-run",
    "replay-execute",
]


def _parser() -> argparse.ArgumentParser:
    parser_holder: dict[str, argparse.ArgumentParser] = {}
    original_parse_args = argparse.ArgumentParser.parse_args

    def capture_parse_args(self: argparse.ArgumentParser, args: list[str] | None = None, namespace: argparse.Namespace | None = None) -> argparse.Namespace:
        parser_holder["parser"] = self
        return argparse.Namespace()

    argparse.ArgumentParser.parse_args = capture_parse_args
    try:
        btq.parse_args([])
    finally:
        argparse.ArgumentParser.parse_args = original_parse_args
    return parser_holder["parser"]


def _subcommands() -> list[str]:
    parser = _parser()
    action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    return list(action.choices)


def _route(monkeypatch: pytest.MonkeyPatch, module: object, handler_name: str, argv: list[str]) -> argparse.Namespace:
    captured: dict[str, argparse.Namespace] = {}

    def fake_handler(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 17

    monkeypatch.setattr(module, handler_name, fake_handler)
    assert btq.run(argv) == 17
    return captured["args"]


def test_every_subcommand_still_registered() -> None:
    assert _subcommands() == EXPECTED_SUBCOMMANDS


def test_health_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(monkeypatch, reports, "handle_health", ["health", "--runtime-root", "runtime", "--emit-metrics"])
    assert args.command == "health"
    assert args.runtime_root == "runtime"
    assert args.emit_metrics is True


def test_backfill_unknown_captures_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(monkeypatch, backfill, "handle_backfill_unknown_captures", ["backfill-unknown-captures", "--dry-run"])
    assert args.command == "backfill-unknown-captures"
    assert args.dry_run is True


def test_backfill_media_to_r2_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(
        monkeypatch,
        backfill.media_backfill_to_r2,
        "handle_backfill_media_to_r2",
        ["backfill-media-to-r2", "--runtime-root", "runtime", "--dry-run", "--limit", "5", "--verbose"],
    )
    assert args.command == "backfill-media-to-r2"
    assert str(args.runtime_root) == "runtime"
    assert args.dry_run is True
    assert args.limit == 5
    assert args.verbose is True


def test_skill_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(monkeypatch, replay_skill, "handle_skill", ["skill", "show", "alpha", "--version", "1"])
    assert args.command == "skill"
    assert args.skill_command == "show"
    assert args.skill_id == "alpha"
    assert args.version == "1"


def test_transcribe_field_audio_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(monkeypatch, field, "handle_transcribe_field_audio", ["transcribe-field-audio", "--limit", "2", "--json"])
    assert args.command == "transcribe-field-audio"
    assert args.limit == 2
    assert args.json is True


def test_replay_plan_parses_args_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    args = _route(monkeypatch, replay_skill, "handle_replay_command", ["replay-plan", "--capture-id", "cap-1", "--json"])
    assert args.command == "replay-plan"
    assert args.capture_id == "cap-1"
    assert args.json is True


def test_unknown_subcommand_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        btq.parse_args(["not-a-command"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'not-a-command'" in capsys.readouterr().err
