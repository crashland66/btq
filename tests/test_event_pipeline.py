import json
from pathlib import Path

from event_to_queue.adapter import event_to_job
from event_pipeline.domain_resolver import normalize_text
from event_pipeline.extractor import extract_visit_event, fallback_extract_events, split_sentences as extractor_split_sentences
from event_pipeline import main as pipeline_main
from event_pipeline.sites import resolve_site, resolve_site_id, resolve_site_note_path
from event_pipeline.validator import validate_event
from processing_core.sentences import split_sentences
from queue_spec import JOB_VISIT_CREATE, JOB_VOICE_MEMO_NOTE, validate_job
from transcription_pipeline.main import split_sentences as transcription_split_sentences


def test_access_constraint_pipeline_generates_valid_events(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18.txt"
    transcript_path.write_text(
        (
            "Team requires badge access through parking gate and elevator. "
            "Offices are locked. Only one key exists. "
            "Second cleaner cannot access. "
            "Work cannot be performed without key holder."
        ),
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0

    valid_dir = tmp_path / "events_valid"
    valid_paths = sorted(valid_dir.glob("*.json"))
    assert len(valid_paths) >= 3

    events = [json.loads(path.read_text(encoding="utf-8")) for path in valid_paths]
    access_events = [event for event in events if event["type"] == "access_constraint"]
    assert len(access_events) >= 3
    assert any(event.get("category") == "badge_access" and event["blocking"] is True for event in access_events)
    assert any(event.get("category") == "key_control" and event["blocking"] is True for event in access_events)
    assert any(event.get("category") == "dependency" and event["blocking"] is True for event in access_events)


def test_resolve_site_handles_continental_voice_memo_aliases() -> None:
    assert resolve_site("This is a report for my visit to Continental Metalworks today.") == (
        "Continental Metalworks",
        "medium",
    )
    assert resolve_site("I am at Continental Metalwork with Cody.") == ("Continental Metalworks", "medium")


def test_resolve_site_7021_cedar_valley(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    expected_path = "Accounts/RHN/Locations/7021 - Cedar Valley Medical Center - RHN/about.md"

    assert resolve_site_id("Cedar Valley Medical Center") == "7021"
    assert resolve_site_id("7021") == "7021"
    assert resolve_site_id("cedarville") == "7021"
    assert resolve_site_note_path("Cedar Valley Medical Center") == expected_path
    assert resolve_site("Cedar Valley Medical Center") == ("Cedar Valley Medical Center", "high")
    assert resolve_site("cedarville") == ("Cedar Valley Medical Center", "high")


def test_resolve_site_7022_hartwell_memorial(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    expected_path = "Accounts/RHN/Locations/7022 - Hartwell Memorial Medical Center - RHN/about.md"

    assert resolve_site_id("7022") == "7022"
    assert resolve_site_id("Hartwell Memorial Medical Center") == "7022"
    assert resolve_site_id("west haven") == "7022"
    assert resolve_site_note_path("Hartwell Memorial Medical Center") == expected_path
    assert resolve_site("Hartwell Memorial Medical Center") == ("Hartwell Memorial Medical Center", "high")
    assert resolve_site("west haven") == ("Hartwell Memorial Medical Center", "high")


def test_resolve_site_7010_brookfield_dlc(monkeypatch) -> None:
    monkeypatch.delenv("BTQ_COUCHDB_URL", raising=False)

    expected_path = "Accounts/Civicsource/Locations/7010 - Brookfield License Center/about.md"

    assert resolve_site_id("7010") == "7010"
    assert resolve_site_id("Brookfield License Center") == "7010"
    assert resolve_site_id("brookfield lc") == "7010"
    assert resolve_site_note_path("Brookfield License Center") == expected_path
    assert resolve_site("Brookfield License Center") == ("Brookfield License Center", "high")
    assert resolve_site("brookfield lc") == ("Brookfield License Center", "high")


def test_extract_visit_event_returns_qc_inspection_for_qc_language(tmp_path: Path) -> None:
    event = extract_visit_event(
        ["I walked the branch today and completed a quality check."],
        tmp_path / "2026-05-15-memo.txt",
        "Summit Wire",
    )

    assert event is not None
    assert event["type"] == "visit_create"
    assert event["visit_type"] == "qc_inspection"


def test_extract_visit_event_returns_service_completion_for_completion_language(tmp_path: Path) -> None:
    event = extract_visit_event(
        ["Summit Wire is all done, looks good."],
        tmp_path / "2026-05-15-memo.txt",
        "Summit Wire",
    )

    assert event is not None
    assert event["type"] == "visit_create"
    assert event["visit_type"] == "service_completion"


def test_extract_visit_event_returns_none_for_unrelated_transcript(tmp_path: Path) -> None:
    event = extract_visit_event(
        ["Summit Wire has a broken dispenser that needs maintenance follow-up."],
        tmp_path / "2026-05-15-memo.txt",
        "Summit Wire",
    )

    assert event is None


def test_fallback_extract_events_includes_visit_create_when_qc_language(tmp_path: Path) -> None:
    events = fallback_extract_events(
        "Summit Wire walked the branch today for a quality check.",
        tmp_path / "2026-05-15-memo.txt",
    )

    assert any(event["type"] == "visit_create" for event in events)


def test_event_to_job_maps_visit_create_event_with_visited_by() -> None:
    job = event_to_job(
        {
            "event_id": "evt-visit-1",
            "type": "visit_create",
            "site": "Summit Wire",
            "confidence": "medium",
            "voice_memo_capture_id": "vm-test-123",
            "visit_type": "qc_inspection",
            "visited_by": "per_test002",
            "source_excerpt": "I walked the branch today for a quality check.",
            "details": "I walked the branch today for a quality check.",
        }
    )

    assert job is not None
    assert job["job_type"] == JOB_VISIT_CREATE
    assert job["payload"]["visited_by"] == "per_test002"


def test_event_to_job_visit_create_omits_visited_by_when_absent() -> None:
    job = event_to_job(
        {
            "event_id": "evt-visit-2",
            "type": "visit_create",
            "site": "Summit Wire",
            "confidence": "medium",
            "capture_id": "field-audio-123",
            "visit_type": "service_completion",
            "source_excerpt": "All done, looks good.",
            "details": "All done, looks good.",
        }
    )

    assert job is not None
    assert job["job_type"] == JOB_VISIT_CREATE
    assert "visited_by" not in job["payload"]


def test_event_to_job_non_visit_voice_memo_still_maps_to_voice_memo_note() -> None:
    event = {
        "event_id": "evt-voice-memo-note-1",
        "type": "site_observation",
        "voice_memo_capture_id": "vm-test-123",
        "capture_id": "vm-test-123",
        "voice_memo_routing_flag": "site_tagged",
        "transcript_text": "General voice memo note.",
        "raw_transcript_path": "/tmp/memo.webm.whisper.txt",
        "audio_file": "memo.webm",
        "timestamp": "2026-05-15T12:00:00Z",
        "site_id": "7060",
        "site": "Summit Wire",
        "note": "site note",
    }

    job = event_to_job(event)

    assert job is not None
    assert job["job_type"] == JOB_VOICE_MEMO_NOTE
    assert job["payload"]["capture_id"] == "vm-test-123"


def test_validator_rejects_invalid_confidence(tmp_path: Path) -> None:
    enriched_dir = tmp_path / "events_enriched"
    enriched_dir.mkdir()
    invalid_event_path = enriched_dir / "bad-event.json"
    invalid_event_path.write_text(
        json.dumps(
            {
                "event_id": "bad-event",
                "type": "access_constraint",
                "site": "Summit Wire",
                "details": "Only one key exists",
                "confidence": "certain",
                "timestamp": "2026-04-18",
                "source_excerpt": "Only one key exists.",
                "blocking": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from event_pipeline.validator import validate_events

    valid_paths, failed_paths = validate_events(enriched_dir, tmp_path / "events_valid", tmp_path / "events_failed")

    assert valid_paths == []
    assert len(failed_paths) == 1
    failed_payload = json.loads(failed_paths[0].read_text(encoding="utf-8"))
    assert failed_payload["validation_error"] == "Invalid confidence: certain"


def test_event_to_queue_maps_access_constraint_blocking_flag() -> None:
    event = {
        "event_id": "evt-access-1",
        "type": "access_constraint",
        "site": "Summit Wire",
        "details": "Only one key exists; second cleaner does not have access",
        "confidence": "high",
        "timestamp": "2026-04-18T08:00:00Z",
        "source_excerpt": "Only one key exists.",
        "blocking": True,
    }

    job = event_to_job(event)

    assert job == {
        "job_id": "evt-access-1",
        "job_type": "flag_access_constraint",
        "payload": {
            "site": "Summit Wire",
            "details": "Only one key exists; second cleaner does not have access",
            "blocking": True,
            "date": "2026-04-18",
        },
    }


def test_staffing_risk_critical_maps_to_trigger_recruiting(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-critical.txt"
    transcript_path.write_text(
        "Summit Wire lost two employees and is critically short",
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0

    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    staffing_events = [event for event in valid_events if event["type"] == "staffing_risk"]
    assert len(staffing_events) >= 1
    assert staffing_events[0]["severity"] == "critical"
    assert staffing_events[0]["open_positions"] == 2

    job = event_to_job(staffing_events[0])
    assert job == {
        "job_id": staffing_events[0]["event_id"],
        "job_type": "trigger_recruiting",
        "payload": {
            "site": "Summit Wire",
            "priority": "emergency",
            "details": staffing_events[0]["details"],
            "date": "2026-04-18",
            "open_positions": 2,
        },
    }


def test_site_context_splits_staffing_events_by_site(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-multi-site.txt"
    transcript_path.write_text(
        "Summit Wire lost two employees and is critically short. "
        "Glenwood High School has been short staffed since January.",
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0

    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    staffing_events = [event for event in valid_events if event["type"] == "staffing_risk"]
    assert len(staffing_events) == 2

    summit_wire_event = next(event for event in staffing_events if event["site"] == "Summit Wire")
    glenco_event = next(event for event in staffing_events if event["site"] == "Glenwood High School")

    assert summit_wire_event["open_positions"] == 2
    assert summit_wire_event["severity"] == "critical"

    assert glenco_event["open_positions"] == 1
    assert glenco_event["severity"] == "medium"


def test_material_site_observation_is_emitted_for_learning_context(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-material-site-note.txt"
    transcript_path.write_text(
        "Lakshore community health flooring is vinyl plank and fairly new, in good condition",
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    observations = [event for event in valid_events if event["type"] == "site_observation"]
    assert len(observations) == 1
    assert observations[0]["site"] == "Lakeshore Community Health"
    assert observations[0]["category"] == "material"


def test_per_site_extraction_term_extends_site_observations(tmp_path: Path, monkeypatch) -> None:
    from event_pipeline import extractor
    from event_pipeline.extraction_terms import load_extraction_terms

    terms_file = tmp_path / "terms.yaml"
    terms_file.write_text(
        'global:\n'
        '  site_obs_extra: []\n'
        'sites:\n'
        '  "7050":\n'
        '    extend:\n'
        '      site_obs_extra: ["coal dust"]\n',
        encoding="utf-8",
    )
    site_terms = load_extraction_terms(terms_file)
    monkeypatch.setattr(extractor, "get_extraction_terms", lambda path=None: site_terms)

    transcript_path = tmp_path / "2026-05-20-summit.txt"
    observations = [
        event
        for event in fallback_extract_events(
            "Visited Summit Wire today. The brick walls have heavy coal dust accumulation.",
            transcript_path,
        )
        if event["type"] == "site_observation"
    ]
    assert len(observations) == 1
    assert observations[0]["site"] == "Summit Wire"
    assert "coal dust" in observations[0]["details"].lower()

    # The same term at a site without the override produces no observation.
    no_override = [
        event
        for event in fallback_extract_events(
            "Visited Apex today. The brick walls have heavy coal dust accumulation.",
            transcript_path,
        )
        if event["type"] == "site_observation"
    ]
    assert no_override == []


def test_apex_site_registry_resolution() -> None:
    assert resolve_site("Apex Powdered Metals") == ("Apex Powdered Metals", "high")
    assert resolve_site("Apex") == ("Apex Powdered Metals", "high")


def test_material_site_observations_are_consolidated_per_site(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-material-merge.txt"
    transcript_path.write_text(
        (
            "Lakshore community health flooring is carpet squares in good condition. "
            "Vinyl plank in exam rooms. "
            "No BCT and vinyl flooring in bathrooms and kitchen."
        ),
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    observations = [event for event in valid_events if event["type"] == "site_observation"]

    assert len(observations) == 1
    assert observations[0]["site"] == "Lakeshore Community Health"
    assert observations[0]["category"] == "material"
    assert "Carpet squares are present" in observations[0]["details"]
    assert "Vinyl plank flooring is used in exam rooms" in observations[0]["details"]
    assert "No VCT flooring was noted" in observations[0]["details"]


def test_operational_site_observation_is_emitted_and_mapped_to_site_note_job(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-operational-site-note.txt"
    transcript_path.write_text(
        "Summit Wire diamond textured metal stalls are difficult to clean and show marks",
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    observations = [event for event in valid_events if event["type"] == "site_observation"]
    assert len(observations) == 1
    assert observations[0]["site"] == "Summit Wire"
    assert observations[0]["category"] == "material"

    assert event_to_job(observations[0]) == {
        "job_id": observations[0]["event_id"],
        "job_type": "append_to_note",
        "payload": {
            "path": "Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md",
            "content": observations[0]["details"],
            "destination": "site_note",
        },
    }


def test_unknown_site_observation_falls_back_to_unknown_journal_job() -> None:
    event = {
        "event_id": "evt-site-observation-unknown-1",
        "type": "site_observation",
        "site": "unknown",
        "details": "Diamond textured stalls are difficult to clean",
        "confidence": "low",
        "timestamp": "2026-04-19T22:13:00Z",
        "source_excerpt": "Diamond textured stalls are difficult to clean.",
        "category": "condition",
    }

    assert event_to_job(event) == {
        "job_id": "evt-site-observation-unknown-1",
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-19-unknown.md",
            "content": "Diamond textured stalls are difficult to clean",
            "destination": "journal_unknown",
        },
    }


def test_sentence_splitter_is_shared_for_extractor_and_transcription_pipeline() -> None:
    text = "First thing happened.  Did it work??\nYes!  fragmented voice memo text without ending"

    expected = [
        "First thing happened",
        "Did it work",
        "Yes",
        "fragmented voice memo text without ending",
    ]
    assert split_sentences(text) == expected
    assert extractor_split_sentences(text) == expected
    assert transcription_split_sentences(text) == expected


def test_affected_role_is_explicit_optional_schema_field() -> None:
    event = {
        "event_id": "evt-affected-role",
        "type": "access_constraint",
        "site": "Summit Wire",
        "details": "Second cleaner cannot access offices.",
        "confidence": "high",
        "timestamp": "2026-04-18T08:00:00Z",
        "source_excerpt": "Second cleaner cannot access offices.",
        "affected_role": "cleaner",
        "blocking": True,
    }

    assert validate_event(event)["affected_role"] == "cleaner"


def test_site_registry_site_ids_are_strings_for_queue_contract() -> None:
    site_id = resolve_site_id("Summit Wire")

    assert site_id == "7050"
    assert validate_job(
        {
            "job_type": "log_site_issue",
            "payload": {
                "site_id": site_id,
                "title": "Restroom issue",
                "summary": "Restroom issue observed.",
                "reported_by": "Jordan",
                "client_notified": False,
                "resolution_trigger": "Issue is resolved.",
            },
        }
    ) is True


def test_real_spoken_language_extracts_multiple_event_types(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-real-language.txt"
    transcript_path.write_text(
        "Summit Wire only Peter has the badge, the stalls are a challenge to clean, and he's not ready to say but is circling that area",
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0

    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]

    assert any(
        event["type"] == "access_constraint"
        and event.get("category") == "dependency"
        and event["site"] == "Summit Wire"
        and event["relationship"] == "employee→site dependency"
        for event in valid_events
    )
    assert any(
        event["type"] == "site_observation"
        and event.get("category") == "condition"
        and event["site"] == "Summit Wire"
        for event in valid_events
    )
    assert any(
        event["type"] == "employee_retention_risk"
        and event["site"] == "Summit Wire"
        and event["confidence"] == "medium"
        and event["relationship"] == "employee→site stability"
        for event in valid_events
    )


def test_staffing_risk_event_includes_relationship_context(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-staffing-relationship.txt"
    transcript_path.write_text("Summit Wire lost two employees and is critically short.", encoding="utf-8")

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    staffing_event = next(event for event in valid_events if event["type"] == "staffing_risk")
    assert staffing_event["relationship"] == "site capacity"


def test_employee_onboarding_visit_is_captured(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-19-onboarding.txt"
    transcript_path.write_text(
        (
            "April 9th, visiting Glenwood Elementary School and visiting with the team here "
            "we had a new employee start Chase. He arrived on time and got him logged into the "
            "training materials and he completed all of the training modules. Then they showed him "
            "around, got him oriented, and got him started in cleaning."
        ),
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    onboarding_event = next(event for event in valid_events if event["type"] == "employee_onboarding")
    assert onboarding_event["site"] == "Glenwood Elementary School"
    assert onboarding_event["employee"] == "Chase"
    assert onboarding_event["role"] == "cleaner"
    assert onboarding_event["relationship"] == "employee→site onboarding"
    job = event_to_job(onboarding_event)
    assert job is not None
    assert job["job_type"] == "add_person"
    assert job["payload"] == {"name": "Chase", "role": "cleaner"}
    assert validate_job(job) is True


def test_employee_onboarding_with_explicit_role_maps_to_add_person(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-19-onboarding-role.txt"
    transcript_path.write_text("Summit Wire new employee named Maya hired as cleaner.", encoding="utf-8")

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    onboarding_event = next(event for event in valid_events if event["type"] == "employee_onboarding")

    assert onboarding_event["employee"] == "Maya"
    assert onboarding_event["role"] == "cleaner"
    job = event_to_job(onboarding_event)
    assert job is not None
    assert job["job_type"] == "add_person"
    assert job["payload"] == {"name": "Maya", "role": "cleaner"}
    assert validate_job(job) is True


def test_employee_onboarding_hired_as_phrase_maps_to_add_person(tmp_path: Path) -> None:
    events = fallback_extract_events(
        "Maria was hired as a cleaner at Summit Wire today.",
        tmp_path / "2026-05-09-onboarding.txt",
    )

    onboarding_event = next(event for event in events if event["type"] == "employee_onboarding")

    assert onboarding_event["employee"] == "Maria"
    assert onboarding_event["role"] == "cleaner"
    job = event_to_job(onboarding_event)
    assert job is not None
    assert job["job_type"] == "add_person"
    assert job["payload"] == {"name": "Maria", "role": "cleaner"}
    assert validate_job(job) is True


def test_employee_onboarding_existing_trigger_missing_role_routes_to_unknown_capture(tmp_path: Path) -> None:
    events = fallback_extract_events(
        "New employee Tom oriented today.",
        tmp_path / "2026-05-09-onboarding-missing-role.txt",
    )

    onboarding_event = next(event for event in events if event["type"] == "employee_onboarding")
    job = event_to_job(onboarding_event)

    assert job is not None
    assert job["job_type"] == "append_to_note"
    assert job["payload"]["destination"] == "journal_unknown"
    assert "type: unknown_capture" in job["payload"]["content"]
    assert validate_job(job) is True


def test_employee_onboarding_we_hired_phrase_maps_to_add_person(tmp_path: Path) -> None:
    events = fallback_extract_events(
        "We hired Carlos as a night porter.",
        tmp_path / "2026-05-09-onboarding-we-hired.txt",
    )

    onboarding_event = next(event for event in events if event["type"] == "employee_onboarding")

    assert onboarding_event["employee"] == "Carlos"
    assert onboarding_event["role"] == "night porter"
    job = event_to_job(onboarding_event)
    assert job is not None
    assert job["job_type"] == "add_person"
    assert job["payload"] == {"name": "Carlos", "role": "night porter"}
    assert validate_job(job) is True


def test_no_onboarding_language_produces_no_employee_onboarding_event(tmp_path: Path) -> None:
    events = fallback_extract_events(
        "Summit Wire lobby was completed today.",
        tmp_path / "2026-05-09-no-onboarding.txt",
    )

    assert not any(event["type"] == "employee_onboarding" for event in events)


def test_employee_onboarding_missing_role_routes_to_unknown_capture() -> None:
    event = {
        "event_id": "evt-onboarding-missing-role",
        "type": "employee_onboarding",
        "site": "Summit Wire",
        "details": "New employee named Maya completed onboarding paperwork.",
        "confidence": "medium",
        "timestamp": "2026-04-19T08:00:00Z",
        "source_excerpt": "New employee named Maya completed onboarding paperwork.",
        "employee": "Maya",
        "relationship": "employee→site onboarding",
    }

    job = event_to_job(event)

    assert job is not None
    assert job["job_type"] == "append_to_note"
    assert job["payload"]["path"] == "Journal/2026-04-19-unknown.md"
    assert job["payload"]["destination"] == "journal_unknown"
    assert "type: unknown_capture" in job["payload"]["content"]
    assert "employee_onboarding_missing_role" in job["payload"]["content"]
    assert validate_job(job) is True


def test_interview_observations_are_captured_for_journal_only(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-20-interview.txt"
    transcript_path.write_text(
        (
            "Summit Wire candidate interview call confirmed evening availability. "
            "Candidate volunteered denial of drug and alcohol issues without prompting. "
            "Phone battery died mid-call and candidate said he would call back."
        ),
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    interview_event = next(event for event in valid_events if event["type"] == "interview_note")

    assert interview_event["site"] == "Summit Wire"
    assert interview_event["details"] == "Summit Wire candidate interview call confirmed evening availability"
    assert interview_event["observations"] == [
        {
            "type": "Candidate volunteered denial of drug and alcohol issues without prompting",
            "confidence": "observed",
        },
        {
            "type": "Phone battery died mid-call; candidate said he would call back",
            "confidence": "observed",
        },
    ]
    assert event_to_job(interview_event) == {
        "job_id": interview_event["event_id"],
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-20.md",
            "content": (
                "Summit Wire candidate interview call confirmed evening availability\n\n"
                "**Observations:**\n"
                "- Candidate volunteered denial of drug and alcohol issues without prompting\n"
                "- Phone battery died mid-call; candidate said he would call back"
            ),
            "destination": "journal",
        },
    }


def test_drug_problem_statement_without_prompt_is_normalized_to_observation(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-20-drug-problem.txt"
    transcript_path.write_text(
        (
            "Candidate interview call covered overnight availability. "
            "Candidate said he doesn't have a drug problem without being asked."
        ),
        encoding="utf-8",
    )

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    valid_events = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "events_valid").glob("*.json"))
    ]
    interview_event = next(event for event in valid_events if event["type"] == "interview_note")

    assert interview_event["observations"] == [
        {
            "type": "Candidate volunteered denial of drug and alcohol issues without prompting",
            "confidence": "observed",
        }
    ]
    assert event_to_job(interview_event) == {
        "job_id": interview_event["event_id"],
        "job_type": "append_to_note",
        "payload": {
            "path": "Journal/2026-04-20.md",
            "content": (
                "Candidate interview call covered overnight availability\n\n"
                "**Observations:**\n"
                "- Candidate volunteered denial of drug and alcohol issues without prompting"
            ),
            "destination": "journal",
        },
    }


def test_observations_do_not_alter_derived_structured_jobs() -> None:
    baseline_event = {
        "event_id": "evt-staffing-1",
        "type": "staffing_risk",
        "site": "Summit Wire",
        "details": "Summit Wire lost two employees and is critically short.",
        "confidence": "high",
        "timestamp": "2026-04-20T08:00:00Z",
        "source_excerpt": "Summit Wire lost two employees and is critically short.",
        "severity": "critical",
        "open_positions": 2,
    }
    observed_event = {
        **baseline_event,
        "observations": [
            {
                "type": "Candidate volunteered denial of drug and alcohol issues without prompting",
                "confidence": "observed",
            }
        ],
    }

    assert event_to_job(observed_event) == event_to_job(baseline_event)


def test_site_registry_exact_match() -> None:
    site, confidence = resolve_site("visited western gas transmission, stalls are hard to clean")

    assert site == "Western Gas Transmission"
    assert confidence == "medium"


def test_site_registry_partial_match() -> None:
    site, confidence = resolve_site("was at western gas, same issue with stalls")

    assert site == "Western Gas Transmission"
    assert confidence == "medium"


def test_site_registry_handles_asr_variant_altuna() -> None:
    site, confidence = resolve_site("these are notes about the Lakshore community health")

    assert site == "Lakeshore Community Health"
    assert confidence == "medium"


def test_domain_resolver_normalizes_vct_variants() -> None:
    normalized, corrections = normalize_text("vct floors in hallway")
    assert normalized == "VCT floors in hallway"
    assert corrections == [{"from": "vct", "to": "VCT"}]

    normalized, corrections = normalize_text("bct tile in rooms")
    assert normalized == "VCT tile in rooms"
    assert corrections == [{"from": "bct", "to": "VCT"}]

    normalized, corrections = normalize_text("vinyl plank in exam rooms")
    assert normalized == "LVP in exam rooms"
    assert corrections == [{"from": "vinyl plank", "to": "LVP"}]


def test_pipeline_writes_normalized_transcript_for_debugging(tmp_path: Path) -> None:
    transcript_path = tmp_path / "2026-04-18-domain.txt"
    transcript_path.write_text("bct tile in rooms. vinyl plank in exam rooms.", encoding="utf-8")

    exit_code = pipeline_main.run([str(transcript_path), "--output-root", str(tmp_path)])

    assert exit_code == 0
    normalized_path = transcript_path.with_suffix(".normalized.txt")
    assert normalized_path.exists()
    normalized_text = normalized_path.read_text(encoding="utf-8")
    assert "VCT tile in rooms" in normalized_text
    assert "LVP in exam rooms" in normalized_text
    corrections_path = transcript_path.with_suffix(".corrections.json")
    assert corrections_path.exists()
    assert corrections_path.read_text(encoding="utf-8").endswith("\n")
    corrections = json.loads(corrections_path.read_text(encoding="utf-8"))
    assert {"from": "bct", "to": "VCT"} in corrections
    assert {"from": "vinyl plank", "to": "LVP"} in corrections


def test_last_known_site_fallback_applies_to_followup_recording(tmp_path: Path) -> None:
    first_transcript = tmp_path / "2026-04-18-site-anchor.txt"
    first_transcript.write_text("Visited Western Gas Transmission and the stalls are hard to clean.", encoding="utf-8")

    exit_code = pipeline_main.run([str(first_transcript), "--output-root", str(tmp_path)])
    assert exit_code == 0

    last_site_path = tmp_path / "state" / "last_site.json"
    assert last_site_path.exists()
    assert last_site_path.read_text(encoding="utf-8").endswith("\n")
    last_site_payload = json.loads(last_site_path.read_text(encoding="utf-8"))
    assert set(last_site_payload) == {"site", "timestamp"}
    assert last_site_payload["site"] == "Western Gas Transmission"

    second_transcript = tmp_path / "2026-04-18-followup.txt"
    second_transcript.write_text("Only one person has the badge.", encoding="utf-8")

    exit_code = pipeline_main.run([str(second_transcript), "--output-root", str(tmp_path)])
    assert exit_code == 0

    access_constraint_path = tmp_path / "events_valid" / "2026-04-18-followup-access-constraint-dependency-1.json"
    event = json.loads(access_constraint_path.read_text(encoding="utf-8"))
    assert event["site"] == "Western Gas Transmission"
    assert event["confidence"] == "medium"
