from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathlib import Path

import json
from urllib.parse import parse_qs

import pytest

import ops_dashboard.common as common
from event_pipeline.couchdb_registry import CouchDBRegistryError
from ops_dashboard.common import KNOWN_JOB_SUMMARY_TYPES, bucket_processing_states, field_capture_media_inventory, humanize_key, parse_display_categories_rows, render_back_link, render_count_badge, render_display_categories_editor, render_fields, render_job_summary, render_kv, render_list, render_relative_time, render_short_filename, render_short_id, render_site_label, render_status_transition, render_table, reset_field_capture_media_inventory_cache, reset_voice_memo_intake_cache, resolve_site_label, voice_memo_intake_state, voice_memo_status
from queue_spec import ALLOWED_JOB_TYPES


def visible_text(html: str) -> str:
    return html.replace('<span class="site-label">', "").replace('<span class="site-id">', "").replace("</span>", "")


class FakeCouchResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeCouchResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeCouchConfig:
    base_url = "http://couchdb.test"
    timeout = 2.5

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": "Basic test"}


def patch_site_registry(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]) -> None:
    class FakeRegistry:
        def list_sites(self) -> list[dict[str, str]]:
            return rows

    monkeypatch.setattr(common, "CouchDBSiteRegistry", FakeRegistry)


def test_parse_display_categories_rows_valid() -> None:
    form = parse_qs("test_label=Restrooms&test_canonical=restrooms&test_label=Lobby&test_canonical=lobby", keep_blank_values=True)

    assert parse_display_categories_rows(form, "test") == [
        {"label": "Restrooms", "canonical": "restrooms"},
        {"label": "Lobby", "canonical": "lobby"},
    ]


def test_parse_display_categories_rows_drops_empty_pairs() -> None:
    form = parse_qs("test_label=Restrooms&test_canonical=restrooms&test_label=&test_canonical=", keep_blank_values=True)

    assert parse_display_categories_rows(form, "test") == [{"label": "Restrooms", "canonical": "restrooms"}]

    with pytest.raises(ValueError):
        parse_display_categories_rows(parse_qs("test_label=Restrooms&test_canonical=", keep_blank_values=True), "test")


def test_render_display_categories_editor_produces_inputs() -> None:
    rendered = render_display_categories_editor("test", [{"label": "Restrooms", "canonical": "restrooms"}])

    assert 'name="test_label"' in rendered
    assert 'name="test_canonical"' in rendered


def test_render_table_empty_items_returns_zero_state() -> None:
    rendered = render_table([], [{"key": "site"}])

    assert 'class="zero-state"' in rendered
    assert "Nothing to show" in rendered


def test_render_table_custom_empty_text() -> None:
    rendered = render_table([], [{"key": "job"}], empty_text="No failed jobs")

    assert 'class="zero-state"' in rendered
    assert "No failed jobs" in rendered


def test_render_table_uses_format_callable() -> None:
    rendered = render_table([{"summary": "Ready"}], [{"key": "summary", "format": lambda value, _item: f"<b>{value}</b>"}])

    assert "<b>Ready</b>" in rendered


def test_render_table_without_format_escapes_value() -> None:
    rendered = render_table([{"summary": "<script>alert(1)</script>"}], [{"key": "summary"}])

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_render_table_priority_attrs_present() -> None:
    rendered = render_table([{"site": "7060", "area": "Restrooms"}], [{"key": "site"}, {"key": "area", "priority": 2}])

    assert '<th data-priority="2">Area</th>' in rendered
    assert '<td data-priority="2">Restrooms</td>' in rendered


def test_render_table_label_defaults_to_title_cased_key() -> None:
    rendered = render_table([{"site_id": "7060"}], [{"key": "site_id"}])

    assert "<th>Site Id</th>" in rendered


def test_render_table_nowrap_true_adds_td_nowrap_class_to_th_and_td() -> None:
    rendered = render_table([{"status": "Approved Draft"}], [{"key": "status", "nowrap": True}])

    assert rendered.count('class="td-nowrap"') == 2


def test_render_table_nowrap_composes_with_existing_class() -> None:
    rendered = render_table([{"x": "value"}], [{"key": "x", "class": "my-class", "nowrap": True}])

    assert '<th class="my-class td-nowrap">X</th>' in rendered
    assert '<td class="my-class td-nowrap">value</td>' in rendered


