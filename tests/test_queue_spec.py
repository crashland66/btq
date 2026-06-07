from queue_spec import (
    JOB_ADD_PERSON,
    JOB_APPEND_TO_NOTE,
    JOB_MARK_EQUIPMENT_APPROVED,
    JOB_MARK_EQUIPMENT_DENIED,
    JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED,
    JOB_MARK_EQUIPMENT_ORDERED,
    JOB_MARK_EQUIPMENT_PROVIDED,
    JOB_MARK_SUPPLY_DELIVERED,
    JOB_MARK_SUPPLY_NO_ACTION_NEEDED,
    JOB_MARK_SUPPLY_ORDERED,
    JOB_MARK_SUPPLY_STOCKED,
    JOB_SCHEMAS,
    JOB_LOG_EQUIPMENT_REQUEST,
    JOB_LOG_PERSONNEL_EVENT,
    JOB_LOG_SITE_ISSUE,
    JOB_LOG_SUPPLY_NEED,
    JOB_UPDATE_SITE_EQUIPMENT,
    JOB_PARSE_SUPPLY_EMAIL,
    JOB_PERSONAL_JOURNAL_ENTRY,
    JOB_PHOTO_CAPTURE,
    JOB_PROMOTE_PROSPECT,
    JOB_RECLASSIFY_UNKNOWN,
    JOB_SET_ENTITY_STATUS,
    JOB_VISIT_CREATE,
    validate_job,
)
from event_pipeline.sites import SITES, resolve_site_id


PHOTO_DATA_URL = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="


def add_person_payload() -> dict:
    return {
        "name": "Eric Daniel Dalton",
        "employee_id": "567",
        "role": "Cleaner",
        "employment_type": "part_time",
        "status": "active",
        "additional_jobs": ["7071"],
        "assignments": [
            {
                "job": "7060",
                "account": "Contworks",
                "location": "Continental Metalworks Holdings",
                "shift": "evening",
            }
        ],
        "contact": {
            "phone": None,
            "email": None,
        },
        "metadata": {
            "source": "manager_journal",
        },
    }


def log_site_issue_payload() -> dict:
    return {
        "site_id": "7050",
        "title": "Restroom drain backup and inoperable stall",
        "summary": "Drain backed up and the sink drain pushed water onto the restroom floor.",
        "observations": [
            "Drain backed up in the restroom.",
            "Sink drain backed up onto the floor.",
            "Metal stall is inoperable.",
        ],
        "category": "maintenance",
        "priority": "high",
        "status": "open",
        "observed_at": "2026-05-06T18:27:03-04:00",
        "reported_by": "Tom Walsh",
        "source": "field_capture",
        "client_notified": True,
        "client_notified_at": "2026-05-08T15:10:00-04:00",
        "client_notified_by": "Jordan",
        "client_notified_method": "email",
        "client_notified_note": "Emailed client with photo/context.",
        "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
        "related_capture_ids": ["cap-photo-summit-drain"],
        "related_candidate_ids": ["ac_386bdf44bf4f08764e5a7bb7"],
        "related_media": ["/media/cap-photo-summit-drain/drain.jpg"],
        "source_artifacts": ["field_capture_review_export"],
    }


def log_supply_need_payload() -> dict:
    return {
        "site_id": "7050",
        "item_name": "BrightWash cleaner",
        "requested_by": "Tom Walsh",
    }


def log_equipment_request_payload() -> dict:
    return {
        "site_id": "7050",
        "equipment_name": "vacuum",
        "requested_by": "Tom Walsh",
    }


def log_personnel_event_payload() -> dict:
    return {
        "employee": "Tate, Marcus",
        "event_type": "attendance",
        "summary": "No call no show for Summit Wire opening shift.",
        "occurred_at": "2026-05-18T05:30:00-04:00",
        "reported_by": "Jordan",
    }


def update_site_equipment_payload() -> dict:
    return {
        "site": "Continental Metalworks",
        "equipment": [
            {
                "description": "Large walk-behind scrubber",
                "brand": "Viper",
                "color": "Red",
                "status": "operational",
            }
        ],
        "inspection_date": "2026-05-13",
        "inspected_by": "Jordan",
    }


