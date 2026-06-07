from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from config import get_config
from epistemic import ASSUMED, DISPUTED, INFERRED, UNCONFIRMED, iter_contradictions
from io_atomic import atomic_write_text
from processing_core.slugs import lower_dash_slug
from processing_core.time import utc_now
from queue_processor.reconciliation import generate_report, iter_evidence
from queue_processor.repair import parse_date


DISPUTED_BY_MANAGER = "DISPUTED_BY_MANAGER"
DISPUTED_BY_CLIENT = "DISPUTED_BY_CLIENT"
DISPUTED_BY_FIELD_REPORT = "DISPUTED_BY_FIELD_REPORT"
UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
DISPUTE_STATES = {
    DISPUTED_BY_MANAGER,
    DISPUTED_BY_CLIENT,
    DISPUTED_BY_FIELD_REPORT,
    UNRESOLVED_CONFLICT,
}

NEEDS_REVIEW = "NEEDS_REVIEW"
HIGH_OPERATIONAL_RISK = "HIGH_OPERATIONAL_RISK"
UNVERIFIED_BUT_ACTIONABLE = "UNVERIFIED_BUT_ACTIONABLE"
REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
ESCALATION_STATES = {
    NEEDS_REVIEW,
    HIGH_OPERATIONAL_RISK,
    UNVERIFIED_BUT_ACTIONABLE,
    REQUIRES_CONFIRMATION,
}

ACK_MANAGER_REVIEWED_CONTRADICTION = "manager reviewed contradiction"
ACK_MANAGER_ACKNOWLEDGED_UNRESOLVED_AMBIGUITY = "manager acknowledged unresolved ambiguity"
ACK_MANAGER_ACCEPTED_REPLAY_RISK = "manager accepted replay risk"
ACKNOWLEDGMENT_TYPES = {
    ACK_MANAGER_REVIEWED_CONTRADICTION,
    ACK_MANAGER_ACKNOWLEDGED_UNRESOLVED_AMBIGUITY,
    ACK_MANAGER_ACCEPTED_REPLAY_RISK,
}

RECORD_REVIEW = "review"
RECORD_DISPUTE = "dispute"
RECORD_ACKNOWLEDGMENT = "acknowledgment"


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    reviewer: str
    timestamp: str
    target_artifact: str
    epistemic_classification_reviewed: str
    prior_state: str
    proposed_updated_state: str
    rationale: str
    supporting_evidence_refs: list[str]
    confidence_rationale: str
    contradictory_evidence_refs: list[str]
    record_type: str = RECORD_REVIEW
    escalation_state: str | None = None
    dispute_state: str | None = None
    acknowledgment_type: str | None = None
    acknowledgment_target: str | None = None
    resolution_status: str = "open"
    observational: bool = True


@dataclass(frozen=True)
class UnresolvedItem:
    item_type: str
    target_artifact: str
    summary: str
    risk: str
    evidence_refs: list[str]
    governance_refs: list[str]
    account_hint: str | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class UnresolvedReport:
    generated_at: str
    unresolved_contradictions: list[UnresolvedItem]
    stale_assumptions: list[UnresolvedItem]
    unreviewed_inferences: list[UnresolvedItem]
    high_risk_unresolved_narratives: list[UnresolvedItem]
    disputed_operational_states: list[UnresolvedItem]


def reviews_root(runtime_root: Path) -> Path:
    return runtime_root / "reviews"


def slugify(value: str) -> str:
    return lower_dash_slug(value, fallback="review")


def review_path(runtime_root: Path, review_id: str) -> Path:
    return reviews_root(runtime_root) / f"{slugify(review_id)[:180]}.json"


def normalize_refs(refs: list[str] | None) -> list[str]:
    return [str(ref) for ref in (refs or []) if str(ref).strip()]


