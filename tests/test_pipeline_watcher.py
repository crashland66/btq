from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest

from event_pipeline import couchdb_config
from field_capture import audio_semantics
from field_capture import pipeline_watcher
from processing_core.artifacts import write_json_object


def write_intent(runtime_root: Path, asset_id: str, *, filename: str | None = None) -> Path:
    path = runtime_root / "reviews" / "photo_vision_retries" / f"{filename or asset_id}.json"
    write_json_object(path, {"photo_asset_id": asset_id, "requested_at": "2026-05-12T12:00:00Z", "actor": "localhost"})
    return path


def cycle_kwargs(runtime_root: Path, calls: list[dict[str, object]], *, run_vision: bool = True, vision_limit: int = 1, raise_for: str = "") -> dict[str, object]:
    def photo_vision_func(*_args: object, **kwargs: object) -> dict[str, int]:
        calls.append(kwargs)
        if kwargs.get("photo_asset_id") == raise_for:
            raise RuntimeError("vision retry failed")
        return {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    return {
        "runtime_root": runtime_root,
        "transcribe_limit": 0,
        "vision_limit": vision_limit,
        "vision_model": "vision-test",
        "ollama_url": "http://localhost:11434",
        "vision_timeout_seconds": 1.0,
        "transcriber_factory": lambda _root, _logger: (lambda _path: "unused"),
        "logger": logging.getLogger(f"test.pipeline.{id(calls)}"),
        "photo_vision_func": photo_vision_func,
        "vision_describe_factory": lambda *_args: object(),
        "run_transcribe": False,
        "run_semantics": False,
        "run_vision": run_vision,
        "run_candidates": False,
    }


class FakeWarmMlxBackend:
    model = "fake-warm-model"
    engine_name = "mlx:fake-warm-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def describe(self, image_path: Path, prompt: str) -> dict[str, object]:
        self.calls.append({"image_path": image_path, "prompt": prompt})
        return {
            "detected_cats": ["Converse"],
            "confidence": "high",
            "image_context": "Converse patrols the test fixture with excellent fake-model posture.",
        }


class FakeWarmMlxDescribe:
    model = "fake-warm-model"
    engine_name = "mlx:fake-warm-model"
    provider = "mlx"

    def __init__(self) -> None:
        self._client = FakeWarmMlxBackend()


def clear_cat_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in sys.modules if name == "cat_pipeline" or name.startswith("cat_pipeline.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def write_fake_cat_repo(
    root: Path,
    *,
    marker: str = "fake",
    sleep_seconds: float = 0.0,
    assert_tokenizers_parallelism: bool = False,
    noisy_stdout: bool = False,
) -> Path:
    repo = root / "fake_cat_repo"
    package = repo / "cat_pipeline"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "vision_runner.py").write_text(
        """
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time

if __NOISY_STDOUT__:
    print("cat import stdout noise")

CAT_VISION_DESCRIPTIONS = {"Converse": "Large orange tabby with white paws."}
MARKER = "__MARKER__"
SLEEP_SECONDS = __SLEEP_SECONDS__
ASSERT_TOKENIZERS_PARALLELISM = __ASSERT_TOKENIZERS__
NOISY_STDOUT = __NOISY_STDOUT__
RUNS = []


@dataclass(frozen=True)
class VisionRunnerConfig:
    vps_ssh_target: str = "fake-vps"
    max_records_per_run: int = 20
    vision_backend: str = "mlx"
    mlx_model: str = "fake-cat-mlx-model"
    mlx_max_tokens: int = 32
    ollama_model: str = "fake-cat-ollama-model"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_timeout_seconds: float = 1.0

    @classmethod
    def from_env(cls):
        return cls()


@dataclass(frozen=True)
class VisionResult:
    record_id: str
    detected_cats: list[str]
    confidence: str
    image_context: str
    raw_response: str


@dataclass(frozen=True)
class VisionRunStats:
    processed: int
    identified: int
    skipped: int
    errored: int


class SSHTransport:
    def __init__(self, ssh_target: str) -> None:
        self.ssh_target = ssh_target


class MlxVisionClient:
    def __init__(self, model: str, max_tokens: int = 32) -> None:
        if NOISY_STDOUT:
            print("cat client stdout noise")
        if ASSERT_TOKENIZERS_PARALLELISM and os.environ.get("TOKENIZERS_PARALLELISM") != "false":
            raise RuntimeError("TOKENIZERS_PARALLELISM was not disabled")
        self.model = model
        self.max_tokens = max_tokens
        self.engine_name = f"mlx:{model}"

    def identify(self, image_path: Path, cat_descriptions):
        return VisionResult(
            record_id=Path(image_path).stem,
            detected_cats=["Converse"],
            confidence="high",
            image_context="Converse patrols the test fixture with excellent fake-model posture.",
            raw_response="{}",
        )


class OllamaVisionClient(MlxVisionClient):
    def __init__(self, model: str, ollama_url: str, timeout_seconds: float) -> None:
        super().__init__(model)
        self.ollama_url = ollama_url
        self.timeout_seconds = timeout_seconds
        self.engine_name = f"ollama:{model}"


def _build_prompt(cat_descriptions):
    return MARKER + " cat prompt: " + ",".join(cat_descriptions)


def _parse_vision_result(parsed, raw, record_id):
    return VisionResult(
        record_id=record_id,
        detected_cats=list(parsed["detected_cats"]),
        confidence=str(parsed["confidence"]),
        image_context=str(parsed["image_context"]),
        raw_response=raw,
    )


def run_once(config, client, transport, cat_descriptions):
    if NOISY_STDOUT:
        print("cat run stdout noise")
    if SLEEP_SECONDS:
        time.sleep(SLEEP_SECONDS)
    result = client.identify(Path("/tmp/fake-cat-record.jpg"), cat_descriptions)
    RUNS.append(
        {
            "client": client,
            "config_limit": config.max_records_per_run,
            "engine_name": client.engine_name,
            "model": client.model,
            "result": result,
            "transport_target": transport.ssh_target,
            "marker": MARKER,
        }
    )
    return VisionRunStats(processed=config.max_records_per_run, identified=1, skipped=0, errored=0)
""".replace("__MARKER__", marker)
        .replace("__SLEEP_SECONDS__", repr(sleep_seconds))
        .replace("__ASSERT_TOKENIZERS__", repr(assert_tokenizers_parallelism))
        .replace("__NOISY_STDOUT__", repr(noisy_stdout)),
        encoding="utf-8",
    )
    return repo


