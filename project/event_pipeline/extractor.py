from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from field_capture.audio_semantics import classify_qc_visit, classify_visit
from event_pipeline.extraction_terms import get_extraction_terms
from event_pipeline.schema import write_event
from event_pipeline.sites import resolve_site, resolve_site_id
from processing_core.sentences import split_sentences


def build_event_id(transcript_path: Path, event_type: str, index: int) -> str:
    return f"{transcript_path.stem}-{event_type}-{index}"


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}

def finalize_site_and_confidence(site: str | None, confidence: str) -> tuple[str, str]:
    if site is None:
        return "unknown", "low"
    return site, confidence


def unique_sentences(sentences: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for sentence in sentences:
        normalized = sentence.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def extract_person_name_from_sentence(sentence: str) -> str | None:
    match = re.search(r"\bonly\s+([A-Z][a-z]+)\b", sentence)
    if match:
        return match.group(1)
    match = re.search(r"\bwithout\s+([A-Z][a-z]+)\b", sentence)
    if match:
        return match.group(1)
    return None


def extract_site_observation(
    sentence: str,
    transcript_path: Path,
    event_index: int,
    site: str | None,
    site_id: str | None = None,
) -> dict | None:
    lowered_sentence = sentence.lower()
    terms = get_extraction_terms()

    if terms.matches_any(
        "site_obs_diamond_stall_trigger", lowered_sentence, site_id=site_id
    ) and terms.matches_any(
        "site_obs_diamond_stall_qualifier", lowered_sentence, site_id=site_id
    ):
        resolved_site, confidence = finalize_site_and_confidence(site, "high")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": "Diamond textured metal stalls are difficult to clean and show marks",
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "material",
        }

    if terms.matches_any("site_obs_elevator_layout", lowered_sentence, site_id=site_id):
        resolved_site, confidence = finalize_site_and_confidence(site, "high")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": "Layout requires elevator access",
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "layout",
        }

    if terms.matches_any("site_obs_traps_dirt", lowered_sentence, site_id=site_id):
        resolved_site, confidence = finalize_site_and_confidence(site, "high")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": "Material traps dirt",
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "condition",
        }

    if terms.matches_any("site_obs_flooring", lowered_sentence, site_id=site_id):
        resolved_site, confidence = finalize_site_and_confidence(site, "medium")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": sentence,
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "material",
        }

    if terms.matches_any("site_obs_hard_to_clean", lowered_sentence, site_id=site_id) or (
        "marks" in lowered_sentence and "clean" in lowered_sentence
    ):
        resolved_site, confidence = finalize_site_and_confidence(site, "high")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": sentence,
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "condition",
        }

    # site_obs_extra is empty globally; it exists as a per-site extension point
    # so new site-specific terms can be added without a code change.
    if terms.matches_any("site_obs_extra", lowered_sentence, site_id=site_id):
        resolved_site, confidence = finalize_site_and_confidence(site, "medium")
        return {
            "event_id": build_event_id(transcript_path, "site-observation", event_index),
            "type": "site_observation",
            "site": resolved_site,
            "details": sentence,
            "confidence": confidence,
            "timestamp": transcript_path.stem,
            "source_excerpt": sentence,
            "category": "condition",
        }

    return None


def extract_open_positions(text: str) -> int | None:
    lowered = text.lower()

    match = re.search(r"\b(\d+)\s+openings?\b", lowered)
    if match:
        return int(match.group(1))

    match = re.search(r"\b(\d+)\s+employees?\s+(?:left|remaining)\b", lowered)
    if match:
        return int(match.group(1))

    match = re.search(r"\blost\s+(\d+)\s+employees?\b", lowered)
    if match:
        return int(match.group(1))

    for word, number in NUMBER_WORDS.items():
        if f"{word} employees left" in lowered or f"{word} employees remaining" in lowered:
            return number
        if f"lost {word} employees" in lowered:
            return number
        if f"{word} employees resigned" in lowered:
            return number
        if f"{word} openings" in lowered:
            return number

    if "multiple resignations" in lowered:
        return 2
    if "positions now vacant" in lowered:
        return 1

    return None


def extract_onboarding_employee_name(sentence: str) -> str | None:
    match = re.search(r"\b([A-Z][a-z]+)\s+was\s+hired\s+as\b", sentence)
    if match:
        return match.group(1)
    match = re.search(r"\bhired\s+([A-Z][a-z]+)\s+as\b", sentence)
    if match:
        return match.group(1)
    match = re.search(r"\bwe\s+hired\s+([A-Z][a-z]+)\b", sentence)
    if match:
        return match.group(1)
    match = re.search(r"\bnew employee(?: start| named)?\s+([A-Z][a-z]+)\b", sentence)
    if match:
        return match.group(1)
    return None


