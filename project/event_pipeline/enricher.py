from __future__ import annotations

import json
import re
from pathlib import Path

from event_pipeline.schema import write_event


def resolve_site_name(site: str) -> str:
    return site.strip()


def enrich_event(event: dict) -> dict:
    enriched = dict(event)
    enriched["site"] = resolve_site_name(str(enriched["site"]))
    enriched["details"] = str(enriched["details"]).strip()
    enriched["source_excerpt"] = str(enriched["source_excerpt"]).strip()
    if "observations" in enriched:
        normalized_observations: list[dict[str, str]] = []
        seen: set[str] = set()
        for observation in enriched["observations"]:
            if not isinstance(observation, dict):
                continue
            observation_type = str(observation.get("type", "")).strip()
            if not observation_type or observation_type in seen:
                continue
            seen.add(observation_type)
            normalized_observations.append(
                {
                    "type": observation_type,
                    "confidence": "observed",
                }
            )
        enriched["observations"] = normalized_observations
    if "blocking" not in enriched:
        enriched["blocking"] = False
    return enriched


SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def aggregate_staffing_risk_events(events: list[dict]) -> list[dict]:
    aggregated_by_site: dict[str, dict] = {}
    passthrough: list[dict] = []

    for event in events:
        if event.get("type") != "staffing_risk":
            passthrough.append(event)
            continue

        site = str(event["site"])
        existing = aggregated_by_site.get(site)
        if existing is None:
            aggregated_by_site[site] = dict(event)
            continue

        existing_count = int(existing.get("open_positions", 0))
        event_count = int(event.get("open_positions", 0))
        total_open_positions = existing_count + event_count
        existing["open_positions"] = total_open_positions if total_open_positions > 0 else existing_count or event_count

        existing_severity = str(existing.get("severity", "low"))
        event_severity = str(event.get("severity", "low"))
        if SEVERITY_ORDER.get(event_severity, -1) > SEVERITY_ORDER.get(existing_severity, -1):
            existing["severity"] = event_severity

        if int(existing.get("open_positions", 0)) >= 2:
            existing["severity"] = "critical"

        existing["details"] = f"{existing['details']}; {event['details']}"
        existing["source_excerpt"] = f"{existing['source_excerpt']} {event['source_excerpt']}".strip()

    return passthrough + [aggregated_by_site[key] for key in sorted(aggregated_by_site)]


def strongest_confidence(events: list[dict]) -> str:
    best = "low"
    for event in events:
        confidence = str(event.get("confidence", "low"))
        if CONFIDENCE_ORDER.get(confidence, -1) > CONFIDENCE_ORDER.get(best, -1):
            best = confidence
    return best


def unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def join_clauses(clauses: list[str]) -> str:
    items = unique_preserve_order(clauses)
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def classify_material_detail(detail: str) -> str:
    lowered = detail.lower()
    if any(token in lowered for token in ("floor", "flooring", "vinyl", "carpet", "tile", "plank", "bct", "vct", "lvp")):
        return "flooring"
    if any(token in lowered for token in ("stall", "surface", "metal", "wall", "counter", "divider")):
        return "surface"
    return "general"


def summarize_material_details(details: list[str], bucket: str) -> str:
    lowered_details = [detail.lower() for detail in details]

    if bucket == "flooring":
        clauses: list[str] = []
        if any("carpet square" in detail or "carpet tile" in detail for detail in lowered_details):
            clauses.append("Carpet squares are present")
        if any("vinyl plank" in detail or re.search(r"\blvp\b", detail) for detail in lowered_details):
            clauses.append("Vinyl plank flooring is used in exam rooms")
        if any("vinyl flooring" in detail or "vinyl floors" in detail for detail in lowered_details):
            clauses.append("Vinyl flooring is used in bathrooms and kitchen areas")
        if any("no vct" in detail or "no bct" in detail or "no vinyl composition tile" in detail for detail in lowered_details):
            clauses.append("No VCT flooring was noted")
        if clauses:
            return join_clauses(clauses)

    if bucket == "surface":
        clauses: list[str] = []
        if any("stall" in detail for detail in lowered_details):
            clauses.append("Stall surfaces require extra attention")
        if any("mark" in detail for detail in lowered_details):
            clauses.append("Surface marks remain visible")
        if any(
            token in detail
            for detail in lowered_details
            for token in ("challenge to clean", "hard to clean", "difficult to clean", "doesn't come clean easily", "doesnt come clean easily")
        ):
            clauses.append("They are difficult to clean")
        if clauses:
            return join_clauses(clauses)

    return join_clauses(details)