def test_run_cycle_consumes_and_clears_retry_intent_files(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    write_intent(runtime_root, "fcp_retry_one")

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, vision_limit=0))

    assert cycle["ok"] is True
    assert len(calls) == 1
    assert calls[0]["photo_asset_id"] == "fcp_retry_one"
    assert calls[0]["replace_failed"] is True
    assert calls[0]["limit"] == 1
    assert not list((runtime_root / "reviews" / "photo_vision_retries").glob("*.json"))


def test_cat_vision_stage_skipped_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BTQ_CAT_VISION_ENABLED", raising=False)
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, vision_limit=0, run_vision=True), run_issue_routing=False)

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert cat_step["status"] == "skipped"
    assert cat_step["error"] == "disabled"


def test_cat_vision_stage_does_not_import_cat_repo_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    monkeypatch.delenv("BTQ_CAT_VISION_ENABLED", raising=False)
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(tmp_path / "does-not-exist"))
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, vision_limit=0, run_vision=True), run_issue_routing=False)

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert cat_step["status"] == "skipped"
    assert "cat_pipeline" not in sys.modules
    assert "cat_pipeline.vision_runner" not in sys.modules


def test_cat_vision_stage_runs_bounded_subprocess_when_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    repo = write_fake_cat_repo(tmp_path)
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(repo))
    monkeypatch.setenv("BTQ_CAT_VISION_LIMIT", "2")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    factory_calls: list[tuple[str, str, float]] = []
    shared_describe = FakeWarmMlxDescribe()

    def fake_factory(model: str, ollama_url: str, timeout_seconds: float) -> FakeWarmMlxDescribe:
        factory_calls.append((model, ollama_url, timeout_seconds))
        return shared_describe

    def fake_photo_vision_func(*_args: object, **kwargs: object) -> dict[str, int]:
        calls.append(kwargs)
        assert kwargs["limit"] == 1
        assert _args[3] is shared_describe
        return {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    monkeypatch.setattr(pipeline_watcher, "mlx_vision_describe_factory", fake_factory)

    kwargs = cycle_kwargs(runtime_root, calls, vision_limit=1, run_vision=True)
    kwargs.update({"vision_backend": "mlx", "photo_vision_func": fake_photo_vision_func, "run_issue_routing": False})
    cycle = pipeline_watcher.run_cycle(**kwargs)

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert len(factory_calls) == 1
    assert cat_step["status"] == "completed"
    assert cat_step["counts"] == {"processed": 2, "identified": 1, "skipped": 0, "errored": 0}
    assert "cat_pipeline.vision_runner" not in sys.modules


def test_cat_vision_child_stdout_noise_does_not_break_parent_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    repo = write_fake_cat_repo(tmp_path, noisy_stdout=True)
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(repo))
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"

    cycle = pipeline_watcher.run_cycle(
        **cycle_kwargs(runtime_root, calls=[], vision_limit=0, run_vision=True),
        vision_backend="mlx",
        run_issue_routing=False,
    )

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert cat_step["status"] == "completed"
    assert cat_step["counts"] == {"processed": 1, "identified": 1, "skipped": 0, "errored": 0}