def write_review(
    runtime_root: Path,
    *,
    review_id: str,
    reviewer: str,
    target_artifact: str,
    epistemic_classification_reviewed: str,
    prior_state: str,
    proposed_updated_state: str,
    rationale: str,
    supporting_evidence_refs: list[str] | None = None,
    confidence_rationale: str,
    contradictory_evidence_refs: list[str] | None = None,
    escalation_state: str | None = None,
    dispute_state: str | None = None,
    record_type: str = RECORD_REVIEW,
    acknowledgment_type: str | None = None,
    acknowledgment_target: str | None = None,
    resolution_status: str = "open",
    timestamp: str | None = None,
) -> Path:
    if escalation_state is not None and escalation_state not in ESCALATION_STATES:
        raise ValueError(f"Unknown escalation state: {escalation_state}")
    if dispute_state is not None and dispute_state not in DISPUTE_STATES:
        raise ValueError(f"Unknown dispute state: {dispute_state}")
    if acknowledgment_type is not None and acknowledgment_type not in ACKNOWLEDGMENT_TYPES:
        raise ValueError(f"Unknown acknowledgment type: {acknowledgment_type}")
    record = ReviewRecord(
        review_id=review_id,
        reviewer=reviewer,
        timestamp=timestamp or utc_now(),
        target_artifact=target_artifact,
        epistemic_classification_reviewed=epistemic_classification_reviewed,
        prior_state=prior_state,
        proposed_updated_state=proposed_updated_state,
        rationale=rationale,
        supporting_evidence_refs=normalize_refs(supporting_evidence_refs),
        confidence_rationale=confidence_rationale,
        contradictory_evidence_refs=normalize_refs(contradictory_evidence_refs),
        record_type=record_type,
        escalation_state=escalation_state,
        dispute_state=dispute_state,
        acknowledgment_type=acknowledgment_type,
        acknowledgment_target=acknowledgment_target,
        resolution_status=resolution_status,
    )
    path = review_path(runtime_root, review_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(asdict(record), indent=2, sort_keys=True) + "\n")
    return path


def write_dispute(
    runtime_root: Path,
    *,
    dispute_id: str,
    reviewer: str,
    target_artifact: str,
    disputed_state: str,
    dispute_state: str,
    rationale: str,
    supporting_evidence_refs: list[str] | None = None,
    contradictory_evidence_refs: list[str] | None = None,
    confidence_rationale: str = "Dispute records preserve disagreement; they do not adjudicate truth.",
    timestamp: str | None = None,
) -> Path:
    return write_review(
        runtime_root,
        review_id=dispute_id,
        reviewer=reviewer,
        timestamp=timestamp,
        target_artifact=target_artifact,
        epistemic_classification_reviewed=DISPUTED,
        prior_state=disputed_state,
        proposed_updated_state=dispute_state,
        rationale=rationale,
        supporting_evidence_refs=supporting_evidence_refs,
        confidence_rationale=confidence_rationale,
        contradictory_evidence_refs=contradictory_evidence_refs,
        dispute_state=dispute_state,
        record_type=RECORD_DISPUTE,
    )


def write_acknowledgment(
    runtime_root: Path,
    *,
    acknowledgment_id: str,
    reviewer: str,
    target_artifact: str,
    acknowledgment_type: str,
    rationale: str,
    supporting_evidence_refs: list[str] | None = None,
    timestamp: str | None = None,
) -> Path:
    return write_review(
        runtime_root,
        review_id=acknowledgment_id,
        reviewer=reviewer,
        timestamp=timestamp,
        target_artifact=target_artifact,
        epistemic_classification_reviewed=UNCONFIRMED,
        prior_state="unacknowledged",
        proposed_updated_state="acknowledged",
        rationale=rationale,
        supporting_evidence_refs=supporting_evidence_refs,
        confidence_rationale="Acknowledgment means a human accepted visibility of uncertainty, not that the claim is true.",
        contradictory_evidence_refs=[],
        record_type=RECORD_ACKNOWLEDGMENT,
        acknowledgment_type=acknowledgment_type,
        acknowledgment_target=target_artifact,
        resolution_status="acknowledged",
    )


def iter_review_records(runtime_root: Path) -> list[dict[str, Any]]:
    root = reviews_root(runtime_root)
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            payload["_path"] = str(path)
            records.append(payload)
    return records


def review_targets(records: list[dict[str, Any]]) -> set[str]:
    return {str(record.get("target_artifact")) for record in records if record.get("record_type") == RECORD_REVIEW}