def test_validate_job_rejects_invalid_job_type() -> None:
    assert validate_job({"job_type": "unknown", "payload": {}}) is False


def test_validate_job_rejects_missing_required_field() -> None:
    assert validate_job({"job_type": JOB_APPEND_TO_NOTE, "payload": {"path": "Journal/2026-04-19-unknown.md"}}) is False


def test_validate_job_accepts_valid_job() -> None:
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-04-19-unknown.md",
                "content": "hello",
                "destination": "journal_unknown",
            },
        }
    ) is True


def test_validate_job_promote_prospect_requires_fields() -> None:
    valid = {"job_type": JOB_PROMOTE_PROSPECT, "payload": {"prospect_id": "x", "site_id": "7050", "actor": "Jordan"}}
    assert validate_job(valid) is True
    for missing in ("prospect_id", "site_id", "actor"):
        payload = dict(valid["payload"])
        payload.pop(missing)
        assert validate_job({"job_type": JOB_PROMOTE_PROSPECT, "payload": payload}) is False


def test_validate_set_entity_status_accepts_site_inactive() -> None:
    assert validate_job(
        {
            "job_type": JOB_SET_ENTITY_STATUS,
            "payload": {
                "entity_type": "site",
                "entity_id": "7030",
                "status": "inactive",
                "reason": "Account closed.",
                "source": "voice_memo",
                "observed_at": "2026-06-01",
                "details": "Operator requested inactive status.",
            },
        }
    ) is True


def test_validate_set_entity_status_accepts_employee_active() -> None:
    assert validate_job(
        {
            "job_type": JOB_SET_ENTITY_STATUS,
            "payload": {
                "entity_type": "employee",
                "entity_id": "Hutton, Maria",
                "status": "active",
                "reason": "Reactivated.",
                "source": "manual_review",
            },
        }
    ) is True


def test_validate_set_entity_status_rejects_unknown_entity_type() -> None:
    payload = {"entity_type": "account", "entity_id": "7030", "status": "inactive", "reason": "Closed.", "source": "voice_memo"}
    assert validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}) is False


def test_validate_set_entity_status_rejects_unknown_status() -> None:
    payload = {"entity_type": "site", "entity_id": "7030", "status": "closed", "reason": "Closed.", "source": "voice_memo"}
    assert validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}) is False


def test_validate_set_entity_status_rejects_unknown_payload_field() -> None:
    payload = {"entity_type": "site", "entity_id": "7030", "status": "inactive", "reason": "Closed.", "source": "voice_memo", "active": False}
    assert validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}) is False


def test_validate_set_entity_status_rejects_invalid_observed_at() -> None:
    payload = {"entity_type": "site", "entity_id": "7030", "status": "inactive", "reason": "Closed.", "source": "voice_memo", "observed_at": "06/01/2026"}
    assert validate_job({"job_type": JOB_SET_ENTITY_STATUS, "payload": payload}) is False


def test_validate_job_accepts_add_person_job() -> None:
    assert validate_job(
        {
            "job_id": "2026-05-01T15-00-00Z__add-eric-dalton",
            "idempotency_key": "ehub-567",
            "job_type": JOB_ADD_PERSON,
            "payload": add_person_payload(),
        }
    ) is True


def test_validate_job_rejects_invalid_idempotency_key() -> None:
    assert validate_job(
        {
            "idempotency_key": "ehub 567",
            "job_type": JOB_ADD_PERSON,
            "payload": add_person_payload(),
        }
    ) is False


def test_validate_job_rejects_add_person_missing_required_fields() -> None:
    payload = add_person_payload()
    del payload["role"]
    assert validate_job({"job_type": JOB_ADD_PERSON, "payload": payload}) is False


def test_validate_job_rejects_add_person_unknown_top_level_field() -> None:
    assert validate_job(
        {
            "job_type": JOB_ADD_PERSON,
            "payload": add_person_payload(),
            "unexpected": True,
        }
    ) is False