def test_render_kv_uses_kv_table_class() -> None:
    rendered = render_kv({"queue_count": 1})

    assert 'class="kv-table"' in rendered


def test_render_kv_formats_dict_value_not_as_python_repr() -> None:
    rendered = render_kv({"disk_usage": {"exists": True, "used_bytes": 1}})

    assert "{'exists'" not in rendered
    assert 'class="kv-table nested-kv-table"' in rendered
    assert "used_bytes" in rendered


def test_render_list_excludes_path_keys_by_default() -> None:
    rendered = render_list([{"name": "intake.json", "path": "/Users/operator/btq_runtime/intake.json"}], "No rows.")

    assert "<th>Name</th>" in rendered
    assert "<th>Path</th>" not in rendered
    assert "/Users/operator/btq_runtime" not in rendered


def test_render_list_exclude_keys_override() -> None:
    rendered = render_list([{"name": "intake.json", "path": "/Users/operator/btq_runtime/intake.json"}], "No rows.", exclude_keys=frozenset())

    assert "<th>Path</th>" in rendered
    assert "/Users/operator/btq_runtime/intake.json" in rendered


def test_render_site_label_id_only() -> None:
    rendered = render_site_label("7060")

    assert rendered == '<span class="site-label">7060</span>'
    assert "(" not in rendered
    assert "—" not in rendered


def test_render_site_label_with_name_and_account() -> None:
    rendered = render_site_label("7060", site_name="Continental Metalworks", account="Contworks")

    assert "Contworks — Continental Metalworks" in rendered
    assert "(7060)" in rendered


def test_render_site_label_with_name_only() -> None:
    rendered = render_site_label("7060", site_name="Continental Metalworks")

    assert "Continental Metalworks" in rendered
    assert "(7060)" in rendered


def test_render_site_label_account_equals_name_is_collapsed() -> None:
    rendered = render_site_label("7060", site_name="Continental Metalworks", account="Continental Metalworks")

    assert "—" not in rendered
    assert visible_text(rendered).count("Continental Metalworks") == 1


def test_render_site_label_empty_id_returns_empty() -> None:
    assert render_site_label("") == ""