def extract_onboarding_role(sentence: str) -> str | None:
    lowered = sentence.lower()
    role_patterns = [
        r"\bhired\s+as\s+(?:a\s+|an\s+)?([a-z][a-z -]{1,40})",
        r"\bstarted\s+as\s+(?:a\s+|an\s+)?([a-z][a-z -]{1,40})",
        r"\bnew employee\s+[A-Z][a-z]+\s+(?:as\s+)?(?:a\s+|an\s+)?([a-z][a-z -]{1,40})",
    ]
    for pattern in role_patterns:
        match = re.search(pattern, sentence)
        if match:
            role = re.split(r"\b(?:at|for|and|who|with|on)\b", match.group(1), maxsplit=1)[0].strip(" .,:;-")
            if role:
                return role
    match = re.search(r"\bhired\s+[A-Z][a-z]+\s+as\s+(?:a\s+|an\s+)?([a-z][a-z -]{1,40})", sentence)
    if match:
        role = re.split(r"\b(?:at|for|and|who|with|on)\b", match.group(1), maxsplit=1)[0].strip(" .,:;-")
        if role:
            return role
    if get_extraction_terms().matches_any("onboarding_role_cleaning", lowered):
        return "cleaner"
    return None


def classify_observation(sentence: str) -> str | None:
    lowered_sentence = sentence.lower()
    terms = get_extraction_terms()

    if terms.matches_any("obs_unprompted", lowered_sentence) and terms.matches_any(
        "obs_drug_alcohol", lowered_sentence
    ):
        return "Candidate volunteered denial of drug and alcohol issues without prompting"

    if terms.matches_any("obs_phone_battery", lowered_sentence):
        return "Phone battery died mid-call; candidate said he would call back"

    if terms.matches_any("obs_call_interrupted", lowered_sentence):
        return sentence.strip()

    if terms.matches_any("obs_urgency", lowered_sentence):
        return sentence.strip()

    return None


def extract_interview_note_event(
    sentences: list[str],
    transcript_path: Path,
    site: str | None,
) -> dict | None:
    observation_pairs = [
        (sentence.strip(), observation)
        for sentence in sentences
        if (observation := classify_observation(sentence)) is not None
    ]
    observation_source_sentences = {sentence for sentence, _observation in observation_pairs}
    observation_texts = unique_sentences([observation for _sentence, observation in observation_pairs])
    if not observation_texts:
        return None

    terms = get_extraction_terms()
    fact_sentences = unique_sentences(
        [
            sentence
            for sentence in sentences
            if sentence.strip() not in observation_source_sentences
            and terms.matches_any("interview_fact_keywords", sentence.lower())
        ]
    )
    details = " ".join(fact_sentences) if fact_sentences else "Interview interaction note recorded"
    source_excerpt_sentences = unique_sentences(fact_sentences + observation_texts)
    resolved_site, confidence = finalize_site_and_confidence(site, "medium" if fact_sentences else "low")
    return {
        "event_id": build_event_id(transcript_path, "interview-note", 1),
        "type": "interview_note",
        "site": resolved_site,
        "details": details,
        "confidence": confidence,
        "timestamp": transcript_path.stem,
        "source_excerpt": " ".join(source_excerpt_sentences),
        "observations": [{"type": observation, "confidence": "observed"} for observation in observation_texts],
    }


def extract_visit_event(sentences: list[str], transcript_path: Path, site: str | None) -> dict | None:
    """Emit a visit_create event when the transcript contains QC or completion language."""
    full_text = " ".join(sentences)
    qc = classify_qc_visit(full_text)
    completion = (not qc) and classify_visit(full_text)
    if not (qc or completion):
        return None
    visit_type = "qc_inspection" if qc else "service_completion"
    resolved_site, confidence = finalize_site_and_confidence(site, "medium")
    source_excerpt = next(
        (sentence for sentence in sentences if classify_qc_visit(sentence) or classify_visit(sentence)),
        sentences[0] if sentences else "",
    )
    return {
        "event_id": build_event_id(transcript_path, "visit", 1),
        "type": "visit_create",
        "site": resolved_site,
        "confidence": confidence,
        "timestamp": transcript_path.stem,
        "visit_type": visit_type,
        "source_excerpt": source_excerpt,
        "details": full_text,
    }