def test_validate_job_rejects_add_person_invalid_employee_id() -> None:
    payload = add_person_payload()
    payload["employee_id"] = "abc-567"
    assert validate_job({"job_type": JOB_ADD_PERSON, "payload": payload}) is False


def test_validate_job_rejects_add_person_path_injection() -> None:
    payload = add_person_payload()
    payload["path"] = "People/Eric Daniel Dalton.md"
    assert validate_job({"job_type": JOB_ADD_PERSON, "payload": payload}) is False


def test_validate_job_rejects_append_to_note_for_onboarding() -> None:
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-01.md",
                "content": "Eric Daniel Dalton hired as cleaner with eHub employee id 567.",
                "destination": "journal",
            },
        }
    ) is False


def test_validate_job_accepts_pay_rate_note_that_mentions_ehub() -> None:
    # eHub is the system of record for pay rates; pay-rate discrepancy
    # journal entries routinely mention it. Without a hiring context word
    # nearby it must not be misclassified as onboarding.
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-18.md",
                "content": (
                    "Lakeshore Behavioral Health pay-rate discrepancy surfaced. "
                    "Megan flagged that she is being paid $13/hr while the job "
                    "rate in eHub is $14/hr. Two corrections needed."
                ),
                "destination": "journal",
            },
        }
    ) is True


def test_validate_job_rejects_ehub_when_paired_with_hire_context() -> None:
    # A note that mentions eHub AND a hiring context word (e.g. "hire" /
    # "hired") still trips the heuristic.
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-01.md",
                "content": "New cleaner hired today; eHub access pending.",
                "destination": "journal",
            },
        }
    ) is False


def test_validate_job_accepts_recruiting_survey_journal_note() -> None:
    # A recruiting/URL-discovery journal entry can contain a creation verb and
    # a person noun far apart and unrelated ("create a public posting" plus an
    # unrelated "cleaner backfill"); that must not be flagged as onboarding.
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-20.md",
                "content": (
                    "Clearpath open-position listing URL recovered. A live posting "
                    "requires someone to create a public posting on the board. "
                    "Do not assume the Continental cleaner backfill is publicly "
                    "advertised just because it is flagged internally."
                ),
                "destination": "journal",
            },
        }
    ) is True


def test_validate_job_rejects_explicit_person_creation_phrasing() -> None:
    # The creation verb adjacent to a person noun is still onboarding.
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-20.md",
                "content": "Add a new cleaner, Maria Gonzalez, to the Summit Wire roster.",
                "destination": "journal",
            },
        }
    ) is False


def test_validate_job_accepts_start_date_note_without_hiring_context() -> None:
    # "Start date" appears in shift-coverage and PTO notes too; it should
    # only flag as onboarding when paired with hiring context.
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-18.md",
                "content": (
                    "Vacation coverage at Cherry Tree: Megan picks up Jordan's "
                    "Tuesday slot starting 2026-05-21. Start date confirmed."
                ),
                "destination": "journal",
            },
        }
    ) is True


def test_validate_job_accepts_site_audio_memo_with_onboarding_context() -> None:
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Accounts/Contworks/Locations/7060 - Continental Metalworks/about.md",
                "content": (
                    "---\n"
                    "type: site_audio_memo\n"
                    "audio_file: Continental 5:7.m4a\n"
                    "---\n\n"
                    "Cody watched the safety briefing and got set up in eHub."
                ),
                "destination": "site_note",
            },
        }
    ) is True


def test_validate_job_accepts_unknown_capture_with_onboarding_context() -> None:
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "Journal/2026-05-01-unknown.md",
                "content": (
                    "---\n"
                    "type: unknown_capture\n"
                    "event_type: employee_onboarding\n"
                    "reason: employee_onboarding_missing_role\n"
                    "---\n\n"
                    "New employee named Maya completed onboarding paperwork."
                ),
                "destination": "journal_unknown",
            },
        }
    ) is True


