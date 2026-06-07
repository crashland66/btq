from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from field_capture import pipeline_watcher


def _client_for_config(vision_runner: object, config: object) -> object:
    backend = str(getattr(config, "vision_backend", "mlx")).strip().lower()
    if backend == "mlx":
        model = str(getattr(config, "mlx_model", ""))
        max_tokens = int(getattr(config, "mlx_max_tokens", 0) or 0)
        if max_tokens > 0:
            return vision_runner.MlxVisionClient(model, max_tokens)
        return vision_runner.MlxVisionClient(model)
    if backend == "ollama":
        return vision_runner.OllamaVisionClient(
            str(getattr(config, "ollama_model", "")),
            str(getattr(config, "ollama_url", "")),
            float(getattr(config, "ollama_timeout_seconds", 0.0) or 0.0),
        )
    raise RuntimeError(f"unsupported cat vision backend: {backend}")


def run_child(*, repo_path: Path, limit: int) -> dict[str, int]:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    repo_resolved = repo_path.expanduser().resolve(strict=False)
    if not repo_resolved.exists():
        raise RuntimeError(f"BTQ_CAT_VISION_REPO does not exist: {repo_resolved}")
    sys.path.insert(0, str(repo_resolved))
    try:
        with redirect_stdout(sys.stderr):
            importlib.invalidate_caches()
            vision_runner = importlib.import_module("cat_pipeline.vision_runner")
            config = vision_runner.VisionRunnerConfig.from_env()
            try:
                config = dataclasses.replace(config, max_records_per_run=limit)
            except TypeError:
                config.max_records_per_run = limit
            client = _client_for_config(vision_runner, config)
            transport = vision_runner.SSHTransport(config.vps_ssh_target)
            stats = vision_runner.run_once(config, client, transport, vision_runner.CAT_VISION_DESCRIPTIONS)
        return pipeline_watcher._counts_from_cat_stats(stats)
    finally:
        try:
            sys.path.remove(str(repo_resolved))
        except ValueError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded cat vision child pass.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    counts = run_child(repo_path=args.repo, limit=max(1, args.limit))
    print(json.dumps(counts, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
