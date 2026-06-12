"""Prompt-343 verification: isolated (load-exit subprocess) MLX semantic stage.

Authored by the INDEPENDENT verification agent (NOT the implementer).

Prompt 343 moves the MLX semantic extraction into a bounded load-exit child
process so the semantic MLX model never co-resides with the vision MLX model
(they were OOMing the Pro GPU). The new pieces under test:

  * ``field_capture/semantic_child.py`` -- ``run(["--runtime-root", root,
    "--json"])`` builds the semantic engine ONCE, runs the audio + note-only
    text semantic passes with that ONE engine instance, prints
    ``{"audio": ..., "text": ...}`` and returns 0.
  * ``pipeline_watcher.semantic_mlx_isolated()`` -- True iff
    ``BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE == "local_model"`` AND
    ``BTQ_SEMANTIC_PROVIDER`` (default "mlx") == "mlx".
  * ``pipeline_watcher.isolated_semantic_subprocess_timeout_seconds()`` -- env
    override ``BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS``.
  * ``pipeline_watcher.run_cycle`` semantic branch -- MLX -> spawn the child via
    ``subprocess.run`` (fail-soft on TimeoutExpired / Exception, watcher
    continues); rules -> the unchanged in-process audio+text path.

ALL exercised with the RULE engine (the hermetic ``local_rule`` default) -- NO
model is ever loaded. The live no-OOM + real-MLX gate is run by the planner.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from event_pipeline.couchdb_capture_adapter import import_couchdb_capture
from field_capture import action_candidates as fc
from field_capture import audio_semantics
from field_capture import audio_transcription
from field_capture import pipeline_watcher
from field_capture import semantic_child
from field_capture import text_semantics
from processing_core import capture_semantics


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _FakeRegistry:
    def resolve_canonical(self, site_id: str) -> str:  # noqa: D401
        return "Continental Metalworks"


def _no_media_runner(args: list[str]):
    raise AssertionError(f"runner must not be called for note-only capture: {args!r}")


def _note_only_doc(note: str, capture_id: str = "cap-text-343-0001") -> dict[str, Any]:
    return {
        "_id": capture_id,
        "type": "field_capture",
        "capture_id": capture_id,
        "site_id": "7060",
        "person_id": "per_unified01",
        "person_name": "Person One",
        "captured_at": "2026-06-08T14:30:00Z",
        "qc_category": "report_an_issue",
        "note": note,
        "photos": [],
        "audio": [],
        "processing_state": "claimed",
    }


def _seed_note_only_intake(runtime_root: Path, note: str, capture_id: str) -> None:
    """Import a note-only CouchDB doc into ``runtime_root`` as an intake job."""
    # Other tests load throwaway brand files into a module-level cache; force a
    # clean reload so this is independent of suite ordering.
    from event_pipeline import site_registry_data as _srd

    _srd._brand_cache = None
    _srd.load_brand_keywords("supply", force_reload=True)

    result = import_couchdb_capture(
        doc=_note_only_doc(note, capture_id=capture_id),
        runtime_root=runtime_root,
        remote_host="vps.example",
        registry=_FakeRegistry(),
        runner=_no_media_runner,
    )
    assert result["ok"] is True, result


_NOTE = (
    "The men's restroom on the second floor is out of paper towels and the "
    "trash is overflowing. Please send someone to restock and empty it today."
)


@pytest.fixture(autouse=True)
def _hermetic_rule_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the hermetic rule engine so no model is ever loaded."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")
    monkeypatch.delenv("BTQ_SEMANTIC_PROVIDER", raising=False)
    monkeypatch.delenv("BTQ_SEMANTIC_MODEL", raising=False)
    # Defensive: clear the watcher's process-level semantic-engine cache so any
    # spy on build_semantic_engine sees a fresh build.
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()


# --------------------------------------------------------------------------- #
# 1. Child end-to-end (the real proof) -- in-process, engine built ONCE
# --------------------------------------------------------------------------- #


def test_semantic_child_end_to_end_writes_artifact_engine_built_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """semantic_child.run(...) builds the engine ONCE, writes the text summary,
    prints ``{"audio": ..., "text": ...}`` and returns 0 -- with NO model load.
    """
    capture_id = "cap-text-343-e2e"
    _seed_note_only_intake(tmp_path, _NOTE, capture_id)

    # Spy build_semantic_engine: count calls + record the instance handed out.
    # The child imports it as capture_semantics.build_semantic_engine, so we
    # patch it there (the symbol the child actually calls).
    real_build = capture_semantics.build_semantic_engine
    built: list[object] = []

    def _spy_build():
        engine = real_build()
        built.append(engine)
        return engine

    monkeypatch.setattr(capture_semantics, "build_semantic_engine", _spy_build)

    # Capture the engine instance each downstream pass receives, to prove both
    # the audio + text passes used the SAME single engine.
    real_process_semantics = audio_semantics.process_semantics
    real_process_text = pipeline_watcher.process_note_only_text_semantics
    audio_engine: list[object] = []
    text_engine: list[object] = []

    def _wrap_audio(transcript_dir, semantic_dir, engine, **kw):
        audio_engine.append(engine)
        return real_process_semantics(transcript_dir, semantic_dir, engine, **kw)

    def _wrap_text(intake_dir, semantic_dir, **kw):
        text_engine.append(kw.get("engine"))
        return real_process_text(intake_dir, semantic_dir, **kw)

    monkeypatch.setattr(audio_semantics, "process_semantics", _wrap_audio)
    # semantic_child references process_note_only_text_semantics imported into
    # its own module namespace -- patch it there.
    monkeypatch.setattr(semantic_child, "process_note_only_text_semantics", _wrap_text)

    rc = semantic_child.run(["--runtime-root", str(tmp_path), "--json"])
    assert rc == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload) == {"audio", "text"}, payload
    assert payload["text"]["discovered"] == 1, payload
    assert payload["text"]["completed"] == 1, payload
    assert payload["text"]["failed"] == 0, payload

    # The engine was built exactly ONCE.
    assert len(built) == 1, f"engine should be built once, got {len(built)}"
    # Both the audio + text passes used that SAME single engine instance.
    assert audio_engine and text_engine
    assert audio_engine[0] is built[0]
    assert text_engine[0] is built[0]
    # It is the hermetic RULE engine -- no model.
    assert isinstance(built[0], capture_semantics.RuleCaptureEngine)

    # The field_text_semantic_summary artifact was written for the note capture.
    sem_path = fc.default_text_semantic_dir(tmp_path) / f"{capture_id}.json"
    assert sem_path.exists(), f"missing text semantic summary: {sem_path}"
    artifact = json.loads(sem_path.read_text(encoding="utf-8"))
    assert artifact["type"] == text_semantics.ARTIFACT_TYPE == "field_text_semantic_summary"
    assert artifact["status"] == "complete"
    assert "extracted_actions" in artifact, artifact
    assert artifact["source_text"] == _NOTE


def test_semantic_child_subprocess_real_exit_zero(tmp_path: Path) -> None:
    """As an actual child process (the real load-exit boundary): exits 0 and
    prints the ``{"audio": ..., "text": ...}`` JSON. Hermetic rule engine."""
    capture_id = "cap-text-343-subproc"
    _seed_note_only_intake(tmp_path, _NOTE, capture_id)

    import os

    env = dict(os.environ)
    env["BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE"] = "local_rule"
    env.pop("BTQ_SEMANTIC_PROVIDER", None)
    env.pop("BTQ_SEMANTIC_MODEL", None)

    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "field_capture.semantic_child",
            "--runtime-root",
            str(tmp_path),
            "--json",
        ],
        cwd=Path(pipeline_watcher.__file__).resolve().parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload) == {"audio", "text"}, payload
    assert payload["text"]["completed"] == 1, payload

    sem_path = fc.default_text_semantic_dir(tmp_path) / f"{capture_id}.json"
    assert sem_path.exists(), f"child subprocess did not write artifact: {sem_path}"


# --------------------------------------------------------------------------- #
# Shared run_cycle driver helpers
# --------------------------------------------------------------------------- #


def _run_semantics_only_cycle(runtime_root: Path):
    """Drive run_cycle with ONLY the semantic stage enabled."""
    logger = pipeline_watcher.configure_logger(
        pipeline_watcher.default_log_path(runtime_root)
    )

    def _never_transcriber(rt, lg):  # pragma: no cover - run_transcribe is False
        raise AssertionError("transcriber factory must not be called")

    return pipeline_watcher.run_cycle(
        runtime_root=runtime_root,
        transcribe_limit=0,
        vision_limit=0,
        vision_model="unused",
        ollama_url="http://127.0.0.1:11434",
        vision_timeout_seconds=1.0,
        transcriber_factory=_never_transcriber,
        logger=logger,
        run_transcribe=False,
        run_semantics=True,
        run_vision=False,
        run_candidates=False,
        run_issue_routing=False,
    )


def _step(cycle: dict, name: str) -> dict | None:
    for s in cycle["steps"]:
        if s["step"] == name:
            return s
    return None


# --------------------------------------------------------------------------- #
# 2. Watcher branch -- MLX spawns the load-exit subprocess
# --------------------------------------------------------------------------- #


def test_run_cycle_mlx_spawns_subprocess_no_in_process_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_model + mlx -> run_cycle spawns field_capture.semantic_child via
    subprocess.run, records a process_semantics_isolated step, and does NOT
    build a semantic engine in-process."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()

    # Spy: the watcher path must NOT build an MLX engine in-process.
    engine_builds: list[Any] = []

    def _no_inproc_build(*a, **k):
        engine_builds.append(True)
        raise AssertionError("watcher must NOT build a semantic engine in-process when isolated")

    monkeypatch.setattr(capture_semantics, "build_semantic_engine", _no_inproc_build)
    monkeypatch.setattr(
        pipeline_watcher, "cached_semantic_engine_factory", _no_inproc_build
    )

    captured_cmds: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0
        stdout = json.dumps(
            {
                "audio": {"discovered": 0, "completed": 0, "failed": 0, "skipped": 0},
                "text": {"discovered": 1, "completed": 1, "failed": 0, "skipped": 0},
            }
        )
        stderr = ""

    def _fake_run(cmd, *a, **k):
        captured_cmds.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(pipeline_watcher.subprocess, "run", _fake_run)

    cycle = _run_semantics_only_cycle(tmp_path)

    assert engine_builds == [], "no in-process semantic engine should be built"
    assert len(captured_cmds) == 1, captured_cmds
    cmd = captured_cmds[0]
    assert "-m" in cmd
    assert "field_capture.semantic_child" in cmd
    assert "--runtime-root" in cmd

    iso = _step(cycle, "process_semantics_isolated")
    assert iso is not None, cycle
    assert iso["status"] == "completed", iso
    assert iso["counts"]["text"]["completed"] == 1, iso
    # The in-process steps must NOT be present.
    assert _step(cycle, "process_field_audio_semantics") is None, cycle
    assert _step(cycle, "process_field_text_semantics") is None, cycle
    assert cycle["ok"] is True, cycle


# --------------------------------------------------------------------------- #
# 3. Watcher branch -- rules run in-process, NO subprocess
# --------------------------------------------------------------------------- #


def test_run_cycle_rules_run_in_process_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """local_rule -> run_cycle runs the in-process audio+text path and does NOT
    spawn the semantic_child subprocess."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")
    monkeypatch.delenv("BTQ_SEMANTIC_PROVIDER", raising=False)
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()

    _seed_note_only_intake(tmp_path, _NOTE, "cap-text-343-rules")

    spawned: list[list[str]] = []
    real_run = subprocess.run

    def _guard_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and any(
            "field_capture.semantic_child" == str(part) for part in cmd
        ):
            spawned.append(list(cmd))
            raise AssertionError("rules path must NOT spawn the semantic_child subprocess")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(pipeline_watcher.subprocess, "run", _guard_run)

    cycle = _run_semantics_only_cycle(tmp_path)

    assert spawned == [], "no semantic_child subprocess should be spawned for rules"
    # The in-process audio step is always recorded; the text step is recorded
    # because the seeded note produced a discovered/completed count.
    assert _step(cycle, "process_field_audio_semantics") is not None, cycle
    assert _step(cycle, "process_semantics_isolated") is None, cycle
    text_step = _step(cycle, "process_field_text_semantics")
    assert text_step is not None and text_step["status"] == "completed", cycle

    # And the real text artifact landed.
    sem_path = fc.default_text_semantic_dir(tmp_path) / "cap-text-343-rules.json"
    assert sem_path.exists(), sem_path