def test_validate_job_rejects_employee_note_append() -> None:
    assert validate_job(
        {
            "job_type": JOB_APPEND_TO_NOTE,
            "payload": {
                "path": "People/Carver, Damon.md",
                "content": "Formal warning issued.",
                "destination": "employee_note",
            },
        }
    ) is False


def test_validate_job_accepts_photo_capture() -> None:
    assert validate_job(
        {
            "job_type": JOB_PHOTO_CAPTURE,
            "payload": {
                "site": "Summit Wire",
                "qc_category": "Restrooms",
                "note": "Trash accumulation at admin entrance.",
                "captured_at": "2026-04-30T14:00:00-04:00",
                "exported_at": "2026-04-30T14:02:00-04:00",
                "photos": [
                    {
                        "filename": "admin entrance.jpg",
                        "mime_type": "image/jpeg",
                        "data_url": PHOTO_DATA_URL,
                    }
                ],
            },
        }
    ) is True


def test_validate_job_accepts_photo_capture_with_stored_path() -> None:
    assert validate_job(
        {
            "job_type": JOB_PHOTO_CAPTURE,
            "payload": {
                "site": "Summit Wire",
                "qc_category": "Restrooms",
                "note": "Trash accumulation at admin entrance.",
                "captured_at": "2026-04-30T14:00:00-04:00",
                "exported_at": "2026-04-30T14:02:00-04:00",
                "photos": [
                    {
                        "filename": "admin entrance.jpg",
                        "mime_type": "image/jpeg",
                        "stored_path": "/srv/btq/runtime/uploads/2026-04-30/capture/admin-entrance.jpg",
                    }
                ],
            },
        }
    ) is True


def test_validate_job_accepts_photo_capture_with_site_identifier_string() -> None:
    assert validate_job(
        {
            "job_type": JOB_PHOTO_CAPTURE,
            "payload": {
                "site": "7050",
                "qc_category": "completed_area",
                "note": "Lobby complete.",
                "captured_at": "2026-05-09T14:30:00Z",
                "exported_at": "2026-05-09T14:31:00Z",
                "photos": [
                    {
                        "filename": "lobby.jpg",
                        "mime_type": "image/jpeg",
                        "stored_path": "/srv/btq/runtime/uploads/2026-05-09/cap/lobby.jpg",
                    }
                ],
            },
        }
    ) is True


def test_validate_job_accepts_manually_authored_add_person_with_string_fields() -> None:
    assert validate_job(
        {
            "job_type": JOB_ADD_PERSON,
            "payload": {
                "name": "Maria",
                "role": "cleaner",
                "employee_id": "7050",
                "job": "7060",
                "status": "active",
            },
        }
    ) is True


def test_resolve_site_id_returns_strings_for_hardcoded_sites(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    for site in SITES:
        result = resolve_site_id(str(site["canonical"]))
        assert isinstance(result, str)
        assert result == str(site["site_id"])


def test_validate_job_rejects_photo_capture_without_photos() -> None:
    assert validate_job(
        {
            "job_type": JOB_PHOTO_CAPTURE,
            "payload": {
                "site": "Summit Wire",
                "qc_category": "Restrooms",
                "note": "",
                "captured_at": "2026-04-30T14:00:00-04:00",
                "exported_at": "2026-04-30T14:02:00-04:00",
                "photos": [],
            },
        }
    ) is False


def test_validate_job_accepts_reclassify_unknown_job() -> None:
    assert validate_job(
        {
            "job_type": JOB_RECLASSIFY_UNKNOWN,
            "payload": {
                "path": "Journal/2026-04-19-unknown.md",
            },
        }
    ) is True


def test_validate_job_accepts_visit_create_job() -> None:
    assert validate_job(
        {
            "job_type": JOB_VISIT_CREATE,
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "high",
                "source": "ingestion",
                "evidence": "I was at Western Gas Transmission.",
            },
        }
    ) is True


def test_validate_job_rejects_visit_create_missing_fields() -> None:
    assert validate_job(
        {
            "job_type": JOB_VISIT_CREATE,
            "payload": {
                "site": "Western Gas Transmission",
                "confidence": "high",
                "source": "ingestion",
            },
        }
    ) is False


