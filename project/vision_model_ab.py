"""Offline A/B harness for photo-vision model/strategy candidates.

Replays recent real field-capture photos through one (model, strategy) pair
and records the description, the derived QC category vs the worker's chosen
category (the ground truth an operator supplied at capture time), latency,
and JSON failures. Writes nothing to the pipeline.

Run once per candidate (separate processes — one model resident at a time,
and never while the pipeline or Studio is inferring):

    python -m vision_model_ab run --model mlx-community/Qwen2.5-VL-7B-Instruct-4bit --strategy single --out /tmp/vab_q25.jsonl --limit 12
    BTQ_MLX_MAX_TOKENS=1024 python -m vision_model_ab run --model mlx-community/Qwen3.5-9B-MLX-4bit --strategy two_pass --out /tmp/vab_q35.jsonl --limit 12

Then:

    python -m vision_model_ab compare /tmp/vab_q25.jsonl /tmp/vab_q35.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from field_capture import photo_vision
from field_capture.photo_vision_categories import derive_vision_category_fields


def _qc_assets(limit: int) -> list[photo_vision.FieldPhotoAsset]:
    assets = photo_vision.discover_photo_assets(
        photo_vision.default_intake_dir(),
        photo_vision.default_upload_dir(),
    )
    usable = [
        asset
        for asset in assets
        if asset.image_path.exists()
        and str(asset.qc_category or "").strip()
        and photo_vision.vision_lane_for(asset.qc_category) == photo_vision.VISION_LANE_QC
    ]
    return usable[-limit:] if limit else usable


def run(model: str, strategy: str, out_path: Path, limit: int) -> int:
    assets = _qc_assets(limit)
    if not assets:
        print("no usable QC photo assets found in intake")
        return 1
    print(f"model={model} strategy={strategy} corpus={len(assets)} photos")

    t_load = time.time()
    if strategy == photo_vision.VISION_STRATEGY_TWO_PASS:
        client = photo_vision.TwoPassMlxVisionClient(model)
    else:
        client = photo_vision.MlxVisionClient(model)
    print(f"loaded in {round(time.time() - t_load, 1)}s")

    try:
        import mlx.core as mx
    except ImportError:  # pragma: no cover - harness runs on Apple Silicon only
        mx = None

    rows: list[dict[str, object]] = []
    for index, asset in enumerate(assets, start=1):
        # Metal cache grows across image inferences; clearing between photos
        # keeps the 16GB M4 out of OOM territory when anything else loads a
        # model mid-run.
        if mx is not None:
            getattr(mx, "clear_cache", lambda: None)()
        t0 = time.time()
        error = None
        description = None
        try:
            description = client.describe_for_qc_category(asset, asset.qc_category)
        except Exception as exc:  # noqa: BLE001 - the failure IS the datum.
            error = f"{exc.__class__.__name__}: {str(exc)[:160]}"
        latency = round(time.time() - t0, 2)

        row: dict[str, object] = {
            "model": model,
            "strategy": strategy,
            "photo_asset_id": asset.photo_asset_id,
            "site_id": asset.site_id,
            "worker_qc_category": asset.qc_category,
            "latency_seconds": latency,
            "error": error,
        }
        if description is not None:
            category_fields = derive_vision_category_fields(description.area_guess, asset.qc_category)
            row.update(
                {
                    "description": description.description,
                    "area_guess": description.area_guess,
                    "vision_category": category_fields["vision_category"],
                    "category_agreement": category_fields["category_agreement"],
                    "possible_issues": description.possible_issues,
                    "confidence": description.confidence,
                    "quality_flags": description.quality_flags,
                }
            )
        rows.append(row)
        status = error or f"{row.get('category_agreement')} ({latency}s)"
        print(f"[{index}/{len(assets)}] {asset.photo_asset_id[:24]} {status}")

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
            rows[str(row["photo_asset_id"])] = row
    return rows


def compare(path_a: Path, path_b: Path) -> int:
    rows_a, rows_b = _load_rows(path_a), _load_rows(path_b)
    shared = sorted(set(rows_a) & set(rows_b))
    if not shared:
        print("no shared photos between runs")
        return 1

    def label(rows: dict[str, dict[str, object]]) -> str:
        first = next(iter(rows.values()))
        return f"{first['model']} [{first['strategy']}]"

    def stats(rows: dict[str, dict[str, object]], name: str) -> None:
        subset = [rows[k] for k in shared]
        errors = sum(1 for r in subset if r.get("error"))
        agreements = Counter(str(r.get("category_agreement")) for r in subset if not r.get("error"))
        latencies = sorted(float(r["latency_seconds"]) for r in subset)
        description_words = [len(str(r.get("description") or "").split()) for r in subset if not r.get("error")]
        mean_words = round(sum(description_words) / len(description_words)) if description_words else 0
        print(f"\n== {name}")
        print(f"   photos: {len(subset)}  errors: {errors}  category agreement: {dict(agreements)}")
        print(f"   latency s median/p90/max: {latencies[len(latencies)//2]:.1f} / {latencies[int(len(latencies)*0.9)]:.1f} / {latencies[-1]:.1f}")
        print(f"   mean description length: {mean_words} words")

    stats(rows_a, label(rows_a))
    stats(rows_b, label(rows_b))

    print("\n== side-by-side (blind-read the descriptions)")
    for key in shared:
        a, b = rows_a[key], rows_b[key]
        print(f"\n-- {key}  worker category: {a.get('worker_qc_category')}")
        for row in (a, b):
            tag = f"{row.get('category_agreement') or row.get('error')}"
            print(f"   [{row['strategy']}] {tag}: {str(row.get('description') or '')[:400]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Replay recent QC photos through one model+strategy")
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--strategy", choices=["single", "two_pass"], default="single")
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--limit", type=int, default=12)
    compare_parser = sub.add_parser("compare", help="Diff two run reports")
    compare_parser.add_argument("run_a", type=Path)
    compare_parser.add_argument("run_b", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        return run(args.model, args.strategy, args.out, args.limit)
    return compare(args.run_a, args.run_b)


if __name__ == "__main__":
    raise SystemExit(main())