def load_last_known_site(last_site_path: Path | None) -> str | None:
    if last_site_path is None or not last_site_path.exists():
        return None

    payload = json.loads(last_site_path.read_text(encoding="utf-8"))
    site = payload.get("site")
    timestamp = payload.get("timestamp")
    if not isinstance(site, str) or not site.strip():
        return None
    if not isinstance(timestamp, str):
        return None

    recorded_at = datetime.fromisoformat(timestamp)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - recorded_at > timedelta(hours=2):
        return None
    return site.strip()


def transcript_has_explicit_site(sentences: list[str]) -> bool:
    for sentence in sentences:
        resolved_site, _confidence = resolve_site(sentence)
        if resolved_site != "unknown":
            return True
    return False


def extract_employee_onboarding_event(
    sentences: list[str],
    transcript_path: Path,
    site: str | None,
) -> dict | None:
    onboarding_sentences: list[str] = []
    employee_name: str | None = None
    role: str | None = None

    terms = get_extraction_terms()
    for sentence in sentences:
        lowered_sentence = sentence.lower()
        if terms.matches_any("onboarding_triggers", lowered_sentence):
            onboarding_sentences.append(sentence)
            if employee_name is None:
                employee_name = extract_onboarding_employee_name(sentence)
            if role is None:
                role = extract_onboarding_role(sentence)

    if not onboarding_sentences:
        return None

    resolved_site, confidence = finalize_site_and_confidence(site, "medium")
    event = {
        "event_id": build_event_id(transcript_path, "employee-onboarding", 1),
        "type": "employee_onboarding",
        "site": resolved_site,
        "details": " ".join(onboarding_sentences),
        "confidence": confidence,
        "timestamp": transcript_path.stem,
        "source_excerpt": " ".join(onboarding_sentences),
        "relationship": "employee→site onboarding",
    }
    if employee_name is not None:
        event["employee"] = employee_name
    if role is not None:
        event["role"] = role
    return event