def test_validate_job_accepts_parse_supply_email_job() -> None:
    assert validate_job(
        {
            "job_type": JOB_PARSE_SUPPLY_EMAIL,
            "payload": {
                "html_path": "data/emails/order-1.html",
                "subject": "Your Staples order confirmation",
                "source_email_date": "2026-04-20T08:15:00+00:00",
            },
        }
    ) is True


def test_validate_job_accepts_personal_journal_entry_job() -> None:
    assert validate_job(
        {
            "job_type": JOB_PERSONAL_JOURNAL_ENTRY,
            "payload": {
                "date": "2026-04-26",
                "timestamp": "2026-04-26T14:00:00+00:00",
                "audio_file": "personal.m4a",
                "body": "Today I need to think privately.",
                "raw_transcript_path": "/tmp/personal.m4a.whisper.txt",
            },
        }
    ) is True


def test_validate_job_accepts_log_site_issue_job() -> None:
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": log_site_issue_payload()}) is True


def test_validate_job_accepts_field_capture_log_site_issue_draft_shape() -> None:
    assert validate_job(
        {
            "job_id": "ajd_ce9aa8ee95f31a91b785df90",
            "job_type": JOB_LOG_SITE_ISSUE,
            "metadata": {
                "candidate_id": "ac_386bdf44bf4f08764e5a7bb7",
                "channel": "field_capture",
                "source": "approved_candidate_draft",
                "draft_path": "/runtime/action_drafts/field_capture/ajd_ce9aa8ee95f31a91b785df90.json",
            },
            "payload": {
                "site_id": "7050",
                "title": "Restroom drain backup and inoperable stall",
                "summary": "Drain backed up and the sink drain pushed water onto the restroom floor.",
                "observations": [
                    "Drain backed up.",
                    "Sink drain appears backed up.",
                    "Water is coming out onto the floor.",
                    "Metal stall is inoperable.",
                ],
                "category": "maintenance",
                "priority": "high",
                "status": "open",
                "observed_at": "2026-05-08T14:12:43.223836+00:00",
                "reported_by": "walsh-tom",
                "source": "field_capture",
                "client_notified": True,
                "client_notified_at": "2026-05-08T14:53:18.033008+00:00",
                "client_notified_by": "Jordan",
                "client_notified_method": "email",
                "client_notified_note": "Emailed client with photo/context.",
                "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
                "related_capture_ids": ["cap-photo-2026-05-06T18-27-03-04-00"],
                "related_candidate_ids": ["ac_386bdf44bf4f08764e5a7bb7"],
                "source_artifacts": [
                    "/runtime/uploads/field_capture/cap-photo-2026-05-06T18-27-03-04-00.semantic.json",
                    "/runtime/uploads/field_capture/cap-photo-2026-05-06T18-27-03-04-00.audio.whisper.txt",
                ],
            },
        }
    ) is True


def test_validate_job_accepts_log_site_issue_with_observations_instead_of_summary() -> None:
    payload = log_site_issue_payload()
    del payload["summary"]
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is True


def test_validate_job_accepts_log_site_issue_status_contract() -> None:
    for status in ("open", "monitoring", "resolved"):
        payload = log_site_issue_payload()
        payload["status"] = status
        assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is True


def test_validate_job_accepts_log_site_issue_with_notes() -> None:
    payload = log_site_issue_payload()
    payload["notes"] = "Linked to the acute overnight cleaning-failure issue; the two are tracked separately."
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is True


def test_validate_job_rejects_log_site_issue_non_string_notes() -> None:
    payload = log_site_issue_payload()
    payload["notes"] = ["not", "a", "string"]
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is False


def test_validate_job_rejects_log_site_issue_missing_required_contract_fields() -> None:
    for field in ("site_id", "title", "reported_by", "client_notified", "resolution_trigger"):
        payload = log_site_issue_payload()
        del payload[field]
        assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is False


