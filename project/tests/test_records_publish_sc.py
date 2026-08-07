from __future__ import annotations

import json
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from btq_vault.entity_types import current_operator_id
from ops_dashboard.sections import records


SAMPLE = """# Area Manager Shift Report — 2026-06-24

## Nightly Summary
Two QCs, supply delivery.

## QC Visits Count
2 accounts.

## Team Members Visited / Trained Count
1 team member.

## Cleaning Fill-In Count
0.

## Interviews / Hires Today
None.

## Quits / Terminations Today
None.

## Notes for Operations Manager
- detail that must not be published
"""


class FakeClient:
    def __init__(self, post_status: int = 201, post_body: dict | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.post_status = post_status
        self.post_body = post_body if post_body is not None else {"audit_id": "audit_NEW"}

    def post_json(self, url, payload, headers):
        self.calls.append(("POST", url))
        return self.post_status, self.post_body

    def put_json(self, url, payload, headers):
        self.calls.append(("PUT", url))
        return 200, {}


def _ctx(tmp_path):
    return SimpleNamespace(runtime_root=tmp_path, query={})


def _doc(**over):
    base = {
        "_id": "shift_report_journal_2026_06_24",
        "type": "shift_report",
        "operator": current_operator_id(),
        "date": "2026-06-24",
        "content": SAMPLE,
    }
    base.update(over)
    return base


# ---- UI rendering ----

def test_unpublished_shift_report_shows_confirm_guarded_publish_form():
    html = records._render_safetyculture_section(_doc())
    assert "Publish to SafetyCulture" in html
    assert "/records/shift_report_journal_2026_06_24/publish-sc" in html
    assert "confirm(" in html
    assert "Re-publish" not in html


def test_published_shift_report_shows_status_link_and_republish():
    html = records._render_safetyculture_section(_doc(sc_audit_id="audit_X", sc_published_at="2026-06-27T00:00:00Z"))
    assert "Published" in html
    assert records.inspection_link("audit_X") in html
    assert "Re-publish" in html
    assert "confirm(" in html


def test_day_record_shows_no_publish_ui():
    html = records._render_safetyculture_section({"_id": "d", "type": records.DAY_RECORD_TYPE})
    assert html == ""


# ---- publish handler ----

def test_publish_happy_path_submits_and_stages_field_write(tmp_path, enqueue_capture):
    client = FakeClient()
    status, _ct, _body, headers = records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(), http_client=client,
        token_loader=lambda: "tok", now=lambda: "2026-06-27T00:00:00Z",
    )
    assert status == HTTPStatus.SEE_OTHER
    assert "sc_audit_id=audit_NEW" in headers["Location"]
    assert client.calls == [("POST", "https://api.safetyculture.io/audits")]  # no archive on first publish
    assert len(enqueue_capture) == 1
    job = enqueue_capture[0]["job"]
    assert job["job_id"].startswith("edit-record-fields-")
    assert job["job_type"] == "edit_record_fields"
    payload = job["payload"]
    assert payload["record_type"] == "shift_report"
    assert payload["fields"] == {"sc_audit_id": "audit_NEW", "sc_published_at": "2026-06-27T00:00:00Z"}
    # Unified transport: nothing lands in the runtime file queue.
    assert not (tmp_path / "queue").exists()


def test_republish_archives_prior_draft_before_creating_new(tmp_path, enqueue_capture):
    client = FakeClient()
    records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(sc_audit_id="audit_OLD"), http_client=client,
        token_loader=lambda: "tok", now=lambda: "2026-06-27T00:00:00Z",
    )
    methods = [m for m, _ in client.calls]
    assert methods == ["PUT", "POST"]  # archive old, then create new
    assert "audit_OLD" in client.calls[0][1]


def test_ops_manager_detail_never_reaches_safetyculture(tmp_path, enqueue_capture):
    captured = {}

    class Recorder(FakeClient):
        def post_json(self, url, payload, headers):
            captured["payload"] = payload
            return super().post_json(url, payload, headers)

    records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(), http_client=Recorder(),
        token_loader=lambda: "tok", now=lambda: "z",
    )
    assert "must not be published" not in json.dumps(captured["payload"])


def test_submit_failure_writes_no_job_and_does_not_500(tmp_path, enqueue_capture):
    client = FakeClient(post_status=400, post_body={"error": "bad"})
    status, _ct, _body, headers = records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(), http_client=client,
        token_loader=lambda: "tok", now=lambda: "z",
    )
    assert status == HTTPStatus.SEE_OTHER
    assert "sc_error=" in headers["Location"]
    assert enqueue_capture == []


def test_non_operator_record_is_not_published(tmp_path, enqueue_capture):
    client = FakeClient()
    status, _ct, _body, headers = records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(operator="someone_else"), http_client=client,
        token_loader=lambda: pytest.fail("token loaded for non-operator"), now=lambda: "z",
    )
    assert status == HTTPStatus.SEE_OTHER
    assert "sc_error=" in headers["Location"]
    assert client.calls == []
    assert enqueue_capture == []


def test_wrong_record_type_is_not_published(tmp_path, enqueue_capture):
    client = FakeClient()
    records.handle_publish_sc_post(
        _ctx(tmp_path), "rid", doc=_doc(type=records.DAY_RECORD_TYPE), http_client=client,
        token_loader=lambda: pytest.fail("token loaded for day_record"), now=lambda: "z",
    )
    assert client.calls == []
    assert enqueue_capture == []