# --------------------------------------------------------------------------- #
# 4. Fail-soft -- TimeoutExpired and non-zero/Exception don't crash the watcher
# --------------------------------------------------------------------------- #


def test_run_cycle_mlx_subprocess_timeout_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.run raising TimeoutExpired -> the isolated step is failed,
    cycle['ok'] is False, and run_cycle does NOT raise."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()

    def _timeout_run(cmd, *a, **k):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=k.get("timeout", 1.0))

    monkeypatch.setattr(pipeline_watcher.subprocess, "run", _timeout_run)

    cycle = _run_semantics_only_cycle(tmp_path)  # must NOT raise

    iso = _step(cycle, "process_semantics_isolated")
    assert iso is not None, cycle
    assert iso["status"] == "failed", iso
    assert iso["error"], iso
    assert cycle["ok"] is False, cycle


def test_run_cycle_mlx_subprocess_nonzero_exit_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """subprocess.run returning a non-zero exit (-> RuntimeError inside
    process_semantics_isolated) is caught: step failed, cycle not ok, no raise."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "child blew up loading the model"

    monkeypatch.setattr(
        pipeline_watcher.subprocess, "run", lambda cmd, *a, **k: _Failed()
    )

    cycle = _run_semantics_only_cycle(tmp_path)  # must NOT raise

    iso = _step(cycle, "process_semantics_isolated")
    assert iso is not None and iso["status"] == "failed", cycle
    assert "child blew up" in iso["error"], iso
    assert cycle["ok"] is False, cycle


def test_run_cycle_mlx_subprocess_generic_exception_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generic Exception from subprocess.run is caught fail-soft."""
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    pipeline_watcher._SEMANTIC_ENGINE_CACHE.clear()

    def _boom(cmd, *a, **k):
        raise OSError("could not fork child")

    monkeypatch.setattr(pipeline_watcher.subprocess, "run", _boom)

    cycle = _run_semantics_only_cycle(tmp_path)  # must NOT raise

    iso = _step(cycle, "process_semantics_isolated")
    assert iso is not None and iso["status"] == "failed", cycle
    assert cycle["ok"] is False, cycle