def test_validate_job_rejects_log_site_issue_without_summary_or_observations() -> None:
    payload = log_site_issue_payload()
    del payload["summary"]
    payload["observations"] = []
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is False


def test_validate_job_rejects_log_site_issue_invalid_enums_and_bool() -> None:
    invalid_cases = [
        ("status", "client_informed"),
        ("priority", "medium"),
        ("category", "maintenance_issue"),
        ("client_notified", "true"),
    ]
    for field, value in invalid_cases:
        payload = log_site_issue_payload()
        payload[field] = value
        assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is False


def test_validate_job_rejects_log_site_issue_extra_payload_field() -> None:
    payload = log_site_issue_payload()
    payload["auth_token"] = "secret"
    assert validate_job({"job_type": JOB_LOG_SITE_ISSUE, "payload": payload}) is False


def test_validate_log_supply_need_minimal_payload_passes() -> None:
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": log_supply_need_payload()}) is True


def test_validate_log_supply_need_with_all_optional_fields_passes() -> None:
    payload = log_supply_need_payload()
    payload.update(
        {
            "quantity_needed": "2 cases",
            "urgency": "high",
            "observed_at": "2026-05-08T14:12:43+00:00",
            "source": "field_capture",
            "notes": "Supply closet is empty.",
            "related_capture_ids": ["cap-supply-1"],
            "related_candidate_ids": ["ac_supply_1"],
            "related_media": ["/media/cap-supply-1/photo.jpg"],
            "source_artifacts": ["/runtime/uploads/supply.semantic.json"],
            "status": "ordered",
            "supply_id": "sup_summit_brightwash",
            "ordered_at": "2026-05-08T15:00:00+00:00",
            "ordered_by": "Jordan",
            "ordered_note": "Ordered from Staples.",
            "delivered_at": "2026-05-09T15:00:00+00:00",
            "delivered_by": "Driver",
            "delivered_note": "Left in closet.",
            "stocked_at": "2026-05-09T16:00:00+00:00",
            "stocked_by": "Tom",
            "stocked_note": "Placed under sink.",
        }
    )
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is True


def test_validate_log_supply_need_rejects_missing_site_id() -> None:
    payload = log_supply_need_payload()
    del payload["site_id"]
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_missing_item_name() -> None:
    payload = log_supply_need_payload()
    del payload["item_name"]
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_missing_requested_by() -> None:
    payload = log_supply_need_payload()
    del payload["requested_by"]
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_unknown_status() -> None:
    payload = log_supply_need_payload()
    payload["status"] = "waiting"
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_unknown_urgency() -> None:
    payload = log_supply_need_payload()
    payload["urgency"] = "medium"
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_unknown_payload_field() -> None:
    payload = log_supply_need_payload()
    payload["path"] = "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_supply_need_rejects_non_string_related_capture_ids() -> None:
    payload = log_supply_need_payload()
    payload["related_capture_ids"] = ["cap-supply-1", 7050]
    assert validate_job({"job_type": JOB_LOG_SUPPLY_NEED, "payload": payload}) is False


def test_validate_log_equipment_request_minimal_payload_passes() -> None:
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": log_equipment_request_payload()}) is True


def test_validate_log_equipment_request_with_all_optional_fields_passes() -> None:
    payload = log_equipment_request_payload()
    payload.update(
        {
            "reason": "Current vacuum will not start.",
            "priority": "urgent",
            "observed_at": "2026-05-08T14:12:43+00:00",
            "source": "field_capture",
            "notes": "Needed for lobby carpet.",
            "related_capture_ids": ["cap-equipment-1"],
            "related_candidate_ids": ["ac_equipment_1"],
            "related_media": ["/media/cap-equipment-1/photo.jpg"],
            "source_artifacts": ["/runtime/uploads/equipment.semantic.json"],
            "status": "approved",
            "equipment_id": "eqr_summit_vacuum",
            "approved_at": "2026-05-08T15:00:00+00:00",
            "approved_by": "Jordan",
            "approval_note": "Approved replacement.",
            "denied_at": "2026-05-09T15:00:00+00:00",
            "denied_by": "Jordan",
            "denial_note": "Duplicate request.",
            "ordered_at": "2026-05-10T15:00:00+00:00",
            "ordered_by": "Jordan",
            "ordered_note": "Ordered from supplier.",
            "provided_at": "2026-05-11T15:00:00+00:00",
            "provided_by": "Tom",
            "provided_note": "Delivered to site.",
        }
    )
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is True


