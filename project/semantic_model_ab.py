"""Offline A/B harness for semantic-extraction model candidates.

Replays the real field-audio transcript corpus (runtime
field_capture/audio_transcripts/*.json) through LocalModelCaptureEngine with a
candidate MLX model and records what each capture would have produced —
extracted actions, classification, latency, and whether the model path failed
and silently fell back to the rule engine (the failure mode a live swap would
otherwise hide).

Run once per candidate (separate processes preserve the production load-exit
pattern — never two models resident at once):

    python -m semantic_model_ab run --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --out /tmp/ab_q25.jsonl
    BTQ_MLX_MAX_TOKENS=1024 python -m semantic_model_ab run --model mlx-community/Qwen3.5-9B-MLX-4bit --out /tmp/ab_q35.jsonl

Then diff the runs:

    python -m semantic_model_ab compare /tmp/ab_q25.jsonl /tmp/ab_q35.jsonl

Reports stay local; transcripts never leave the machine.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from field_capture import audio_semantics
from processing_core.capture_semantics import LocalModelCaptureEngine


class RecordingClient:
    """Delegate to a real client while capturing the error the engine's
    silent rule-engine fallback would otherwise swallow."""

    def __init__(self, client: object) -> None:
        self._client = client
        self.provider = getattr(client, "provider", "local")
        self.model = getattr(client, "model", "unknown")
        self.last_error: str | None = None

    def generate_json(self, prompt: str) -> dict:
        self.last_error = None
        try:
            return self._client.generate_json(prompt)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the engine's fallback.
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            raise


def _action_row(action: object) -> dict[str, object]:
    proposed = getattr(action, "proposed_queue_job", None) or {}
    return {
        "action_key": getattr(action, "action_key", ""),
        "candidate_type": getattr(action, "candidate_type", ""),
        "job_type": getattr(action, "job_type", "") or (proposed.get("job_type") if isinstance(proposed, dict) else ""),
        "summary": str(getattr(action, "summary", ""))[:120],
        "proposed_queue_job_error": getattr(action, "proposed_queue_job_error", ""),
    }


def run(model: str, out_path: Path, transcript_dir: Path | None, limit: int) -> int:
    from vision_backends import MlxTextClient

    directory = transcript_dir or audio_semantics.default_transcript_dir()
    transcripts = audio_semantics.discover_completed_transcripts(directory)
    if limit:
        transcripts = transcripts[-limit:]
    if not transcripts:
        print(f"no completed transcripts under {directory}")
        return 1

    print(f"model={model} corpus={len(transcripts)} transcripts")
    t_load = time.time()
    recorder = RecordingClient(MlxTextClient(model))
    engine = LocalModelCaptureEngine(recorder)
    load_seconds = round(time.time() - t_load, 1)
    print(f"loaded in {load_seconds}s")

    rows: list[dict[str, object]] = []
    for index, transcript in enumerate(transcripts, start=1):
        t0 = time.time()
        result = engine(transcript)
        latency = round(time.time() - t0, 2)
        row = {
            "model": model,
            "audio_asset_id": transcript.audio_asset_id,
            "site_id": transcript.site_id,
            "captured_at": transcript.captured_at,
            "raw_text": transcript.raw_text[:300],
            "latency_seconds": latency,
            "model_error": recorder.last_error,
            "fell_back_to_rules": recorder.last_error is not None,
            "issue_type": result.issue_type,
            "urgency": result.urgency,
            "visit_proposed": result.visit_proposed,
            "actions": [_action_row(action) for action in (result.extracted_actions or [])],
        }
        rows.append(row)
        status = "FALLBACK" if row["fell_back_to_rules"] else f"{len(row['actions'])} action(s)"
        print(f"[{index}/{len(transcripts)}] {transcript.audio_asset_id[:24]} {latency}s {status}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"wrote {len(rows)} rows -> {out_path}")
    return 0


def _load_rows(path: Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[str(row["audio_asset_id"])] = row
    return rows


def _action_types(row: dict[str, object]) -> tuple[str, ...]:
    return tuple(sorted(str(a.get("candidate_type") or a.get("job_type") or "?") for a in row.get("actions", [])))


def compare(path_a: Path, path_b: Path) -> int:
    rows_a, rows_b = _load_rows(path_a), _load_rows(path_b)
    shared = sorted(set(rows_a) & set(rows_b))
    if not shared:
        print("no shared captures between runs")
        return 1
    model_a = str(next(iter(rows_a.values()))["model"])
    model_b = str(next(iter(rows_b.values()))["model"])

    def stats(rows: dict[str, dict[str, object]], label: str) -> None:
        subset = [rows[k] for k in shared]
        latencies = sorted(float(r["latency_seconds"]) for r in subset)
        fallbacks = sum(1 for r in subset if r["fell_back_to_rules"])
        total_actions = sum(len(r.get("actions", [])) for r in subset)
        job_errors = sum(
            1 for r in subset for a in r.get("actions", []) if a.get("proposed_queue_job_error")
        )
        print(f"\n== {label}")
        print(f"   captures: {len(subset)}  fallbacks-to-rules: {fallbacks}  actions: {total_actions}  proposed-job errors: {job_errors}")
        print(f"   latency s median/p90/max: {latencies[len(latencies)//2]:.1f} / {latencies[int(len(latencies)*0.9)]:.1f} / {latencies[-1]:.1f}")
        print(f"   issue types: {Counter(str(r['issue_type']) for r in subset).most_common(6)}")

    stats(rows_a, model_a)
    stats(rows_b, model_b)

    same = [k for k in shared if _action_types(rows_a[k]) == _action_types(rows_b[k])]
    print(f"\n== agreement: identical action-type sets on {len(same)}/{len(shared)} captures")
    for key in shared:
        if key in same:
            continue
        a, b = rows_a[key], rows_b[key]
        print(f"\n-- {key}")
        print(f"   text: {str(a['raw_text'])[:160]}")
        print(f"   {model_a}: {list(_action_types(a))} (fallback={a['fell_back_to_rules']})")
        print(f"   {model_b}: {list(_action_types(b))} (fallback={b['fell_back_to_rules']})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Replay the corpus through one model")
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--transcript-dir", type=Path, default=None)
    run_parser.add_argument("--limit", type=int, default=0)
    compare_parser = sub.add_parser("compare", help="Diff two run reports")
    compare_parser.add_argument("run_a", type=Path)
    compare_parser.add_argument("run_b", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        return run(args.model, args.out, args.transcript_dir, args.limit)
    return compare(args.run_a, args.run_b)


if __name__ == "__main__":
    raise SystemExit(main())