def acknowledged_targets(records: list[dict[str, Any]]) -> set[str]:
    targets: set[str] = set()
    for record in records:
        if record.get("record_type") != RECORD_ACKNOWLEDGMENT:
            continue
        if record.get("resolution_status") != "acknowledged":
            continue
        target = record.get("acknowledgment_target") or record.get("target_artifact")
        if isinstance(target, str):
            targets.add(target)
    return targets


def item_matches_filters(item: UnresolvedItem, *, account: str | None, since: datetime | None, high_risk_only: bool) -> bool:
    if account and account.lower() not in json.dumps(asdict(item)).lower():
        return False
    if since and item.timestamp:
        try:
            parsed = datetime.fromisoformat(item.timestamp)
        except ValueError:
            parsed = None
        if parsed is not None and parsed < since.replace(tzinfo=parsed.tzinfo):
            return False
    if high_risk_only and item.risk not in {"high", "critical"}:
        return False
    return True


def evidence_target(record: dict[str, Any]) -> str:
    job_id = record.get("job_id")
    if isinstance(job_id, str) and job_id:
        return job_id
    target = record.get("target_path")
    return str(target or record.get("_evidence_path", "unknown"))


def evidence_summary(record: dict[str, Any]) -> str:
    intent = record.get("intent") if isinstance(record.get("intent"), dict) else {}
    return str(intent.get("reason") or record.get("mutation_intent_summary") or record.get("nearby_content_excerpt") or "")


def build_unresolved_report(
    runtime_root: Path,
    vault_root: Path,
    personal_vault_root: Path,
    *,
    since: datetime | None = None,
    account: str | None = None,
    high_risk_only: bool = False,
) -> UnresolvedReport:
    reviews = iter_review_records(runtime_root)
    reviewed = review_targets(reviews)
    acknowledged = acknowledged_targets(reviews)
    unresolved_contradictions: list[UnresolvedItem] = []
    for contradiction in iter_contradictions(runtime_root):
        target = str(contradiction.get("contradiction_id") or contradiction.get("_path"))
        if target in acknowledged:
            continue
        item = UnresolvedItem(
            item_type="unresolved_contradiction",
            target_artifact=target,
            summary=str(contradiction.get("reason", "contradiction requires human interpretation")),
            risk="high",
            evidence_refs=[str(contradiction.get("_path", ""))],
            governance_refs=[],
            account_hint=None,
            timestamp=str(contradiction.get("created_at", "")),
        )
        if item_matches_filters(item, account=account, since=since, high_risk_only=high_risk_only):
            unresolved_contradictions.append(item)

    stale_assumptions: list[UnresolvedItem] = []
    unreviewed_inferences: list[UnresolvedItem] = []
    disputed_operational_states: list[UnresolvedItem] = []
    for record in iter_evidence(runtime_root):
        epistemic = record.get("epistemic_state") if isinstance(record.get("epistemic_state"), dict) else {}
        classification = str(epistemic.get("classification", ""))
        temporal_state = str(epistemic.get("temporal_state", ""))
        target = evidence_target(record)
        source = str(record.get("_evidence_path", ""))
        timestamp = str(record.get("mutation_timestamp", ""))
        if classification == ASSUMED or temporal_state == "STALE":
            item = UnresolvedItem(
                item_type="stale_assumption",
                target_artifact=target,
                summary=evidence_summary(record),
                risk="medium" if classification == ASSUMED else "high",
                evidence_refs=[source],
                governance_refs=[],
                account_hint=str(record.get("target_path", "")),
                timestamp=timestamp,
            )
            if target not in acknowledged and item_matches_filters(item, account=account, since=since, high_risk_only=high_risk_only):
                stale_assumptions.append(item)
        if classification in {INFERRED, ASSUMED, UNCONFIRMED} and target not in reviewed and target not in acknowledged:
            item = UnresolvedItem(
                item_type="unreviewed_inference",
                target_artifact=target,
                summary=evidence_summary(record),
                risk="high" if classification == INFERRED else "medium",
                evidence_refs=[source],
                governance_refs=[],
                account_hint=str(record.get("target_path", "")),
                timestamp=timestamp,
            )
            if item_matches_filters(item, account=account, since=since, high_risk_only=high_risk_only):
                unreviewed_inferences.append(item)

    for review in reviews:
        if review.get("record_type") != RECORD_DISPUTE:
            continue
        if review.get("resolution_status") not in {"open", None}:
            continue
        item = UnresolvedItem(
            item_type="disputed_operational_state",
            target_artifact=str(review.get("target_artifact", "")),
            summary=str(review.get("rationale", "")),
            risk="high",
            evidence_refs=normalize_refs(review.get("supporting_evidence_refs") if isinstance(review.get("supporting_evidence_refs"), list) else []),
            governance_refs=[str(review.get("_path", ""))],
            account_hint=str(review.get("target_artifact", "")),
            timestamp=str(review.get("timestamp", "")),
        )
        if item_matches_filters(item, account=account, since=since, high_risk_only=high_risk_only):
            disputed_operational_states.append(item)

    reconciliation = generate_report(runtime_root, vault_root, personal_vault_root, since=since, high_risk_only=high_risk_only)
    high_risk_unresolved_narratives: list[UnresolvedItem] = []
    for risk in reconciliation.replay_risks:
        target = str(risk.get("job_id") or risk.get("original_queue_file") or "")
        if target in acknowledged:
            continue
        item = UnresolvedItem(
            item_type="high_risk_unresolved_narrative",
            target_artifact=target,
            summary=str(risk.get("replay_reason", "replay risk requires review")),
            risk="high" if risk.get("replay_risk_classification") not in {"SAFE", "LOW_CONFIDENCE"} else "medium",
            evidence_refs=[str(risk.get("original_queue_file", ""))],
            governance_refs=[],
            account_hint=str(risk.get("target_path", "")),
            timestamp=None,
        )
        if item_matches_filters(item, account=account, since=since, high_risk_only=high_risk_only):
            high_risk_unresolved_narratives.append(item)

    return UnresolvedReport(
        generated_at=utc_now(),
        unresolved_contradictions=unresolved_contradictions,
        stale_assumptions=stale_assumptions,
        unreviewed_inferences=unreviewed_inferences,
        high_risk_unresolved_narratives=high_risk_unresolved_narratives,
        disputed_operational_states=disputed_operational_states,
    )