def test_validate_log_equipment_request_rejects_missing_site_id() -> None:
    payload = log_equipment_request_payload()
    del payload["site_id"]
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_missing_equipment_name() -> None:
    payload = log_equipment_request_payload()
    del payload["equipment_name"]
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_missing_requested_by() -> None:
    payload = log_equipment_request_payload()
    del payload["requested_by"]
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_unknown_status() -> None:
    payload = log_equipment_request_payload()
    payload["status"] = "waiting"
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_unknown_priority() -> None:
    payload = log_equipment_request_payload()
    payload["priority"] = "critical"
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_unknown_payload_field() -> None:
    payload = log_equipment_request_payload()
    payload["path"] = "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md"
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_equipment_request_rejects_non_string_related_capture_ids() -> None:
    payload = log_equipment_request_payload()
    payload["related_capture_ids"] = ["cap-equipment-1", 7050]
    assert validate_job({"job_type": JOB_LOG_EQUIPMENT_REQUEST, "payload": payload}) is False


def test_validate_log_personnel_event_passes_with_required_fields() -> None:
    assert validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": log_personnel_event_payload()}) is True


def test_validate_log_personnel_event_rejects_unknown_event_type() -> None:
    payload = log_personnel_event_payload()
    payload["event_type"] = "small_talk"
    assert validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": payload}) is False


def test_validate_log_personnel_event_rejects_invalid_severity() -> None:
    payload = log_personnel_event_payload()
    payload["severity"] = "execute"
    assert validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": payload}) is False

    payload = log_personnel_event_payload()
    payload["severity"] = None
    assert validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": payload}) is True


def test_validate_log_personnel_event_rejects_unknown_payload_fields() -> None:
    payload = log_personnel_event_payload()
    payload["gossip"] = "not structured personnel evidence"
    assert validate_job({"job_type": JOB_LOG_PERSONNEL_EVENT, "payload": payload}) is False


def test_validate_update_site_equipment_minimum_payload() -> None:
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": update_site_equipment_payload()}) is True


def test_validate_update_site_equipment_accepts_site_id_alternate() -> None:
    payload = update_site_equipment_payload()
    del payload["site"]
    payload["site_id"] = "7060"
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is True


def test_validate_update_site_equipment_requires_site_or_site_id() -> None:
    payload = update_site_equipment_payload()
    del payload["site"]
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_requires_inspection_date_format() -> None:
    payload = update_site_equipment_payload()
    payload["inspection_date"] = "May 13, 2026"
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_requires_inspected_by() -> None:
    payload = update_site_equipment_payload()
    del payload["inspected_by"]
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False

    payload = update_site_equipment_payload()
    payload["inspected_by"] = " "
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_requires_non_empty_equipment_list() -> None:
    payload = update_site_equipment_payload()
    payload["equipment"] = []
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False

    payload = update_site_equipment_payload()
    del payload["equipment"]
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_rejects_unknown_item_status() -> None:
    payload = update_site_equipment_payload()
    payload["equipment"][0]["status"] = "repaired"
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_rejects_unknown_top_level_field() -> None:
    payload = update_site_equipment_payload()
    payload["source"] = "field_capture"
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_rejects_unknown_item_field() -> None:
    payload = update_site_equipment_payload()
    payload["equipment"][0]["acquired_date"] = "2025-01-01"
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is False


def test_validate_update_site_equipment_allows_optional_section_notes() -> None:
    payload = update_site_equipment_payload()
    payload["section_notes"] = "Repair path for blue scrubber is the current open action."
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is True

    payload = update_site_equipment_payload()
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is True


