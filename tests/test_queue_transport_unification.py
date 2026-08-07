"""Drift guards for the 2026-08-07 queue-transport unification.

The BTQ queue runs on ONE transport: every authoring path writes a
``btq_queue`` CouchDB doc (via ``event_pipeline.btq_client.enqueue``), and
``queue_processor.couchdb_queue_watcher`` is the sole processor — it
materializes docs into the runtime spool (``runtime/queue``) and runs the
durable processing pass itself. ``runtime/queue`` is the daemon's internal
spool; ``processed/``/``failed/``/``evidence/``/``processed_index.jsonl``
remain as the files-as-audit layer (replay/repair are defined over them).

Files-as-TRANSPORT is retired. These guards pin the retirement:
  * no authoring surface takes a runtime queue dir or writes queue files,
  * the retired ``com.btq.queue-watch`` daemon stays retired (no installer,
    not restarted by deploy, not expected by the health dashboard),
  * the CouchDB watcher processes by default (materialize-only is opt-in).

If one of these fails, a file-queue authoring or processing path has crept
back in — repoint it through btq_client.enqueue instead.
"""

from __future__ import annotations

import inspect
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_writers_author_couchdb_docs_not_files() -> None:
    from ops_dashboard import common

    for writer_name in (
        "write_mark_job",
        "write_edit_record_fields_job",
        "write_set_employee_home_address_job",
        "write_set_employee_uniform_job",
        "write_deep_analysis_job",
        "write_shift_report_note_job",
    ):
        writer = getattr(common, writer_name)
        params = inspect.signature(writer).parameters
        assert "runtime_root" not in params and "queue_dir" not in params, (
            f"{writer_name} takes a queue directory again — dashboard jobs must "
            "author btq_queue docs via enqueue_queue_job"
        )
        source = inspect.getsource(writer)
        assert "enqueue_queue_job(job)" in source, writer_name

    assert "btq_client.enqueue" in inspect.getsource(common.enqueue_queue_job)


def test_site_detail_writers_author_couchdb_docs_not_files() -> None:
    from ops_dashboard.sections import site_detail

    for writer_name in (
        "_write_site_url_job",
        "_write_site_hours_job",
        "_write_site_operational_calendar_job",
        "_write_set_contact_job",
    ):
        writer = getattr(site_detail, writer_name)
        params = inspect.signature(writer).parameters
        assert "runtime_root" not in params, writer_name
        assert "enqueue_queue_job(job)" in inspect.getsource(writer), writer_name


def test_transcription_pipeline_enqueues_instead_of_staging_files() -> None:
    from transcription_pipeline import main as pipeline

    params = inspect.signature(pipeline.stage_queue_jobs).parameters
    assert list(params) == ["job_paths"], (
        "stage_queue_jobs takes a runtime root again — voice jobs must be "
        "authored as btq_queue docs"
    )
    assert "btq_client" in inspect.getsource(pipeline.stage_queue_jobs)


def test_issue_routing_enqueues_instead_of_writing_queue_files() -> None:
    from field_capture import issue_routing

    params = inspect.signature(issue_routing.route_field_reported_issues).parameters
    assert "queue_dir" not in params
    assert "btq_client.enqueue" in inspect.getsource(issue_routing.route_field_reported_issues)


def test_field_capture_server_stage_queue_job_enqueues() -> None:
    from field_capture import server

    assert "btq_client.enqueue" in inspect.getsource(server.stage_queue_job)


def test_job_draft_watcher_authors_queue_docs() -> None:
    from queue_processor import job_draft_queue_watcher as jdw

    assert not hasattr(jdw, "queue_path_for_draft_job"), (
        "job_draft watcher writes queue files again — approved drafts must "
        "author btq_queue docs"
    )
    assert "btq_client.enqueue" in inspect.getsource(jdw.materialize_draft_job)


def test_draft_staging_enqueues_and_treats_duplicate_as_skipped() -> None:
    from processing_core import draft_staging

    source = inspect.getsource(draft_staging.stage_draft)
    assert "btq_client.enqueue" in source
    assert "already enqueued in the CouchDB queue" in source


def test_couchdb_queue_watcher_processes_by_default() -> None:
    from queue_processor import couchdb_queue_watcher as watcher

    # materialize-only is an explicit opt-in escape hatch, never the default.
    params = inspect.signature(watcher.process_one).parameters
    assert params["materialize_only"].default is False
    # The daemon wires the maintenance drain (stale sweep + unknowns pass +
    # in-flight spool drain) at startup and periodically.
    assert hasattr(watcher, "run_maintenance_drain")
    assert hasattr(watcher, "run_processing_pass")


def test_file_queue_daemon_stays_retired() -> None:
    assert not (REPO_ROOT / "scripts" / "install-queue-launch-agent").exists(), (
        "the com.btq.queue-watch installer is back — the file-queue daemon is retired"
    )

    deploy_script = (REPO_ROOT / "scripts" / "btq-deploy-pro").read_text(encoding="utf-8")
    restart_block = deploy_script.split("for d in", 1)[1].split("; do", 1)[0]
    assert "com.btq.queue-watch" not in restart_block, (
        "btq-deploy-pro restarts the retired com.btq.queue-watch daemon again"
    )

    from ops_dashboard.sections import health_pipeline

    assert "com.btq.queue-watch" not in health_pipeline.WATCHER_LABELS
    assert "com.btq.couchdb-queue-watcher" in health_pipeline.WATCHER_LABELS