def test_resolve_site_label_canonical_single(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_site_registry(monkeypatch, [{"site_id": "7060", "canonical": "Continental Metalworks"}])
    monkeypatch.setattr(
        common,
        "_location_account",
        lambda _site_id: (_ for _ in ()).throw(AssertionError("account lookup should not run for unique names")),
    )

    rendered = resolve_site_label("7060", object())

    assert "Continental Metalworks" in rendered
    assert "Contworks —" not in rendered


def test_resolve_site_label_canonical_name_collision_shows_account(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_site_registry(
        monkeypatch,
        [
            {"site_id": "7060", "canonical": "Plant One"},
            {"site_id": "1300", "canonical": "Plant One"},
        ],
    )
    requests: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> FakeCouchResponse:
        requests.append(req)
        assert timeout == FakeCouchConfig.timeout
        return FakeCouchResponse(
            {"docs": [{"_id": "location_7060", "type": "location", "account": "Contworks", "location": "Plant One"}]}
        )

    monkeypatch.setattr(common.couchdb_config, "from_env", lambda: FakeCouchConfig())
    monkeypatch.setattr(common.couchdb_config, "vault_database", lambda: "btq_vault")
    monkeypatch.setattr(common.urllib_request, "urlopen", fake_urlopen)

    rendered = resolve_site_label("7060", object())

    assert "Contworks — Plant One" in rendered
    assert requests
    req = requests[0]
    assert getattr(req, "full_url") == "http://couchdb.test/btq_vault/_find"
    assert json.loads(getattr(req, "data").decode("utf-8")) == {
        "selector": {"_id": "location_7060", "type": "location"},
        "fields": ["_id", "account", "location", "site_id", "job", "type"],
        "limit": 1,
    }


def test_resolve_site_label_registry_error_falls_back_to_bare(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    class ErrorRegistry:
        def list_sites(self) -> list[dict[str, str]]:
            raise CouchDBRegistryError("registry unavailable")

    monkeypatch.setattr(common, "CouchDBSiteRegistry", ErrorRegistry)

    assert resolve_site_label("7060", object()) == '<span class="site-label">7060</span>'
    assert "site label resolution failed site_id=7060: registry unavailable" in caplog.text


def test_resolve_site_label_unknown_site_id_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_site_registry(monkeypatch, [{"site_id": "7060", "canonical": "Continental Metalworks"}])
    monkeypatch.setattr(
        common,
        "_location_account",
        lambda _site_id: (_ for _ in ()).throw(AssertionError("account lookup should not run for unknown sites")),
    )

    assert resolve_site_label("missing", object()) == '<span class="site-label">missing</span>'


def test_render_status_transition_emits_paired_pills() -> None:
    rendered = render_status_transition("open", "ordered")

    assert 'class="pill status-open"' in rendered
    assert 'class="pill status-ordered"' in rendered
    assert "→" in rendered
    assert rendered.index("status-open") < rendered.index("→") < rendered.index("status-ordered")


def test_render_status_transition_sanitizes_class_names() -> None:
    rendered = render_status_transition("open!@#", "no action needed")

    assert 'class="pill status-open"' in rendered
    assert 'class="pill status-noactionneeded"' in rendered
    assert "open!@#" in rendered
    assert "status-open!@#" not in rendered


def test_render_status_transition_both_empty_returns_empty() -> None:
    assert render_status_transition("", None) == ""


def test_render_back_link_renders_arrow_and_label() -> None:
    rendered = render_back_link("/failed", "Failed")

    assert 'class="back-link"' in rendered
    assert 'href="/failed"' in rendered
    assert "← Failed" in rendered


def test_render_short_id_below_threshold_unchanged() -> None:
    rendered = render_short_id("abc123")

    assert rendered == "abc123"
    assert "…" not in rendered


def test_render_short_id_long_value_truncates_with_title() -> None:
    value = "fcp_abcdefghijklmnopqrstuvwxyz"
    rendered = render_short_id(value)

    assert "…" in rendered
    assert f'title="{value}"' in rendered
    assert len(rendered.split(">", 1)[1].split("<", 1)[0]) < len(value)


def test_site_summary_omits_account_prefix() -> None:
    rendered = render_job_summary("log_site_issue", {"site_id": "7060", "site_name": "Continental Metalworks", "account": "Contworks", "title": "Drain"})

    assert "Continental Metalworks" in rendered
    assert "Contworks —" not in rendered


def test_render_short_id_keeps_meaningful_suffix() -> None:
    value = "2026-05-12T17-00-00Z__cherry-tree-vacuum-product-details"
    rendered = render_short_id(value)

    assert "…vacuum-product-details" in rendered
    assert "2026-05" not in rendered.split(">", 1)[1].split("<", 1)[0]
    assert f'title="{value}"' in rendered


def test_render_short_id_short_value_unchanged() -> None:
    assert render_short_id("ajd_short") == "ajd_short"


def test_render_count_badge_zero_is_neutral_even_for_danger_kind() -> None:
    rendered = render_count_badge(0, kind="danger")

    assert 'class="count-badge count-neutral"' in rendered


def test_render_count_badge_danger_kind_nonzero_is_danger() -> None:
    rendered = render_count_badge(2, kind="danger")

    assert 'class="count-badge count-danger"' in rendered


def test_render_count_badge_pending_kind_nonzero_is_pending() -> None:
    rendered = render_count_badge(3, kind="pending")

    assert 'class="count-badge count-pending"' in rendered


def test_humanize_key_maps_snake_case_to_label() -> None:
    assert humanize_key("pending_candidates") == "Pending Candidates"
    assert humanize_key("site_id") == "Site ID"
    assert humanize_key("fc_intake_count") == "FC Intake Count"


def test_render_kv_applies_label_map_without_changing_values() -> None:
    rendered = render_kv({"pending_candidates": 3}, labels={"pending_candidates": "Pending Candidates"})

    assert "<th>Pending Candidates</th>" in rendered
    assert "<td>3</td>" in rendered
    assert "pending_candidates" not in rendered


def test_render_relative_time_just_now() -> None:
    assert "just now" in render_relative_time(30)


def test_render_relative_time_minutes() -> None:
    assert "10 minute" in render_relative_time(600)


def test_render_relative_time_hours() -> None:
    assert "2 hour" in render_relative_time(7200)


def test_render_relative_time_days_falls_back_to_date() -> None:
    now = datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc)

    rendered = render_relative_time(now - timedelta(days=8), now=now)

    assert "May" in rendered


def test_render_relative_time_iso_string_input() -> None:
    rendered = render_relative_time("2026-05-12T15:00:00Z", now=datetime(2026, 5, 12, 16, 0, tzinfo=timezone.utc))

    assert "1 hour ago" in rendered


def test_render_relative_time_empty_returns_empty() -> None:
    assert render_relative_time("") == ""
    assert render_relative_time(None) == ""
    assert render_relative_time("not a timestamp") == ""


def test_render_short_filename_strips_timestamp_prefix() -> None:
    filename = "2026-05-12T16-30-00Z__continental-blue-scrubber.json"
    rendered = render_short_filename(filename)

    assert ">continental-blue-scrubber<" in rendered
    assert f'title="{filename}"' in rendered


def test_render_short_filename_non_matching_passes_through() -> None:
    assert render_short_filename("just-a-name.md") == "just-a-name.md"


def test_render_job_summary_append_to_note() -> None:
    rendered = render_job_summary("append_to_note", {"path": "Accounts/Test/2026-05-12T16-30-00Z__site-note.md"})

    assert "site-note" in rendered


def test_render_job_summary_add_person() -> None:
    rendered = render_job_summary("add_person", {"name": "Kevin Barnes", "role": "cleaner"})

    assert "Kevin Barnes" in rendered
    assert "cleaner" in rendered


def test_render_job_summary_trigger_recruiting() -> None:
    rendered = render_job_summary("trigger_recruiting", {"site": "7060", "priority": "urgent"})

    assert "7060" in rendered
    assert "urgent" in rendered


def test_render_job_summary_remove_from_schedule() -> None:
    rendered = render_job_summary("remove_from_schedule", {"employee": "Avery", "site": "7060"})

    assert "Avery" in rendered
    assert "7060" in rendered


def test_render_job_summary_flag_access_constraint() -> None:
    rendered = render_job_summary("flag_access_constraint", {"site": "7060"})

    assert "7060" in rendered


def test_render_job_summary_flag_retention_risk() -> None:
    rendered = render_job_summary("flag_retention_risk", {"employee": "Avery", "site": "7060"})

    assert "Avery" in rendered
    assert "7060" in rendered


def test_render_job_summary_reclassify_unknown() -> None:
    rendered = render_job_summary("reclassify_unknown", {"path": "Unknown/item.md"})

    assert "Unknown/item.md" in rendered


def test_render_job_summary_visit_create() -> None:
    rendered = render_job_summary("visit_create", {"site": "7060"})

    assert "7060" in rendered


def test_render_job_summary_parse_supply_email() -> None:
    rendered = render_job_summary("parse_supply_email", {"subject": "Supply request"})

    assert "Supply request" in rendered


def test_render_job_summary_personal_journal_entry() -> None:
    rendered = render_job_summary("personal_journal_entry", {"date": "2026-05-12"})

    assert "2026-05-12" in rendered


def test_render_job_summary_photo_capture() -> None:
    rendered = render_job_summary("photo_capture", {"site": "7060", "qc_category": "restroom"})

    assert "7060" in rendered
    assert "restroom" in rendered


def test_render_job_summary_log_site_issue() -> None:
    rendered = render_job_summary("log_site_issue", {"site_id": "7060", "title": "Broken scrubber"})

    assert "7060" in rendered
    assert "Broken scrubber" in rendered


def test_render_job_summary_log_supply_need() -> None:
    rendered = render_job_summary("log_supply_need", {"site_id": "7060", "item_name": "BrightWash cleaner"})

    assert "7060" in rendered
    assert "BrightWash cleaner" in rendered


def test_render_job_summary_log_equipment_request() -> None:
    rendered = render_job_summary("log_equipment_request", {"site_id": "7060", "equipment_name": "small blue scrubber"})

    assert "7060" in rendered
    assert "small blue scrubber" in rendered


def test_log_personnel_event_has_one_line_summary() -> None:
    rendered = render_job_summary("log_personnel_event", {"employee": "Tate, Marcus", "event_type": "attendance"})

    assert "Log personnel event" in rendered
    assert "attendance" in rendered
    assert "Tate, Marcus" in rendered


def test_render_job_summary_log_availability_constraint() -> None:
    rendered = render_job_summary(
        "log_availability_constraint",
        {
            "employee": "Yuhas, Richard",
            "constraint_type": "last_working_day",
            "date": "2026-06-30",
            "related_site": "705",
        },
    )

    assert "Log availability constraint" in rendered
    assert "last_working_day" in rendered
    assert "Yuhas, Richard" in rendered
    assert "2026-06-30" in rendered
    assert "705" in rendered


def test_render_job_summary_set_entity_status() -> None:
    rendered = render_job_summary("set_entity_status", {"entity_type": "site", "entity_id": "7030", "status": "inactive"})

    assert "Set entity status" in rendered
    assert "site" in rendered
    assert "7030" in rendered
    assert "inactive" in rendered


def test_render_job_summary_mark_supply_ordered() -> None:
    rendered = render_job_summary("mark_supply_ordered", {"supply_id": "supply_abcdefghijklmnopqrstuvwxyz", "actor": "Jordan"})

    assert "supply_a" in rendered
    assert "ordered" in rendered
    assert "Jordan" in rendered


def test_render_job_summary_mark_supply_delivered() -> None:
    rendered = render_job_summary("mark_supply_delivered", {"supply_id": "supply_abcdefghijklmnopqrstuvwxyz", "actor": "Jordan"})

    assert "supply_a" in rendered
    assert "delivered" in rendered
    assert "Jordan" in rendered


def test_render_job_summary_mark_equipment_approved() -> None:
    rendered = render_job_summary("mark_equipment_approved", {"equipment_id": "equip_abcdefghijklmnopqrstuvwxyz", "actor": "Jordan"})

    assert "equip_ab" in rendered
    assert "approved" in rendered
    assert "Jordan" in rendered


def test_render_job_summary_mark_equipment_provided() -> None:
    rendered = render_job_summary("mark_equipment_provided", {"equipment_id": "equip_abcdefghijklmnopqrstuvwxyz", "actor": "Jordan"})

    assert "equip_ab" in rendered
    assert "provided" in rendered
    assert "Jordan" in rendered


def test_render_job_summary_mark_issue_resolved() -> None:
    rendered = render_job_summary("mark_issue_resolved", {"issue_id": "issue_abcdefghijklmnopqrstuvwxyz", "actor": "Jordan"})

    assert "issue_a" in rendered
    assert "resolved" in rendered
    assert "Jordan" in rendered


def test_render_job_summary_mark_record_archived() -> None:
    rendered = render_job_summary("mark_record_archived", {"record_type": "site_issue", "record_id": "issue_abc", "actor": "Jordan"})

    assert "Archive record" in rendered
    assert "site_issue" in rendered
    assert "issue_abc" in rendered


def test_render_job_summary_edit_record_fields() -> None:
    rendered = render_job_summary("edit_record_fields", {"record_type": "site_issue", "record_id": "issue_abc", "fields": {"summary": "Updated"}})

    assert "Edit record" in rendered
    assert "site_issue" in rendered
    assert "issue_abc" in rendered
    assert "1 field" in rendered


def test_render_job_summary_voice_memo_note() -> None:
    rendered = render_job_summary("voice_memo_note", {"transcript_text": "This is a short note from the field."})

    assert "This is a short note" in rendered


def test_render_job_summary_set_site_hours() -> None:
    rendered = render_job_summary("set_site_hours", {"site_id": "7060", "action": "set", "facility_hours": {"status": "verified"}})

    assert "Set facility hours" in rendered
    assert "7060" in rendered
    assert "verified" in rendered


def test_render_job_summary_unknown_job_type_fallback() -> None:
    rendered = render_job_summary("future_job", {"alpha": "one", "beta": "two"})

    assert rendered.startswith("future_job:")
    assert "alpha=one" in rendered


def test_render_job_summary_missing_required_field_does_not_raise() -> None:
    rendered = render_job_summary("log_equipment_request", {"site_id": "7060"})

    assert "Log equipment request" in rendered
    assert "7060" in rendered


def test_all_executable_job_types_have_a_summary_branch() -> None:
    assert ALLOWED_JOB_TYPES <= KNOWN_JOB_SUMMARY_TYPES
    for job_type in ALLOWED_JOB_TYPES:
        assert not render_job_summary(job_type, {}).startswith(f"{job_type}:")


def test_voice_memo_status_counts_pipeline_artifacts(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    inbox = tmp_path / "inbox"
    (runtime_root / "queue").mkdir(parents=True)
    (runtime_root / "failed").mkdir(parents=True)
    inbox.mkdir()

    (inbox / "vm-2026-05-20T18-28-04-b9ddc3.metadata.json").write_text("{}", encoding="utf-8")
    (inbox / "other-capture.json").write_text("{}", encoding="utf-8")
    (runtime_root / "queue" / "job_vm-2026-05-21-aaa-voice-memo-observation.json").write_text("{}", encoding="utf-8")
    (runtime_root / "queue" / "job_cap-photo-1.json").write_text("{}", encoding="utf-8")
    (runtime_root / "failed" / "job_vm-2026-05-19-bad-voice-memo-observation.json").write_text("{}", encoding="utf-8")

    (runtime_root / "processed_index.jsonl").write_text(
        '{"job_type": "voice_memo_note", "capture_id": "vm-2026-05-20T18-28-04-b9ddc3", '
        '"timestamp": "2026-05-20T18:29:29Z", '
        '"target_path": "location_7094"}\n'
        '{"job_type": "log_site_issue", "capture_id": "cap-1", "timestamp": "2026-05-20T10:00:00Z"}\n',
        encoding="utf-8",
    )

    status = voice_memo_status(runtime_root, inbox)
    assert status["intake_count"] == 1
    assert status["queued_count"] == 1
    assert status["failed_count"] == 1
    assert status["note_count"] == 1
    assert len(status["recent_notes"]) == 1
    note = status["recent_notes"][0]
    assert note["capture_id"] == "vm-2026-05-20T18-28-04-b9ddc3"
    assert note["site"] == "7094"
    assert note["processed_at"] == "2026-05-20T18:29:29Z"


def test_voice_memo_status_handles_missing_runtime(tmp_path: Path) -> None:
    status = voice_memo_status(tmp_path / "missing", tmp_path / "no-inbox")
    assert status["intake_count"] == 0
    assert status["queued_count"] == 0
    assert status["failed_count"] == 0
    assert status["note_count"] == 0
    assert status["recent_notes"] == []


def test_bucket_processing_states_counts_and_pools_unknown() -> None:
    docs = [
        {"processing_state": "pending"},
        {"processing_state": "pending"},
        {"processing_state": "claimed"},
        {"processing_state": "intake_done"},
        {"processing_state": "intake_failed"},
        {"processing_state": "weird"},
        {"no_state": "x"},
        "not-a-dict",
    ]
    counts = bucket_processing_states(docs)
    assert counts["pending"] == 2
    assert counts["claimed"] == 1
    assert counts["intake_done"] == 1
    assert counts["intake_failed"] == 1
    assert counts["other"] == 1


def test_bucket_processing_states_handles_non_list() -> None:
    assert bucket_processing_states(None) == {
        "pending": 0,
        "claimed": 0,
        "intake_done": 0,
        "intake_failed": 0,
    }


def test_voice_memo_intake_state_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_MEMO_COUCHDB_URL", raising=False)
    reset_voice_memo_intake_cache()
    state = voice_memo_intake_state()
    assert state["available"] is False
    assert state["reason"] == "not configured"


def test_voice_memo_intake_state_available(monkeypatch) -> None:
    import ops_dashboard.common as common

    monkeypatch.setenv("VOICE_MEMO_COUCHDB_URL", "http://example:5984")
    reset_voice_memo_intake_cache()
    monkeypatch.setattr(
        common,
        "query_couchdb_find",
        lambda config, database, selector: {"docs": [{"processing_state": "pending"}, {"processing_state": "intake_done"}]},
    )
    state = voice_memo_intake_state()
    assert state["available"] is True
    assert state["counts"]["pending"] == 1
    assert state["counts"]["intake_done"] == 1
    assert state["total"] == 2


def test_voice_memo_intake_state_couchdb_unreachable(monkeypatch) -> None:
    import ops_dashboard.common as common

    monkeypatch.setenv("VOICE_MEMO_COUCHDB_URL", "http://example:5984")
    reset_voice_memo_intake_cache()

    def boom(config, database, selector):
        raise common.VoiceMemoCouchDBError("down")

    monkeypatch.setattr(common, "query_couchdb_find", boom)
    state = voice_memo_intake_state()
    assert state["available"] is False
    assert state["reason"] == "couchdb unreachable"


def test_field_capture_media_inventory_counts_durable_media(monkeypatch) -> None:
    reset_field_capture_media_inventory_cache()
    monkeypatch.setattr(
        common,
        "_query_field_capture_docs_all_result",
        lambda fields, limit=5000: (
            [
                {"_id": "cap-1", "photos": [{"upload_id": "one.jpg"}, {"upload_id": "two.jpg"}], "audio": []},
                {"_id": "cap-2", "photos": [], "audio": [{"upload_id": "voice.webm"}]},
                {"_id": "cap-text", "photos": [], "audio": []},
            ],
            True,
        ),
    )

    inventory = field_capture_media_inventory()

    assert inventory == {
        "available": True,
        "total_count": 3,
        "image_count": 2,
        "audio_count": 1,
        "capture_count": 2,
    }
    reset_field_capture_media_inventory_cache()


# --- prompt 299: render_fields field-group panel sibling of render_kv ---


def test_render_fields_uses_field_group_panel_markup_not_kv_table() -> None:
    rendered = render_fields({"site_id": "loc-1", "area": "lobby"})

    assert '<dl class="fields">' in rendered
    assert 'class="kv-table"' not in rendered
    # documented field-row / dt / dd structure, one row per key.
    assert rendered.count('<div class="field-row">') == 2
    assert "<dt>Site Id</dt><dd>loc-1</dd>" in rendered
    assert "<dt>Area</dt><dd>lobby</dd>" in rendered


def test_render_fields_label_map_overrides_dt_text() -> None:
    rendered = render_fields(
        {"pending_candidates": 3}, labels={"pending_candidates": "Pending Candidates"}
    )

    assert "<dt>Pending Candidates</dt>" in rendered
    assert "<dd>3</dd>" in rendered
    # the raw key must not leak as a label when overridden.
    assert "pending_candidates" not in rendered


def test_render_fields_renders_list_value_with_items_visible() -> None:
    rendered = render_fields({"warnings": ["wet floor", "broken light"]})

    assert '<dl class="fields">' in rendered
    # impl renders lists as a <ul>; assert both items appear.
    assert "<ul>" in rendered
    assert "<li>wet floor</li>" in rendered
    assert "<li>broken light</li>" in rendered


def test_render_fields_nested_dict_recurses_into_nested_panel() -> None:
    rendered = render_fields({"payload": {"entity_id": "loc-9", "new_state": "active"}})

    # nested dict must recurse to a nested field-group panel, not str(dict).
    assert rendered.count('<dl class="fields">') == 2
    assert "{'entity_id'" not in rendered
    assert "<dt>Entity Id</dt><dd>loc-9</dd>" in rendered
    assert "<dt>New State</dt><dd>active</dd>" in rendered


def test_render_fields_shows_empty_valued_keys_like_render_kv() -> None:
    rendered = render_fields({"a": "", "b": "x"})

    # render_kv shows ALL keys including empties; render_fields must match.
    assert rendered.count('<div class="field-row">') == 2
    assert "<dt>A</dt><dd></dd>" in rendered
    assert "<dt>B</dt><dd>x</dd>" in rendered


def test_render_fields_escapes_values_and_labels() -> None:
    rendered = render_fields({"note": "<script>alert(1)</script> & done"})

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "&amp; done" in rendered


def test_render_kv_still_emits_kv_table_after_prompt_299() -> None:
    # regression pin: render_kv is unchanged, other sections keep kv-tables.
    rendered = render_kv({"queue_count": 1})
    assert 'class="kv-table"' in rendered
    assert '<dl class="fields">' not in rendered


def test_default_actor_falls_back_to_primary_operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(common.DEFAULT_ACTOR_ENV, raising=False)
    assert common.default_actor() == "Greg"


def test_default_actor_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(common.DEFAULT_ACTOR_ENV, "Jordan")
    assert common.default_actor() == "Jordan"
    # blank/whitespace override falls back rather than pre-filling an empty name
    monkeypatch.setenv(common.DEFAULT_ACTOR_ENV, "   ")
    assert common.default_actor() == "Greg"
