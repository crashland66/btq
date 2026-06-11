from __future__ import annotations

import argparse
from pathlib import Path

from field_capture import action_candidates as field_action_candidates
from field_capture import approved_job_drafts as field_approved_job_drafts
from field_capture import audio_semantics as field_audio_semantics
from field_capture import audio_transcription as field_audio_transcription
from field_capture import client_notifications as field_client_notifications
from field_capture import draft_staging as field_draft_staging
from field_capture import pipeline_watcher as field_pipeline_watcher
from field_capture import pilot_audit as field_pilot_audit
from field_capture import photo_vision as field_photo_vision
from field_capture import pull_bundle as field_pull_bundle
from field_capture import review_dashboard as field_review_dashboard
from field_capture import review_items as field_review_items
from field_capture import review_maintenance as field_review_maintenance
from field_capture import review_status as field_review_status
from field_capture import site_status_export as field_site_status_export


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    field_audio_parser = subparsers.add_parser("transcribe-field-audio", help="Transcribe uploaded field-capture voice notes.")
    field_audio_parser.add_argument("--runtime-root")
    field_audio_parser.add_argument("--intake-dir")
    field_audio_parser.add_argument("--upload-dir")
    field_audio_parser.add_argument("--transcript-dir")
    field_audio_parser.add_argument("--log-path")
    field_audio_parser.add_argument("--model")
    field_audio_parser.add_argument("--initial-prompt")
    field_audio_parser.add_argument("--no-initial-prompt", action="store_true")
    field_audio_parser.add_argument("--worker-timeout-seconds", type=float)
    field_audio_parser.add_argument("--limit", type=int)
    field_audio_parser.add_argument("--json", action="store_true")
    field_audio_parser.set_defaults(func=handle_transcribe_field_audio)
    field_semantic_parser = subparsers.add_parser("process-field-audio-semantics", help="Create semantic artifacts for field-capture audio transcripts.")
    field_semantic_parser.add_argument("--runtime-root")
    field_semantic_parser.add_argument("--transcript-dir")
    field_semantic_parser.add_argument("--semantic-dir")
    field_semantic_parser.add_argument("--log-path")
    field_semantic_parser.add_argument("--json", action="store_true")
    field_semantic_parser.set_defaults(func=handle_process_field_audio_semantics)
    photo_vision_parser = subparsers.add_parser("describe-field-photos", help="Describe field-capture photos with local vision sidecars.")
    photo_vision_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    photo_vision_parser.add_argument("--runtime-root")
    photo_vision_parser.add_argument("--intake-dir")
    photo_vision_parser.add_argument("--upload-dir")
    photo_vision_parser.add_argument("--photo-vision-dir")
    photo_vision_parser.add_argument("--dry-run", action="store_true")
    photo_vision_parser.add_argument("--capture-id")
    photo_vision_parser.add_argument("--site-id")
    photo_vision_parser.add_argument("--date")
    photo_vision_parser.add_argument("--photo-asset-id")
    photo_vision_parser.add_argument("--limit", type=int)
    photo_vision_parser.add_argument("--replace-failed", action="store_true")
    photo_vision_parser.add_argument("--replace-flagged-judgment-language", action="store_true")
    photo_vision_parser.add_argument("--model")
    photo_vision_parser.add_argument(
        "--backend",
        choices=["ollama", "mlx"],
        default=None,
        help="Vision inference backend. 'mlx' runs a HuggingFace model locally via mlx-vlm (Apple Silicon).",
    )
    photo_vision_parser.add_argument("--ollama-url")
    photo_vision_parser.add_argument("--timeout-seconds", type=float)
    photo_vision_parser.add_argument("--json", action="store_true")
    photo_vision_parser.set_defaults(func=handle_describe_field_photos)
    pilot_audit_parser = subparsers.add_parser("audit-field-capture-pilot", help="Read-only audit of local field-capture pilot data.")
    pilot_audit_parser.add_argument("--runtime-root")
    pilot_audit_parser.add_argument("--site-id", required=True)
    pilot_audit_parser.add_argument("--date", required=True)
    pilot_audit_parser.add_argument("--include-paths", action="store_true")
    pilot_audit_parser.add_argument("--limit", type=int)
    pilot_audit_parser.add_argument("--output-md")
    pilot_audit_parser.add_argument("--output-json")
    pilot_audit_parser.add_argument("--json", action="store_true")
    pilot_audit_parser.set_defaults(func=handle_audit_field_capture_pilot)
    collect_candidates_parser = subparsers.add_parser("collect-action-candidates", help="Collect review candidates from semantic artifacts.")
    collect_candidates_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    collect_candidates_parser.add_argument("--runtime-root")
    collect_candidates_parser.add_argument("--semantic-dir")
    collect_candidates_parser.add_argument("--candidate-dir")
    collect_candidates_parser.add_argument("--dry-run", action="store_true")
    collect_candidates_parser.add_argument("--json", action="store_true")
    collect_candidates_parser.set_defaults(func=handle_collect_action_candidates)
    list_candidates_parser = subparsers.add_parser("list-action-candidates", help="List review candidates for operator approval.")
    list_candidates_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    list_candidates_parser.add_argument("--runtime-root")
    list_candidates_parser.add_argument("--candidate-dir")
    list_candidates_parser.add_argument("--status", choices=["pending_review", "approved", "rejected", "failed"])
    list_candidates_parser.add_argument("--limit", type=int)
    list_candidates_parser.add_argument("--include-source", action="store_true")
    list_candidates_parser.add_argument("--json", action="store_true")
    list_candidates_parser.set_defaults(func=handle_list_action_candidates)
    generate_drafts_parser = subparsers.add_parser("generate-approved-drafts", help="Generate approved queue-job drafts from approved candidates.")
    generate_drafts_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    generate_drafts_parser.add_argument("--runtime-root")
    generate_drafts_parser.add_argument("--candidate-dir")
    generate_drafts_parser.add_argument("--draft-dir")
    generate_drafts_parser.add_argument("--dry-run", action="store_true")
    generate_drafts_parser.add_argument("--json", action="store_true")
    generate_drafts_parser.set_defaults(func=handle_generate_approved_drafts)
    list_drafts_parser = subparsers.add_parser("list-approved-drafts", help="List approved review drafts before staging.")
    list_drafts_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    list_drafts_parser.add_argument("--runtime-root")
    list_drafts_parser.add_argument("--draft-dir")
    list_drafts_parser.add_argument("--staging-dir")
    list_drafts_parser.add_argument("--queue-dir")
    list_drafts_parser.add_argument("--processed-dir")
    list_drafts_parser.add_argument("--failed-dir")
    list_drafts_parser.add_argument("--status", choices=["approved_draft", "failed_draft"])
    list_drafts_parser.add_argument("--limit", type=int)
    list_drafts_parser.add_argument("--include-payload", action="store_true")
    list_drafts_parser.add_argument("--include-source", action="store_true")
    list_drafts_parser.add_argument("--json", action="store_true")
    list_drafts_parser.set_defaults(func=handle_list_approved_drafts)
    review_candidate_parser = subparsers.add_parser("review-candidate", help="Approve or reject one review candidate.")
    review_candidate_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    review_candidate_parser.add_argument("--runtime-root")
    review_candidate_parser.add_argument("--candidate-dir")
    review_candidate_parser.add_argument("--candidate-id", required=True)
    review_candidate_parser.add_argument("--status", choices=["approved", "rejected"], required=True)
    review_candidate_parser.add_argument("--reviewer", required=True)
    review_candidate_parser.add_argument("--rationale", required=True)
    review_candidate_parser.add_argument("--json", action="store_true")
    review_candidate_parser.set_defaults(func=handle_review_candidate)
    mark_client_informed_parser = subparsers.add_parser("mark-client-informed", help="Mark an approved field-capture candidate as client informed.")
    mark_client_informed_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    mark_client_informed_parser.add_argument("--runtime-root")
    mark_client_informed_parser.add_argument("--candidate-dir")
    mark_client_informed_parser.add_argument("--notification-dir")
    mark_client_informed_parser.add_argument("--candidate-id", required=True)
    mark_client_informed_parser.add_argument("--method", choices=sorted(field_client_notifications.METHODS), required=True)
    mark_client_informed_parser.add_argument("--by", required=True)
    mark_client_informed_parser.add_argument("--note", default="")
    mark_client_informed_parser.add_argument("--json", action="store_true")
    mark_client_informed_parser.set_defaults(func=handle_mark_client_informed)
    show_review_item_parser = subparsers.add_parser("show-review-item", help="Show full detail for one review candidate or draft.")
    show_review_item_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    show_review_item_parser.add_argument("--runtime-root")
    show_review_item_parser.add_argument("--candidate-dir")
    show_review_item_parser.add_argument("--draft-dir")
    show_review_item_parser.add_argument("--staging-dir")
    show_review_item_parser.add_argument("--queue-dir")
    show_review_item_parser.add_argument("--processed-dir")
    show_review_item_parser.add_argument("--failed-dir")
    show_review_item_group = show_review_item_parser.add_mutually_exclusive_group(required=True)
    show_review_item_group.add_argument("--candidate-id")
    show_review_item_group.add_argument("--draft-id")
    show_review_item_parser.add_argument("--json", action="store_true")
    show_review_item_parser.set_defaults(func=handle_show_review_item)
    stage_drafts_parser = subparsers.add_parser("stage-approved-drafts", help="Stage approved review drafts into the runtime queue.")
    stage_drafts_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    stage_drafts_parser.add_argument("--runtime-root")
    stage_drafts_parser.add_argument("--draft-dir")
    stage_drafts_parser.add_argument("--queue-dir")
    stage_drafts_parser.add_argument("--status-dir")
    stage_drafts_parser.add_argument("--dry-run", action="store_true")
    stage_drafts_parser.add_argument("--json", action="store_true")
    stage_drafts_parser.set_defaults(func=handle_stage_approved_drafts)
    review_status_parser = subparsers.add_parser("review-status", help="Report review pipeline status.")
    review_status_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    review_status_parser.add_argument("--runtime-root")
    review_status_parser.add_argument("--semantic-dir")
    review_status_parser.add_argument("--candidate-dir")
    review_status_parser.add_argument("--draft-dir")
    review_status_parser.add_argument("--staging-dir")
    review_status_parser.add_argument("--queue-dir")
    review_status_parser.add_argument("--processed-dir")
    review_status_parser.add_argument("--failed-dir")
    review_status_parser.add_argument("--json", action="store_true")
    review_status_parser.set_defaults(func=handle_review_status)
    review_maintenance_parser = subparsers.add_parser("review-maintenance-status", help="Report read-only review artifact maintenance status.")
    review_maintenance_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    review_maintenance_parser.add_argument("--runtime-root")
    review_maintenance_parser.add_argument("--candidate-dir")
    review_maintenance_parser.add_argument("--draft-dir")
    review_maintenance_parser.add_argument("--staging-dir")
    review_maintenance_parser.add_argument("--queue-dir")
    review_maintenance_parser.add_argument("--processed-dir")
    review_maintenance_parser.add_argument("--failed-dir")
    review_maintenance_parser.add_argument("--stale-days", type=int)
    review_maintenance_parser.add_argument("--include-paths", action="store_true")
    review_maintenance_parser.add_argument("--json", action="store_true")
    review_maintenance_parser.set_defaults(func=handle_review_maintenance_status)
    review_dashboard_parser = subparsers.add_parser("review-dashboard", help="Show a read-only review workflow dashboard.")
    review_dashboard_parser.add_argument("--channel", choices=["field_capture"], default="field_capture")
    review_dashboard_parser.add_argument("--runtime-root")
    review_dashboard_parser.add_argument("--stale-days", type=int)
    review_dashboard_parser.add_argument("--limit", type=int)
    review_dashboard_parser.add_argument("--include-paths", action="store_true")
    review_dashboard_parser.add_argument("--json", action="store_true")
    review_dashboard_parser.set_defaults(func=handle_review_dashboard)
    site_status_export_parser = subparsers.add_parser("export-field-capture-site-status", help="Export reviewed field-capture site viewer status JSON.")
    site_status_export_parser.add_argument("--site-id", required=True)
    site_status_export_parser.add_argument("--runtime-root")
    site_status_export_parser.add_argument("--candidate-dir")
    site_status_export_parser.add_argument("--queue-dir")
    site_status_export_parser.add_argument("--intake-dir")
    site_status_export_parser.add_argument("--upload-dir")
    site_status_export_parser.add_argument("--transcript-dir")
    site_status_export_parser.add_argument("--photo-vision-dir")
    site_status_export_parser.add_argument("--notification-dir")
    site_status_export_parser.add_argument("--vault-root")
    site_status_export_parser.add_argument("--include-issues", action="store_true")
    site_status_export_parser.add_argument("--output")
    site_status_export_parser.add_argument("--json", action="store_true")
    site_status_export_parser.set_defaults(func=handle_export_field_capture_site_status)
    pull_field_capture_parser = subparsers.add_parser("pull-field-capture", help="Import one exported field-capture upload bundle.")
    pull_field_capture_parser.add_argument("--capture-id", required=True)
    pull_field_capture_parser.add_argument("--bundle-path", required=True)
    pull_field_capture_parser.add_argument("--runtime-root")
    pull_field_capture_parser.add_argument("--dry-run", action="store_true")
    pull_field_capture_parser.add_argument("--json", action="store_true")
    pull_field_capture_parser.set_defaults(func=handle_pull_field_capture)
    watch_pipeline_parser = subparsers.add_parser("watch-field-capture-pipeline", help="Watch field-capture intake through local review candidate collection.")
    watch_pipeline_parser.add_argument("--runtime-root")
    watch_pipeline_parser.add_argument("--poll-seconds", type=float)
    watch_pipeline_parser.add_argument("--once", action="store_true")
    watch_pipeline_parser.add_argument("--json", action="store_true")
    watch_pipeline_parser.add_argument("--transcribe-limit", type=int)
    watch_pipeline_parser.add_argument("--vision-limit", type=int)
    watch_pipeline_parser.add_argument("--no-transcribe", action="store_true")
    watch_pipeline_parser.add_argument("--no-semantics", action="store_true")
    watch_pipeline_parser.add_argument("--no-vision", action="store_true")
    watch_pipeline_parser.add_argument("--no-candidates", action="store_true")
    watch_pipeline_parser.add_argument("--log-path")
    watch_pipeline_parser.add_argument("--model")
    watch_pipeline_parser.add_argument("--vision-model")
    watch_pipeline_parser.add_argument("--ollama-url")
    watch_pipeline_parser.add_argument("--initial-prompt")
    watch_pipeline_parser.add_argument("--no-initial-prompt", action="store_true")
    watch_pipeline_parser.add_argument("--worker-timeout-seconds", type=float)
    watch_pipeline_parser.add_argument("--vision-timeout-seconds", type=float)
    watch_pipeline_parser.add_argument("--vision-auto-retry-per-cycle", type=int)
    watch_pipeline_parser.add_argument("--vision-auto-retry-max-attempts", type=int)
    watch_pipeline_parser.add_argument("--vision-auto-retry-cooldown-seconds", type=float)
    watch_pipeline_parser.set_defaults(func=handle_watch_field_capture_pipeline)
    couchdb_watcher_parser = subparsers.add_parser(
        "watch-couchdb-field-captures",
        help="Listen to CouchDB changes feed and import field-capture docs into local intake.",
    )
    couchdb_watcher_parser.add_argument("--runtime-root", type=Path)
    couchdb_watcher_parser.add_argument("--remote-host")
    couchdb_watcher_parser.add_argument("--database")
    couchdb_watcher_parser.add_argument("--state-field")
    couchdb_watcher_parser.add_argument("--dry-run", action="store_true")
    couchdb_watcher_parser.add_argument("--once", action="store_true")
    couchdb_watcher_parser.add_argument("--json", action="store_true")
    couchdb_watcher_parser.add_argument("--log-path", type=Path)
    couchdb_watcher_parser.set_defaults(func=handle_watch_couchdb_field_captures)
    queue_watcher_parser = subparsers.add_parser(
        "watch-couchdb-queue",
        help="Listen to CouchDB queue changes feed and materialize docs into the local runtime queue.",
    )
    queue_watcher_parser.add_argument("--runtime-root", type=Path)
    queue_watcher_parser.add_argument("--remote-host")
    queue_watcher_parser.add_argument("--database")
    queue_watcher_parser.add_argument("--state-field")
    queue_watcher_parser.add_argument("--dry-run", action="store_true")
    queue_watcher_parser.add_argument("--once", action="store_true")
    queue_watcher_parser.add_argument("--json", action="store_true")
    queue_watcher_parser.add_argument("--log-path", type=Path)
    queue_watcher_parser.set_defaults(func=handle_watch_couchdb_queue)
    job_draft_watcher_parser = subparsers.add_parser(
        "watch-couchdb-job-drafts",
        help="Watch CouchDB approved job_drafts and materialize them into the local runtime queue.",
    )
    job_draft_watcher_parser.add_argument("--runtime-root", type=Path)
    job_draft_watcher_parser.add_argument("--database")
    job_draft_watcher_parser.add_argument("--poll-seconds", type=float)
    job_draft_watcher_parser.add_argument("--limit", type=int)
    job_draft_watcher_parser.add_argument("--dry-run", action="store_true")
    job_draft_watcher_parser.add_argument("--once", action="store_true")
    job_draft_watcher_parser.add_argument("--json", action="store_true")
    job_draft_watcher_parser.add_argument("--log-path", type=Path)
    job_draft_watcher_parser.set_defaults(func=handle_watch_couchdb_job_drafts)
    voice_memo_watcher_parser = subparsers.add_parser(
        "watch-couchdb-voice-memos",
        help="Listen to CouchDB voice memos and place audio plus metadata sidecars into the transcription inbox.",
    )
    voice_memo_watcher_parser.add_argument("--runtime-root", type=Path)
    voice_memo_watcher_parser.add_argument("--remote-host")
    voice_memo_watcher_parser.add_argument("--database")
    voice_memo_watcher_parser.add_argument("--state-field")
    voice_memo_watcher_parser.add_argument("--dry-run", action="store_true")
    voice_memo_watcher_parser.add_argument("--once", action="store_true")
    voice_memo_watcher_parser.add_argument("--json", action="store_true")
    voice_memo_watcher_parser.add_argument("--log-path", type=Path)
    voice_memo_watcher_parser.add_argument("--inbox-dir", type=Path)
    voice_memo_watcher_parser.set_defaults(func=handle_watch_couchdb_voice_memos)


