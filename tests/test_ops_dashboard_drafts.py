from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from field_capture import approved_job_drafts
import ops_dashboard.common as common
from ops_dashboard.app import route_response_with_headers
from processing_core.action_candidates import STATUS_APPROVED, action_candidate_payload, write_action_candidate_review
from processing_core.approved_job_drafts import approved_job_draft_payload, write_approved_job_draft
from processing_core.artifacts import write_json_object
from tests.test_ops_dashboard import request_text


def approved_candidate(runtime_root: Path, summary: str = "Broken restroom drain needs maintenance.", *, site_id: str = "7050") -> dict[str, object]:
    return action_candidate_payload(
        candidate_type="field_capture_follow_up",
        summary=summary,
        rationale=summary,
        source_text=summary,
        source_context=summary,
        provenance={"semantic_artifact_path": str(runtime_root / "field_capture" / "audio_semantics" / f"{summary[:6]}.json")},
        channel_metadata={"channel": "field_capture", "site_id": site_id, "area": "Restrooms", "upload_id": "cap-one"},
        status=STATUS_APPROVED,
    )


def write_candidate(runtime_root: Path, summary: str = "Broken restroom drain needs maintenance.", *, site_id: str = "7050") -> dict[str, object]:
    candidate = approved_candidate(runtime_root, summary, site_id=site_id)
    write_action_candidate_review(approved_job_drafts.default_candidate_dir(runtime_root), candidate)
    return candidate


def voice_status_candidate(intent: str, metadata: dict[str, object], *, summary: str = "Set inactive.") -> dict[str, object]:
    return action_candidate_payload(
        candidate_type="voice_memo_operator_action",
        summary=summary,
        rationale="Operator requested inactive status.",
        source_text="Please set this inactive.",
        source_context="voice memo",
        provenance={"semantic_artifact_path": "/tmp/semantic.json"},
        channel_metadata={"channel": "voice_memo", "intent": intent, "action": "set_inactive", **metadata},
        status=STATUS_APPROVED,
    )


def write_draft(runtime_root: Path, draft_id: str, candidate_id: str = "ac_test", *, status: str = "approved_draft", site_id: str = "7050") -> dict[str, object]:
    draft = {
        "type": "approved_queue_job_draft",
        "draft_id": draft_id,
        "status": status,
        "candidate_id": candidate_id,
        "candidate_artifact_path": str(approved_job_drafts.default_candidate_dir(runtime_root) / f"{candidate_id}.json"),
        "provenance": {"candidate_artifact_path": str(approved_job_drafts.default_candidate_dir(runtime_root) / f"{candidate_id}.json")},
        "proposed_job_type": "append_to_note",
        "proposed_payload": {"path": "Accounts/Test/Site.md", "destination": "site_note", "content": f"Follow up for {draft_id}\n", "site_id": site_id},
        "rationale": f"Rationale for {draft_id}",
        "confidence": "high",
        "source_context": f"Evidence context for {draft_id}",
        "error": None,
    }
    write_approved_job_draft(approved_job_drafts.default_draft_dir(runtime_root), draft)
    return draft


def write_vault_site(vault_root: Path, site_id: str, account: str, name: str) -> None:
    path = vault_root / "Accounts" / account / "Locations" / f"{site_id} - {name}" / "about.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""---