def summarize_condition_details(details: list[str]) -> str:
    lowered_details = [detail.lower() for detail in details]
    clauses: list[str] = []

    if any("stall" in detail for detail in lowered_details):
        subject = "Stalls"
    elif any("material" in detail for detail in lowered_details):
        subject = "Materials"
    else:
        subject = "Site surfaces"

    if any(
        token in detail
        for detail in lowered_details
        for token in ("challenge to clean", "hard to clean", "difficult to clean", "doesn't come clean easily", "doesnt come clean easily")
    ):
        clauses.append("are difficult to clean")
    if any("mark" in detail for detail in lowered_details):
        clauses.append("show marks")
    if any("complaint" in detail for detail in lowered_details):
        clauses.append("generate complaints")
    if clauses:
        return f"{subject} {join_clauses(clauses)}"

    return join_clauses(details)


def summarize_layout_details(details: list[str]) -> str:
    return join_clauses(details)


def merge_site_observation_group(events: list[dict], bucket: str | None = None) -> dict:
    base_event = dict(events[0])
    details = unique_preserve_order([str(event["details"]) for event in events])
    excerpts = unique_preserve_order([str(event["source_excerpt"]) for event in events])
    category = str(base_event["category"])

    if category == "material":
        merged_details = summarize_material_details(details, bucket or "general")
    elif category == "condition":
        merged_details = summarize_condition_details(details)
    else:
        merged_details = summarize_layout_details(details)

    merged_event = dict(base_event)
    if bucket is not None:
        merged_event["event_id"] = f"{base_event['event_id']}-{bucket}-merged"
    else:
        merged_event["event_id"] = f"{base_event['event_id']}-merged"
    merged_event["details"] = merged_details
    merged_event["source_excerpt"] = " ".join(excerpts)
    merged_event["confidence"] = strongest_confidence(events)
    return merged_event


def consolidate_site_observations(events: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    passthrough: list[dict] = []

    for event in events:
        if event.get("type") != "site_observation":
            passthrough.append(event)
            continue
        key = (str(event["site"]), str(event["category"]))
        grouped.setdefault(key, []).append(event)

    merged_events: list[dict] = []
    for key in sorted(grouped):
        site, category = key
        group_events = grouped[key]
        if category != "material":
            merged_events.append(merge_site_observation_group(group_events))
            continue

        material_buckets: dict[str, list[dict]] = {}
        for event in group_events:
            bucket = classify_material_detail(str(event["details"]))
            material_buckets.setdefault(bucket, []).append(event)

        for bucket in ("flooring", "surface"):
            bucket_events = material_buckets.pop(bucket, [])
            if bucket_events:
                merged_events.append(merge_site_observation_group(bucket_events, bucket))

        if material_buckets:
            remaining_events: list[dict] = []
            for bucket in sorted(material_buckets):
                remaining_events.extend(material_buckets[bucket])
            if merged_events and sum(1 for event in merged_events if event["type"] == "site_observation" and event["site"] == site and event["category"] == category) >= 2:
                last_material_event = next(
                    event
                    for event in reversed(merged_events)
                    if event["type"] == "site_observation" and event["site"] == site and event["category"] == category
                )
                merged_details = join_clauses([last_material_event["details"]] + [str(event["details"]) for event in remaining_events])
                last_material_event["details"] = merged_details
                last_material_event["source_excerpt"] = " ".join(
                    unique_preserve_order([last_material_event["source_excerpt"]] + [str(event["source_excerpt"]) for event in remaining_events])
                )
                last_material_event["confidence"] = strongest_confidence([last_material_event] + remaining_events)
            else:
                merged_events.append(merge_site_observation_group(remaining_events, "general"))

    return passthrough + merged_events


def enrich_events(raw_dir: Path, enriched_dir: Path, input_paths: list[Path] | None = None) -> list[Path]:
    paths = sorted(input_paths) if input_paths is not None else sorted(raw_dir.glob("*.json"))
    events = [enrich_event(json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    aggregated_events = aggregate_staffing_risk_events(events)
    consolidated_events = consolidate_site_observations(aggregated_events)
    written_paths: list[Path] = []
    for event in consolidated_events:
        written_paths.append(write_event(enriched_dir, event))
    return written_paths
