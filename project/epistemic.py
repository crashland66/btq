from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from processing_core.slugs import lower_dash_slug
from processing_core.time import utc_now


OBSERVED = "OBSERVED"
REPORTED_BY_HUMAN = "REPORTED_BY_HUMAN"
INFERRED = "INFERRED"
ASSUMED = "ASSUMED"
UNCONFIRMED = "UNCONFIRMED"
CONTRADICTED = "CONTRADICTED"
HISTORICALLY_TRUE = "HISTORICALLY_TRUE"
CURRENTLY_VALID = "CURRENTLY_VALID"
STALE = "STALE"
DISPUTED = "DISPUTED"

CLASSIFICATIONS = {
    OBSERVED,
    REPORTED_BY_HUMAN,
    INFERRED,
    ASSUMED,
    UNCONFIRMED,
    CONTRADICTED,
    HISTORICALLY_TRUE,
    CURRENTLY_VALID,
    STALE,
    DISPUTED,
}


@dataclass(frozen=True)
class EpistemicState:
    classification: str
    source_type: str
    confidence: str
    derived_from: str
    timestamp_context: str
    confidence_basis: list[str]
    temporal_state: str = CURRENTLY_VALID
    contradicted_by: list[str] | None = None


def classify_statement(text: str, *, source_type: str = "transcript", timestamp_context: str | None = None) -> EpistemicState:
    lowered = text.lower()
    classification = OBSERVED
    confidence = "medium"
    basis = ["explicit operational statement"]
    if any(phrase in lowered for phrase in ("presumed", "assume", "assumed", "likely", "probably", "appears to", "seems")):
        classification = ASSUMED
        confidence = "low"
        basis = ["assumption language"]
    if any(phrase in lowered for phrase in ("did not respond", "no response", "didn't respond", "has not responded")):
        classification = OBSERVED
        confidence = "medium"
        basis = ["directly reported non-response"]
    if any(phrase in lowered for phrase in ("resigned", "quit", "left the job")) and any(phrase in lowered for phrase in ("presumed", "assume", "no response", "did not respond")):
        classification = INFERRED
        confidence = "low"
        basis = ["inferred from absence or assumption wording"]
    if any(phrase in lowered for phrase in ("client said", "customer said", "reported by", "client reported", "customer reported", "discovered by client")):
        classification = REPORTED_BY_HUMAN
        confidence = "medium"
        basis = ["human report attribution"]
    if any(phrase in lowered for phrase in ("unconfirmed", "needs confirmation", "not confirmed")):
        classification = UNCONFIRMED
        confidence = "low"
        basis = ["explicit uncertainty language"]
    return EpistemicState(
        classification=classification,
        source_type=source_type,
        confidence=confidence,
        derived_from=text[:500],
        timestamp_context=timestamp_context or utc_now(),
        confidence_basis=basis,
    )


def apply_epistemic_state(payload: dict[str, Any], text: str, *, source_type: str = "transcript") -> dict[str, Any]:
    updated = dict(payload)
    if "epistemic_state" not in updated:
        updated["epistemic_state"] = asdict(classify_statement(text, source_type=source_type, timestamp_context=str(payload.get("timestamp", utc_now()))))
    return updated


def contradiction_dir(runtime_root: Path) -> Path:
    return runtime_root / "contradictions"


def contradiction_path(runtime_root: Path, contradiction_id: str) -> Path:
    return contradiction_dir(runtime_root) / f"{contradiction_id}.json"


def slugify(value: str) -> str:
    return lower_dash_slug(value, fallback="contradiction")


def iter_contradictions(runtime_root: Path, capture_id: str | None = None) -> list[dict[str, Any]]:
    root = contradiction_dir(runtime_root)
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if capture_id and payload.get("capture_id") != capture_id:
            continue
        payload["_path"] = str(path)
        records.append(payload)
    return records


def transition_temporal_state(epistemic_state: dict[str, Any], new_state: str, reason: str) -> dict[str, Any]:
    updated = dict(epistemic_state)
    transitions = list(updated.get("temporal_transitions") if isinstance(updated.get("temporal_transitions"), list) else [])
    transitions.append({"from": updated.get("temporal_state"), "to": new_state, "reason": reason, "timestamp": utc_now()})
    updated["temporal_state"] = new_state
    updated["temporal_transitions"] = transitions
    if new_state == CONTRADICTED:
        updated["classification"] = CONTRADICTED
    if new_state == STALE and updated.get("classification") == CURRENTLY_VALID:
        updated["classification"] = HISTORICALLY_TRUE
    return updated