type: location
site_id: "{site_id}"
account: {account}
location: {name}
---
# {name}
""",
        encoding="utf-8",
    )


def audit_lines(runtime_root: Path) -> list[dict[str, object]]:
    path = runtime_root / "logs" / "admin_audit.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_drafts_list_renders_with_filter_rail(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_one")

    status, content_type, body = request_text("GET", "/drafts", runtime_root)

    assert status == HTTPStatus.OK
    assert "text/html" in content_type
    assert "filter-rail" in body
    assert "Approved Drafts" in body
    assert "ajd_one" in body


def test_drafts_table_uses_data_table_class(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_table")

    status, _content_type, body = request_text("GET", "/drafts", runtime_root)

    assert status == HTTPStatus.OK
    assert 'class="data-table"' in body
    assert "<table><tr><th>draft_id" not in body


def test_drafts_site_column_has_no_redundant_account_prefix(tmp_path: Path, monkeypatch) -> None:
    class FakeRegistry:
        def list_sites(self) -> list[dict[str, str]]:
            return [{"site_id": "7060", "canonical": "Continental Metalworks"}]

    runtime_root = tmp_path / "runtime"
    vault = tmp_path / "vault"
    write_vault_site(vault, "7060", "Contworks", "Continental Metalworks")
    write_draft(runtime_root, "ajd_site", site_id="7060")
    monkeypatch.setattr(common, "CouchDBSiteRegistry", FakeRegistry)
    monkeypatch.setattr("ops_dashboard.app.get_config", lambda: type("Config", (), {"vault_dir": vault})())

    status, _content_type, body = request_text("GET", "/drafts", runtime_root)

    assert status == HTTPStatus.OK
    assert "Continental Metalworks" in body
    assert "Continental — Continental Metalworks" not in body


def test_drafts_list_filters_by_status_and_site(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_keep", status="approved_draft", site_id="7050")
    write_draft(runtime_root, "ajd_other", status="failed", site_id="9999")

    status, _content_type, body = request_text("GET", "/drafts?status=approved_draft&site=7050", runtime_root)

    assert status == HTTPStatus.OK
    assert "ajd_keep" in body
    assert "ajd_other" not in body


def test_drafts_detail_renders_proposed_payload_and_evidence(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_detail")

    status, _content_type, body = request_text("GET", "/drafts?draft_id=ajd_detail", runtime_root)

    assert status == HTTPStatus.OK
    assert "Proposed payload" in body
    assert "Accounts/Test/Site.md" in body
    assert "Queue State" in body
    assert "Not Staged" in body


def test_drafts_generate_dry_run_renders_proposed_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate = write_candidate(runtime_root)

    status, _content_type, body = request_text("GET", f"/drafts?candidate_id={candidate['candidate_id']}&action=generate", runtime_root)

    assert status == HTTPStatus.OK
    assert "Generate Draft Preview" in body
    assert "would_create" in body
    assert str(candidate["candidate_id"]) in body


def test_drafts_generate_post_writes_one_draft_for_one_candidate(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    target = write_candidate(runtime_root, "Broken restroom drain needs maintenance.")
    write_candidate(runtime_root, "Another approved item should not be drafted.", site_id="9999")

    status, _content_type, _body, headers = route_response_with_headers(
        "POST",
        "/drafts/generate",
        runtime_root,
        f"candidate_id={target['candidate_id']}&confirm_dryrun=1".encode(),
    )

    drafts = sorted(approved_job_drafts.default_draft_dir(runtime_root).glob("*.json"))
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/drafts?draft_id=")
    assert len(drafts) == 1
    assert json.loads(drafts[0].read_text(encoding="utf-8"))["candidate_id"] == target["candidate_id"]


def test_drafts_generate_post_refuses_without_confirm_dryrun(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    candidate = write_candidate(runtime_root)

    status, _content_type, _body, headers = route_response_with_headers("POST", "/drafts/generate", runtime_root, f"candidate_id={candidate['candidate_id']}".encode())

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(urlsplit(headers["Location"]).query)["error"] == ["confirm_required"]
    assert not approved_job_drafts.default_draft_dir(runtime_root).exists()


def test_voice_memo_site_status_candidate_generates_set_entity_status_draft() -> None:
    candidate = voice_status_candidate("site_status_change", {"target_id": "7030", "site_id": "7030"})

    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate)

    assert error == ""
    assert job_type == "set_entity_status"
    assert payload["entity_type"] == "site"
    assert payload["entity_id"] == "7030"
    assert payload["status"] == "inactive"
    assert payload["source"] == "voice_memo"


def test_voice_memo_site_inactive_candidate_uses_couchdb_employee_view(monkeypatch) -> None:
    monkeypatch.setenv("BTQ_COUCHDB_URL", "http://couchdb.test")
    monkeypatch.setattr(
        approved_job_drafts,
        "query_view",
        lambda *_args, **_kwargs: [
            {"doc": {"type": "employee", "name": "Avery Archer", "status": "active"}},
            {"doc": {"type": "employee", "name": "Casey Closed", "status": "inactive"}},
            {"doc": {"type": "person", "name": "Morgan Manager", "status": "active"}},
            {
                "doc": {
                    "type": "employee",
                    "name": "Jordan Avery",
                    "status": "active",
                    "role": "Area Manager",
                    "site_ids": ["7030", "7060", "7050"],
                }
            },
        ],
    )
    candidate = voice_status_candidate("site_status_change", {"target_id": "7030", "site_id": "7030"})

    jobs = approved_job_drafts.proposed_queue_jobs(candidate)

    assert [(job_type, payload["entity_type"], payload["entity_id"]) for job_type, payload, error in jobs if not error] == [
        ("set_entity_status", "site", "7030"),
        ("set_entity_status", "employee", "Avery Archer"),
    ]


def test_voice_memo_site_inactive_candidate_skips_employee_cascade_without_couchdb(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)
    monkeypatch.delenv("BTQ_COUCHDB_USER", raising=False)
    monkeypatch.delenv("BTQ_COUCHDB_PASSWORD", raising=False)
    candidate = voice_status_candidate("site_status_change", {"target_id": "7030", "site_id": "7030"})

    jobs = approved_job_drafts.proposed_queue_jobs(candidate)

    assert [(job_type, payload["entity_type"], payload["entity_id"], error) for job_type, payload, error in jobs] == [
        ("set_entity_status", "site", "7030", "")
    ]


def test_voice_memo_employee_status_candidate_generates_set_entity_status_draft() -> None:
    candidate = voice_status_candidate(
        "employee_status_change",
        {"target_id": "hutton-maria", "employee_slugs": ["hutton-maria"], "employee_names": ["Maria Hutton"]},
    )

    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate)

    assert error == ""
    assert job_type == "set_entity_status"
    assert payload["entity_type"] == "employee"
    assert payload["entity_id"] == "Maria Hutton"
    assert payload["status"] == "inactive"


def test_voice_memo_employee_status_candidate_matches_target_slug_to_name() -> None:
    candidate = voice_status_candidate(
        "employee_status_change",
        {
            "target_id": "taylor-alex",
            "employee_slugs": ["hutton-maria", "taylor-alex"],
            "employee_names": ["Maria Hutton", "Alex Taylor"],
        },
    )

    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate)

    assert error == ""
    assert job_type == "set_entity_status"
    assert payload["entity_id"] == "Alex Taylor"


def test_voice_memo_multi_employee_status_candidate_generates_one_job_per_employee() -> None:
    candidate = voice_status_candidate(
        "employee_status_change",
        {
            "target_id": "hutton-maria,taylor-alex",
            "employee_slugs": ["hutton-maria", "taylor-alex"],
            "employee_names": ["Maria Hutton", "Alex Taylor"],
        },
    )

    jobs = approved_job_drafts.proposed_queue_jobs(candidate)

    assert [job_type for job_type, _payload, error in jobs if not error] == ["set_entity_status", "set_entity_status"]
    assert [payload["entity_id"] for _job_type, payload, _error in jobs] == ["Maria Hutton", "Alex Taylor"]
    assert [payload["status"] for _job_type, payload, _error in jobs] == ["inactive", "inactive"]


def test_voice_memo_employee_status_candidate_rejects_unmatched_multi_employee_target() -> None:
    candidate = voice_status_candidate(
        "employee_status_change",
        {
            "target_id": "unknown-person",
            "employee_slugs": ["hutton-maria", "taylor-alex"],
            "employee_names": ["Maria Hutton", "Alex Taylor"],
        },
    )

    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate)

    assert job_type == ""
    assert payload == {}
    assert "missing target employee" in error


def test_voice_memo_status_candidate_without_target_fails_draft_mapping() -> None:
    candidate = voice_status_candidate("site_status_change", {"target_id": "", "site_id": ""})

    job_type, payload, error = approved_job_drafts.proposed_queue_job(candidate)

    assert job_type == ""
    assert payload == {}
    assert "missing target" in error


def test_drafts_stage_post_writes_one_queue_file_for_one_draft(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_stage")
    write_draft(runtime_root, "ajd_skip")

    status, _content_type, _body, headers = route_response_with_headers("POST", "/drafts/stage", runtime_root, b"draft_id=ajd_stage&confirm_dryrun=1")

    queue_files = sorted((runtime_root / "queue").glob("*.json"))
    assert status == HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/drafts?draft_id=ajd_stage&message=staged"
    assert len(queue_files) == 1
    assert json.loads(queue_files[0].read_text(encoding="utf-8"))["metadata"]["draft_id"] == "ajd_stage"


def test_drafts_stage_post_refuses_without_confirm_dryrun(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_gate")

    status, _content_type, _body, headers = route_response_with_headers("POST", "/drafts/stage", runtime_root, b"draft_id=ajd_gate")

    assert status == HTTPStatus.SEE_OTHER
    assert parse_qs(urlsplit(headers["Location"]).query)["error"] == ["confirm_required"]
    assert not (runtime_root / "queue").exists()


def test_drafts_post_audits_with_draft_id_and_queue_path(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    write_draft(runtime_root, "ajd_audit")

    route_response_with_headers("POST", "/drafts/stage", runtime_root, b"draft_id=ajd_audit&confirm_dryrun=1")

    [line] = audit_lines(runtime_root)
    assert line["route"] == "/drafts/stage"
    assert line["payload"]["draft_id"] == "ajd_audit"
    assert "draft_id=ajd_audit" in line["result_summary"]
    assert "queue_path=" in line["result_summary"]