def test_cat_vision_stage_disables_tokenizer_parallelism_before_model_load(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    repo = write_fake_cat_repo(tmp_path, assert_tokenizers_parallelism=True)
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(repo))
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []

    cycle = pipeline_watcher.run_cycle(
        **cycle_kwargs(runtime_root, calls, vision_limit=0, run_vision=True),
        vision_backend="mlx",
        run_issue_routing=False,
    )

    assert cycle["ok"] is True
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"


def test_cat_vision_stage_can_run_when_field_photo_limit_is_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    repo = write_fake_cat_repo(tmp_path)
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(repo))
    monkeypatch.setenv("BTQ_CAT_VISION_LIMIT", "1")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    factory_calls: list[tuple[str, str, float]] = []
    shared_describe = FakeWarmMlxDescribe()

    def fake_factory(model: str, ollama_url: str, timeout_seconds: float) -> FakeWarmMlxDescribe:
        factory_calls.append((model, ollama_url, timeout_seconds))
        return shared_describe

    monkeypatch.setattr(pipeline_watcher, "mlx_vision_describe_factory", fake_factory)

    cycle = pipeline_watcher.run_cycle(
        **cycle_kwargs(runtime_root, calls, vision_limit=0, run_vision=True),
        vision_backend="mlx",
        run_issue_routing=False,
    )

    photo_step = next(step for step in cycle["steps"] if step["step"] == "describe_field_photos")
    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert calls == []
    assert factory_calls == []
    assert photo_step["status"] == "skipped"
    assert photo_step["error"] == "limit=0"
    assert cat_step["status"] == "completed"
    assert cat_step["counts"]["processed"] == 1


