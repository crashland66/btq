from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Iterable

from processing_core.capture_semantics import LocalModelCaptureEngine, RuleCaptureEngine, SemanticResult, validate_semantic_result
from voice_memo.semantics import (
    DEFAULT_OLLAMA_SEMANTIC_URL,
    VoiceMemoSemanticEngine,
    VoiceMemoTranscriptContext,
    capture_semantic_input,
)


DEFAULT_FIXTURE_PATH = Path(__file__).with_name("semantic_eval_fixtures.json")


def context_from_payload(payload: dict[str, Any]) -> VoiceMemoTranscriptContext:
    return VoiceMemoTranscriptContext(
        capture_id=str(payload.get("capture_id") or ""),
        routing_flag=str(payload.get("routing_flag") or ""),
        raw_text=str(payload.get("raw_text") or ""),
        raw_transcript_path=Path(str(payload.get("raw_transcript_path") or "")),
        audio_file=str(payload.get("audio_file") or ""),
        site_id=str(payload.get("site_id") or ""),
        site=str(payload.get("site") or ""),
        employees=payload.get("employees") if isinstance(payload.get("employees"), list) else [],
        note=str(payload.get("note") or ""),
        captured_at=str(payload.get("captured_at") or ""),
    )


def load_fixture_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("semantic eval fixture must be a JSON array")
    cases: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"semantic eval fixture item {index} must be an object")
        case_id = str(item.get("id") or "").strip()
        context_payload = item.get("context")
        expected = item.get("expected")
        if not case_id:
            raise ValueError(f"semantic eval fixture item {index} is missing id")
        if not isinstance(context_payload, dict):
            raise ValueError(f"semantic eval fixture item {case_id} is missing context")
        if not isinstance(expected, dict):
            raise ValueError(f"semantic eval fixture item {case_id} is missing expected")
        cases.append({"id": case_id, "context": context_from_payload(context_payload), "expected": dict(expected)})
    return cases


def action_projection(result: SemanticResult) -> dict[str, Any]:
    actions = list(result.extracted_actions or [])
    primary = actions[0] if actions else None
    if primary is None:
        return {
            "intent": "unknown",
            "target_type": "general",
            "target_id": "",
            "action": "",
            "review_required": False,
            "confidence": "normal",
        }
    action = primary.action_key
    intent = primary.action_key
    if primary.job_type == "set_entity_status":
        status = ""
        if isinstance(primary.payload_fields, dict):
            status = str(primary.payload_fields.get("status") or "")
        action = "set_inactive" if status == "inactive" else "status_change"
        intent = "site_status_change" if primary.target_type == "site" else "employee_status_change"
    return {
        "intent": intent,
        "target_type": primary.target_type,
        "target_id": primary.target_id,
        "action": action,
        "review_required": True,
        "confidence": primary.confidence,
    }


def expected_matches(result: SemanticResult, expected: dict[str, Any]) -> bool:
    projection = action_projection(result)
    for field in ("intent", "target_type", "target_id", "action", "review_required"):
        if field in expected and projection[field] != expected[field]:
            return False
    return True


def evaluate_cases(
    cases: Iterable[dict[str, Any]],
    engine: VoiceMemoSemanticEngine,
    *,
    model: str,
    provider: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        expected = case["expected"]
        context = case["context"]
        row: dict[str, Any] = {
            "fixture_id": case["id"],
            "model": model,
            "engine": provider,
            "provider": provider,
            "expected": expected,
            "valid_json": False,
            "validated": False,
            "pass": False,
            "elapsed_ms": 0.0,
            "error": "",
        }
        try:
            result = engine(capture_semantic_input(context))
            row["valid_json"] = True
            validate_semantic_result(result)
            row["validated"] = True
            row["actual"] = action_projection(result)
            row["actual"]["action_count"] = len(result.extracted_actions or [])
            row["pass"] = expected_matches(result, expected)
        except Exception as exc:  # noqa: BLE001 - eval rows should capture failures
            row["error"] = str(exc)
        finally:
            row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def summary_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["elapsed_ms"]) for row in rows if isinstance(row.get("elapsed_ms"), (int, float))]
    passed = sum(1 for row in rows if row.get("pass") is True)
    validated = sum(1 for row in rows if row.get("validated") is True)
    valid_json = sum(1 for row in rows if row.get("valid_json") is True)
    summary: dict[str, Any] = {
        "cases": len(rows),
        "passed": passed,
        "valid_json": valid_json,
        "validated": validated,
    }
    if latencies:
        sorted_latencies = sorted(latencies)
        summary["latency_p50_ms"] = round(statistics.median(sorted_latencies), 3)
        p95_index = min(len(sorted_latencies) - 1, int(0.95 * (len(sorted_latencies) - 1)))
        summary["latency_p95_ms"] = round(sorted_latencies[p95_index], 3)
    return summary


def build_engine(args: argparse.Namespace) -> VoiceMemoSemanticEngine:
    if args.engine == "rule":
        return RuleCaptureEngine()
    if args.engine == "ollama":
        if not args.model:
            raise ValueError("--model is required when --engine ollama")
        from vision_backends import OllamaTextClient

        return LocalModelCaptureEngine(
            OllamaTextClient(
                model=args.model,
                base_url=args.url,
                timeout_seconds=args.timeout,
                keep_alive=args.keep_alive,
                disable_thinking=not args.enable_thinking,
            ),
            fallback=RuleCaptureEngine(),
        )
    raise ValueError(f"unsupported semantic eval engine: {args.engine}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate voice memo semantic models without mutating BTQ state.")
    parser.add_argument("--engine", choices=("rule", "ollama"), default="ollama")
    parser.add_argument("--url", default=DEFAULT_OLLAMA_SEMANTIC_URL)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--keep-alive", default="10m")
    parser.add_argument("--enable-thinking", action="store_true", help="Allow Ollama reasoning mode for models that support it.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_fixture_cases(args.fixture)
    engine = build_engine(args)
    model = args.model or getattr(engine, "engine_name", args.engine)
    rows = evaluate_cases(cases, engine, model=model, provider=args.engine)
    write_jsonl(args.output, rows)
    return summary_for_rows(rows)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run(args)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