# --------------------------------------------------------------------------- #
# 5. semantic_mlx_isolated() truth table + timeout env override
# --------------------------------------------------------------------------- #


def test_semantic_mlx_isolated_truth_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # local_rule -> False (regardless of provider)
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_rule")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    assert pipeline_watcher.semantic_mlx_isolated() is False

    # local_model + mlx (explicit) -> True
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_ENGINE", "local_model")
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "mlx")
    assert pipeline_watcher.semantic_mlx_isolated() is True

    # local_model + provider defaulted (mlx) -> True
    monkeypatch.delenv("BTQ_SEMANTIC_PROVIDER", raising=False)
    assert pipeline_watcher.semantic_mlx_isolated() is True

    # local_model + ollama -> False
    monkeypatch.setenv("BTQ_SEMANTIC_PROVIDER", "ollama")
    assert pipeline_watcher.semantic_mlx_isolated() is False


def test_isolated_semantic_subprocess_timeout_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default when unset.
    monkeypatch.delenv("BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS", raising=False)
    assert pipeline_watcher.isolated_semantic_subprocess_timeout_seconds() == 240.0
    assert pipeline_watcher.isolated_semantic_subprocess_timeout_seconds(default=99.0) == 99.0

    # Honors a valid override.
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS", "12.5")
    assert pipeline_watcher.isolated_semantic_subprocess_timeout_seconds() == 12.5

    # Ignores junk / non-positive -> falls back to default.
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS", "not-a-number")
    assert pipeline_watcher.isolated_semantic_subprocess_timeout_seconds(default=7.0) == 7.0
    monkeypatch.setenv("BTQ_FIELD_CAPTURE_SEMANTIC_SUBPROCESS_TIMEOUT_SECONDS", "0")
    assert pipeline_watcher.isolated_semantic_subprocess_timeout_seconds(default=7.0) == 7.0