def test_cat_vision_native_hang_cannot_block_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cat_modules(monkeypatch)
    repo = write_fake_cat_repo(tmp_path, sleep_seconds=5.0)
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(repo))
    monkeypatch.setenv("BTQ_CAT_VISION_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []

    cycle = pipeline_watcher.run_cycle(
        **cycle_kwargs(runtime_root, calls, vision_limit=0, run_vision=True),
        vision_backend="mlx",
        run_issue_routing=False,
    )

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is False
    assert cat_step["status"] == "failed"
    assert cat_step["error"] == "cat vision subprocess timed out after 0.2s"


def test_cat_vision_does_not_overlap_field_mlx_work(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(tmp_path))
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"
    field_active = False
    observed: list[bool] = []

    def fake_photo_vision_func(*_args: object, **_kwargs: object) -> dict[str, int]:
        nonlocal field_active
        field_active = True
        time.sleep(0.01)
        field_active = False
        return {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    def fake_cat_stage(**_kwargs: object) -> dict[str, int]:
        observed.append(field_active)
        return {"processed": 1, "identified": 1, "skipped": 0, "errored": 0}

    monkeypatch.setattr(pipeline_watcher, "mlx_vision_describe_factory", lambda *_args: object())
    monkeypatch.setattr(pipeline_watcher, "process_cat_vision_bounded_subprocess", fake_cat_stage)
    kwargs = cycle_kwargs(runtime_root, calls=[], vision_limit=1, run_vision=True)
    kwargs.update({"vision_backend": "mlx", "photo_vision_func": fake_photo_vision_func, "run_issue_routing": False})

    cycle = pipeline_watcher.run_cycle(**kwargs)

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert cat_step["status"] == "completed"
    assert observed == [False]


def test_cat_vision_runs_after_field_photo_retry_intents(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(tmp_path))
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    monkeypatch.setattr(pipeline_watcher, "_MLX_CLIENT_CACHE", {})
    monkeypatch.setattr(pipeline_watcher, "mlx_vision_describe_factory", lambda *_args: object())
    runtime_root = tmp_path / "runtime"
    write_intent(runtime_root, "fcp_retry_before_cat")
    order: list[str] = []

    def fake_photo_vision_func(*_args: object, **_kwargs: object) -> dict[str, int]:
        order.append("retry")
        return {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    def fake_cat_stage(**_kwargs: object) -> dict[str, int]:
        order.append("cat")
        return {"processed": 1, "identified": 1, "skipped": 0, "errored": 0}

    kwargs = cycle_kwargs(runtime_root, calls=[], vision_limit=0, run_vision=True)
    kwargs.update(
        {
            "vision_backend": "mlx",
            "photo_vision_func": fake_photo_vision_func,
            "run_issue_routing": False,
        }
    )
    monkeypatch.setattr(pipeline_watcher, "process_cat_vision_bounded_subprocess", fake_cat_stage)

    cycle = pipeline_watcher.run_cycle(**kwargs)

    step_names = [step["step"] for step in cycle["steps"]]
    assert cycle["ok"] is True
    assert order == ["retry", "cat"]
    assert step_names.index("photo_vision_retry_intents") < step_names.index("process_cat_vision")


def test_cat_vision_skips_when_field_mlx_is_not_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BTQ_CAT_VISION_ENABLED", "1")
    monkeypatch.setenv("BTQ_CAT_VISION_REPO", str(tmp_path))
    monkeypatch.delenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", raising=False)
    runtime_root = tmp_path / "runtime"

    cycle = pipeline_watcher.run_cycle(
        **cycle_kwargs(runtime_root, calls=[], vision_limit=0, run_vision=True),
        vision_backend="mlx",
        run_issue_routing=False,
    )

    cat_step = next(step for step in cycle["steps"] if step["step"] == "process_cat_vision")
    assert cycle["ok"] is True
    assert cat_step == {
        "step": "process_cat_vision",
        "status": "skipped",
        "counts": {},
        "error": "requires BTQ_FIELD_CAPTURE_VISION_ISOLATED=1",
    }


def test_run_cycle_retry_intent_skipped_when_vision_disabled(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    intent = write_intent(runtime_root, "fcp_disabled")

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, run_vision=False))

    assert cycle["ok"] is True
    assert calls == []
    assert intent.exists()


def test_run_cycle_retry_intent_invalid_id_logged_and_skipped(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    write_intent(runtime_root, "BAD-ID", filename="bad")

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, vision_limit=0))

    retry_step = next(step for step in cycle["steps"] if step["step"] == "photo_vision_retry_intents")
    assert calls == []
    assert retry_step["counts"]["skipped"] == 1
    assert not list((runtime_root / "reviews" / "photo_vision_retries").glob("*.json"))


def test_run_cycle_retry_intent_does_not_count_against_vision_limit(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    for index in range(3):
        write_intent(runtime_root, f"fcp_retry_{index}")

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, vision_limit=0))

    assert cycle["ok"] is True
    assert [call["photo_asset_id"] for call in calls] == ["fcp_retry_0", "fcp_retry_1", "fcp_retry_2"]
    assert all(call["limit"] == 1 for call in calls)