def format_unresolved_report(report: UnresolvedReport, json_output: bool) -> str:
    if json_output:
        return json.dumps(asdict(report), indent=2, sort_keys=True)
    sections = [
        ("Unresolved contradictions", report.unresolved_contradictions),
        ("Stale assumptions", report.stale_assumptions),
        ("Unreviewed inferences", report.unreviewed_inferences),
        ("High-risk unresolved narratives", report.high_risk_unresolved_narratives),
        ("Disputed operational states", report.disputed_operational_states),
    ]
    lines = ["BTQ unresolved ambiguity report", f"Generated: {report.generated_at}"]
    for heading, items in sections:
        lines.append(heading)
        if not items:
            lines.append("- none")
            continue
        for item in items:
            lines.append(f"- [{item.risk}] {item.target_artifact}: {item.summary}")
            if item.evidence_refs:
                lines.append(f"  evidence: {', '.join(ref for ref in item.evidence_refs if ref)}")
            if item.governance_refs:
                lines.append(f"  governance: {', '.join(ref for ref in item.governance_refs if ref)}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = get_config()
    parser = argparse.ArgumentParser(description="Surface unresolved epistemic governance items.")
    parser.add_argument("--runtime-root", type=Path, default=config.project_runtime_root)
    parser.add_argument("--vault-root", type=Path, default=config.vault_dir)
    parser.add_argument("--personal-vault-root", type=Path, default=config.personal_vault_dir)
    parser.add_argument("--since")
    parser.add_argument("--account")
    parser.add_argument("--high-risk-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_unresolved_report(
        args.runtime_root.expanduser(),
        args.vault_root.expanduser(),
        args.personal_vault_root.expanduser(),
        since=parse_date(args.since),
        account=args.account,
        high_risk_only=args.high_risk_only,
    )
    print(format_unresolved_report(report, args.json))
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