def test_validate_update_site_equipment_allows_optional_item_notes() -> None:
    payload = update_site_equipment_payload()
    payload["equipment"][0]["notes"] = "Used weekly."
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is True

    payload = update_site_equipment_payload()
    assert validate_job({"job_type": JOB_UPDATE_SITE_EQUIPMENT, "payload": payload}) is True


def test_validate_mark_supply_payload_passes_with_required_fields() -> None:
    assert validate_job(
        {
            "job_type": JOB_MARK_SUPPLY_ORDERED,
            "payload": {"supply_id": "sup_summit_brightwash", "actor": "Jordan"},
        }
    ) is True
    assert validate_job(
        {
            "job_type": JOB_MARK_SUPPLY_DELIVERED,
            "payload": {
                "supply_id": "sup_summit_brightwash",
                "actor": "Tom",
                "note": "Delivered to the closet.",
                "occurred_at": "2026-05-08T18:00:00+00:00",
            },
        }
    ) is True


def test_validate_mark_supply_payload_rejects_missing_supply_id() -> None:
    assert validate_job({"job_type": JOB_MARK_SUPPLY_ORDERED, "payload": {"actor": "Jordan"}}) is False


def test_validate_mark_supply_payload_rejects_missing_actor() -> None:
    assert validate_job({"job_type": JOB_MARK_SUPPLY_ORDERED, "payload": {"supply_id": "sup_summit_brightwash"}}) is False


def test_validate_mark_supply_payload_rejects_non_string_note() -> None:
    assert validate_job(
        {
            "job_type": JOB_MARK_SUPPLY_ORDERED,
            "payload": {"supply_id": "sup_summit_brightwash", "actor": "Jordan", "note": ["ordered"]},
        }
    ) is False


def test_validate_mark_supply_payload_rejects_unknown_field() -> None:
    assert validate_job(
        {
            "job_type": JOB_MARK_SUPPLY_ORDERED,
            "payload": {"supply_id": "sup_summit_brightwash", "actor": "Jordan", "status": "ordered"},
        }
    ) is False


def test_validate_mark_equipment_payload_passes_with_required_fields() -> None:
    assert validate_job(
        {
            "job_type": JOB_MARK_EQUIPMENT_APPROVED,
            "payload": {"equipment_id": "eqr_summit_vacuum", "actor": "Jordan"},
        }
    ) is True
    assert validate_job(
        {
            "job_type": JOB_MARK_EQUIPMENT_ORDERED,
            "payload": {
                "equipment_id": "eqr_summit_vacuum",
                "actor": "Jordan",
                "note": "Ordered from supplier.",
                "occurred_at": "2026-05-08T18:00:00+00:00",
            },
        }
    ) is True


def test_validate_mark_equipment_payload_rejects_missing_equipment_id() -> None:
    assert validate_job({"job_type": JOB_MARK_EQUIPMENT_APPROVED, "payload": {"actor": "Jordan"}}) is False


def test_validate_mark_equipment_payload_rejects_missing_actor() -> None:
    assert validate_job({"job_type": JOB_MARK_EQUIPMENT_APPROVED, "payload": {"equipment_id": "eqr_summit_vacuum"}}) is False


def test_job_schemas_register_all_mark_supply_jobs() -> None:
    assert JOB_SCHEMAS[JOB_MARK_SUPPLY_ORDERED] == ["supply_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_SUPPLY_DELIVERED] == ["supply_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_SUPPLY_STOCKED] == ["supply_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_SUPPLY_NO_ACTION_NEEDED] == ["supply_id", "actor"]


def test_job_schemas_register_all_mark_equipment_jobs() -> None:
    assert JOB_SCHEMAS[JOB_MARK_EQUIPMENT_APPROVED] == ["equipment_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_EQUIPMENT_DENIED] == ["equipment_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_EQUIPMENT_ORDERED] == ["equipment_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_EQUIPMENT_PROVIDED] == ["equipment_id", "actor"]
    assert JOB_SCHEMAS[JOB_MARK_EQUIPMENT_NO_ACTION_NEEDED] == ["equipment_id", "actor"]