def test_run_cycle_auto_retry_synthesis_runs_with_isolated_mlx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_VISION_ISOLATED", "1")
    runtime_root = tmp_path / "runtime"
    sidecar_path = runtime_root / "field_capture" / "photo_vision" / "fcp_retry_isolated.json"
    write_json_object(
        sidecar_path,
        {
            "photo_asset_id": "fcp_retry_isolated",
            "status": "failed",
            "auto_retry_attempts": 0,
            "error": {"type": "TimeoutError", "message": "timed out", "can_retry": True},
        },
    )
    calls: list[dict[str, object]] = []

    def fake_isolated_process(*_args: object, **kwargs: object) -> dict[str, int]:
        calls.append(kwargs)
        return {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}

    monkeypatch.setattr(pipeline_watcher, "isolated_mlx_vision_describe_factory", lambda *_args: object())
    monkeypatch.setattr(pipeline_watcher, "process_mlx_photo_assets_isolated", fake_isolated_process)
    kwargs = cycle_kwargs(runtime_root, calls=[], vision_limit=0, run_vision=True)
    kwargs.update(
        {
            "vision_backend": "mlx",
            "photo_vision_func": pipeline_watcher.photo_vision.process_photo_assets,
            "run_issue_routing": False,
            "vision_auto_retry_per_cycle": 1,
            "vision_auto_retry_cooldown_seconds": 0,
        }
    )

    cycle = pipeline_watcher.run_cycle(**kwargs)

    auto_step = next(step for step in cycle["steps"] if step["step"] == "photo_vision_auto_retry_synthesis")
    retry_step = next(step for step in cycle["steps"] if step["step"] == "photo_vision_retry_intents")
    assert cycle["ok"] is True
    assert auto_step["counts"]["queued"] == 1
    assert retry_step["counts"] == {"completed": 1, "failed": 0, "skipped": 0}
    assert calls[0]["photo_asset_id"] == "fcp_retry_isolated"
    assert calls[0]["replace_failed"] is True


def test_run_cycle_retry_intent_file_deleted_on_exception(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []
    write_intent(runtime_root, "fcp_raises")

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls, raise_for="fcp_raises"))

    retry_step = next(step for step in cycle["steps"] if step["step"] == "photo_vision_retry_intents")
    assert cycle["ok"] is True
    assert retry_step["counts"]["failed"] == 1
    assert not list((runtime_root / "reviews" / "photo_vision_retries").glob("*.json"))