def fallback_extract_events(transcript_text: str, transcript_path: Path, last_site_path: Path | None = None) -> list[dict]:
    events: list[dict] = []
    sentences = split_sentences(transcript_text)
    current_site: str | None = None
    fallback_site = None if transcript_has_explicit_site(sentences) else load_last_known_site(last_site_path)

    for sentence in sentences:
        resolved_site, _site_confidence = resolve_site(sentence)
        if resolved_site != "unknown":
            current_site = resolved_site
            break

    onboarding_event = extract_employee_onboarding_event(sentences, transcript_path, current_site or fallback_site)
    if onboarding_event is not None:
        events.append(onboarding_event)

    interview_note_event = extract_interview_note_event(sentences, transcript_path, current_site or fallback_site)
    if interview_note_event is not None:
        events.append(interview_note_event)

    visit_event = extract_visit_event(sentences, transcript_path, current_site or fallback_site)
    if visit_event is not None:
        events.append(visit_event)

    for sentence in sentences:
        lowered_sentence = sentence.lower()
        resolved_site, site_confidence = resolve_site(sentence)
        if resolved_site != "unknown":
            current_site = resolved_site

        sentence_site = current_site or fallback_site
        sentence_site_confidence = site_confidence if current_site is not None else ("medium" if fallback_site is not None else site_confidence)
        sentence_site_id = resolve_site_id(sentence_site) if sentence_site else None
        terms = get_extraction_terms()

        if terms.matches_all("access_badge_gate_elevator", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if sentence_site_confidence == "high" else "medium",
            )
            events.append(
                {
                    "event_id": build_event_id(transcript_path, "access-constraint-badge", len(events) + 1),
                    "type": "access_constraint",
                    "site": site,
                    "details": "Access requires badge through secured parking gate and elevator",
                    "confidence": confidence,
                    "timestamp": transcript_path.stem,
                    "source_excerpt": sentence,
                    "category": "badge_access",
                    "relationship": "employee→site dependency",
                    "blocking": True,
                }
            )

        dependency_person = extract_person_name_from_sentence(sentence)
        if (
            "badge" in lowered_sentence
            and terms.matches_any("access_badge_only", lowered_sentence, site_id=sentence_site_id)
        ) or terms.matches_any("access_dependency", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if sentence_site_confidence == "high" else "medium",
            )
            detail = sentence
            if dependency_person is not None and "badge" in lowered_sentence:
                detail = f"Only {dependency_person} has the badge or access needed to work independently"
            events.append(
                {
                    "event_id": build_event_id(transcript_path, "access-constraint-dependency", len(events) + 1),
                    "type": "access_constraint",
                    "site": site,
                    "details": detail,
                    "confidence": confidence,
                    "timestamp": transcript_path.stem,
                    "source_excerpt": sentence,
                    "category": "dependency",
                    "relationship": "employee→site dependency",
                    "blocking": True,
                }
            )

        if terms.matches_any("access_key_control", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if sentence_site_confidence == "high" else "medium",
            )
            events.append(
                {
                    "event_id": build_event_id(transcript_path, "access-constraint-key", len(events) + 1),
                    "type": "access_constraint",
                    "site": site,
                    "details": "Only one key exists; second cleaner does not have access to offices",
                    "confidence": confidence,
                    "timestamp": transcript_path.stem,
                    "source_excerpt": sentence,
                    "category": "key_control",
                    "relationship": "employee→site dependency",
                    "affected_role": "cleaner",
                    "blocking": True,
                }
            )

        if terms.matches_any("access_key_holder", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if sentence_site_confidence == "high" else "medium",
            )
            events.append(
                {
                    "event_id": build_event_id(transcript_path, "access-constraint-dependency", len(events) + 1),
                    "type": "access_constraint",
                    "site": site,
                    "details": "Work cannot be performed without key holder present",
                    "confidence": confidence,
                    "timestamp": transcript_path.stem,
                    "source_excerpt": sentence,
                    "category": "dependency",
                    "relationship": "employee→site dependency",
                    "blocking": True,
                }
            )

        if terms.matches_any("access_extra", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if sentence_site_confidence == "high" else "medium",
            )
            events.append(
                {
                    "event_id": build_event_id(transcript_path, "access-constraint-extra", len(events) + 1),
                    "type": "access_constraint",
                    "site": site,
                    "details": sentence,
                    "confidence": confidence,
                    "timestamp": transcript_path.stem,
                    "source_excerpt": sentence,
                    "category": "dependency",
                    "relationship": "employee→site dependency",
                    "blocking": True,
                }
            )

        if terms.matches_any("retention_risk", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(sentence_site, "medium")
            retention_event = {
                "event_id": build_event_id(transcript_path, "employee-retention-risk", len(events) + 1),
                "type": "employee_retention_risk",
                "site": site,
                "details": sentence,
                "confidence": confidence,
                "timestamp": transcript_path.stem,
                "source_excerpt": sentence,
                "relationship": "employee→site stability",
            }
            if dependency_person is not None:
                retention_event["employee"] = dependency_person
            events.append(retention_event)

        if terms.matches_any("staffing_risk", lowered_sentence, site_id=sentence_site_id):
            site, confidence = finalize_site_and_confidence(
                sentence_site,
                "high" if "critically short" in lowered_sentence and sentence_site_confidence == "high" else "medium",
            )
            open_positions = extract_open_positions(sentence)
            if open_positions is None and ("short staffed" in lowered_sentence or "short-staffed" in lowered_sentence):
                open_positions = 1
            severity = "critical" if (
                "critically short" in lowered_sentence
                or "multiple resignations" in lowered_sentence
                or (open_positions is not None and open_positions >= 2)
            ) else "medium"
            staffing_event = {
                "event_id": build_event_id(transcript_path, "staffing-risk", len(events) + 1),
                "type": "staffing_risk",
                "site": site,
                "details": sentence,
                "confidence": confidence,
                "timestamp": transcript_path.stem,
                "source_excerpt": sentence,
                "severity": severity,
                "relationship": "site capacity",
            }
            if open_positions is not None:
                staffing_event["open_positions"] = open_positions
            events.append(staffing_event)

        site_observation = extract_site_observation(
            sentence, transcript_path, len(events) + 1, sentence_site, sentence_site_id
        )
        if site_observation is not None:
            events.append(site_observation)

    return events


def extract_events(
    transcript_path: Path,
    output_dir: Path,
    transcript_text: str | None = None,
    last_site_path: Path | None = None,
) -> list[Path]:
    normalized_text = transcript_path.read_text(encoding="utf-8").strip() if transcript_text is None else transcript_text.strip()
    events = fallback_extract_events(
        normalized_text,
        transcript_path,
        last_site_path=last_site_path,
    )

    written_paths: list[Path] = []
    for event in events:
        written_paths.append(write_event(output_dir, event))
    return written_paths
