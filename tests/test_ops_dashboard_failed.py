from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path

from ops_dashboard.app import route_response_with_headers
from processing_core.artifacts import write_json_object
from tests.test_ops_dashboard import request_text


def write_failed_job(runtime_root: Path, job_id: str = "job_failed", *, draft_id: str = "ajd_failed") -> Path:
    path = runtime_root / "failed" / f"{job_id}.json"
    write_json_object(path, {"job_id": job_id, "job_type": "append_to_note", "payload": {"site_id": "7050"}, "metadata": {"draft_id": draft_id, "site_id": "7050"}})
    return path


def write_sidecar(runtime_root: Path, asset_id: str, status: str) -> Path:
    path = runtime_root / "field_capture" / "photo_vision" / f"{asset_id}.json"
    write_json_object(
        path,
        {
            "type": "field_capture_photo_vision",
            "status": status,
            "photo_asset_id": asset_id,
            "capture_id": "cap-one",
            "site_id": "7050",
            "submitted_area": "Restrooms",
            "error": {"type": "timeout", "message": "vision timeout"} if status == "failed" else {},
        },
    )
    return path


def test_failed_jobs_list_parses_error_excerpt_from_sibling_error_txt(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    path = write_failed_job(runtime_root)
    path.with_suffix(".error.txt").write_text("Traceback first line\nsecond line\n", encoding="utf-8")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert "Traceback first line" in body


def test_failed_jobs_list_parses_error_excerpt_from_queue_watch_log(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_failed_job(runtime_root, "job_log")
    log_path = runtime_root / "logs" / "queue_watch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("2026-05-12 ERROR job_log queue processor exploded\n", encoding="utf-8")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert "queue processor exploded" in body


def test_failed_jobs_detail_links_to_corresponding_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_failed_job(runtime_root, "job_detail", draft_id="ajd_detail")

    status, _content_type, body = request_text("GET", "/failed?job_id=job_detail", runtime_root)

    assert status == HTTPStatus.OK
    assert "/drafts?draft_id=ajd_detail" in body
    assert "Open matching draft" in body


def test_failed_sidecars_list_filters_by_status_failed(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_sidecar(runtime_root, "fcp_failed", "failed")
    write_sidecar(runtime_root, "fcp_done", "completed")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert "fcp_failed" in body
    assert "fcp_done" not in body


def test_failed_page_stacks_sections_vertically(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_failed_job(runtime_root)
    write_sidecar(runtime_root, "fcp_failed", "failed")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert '<div class="grid">' not in body
    assert body.index("<h2>Failed queue jobs</h2>") < body.index("<h2>Failed photo-vision sidecars</h2>")


def test_failed_tables_use_data_table_class(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_failed_job(runtime_root)
    write_sidecar(runtime_root, "fcp_failed", "failed")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert body.count('class="data-table"') == 2


def test_failed_sidecar_rows_have_inline_retry_form(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_sidecar(runtime_root, "fcp_failed_one", "failed")
    write_sidecar(runtime_root, "fcp_failed_two", "failed")

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert body.count('action="/failed/retry-sidecar"') == 2
    assert '<input type="hidden" name="photo_asset_id" value="fcp_failed_one">' in body
    assert '<input type="hidden" name="photo_asset_id" value="fcp_failed_two">' in body


def test_failed_queue_jobs_table_has_age_column(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_failed_job(runtime_root)

    status, _content_type, body = request_text("GET", "/failed", runtime_root)

    assert status == HTTPStatus.OK
    assert "<th>Age</th>" in body


def test_retry_sidecar_writes_intent_file_and_audits(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers("POST", "/failed/retry-sidecar", runtime_root, b"photo_asset_id=fcp_retry")

    intent = runtime_root / "reviews" / "photo_vision_retries" / "fcp_retry.json"
    audit = json.loads((runtime_root / "logs" / "admin_audit.log").read_text(encoding="utf-8").splitlines()[0])
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/failed?sidecar_id=fcp_retry&message=retry_queued"
    assert json.loads(intent.read_text(encoding="utf-8"))["photo_asset_id"] == "fcp_retry"
    assert audit["route"] == "/failed/retry-sidecar"
    assert audit["payload"]["photo_asset_id"] == "fcp_retry"


def test_retry_sidecar_is_idempotent_on_repeat_post(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    route_response_with_headers("POST", "/failed/retry-sidecar", runtime_root, b"photo_asset_id=fcp_repeat")
    first = json.loads((runtime_root / "reviews" / "photo_vision_retries" / "fcp_repeat.json").read_text(encoding="utf-8"))
    route_response_with_headers("POST", "/failed/retry-sidecar", runtime_root, b"photo_asset_id=fcp_repeat")

    [intent] = sorted((runtime_root / "reviews" / "photo_vision_retries").glob("*.json"))
    second = json.loads(intent.read_text(encoding="utf-8"))
    assert intent.name == "fcp_repeat.json"
    assert second["photo_asset_id"] == first["photo_asset_id"]
    assert "requested_at" in second


def test_retry_sidecar_rejects_invalid_photo_asset_id_pattern(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"

    status, _content_type, _body, headers = route_response_with_headers("POST", "/failed/retry-sidecar", runtime_root, b"photo_asset_id=BAD-ID")

    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/failed?error=invalid_photo_asset_id"
    assert not (runtime_root / "reviews" / "photo_vision_retries").exists()


def test_runtime_file_serves_path_under_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    path = write_failed_job(runtime_root, "job_download")

    status, content_type, body, _headers = route_response_with_headers("GET", f"/runtime-file?path={path.relative_to(runtime_root)}", runtime_root)

    assert status == HTTPStatus.OK
    assert "application/json" in content_type
    assert json.loads(body)["job_id"] == "job_download"


def test_runtime_file_refuses_path_outside_runtime_root(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = runtime_root / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)

    status, content_type, _body, _headers = route_response_with_headers("GET", "/runtime-file?path=link.txt", runtime_root)

    assert status == HTTPStatus.NOT_FOUND
    assert "application/json" in content_type


def test_runtime_file_refuses_dotdot_path(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/runtime-file?path=../secret.txt", tmp_path / "runtime")

    assert status == HTTPStatus.NOT_FOUND
    assert json.loads(body)["error"] == "not_found"


def test_runtime_file_refuses_absolute_path(tmp_path: Path) -> None:
    status, _content_type, body, _headers = route_response_with_headers("GET", "/runtime-file?path=/etc/passwd", tmp_path / "runtime")

    assert status == HTTPStatus.NOT_FOUND
    assert json.loads(body)["error"] == "not_found"