def test_run_cycle_runs_issue_routing_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    calls: list[dict[str, object]] = []

    def fake_route_field_reported_issues(
        intake_dir: Path,
        *,
        runtime_root: Path,
        logger: logging.Logger,
        limit: int | None = None,
    ) -> dict[str, int]:
        calls.append(
            {
                "intake_dir": intake_dir,
                "runtime_root": runtime_root,
                "logger": logger,
                "limit": limit,
            }
        )
        return {"discovered": 1, "routed": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr(
        "field_capture.issue_routing.route_field_reported_issues",
        fake_route_field_reported_issues,
    )

    cycle = pipeline_watcher.run_cycle(**cycle_kwargs(runtime_root, calls=[], vision_limit=0), issue_routing_limit=7)

    assert len(calls) == 1
    assert calls[0]["intake_dir"] == runtime_root / "field_capture" / "intake"
    assert calls[0]["runtime_root"] == runtime_root.resolve(strict=False)
    assert calls[0]["limit"] == 7
    step = next(step for step in cycle["steps"] if step["step"] == "route_field_reported_issues")
    assert step["status"] == "completed"
    assert step["counts"] == {"discovered": 1, "routed": 1, "skipped": 0, "failed": 0}


def test_run_cycle_uses_injectable_semantic_engine_factory(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    transcript_dir = runtime_root / "field_capture" / "audio_transcripts"
    write_json_object(
        transcript_dir / "fca_semantic.json",
        {
            "type": "field_audio_transcript",
            "site_id": "7050",
            "upload_id": "cap-audio",
            "area": "Restrooms",
            "phase": "issue",
            "audio_asset_id": "fca_semantic",
            "status": "complete",
            "raw_text": "Bathroom two has water under the sink.",
        },
    )

    class FakeSemanticEngine:
        engine_name = "fake-semantic"

        def __call__(self, transcript: audio_semantics.FieldAudioTranscript) -> audio_semantics.SemanticResult:
            return audio_semantics.SemanticResult(
                cleaned_internal_note=f"fake engine saw {transcript.audio_asset_id}",
                client_safe_note="Client-safe note.",
                operational_summary="Operational summary.",
                issue_detected=True,
                issue_type="water",
                urgency="normal",
                suggested_tags=["field-audio/water"],
                action_candidates=["Inspect the sink area."],
            )

    kwargs = cycle_kwargs(runtime_root, calls=[], vision_limit=0)
    kwargs.update(
        {
            "run_semantics": True,
            "run_issue_routing": False,
            "semantic_engine_factory": lambda: FakeSemanticEngine(),
        }
    )

    cycle = pipeline_watcher.run_cycle(**kwargs)

    step = next(step for step in cycle["steps"] if step["step"] == "process_field_audio_semantics")
    assert step["status"] == "completed"
    assert step["counts"] == {"discovered": 1, "skipped": 0, "completed": 1, "failed": 0}
    payload = json.loads((runtime_root / "field_capture" / "audio_semantics" / "fca_semantic.json").read_text(encoding="utf-8"))
    assert payload["semantic_engine"] == "fake-semantic"
    assert payload["cleaned_internal_note"] == "fake engine saw fca_semantic"


def test_cached_semantic_engine_factory_returns_same_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_watcher, "_SEMANTIC_ENGINE_CACHE", {})
    engine1 = pipeline_watcher.cached_semantic_engine_factory()
    engine2 = pipeline_watcher.cached_semantic_engine_factory()
    assert engine1 is engine2


def test_pipeline_watcher_exits_on_bad_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class BadConfig:
        def assert_can_authenticate(self) -> None:
            raise couchdb_config.CouchDBConfigError("bad credentials")

    cycle_started = False

    def fail_if_cycle_starts(**_kwargs: object) -> dict[str, object]:
        nonlocal cycle_started
        cycle_started = True
        raise AssertionError("run_cycle should not start")

    monkeypatch.setattr(pipeline_watcher.couchdb_config, "from_env", lambda: BadConfig())
    monkeypatch.setattr(pipeline_watcher, "configure_logger", lambda _path: logging.getLogger("test.pipeline.bad_auth"))
    monkeypatch.setattr(pipeline_watcher, "run_cycle", fail_if_cycle_starts)

    with pytest.raises(SystemExit) as excinfo:
        pipeline_watcher.run(["--once", "--runtime-root", str(tmp_path)])

    assert excinfo.value.code == 2
    assert cycle_started is False


def test_text_semantics_processes_note_with_photo_no_audio(tmp_path: Path) -> None:
    """A photo+note capture (no audio) must still have its typed note extracted —
    the common 'take a photo, type what's needed' case. Regression for the gate
    that skipped note processing whenever photos were present."""
    from field_capture import pipeline_watcher, audio_transcription
    from field_capture import action_candidates as fc

    intake_dir = audio_transcription.default_intake_dir(tmp_path)
    sem_dir = fc.default_text_semantic_dir(tmp_path)
    intake_dir.mkdir(parents=True, exist_ok=True)
    cap = "cap-photo-note-0001"
    intake = {
        "metadata": {"capture_id": cap, "site_id": "SANDBOX", "person_id": "sandbox-user"},
        "payload": {
            "capture_id": cap,
            "site_id": "SANDBOX",
            "captured_at": "2026-06-10T10:07:18Z",
            "qc_category": "Supply Levels",
            "note": "A new vacuum and heavy duty lint brushes are the required supplies.",
            "photos": [{"upload_id": "2026-06-10/x/img.jpg"}],
            "audio": [],
        },
    }
    (intake_dir / f"{cap}.json").write_text(json.dumps(intake), encoding="utf-8")

    counts = pipeline_watcher.process_note_only_text_semantics(intake_dir, sem_dir, runtime_root=tmp_path)
    assert counts["discovered"] == 1, counts
    assert counts["completed"] == 1, counts
    assert (sem_dir / f"{cap}.json").exists()


def test_text_semantics_skips_note_with_audio(tmp_path: Path) -> None:
    """A capture WITH audio routes its semantics through the audio path, so the
    text-note stage skips it (no double-processing)."""
    from field_capture import pipeline_watcher, audio_transcription
    from field_capture import action_candidates as fc

    intake_dir = audio_transcription.default_intake_dir(tmp_path)
    sem_dir = fc.default_text_semantic_dir(tmp_path)
    intake_dir.mkdir(parents=True, exist_ok=True)
    cap = "cap-note-and-audio-0001"
    intake = {
        "metadata": {"capture_id": cap, "site_id": "SANDBOX"},
        "payload": {"capture_id": cap, "site_id": "SANDBOX", "note": "need vacuum", "photos": [], "audio": [{"upload_id": "a.webm"}]},
    }
    (intake_dir / f"{cap}.json").write_text(json.dumps(intake), encoding="utf-8")
    counts = pipeline_watcher.process_note_only_text_semantics(intake_dir, sem_dir, runtime_root=tmp_path)
    assert counts["discovered"] == 0, counts