def handle_transcribe_field_audio(args: argparse.Namespace) -> int:
    field_audio_args: list[str] = []
    for name in ("runtime_root", "intake_dir", "upload_dir", "transcript_dir", "log_path", "model", "initial_prompt", "worker_timeout_seconds", "limit"):
        value = getattr(args, name)
        if value is not None:
            field_audio_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.no_initial_prompt:
        field_audio_args.append("--no-initial-prompt")
    if args.json:
        field_audio_args.append("--json")
    return field_audio_transcription.run(field_audio_args)


def handle_process_field_audio_semantics(args: argparse.Namespace) -> int:
    field_semantic_args: list[str] = []
    for name in ("runtime_root", "transcript_dir", "semantic_dir", "log_path"):
        value = getattr(args, name)
        if value is not None:
            field_semantic_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        field_semantic_args.append("--json")
    return field_audio_semantics.run(field_semantic_args)


def handle_describe_field_photos(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    photo_vision_args: list[str] = ["--channel", args.channel]
    for name in ("runtime_root", "intake_dir", "upload_dir", "photo_vision_dir", "capture_id", "site_id", "date", "photo_asset_id", "limit", "model", "backend", "ollama_url", "timeout_seconds"):
        value = getattr(args, name)
        if value is not None:
            photo_vision_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.replace_failed:
        photo_vision_args.append("--replace-failed")
    if args.replace_flagged_judgment_language:
        photo_vision_args.append("--replace-flagged-judgment-language")
    if args.dry_run:
        photo_vision_args.append("--dry-run")
    if args.json:
        photo_vision_args.append("--json")
    return field_photo_vision.run(photo_vision_args)


def handle_audit_field_capture_pilot(args: argparse.Namespace) -> int:
    audit_args: list[str] = ["--site-id", args.site_id, "--date", args.date]
    for name in ("runtime_root", "limit", "output_md", "output_json"):
        value = getattr(args, name)
        if value is not None:
            audit_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_paths:
        audit_args.append("--include-paths")
    if args.json:
        audit_args.append("--json")
    return field_pilot_audit.run(audit_args)


def handle_collect_action_candidates(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    collect_args: list[str] = []
    for name in ("runtime_root", "semantic_dir", "candidate_dir"):
        value = getattr(args, name)
        if value is not None:
            collect_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.dry_run:
        collect_args.append("--dry-run")
    if args.json:
        collect_args.append("--json")
    return field_action_candidates.run(collect_args)


def handle_list_action_candidates(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    list_args: list[str] = []
    for name in ("runtime_root", "candidate_dir", "status", "limit"):
        value = getattr(args, name)
        if value is not None:
            list_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_source:
        list_args.append("--include-source")
    if args.json:
        list_args.append("--json")
    return field_action_candidates.run_list(list_args)


def handle_generate_approved_drafts(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    draft_args: list[str] = []
    for name in ("runtime_root", "candidate_dir", "draft_dir"):
        value = getattr(args, name)
        if value is not None:
            draft_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.dry_run:
        draft_args.append("--dry-run")
    if args.json:
        draft_args.append("--json")
    return field_approved_job_drafts.run(draft_args)


def handle_list_approved_drafts(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    list_args: list[str] = []
    for name in ("runtime_root", "draft_dir", "staging_dir", "queue_dir", "processed_dir", "failed_dir", "status", "limit"):
        value = getattr(args, name)
        if value is not None:
            list_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_payload:
        list_args.append("--include-payload")
    if args.include_source:
        list_args.append("--include-source")
    if args.json:
        list_args.append("--json")
    return field_approved_job_drafts.run_list(list_args)


def handle_review_candidate(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    review_candidate_args: list[str] = []
    for name in ("runtime_root", "candidate_dir", "candidate_id", "status", "reviewer", "rationale"):
        value = getattr(args, name)
        if value is not None:
            review_candidate_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        review_candidate_args.append("--json")
    return field_action_candidates.run_review(review_candidate_args)


def handle_mark_client_informed(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    informed_args: list[str] = ["--channel", args.channel]
    for name in ("runtime_root", "candidate_dir", "notification_dir", "candidate_id", "method", "by", "note"):
        value = getattr(args, name)
        if value is not None:
            informed_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        informed_args.append("--json")
    return field_client_notifications.run(informed_args)


def handle_show_review_item(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    show_args: list[str] = []
    for name in ("runtime_root", "candidate_dir", "draft_dir", "staging_dir", "queue_dir", "processed_dir", "failed_dir", "candidate_id", "draft_id"):
        value = getattr(args, name)
        if value is not None:
            show_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        show_args.append("--json")
    return field_review_items.run(show_args)


def handle_stage_approved_drafts(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    stage_args: list[str] = []
    for name in ("runtime_root", "draft_dir", "queue_dir", "status_dir"):
        value = getattr(args, name)
        if value is not None:
            stage_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.dry_run:
        stage_args.append("--dry-run")
    if args.json:
        stage_args.append("--json")
    return field_draft_staging.run(stage_args)


def handle_review_status(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    review_args: list[str] = []
    for name in ("runtime_root", "semantic_dir", "candidate_dir", "draft_dir", "staging_dir", "queue_dir", "processed_dir", "failed_dir"):
        value = getattr(args, name)
        if value is not None:
            review_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.json:
        review_args.append("--json")
    return field_review_status.run(review_args)


def handle_review_maintenance_status(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    maintenance_args: list[str] = []
    for name in ("runtime_root", "candidate_dir", "draft_dir", "staging_dir", "queue_dir", "processed_dir", "failed_dir", "stale_days"):
        value = getattr(args, name)
        if value is not None:
            maintenance_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_paths:
        maintenance_args.append("--include-paths")
    if args.json:
        maintenance_args.append("--json")
    return field_review_maintenance.run(maintenance_args)


def handle_review_dashboard(args: argparse.Namespace) -> int:
    if args.channel != "field_capture":
        raise SystemExit(f"Unsupported channel: {args.channel}")
    dashboard_args: list[str] = []
    for name in ("runtime_root", "stale_days", "limit"):
        value = getattr(args, name)
        if value is not None:
            dashboard_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_paths:
        dashboard_args.append("--include-paths")
    if args.json:
        dashboard_args.append("--json")
    return field_review_dashboard.run(dashboard_args)


def handle_export_field_capture_site_status(args: argparse.Namespace) -> int:
    export_args: list[str] = ["--site-id", args.site_id]
    for name in ("runtime_root", "candidate_dir", "queue_dir", "intake_dir", "upload_dir", "transcript_dir", "photo_vision_dir", "notification_dir", "vault_root", "output"):
        value = getattr(args, name)
        if value is not None:
            export_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.include_issues:
        export_args.append("--include-issues")
    if args.json:
        export_args.append("--json")
    return field_site_status_export.run(export_args)


def handle_pull_field_capture(args: argparse.Namespace) -> int:
    pull_args: list[str] = ["--capture-id", args.capture_id]
    for name in ("bundle_path", "runtime_root"):
        value = getattr(args, name)
        if value is not None:
            pull_args.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.dry_run:
        pull_args.append("--dry-run")
    if args.json:
        pull_args.append("--json")
    return field_pull_bundle.run(pull_args)


def handle_watch_field_capture_pipeline(args: argparse.Namespace) -> int:
    watch_args: list[str] = []
    for name in (
        "runtime_root",
        "poll_seconds",
        "transcribe_limit",
        "vision_limit",
        "log_path",
        "model",
        "vision_model",
        "ollama_url",
        "initial_prompt",
        "worker_timeout_seconds",
        "vision_timeout_seconds",
        "vision_auto_retry_per_cycle",
        "vision_auto_retry_max_attempts",
        "vision_auto_retry_cooldown_seconds",
    ):
        value = getattr(args, name)
        if value is not None:
            watch_args.extend([f"--{name.replace('_', '-')}", str(value)])
    for name in ("once", "json", "no_transcribe", "no_semantics", "no_vision", "no_candidates", "no_initial_prompt"):
        if getattr(args, name):
            watch_args.append(f"--{name.replace('_', '-')}")
    return field_pipeline_watcher.run(watch_args)


def handle_watch_couchdb_field_captures(args: argparse.Namespace) -> int:
    from field_capture import couchdb_watcher

    argv_pass = []
    if args.runtime_root:
        argv_pass += ["--runtime-root", str(args.runtime_root)]
    if args.remote_host:
        argv_pass += ["--remote-host", args.remote_host]
    if args.database:
        argv_pass += ["--database", args.database]
    if args.state_field:
        argv_pass += ["--state-field", args.state_field]
    if args.dry_run:
        argv_pass.append("--dry-run")
    if args.once:
        argv_pass.append("--once")
    if args.json:
        argv_pass.append("--json")
    if args.log_path:
        argv_pass += ["--log-path", str(args.log_path)]
    return couchdb_watcher.run(argv_pass)


def handle_watch_couchdb_queue(args: argparse.Namespace) -> int:
    from queue_processor import couchdb_queue_watcher

    argv_pass = []
    if args.runtime_root:
        argv_pass += ["--runtime-root", str(args.runtime_root)]
    if args.remote_host:
        argv_pass += ["--remote-host", args.remote_host]
    if args.database:
        argv_pass += ["--database", args.database]
    if args.state_field:
        argv_pass += ["--state-field", args.state_field]
    if args.dry_run:
        argv_pass.append("--dry-run")
    if args.once:
        argv_pass.append("--once")
    if args.json:
        argv_pass.append("--json")
    if args.log_path:
        argv_pass += ["--log-path", str(args.log_path)]
    return couchdb_queue_watcher.run(argv_pass)


def handle_watch_couchdb_job_drafts(args: argparse.Namespace) -> int:
    from queue_processor import job_draft_queue_watcher

    argv_pass = []
    for name in ("runtime_root", "database", "poll_seconds", "limit"):
        value = getattr(args, name)
        if value is not None:
            argv_pass.extend([f"--{name.replace('_', '-')}", str(value)])
    if args.dry_run:
        argv_pass.append("--dry-run")
    if args.once:
        argv_pass.append("--once")
    if args.json:
        argv_pass.append("--json")
    if args.log_path:
        argv_pass += ["--log-path", str(args.log_path)]
    return job_draft_queue_watcher.run(argv_pass)


def handle_watch_couchdb_voice_memos(args: argparse.Namespace) -> int:
    from voice_memo import couchdb_watcher

    argv_pass = []
    if args.runtime_root:
        argv_pass += ["--runtime-root", str(args.runtime_root)]
    if args.remote_host:
        argv_pass += ["--remote-host", args.remote_host]
    if args.database:
        argv_pass += ["--database", args.database]
    if args.state_field:
        argv_pass += ["--state-field", args.state_field]
    if args.dry_run:
        argv_pass.append("--dry-run")
    if args.once:
        argv_pass.append("--once")
    if args.json:
        argv_pass.append("--json")
    if args.log_path:
        argv_pass += ["--log-path", str(args.log_path)]
    if args.inbox_dir:
        argv_pass += ["--inbox-dir", str(args.inbox_dir)]
    return couchdb_watcher.run(argv_pass)
