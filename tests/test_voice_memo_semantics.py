import json
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import pytest

import vision_backends
from processing_core.capture_semantics import CaptureSemanticInput, RuleCaptureEngine, SemanticResult
from processing_core.extracted_actions import ExtractedAction
from voice_memo import semantic_eval
from voice_memo.semantics import (
    VoiceMemoTranscriptContext,
    capture_semantic_input,
    default_semantic_engine_from_env,
    run_semantic_pass,
)


WESTERN_GAS_TRANSCRIPT = (
    "This is another voicenote for Western Gas Transmission from the BTQ Office Dashboard. "
    "And I have selected Western Gas Transmission from the site. And the voicenote is just "
    "the fact that Western Gas Transmission is no longer one of my accounts, therefore needs "
    "to be set to be inactive. So if there is a job that can generate an action that will set "
    "the Western Gas site to be inactive, that would be perfect."
)


def context(tmp_path: Path) -> VoiceMemoTranscriptContext:
    transcript_path = tmp_path / "western-gas.webm.whisper.txt"
    transcript_path.write_text(WESTERN_GAS_TRANSCRIPT, encoding="utf-8")
    return VoiceMemoTranscriptContext(
        capture_id="vm-western-gas",
        routing_flag="site_tagged",
        raw_text=WESTERN_GAS_TRANSCRIPT,
        raw_transcript_path=transcript_path,
        audio_file="western-gas.webm",
        site_id="7030",
        site="Western Gas Transmission",
        employees=[],
        note="",
        captured_at="2026-05-31T14:00:00+00:00",
    )


def test_voice_memo_routes_status_change_through_shared_engine_and_collector(tmp_path: Path, couchdb_review) -> None:
    semantic_pass = run_semantic_pass(context(tmp_path), tmp_path / "runtime", engine=RuleCaptureEngine())

    assert semantic_pass.error == ""
    assert len(semantic_pass.candidate_paths) == 1
    artifact = json.loads(semantic_pass.artifact_path.read_text(encoding="utf-8"))
    assert artifact["type"] == "voice_memo_semantic_summary"
    assert artifact["status"] == "complete"
    assert artifact["source_kind"] == "voice_memo"
    assert artifact["intent"] == "site_status_change"
    assert artifact["action"] == "set_inactive"
    assert artifact["target_type"] == "site"
    assert artifact["target_id"] == "7030"
    assert artifact["review_required"] is True
    assert artifact["extracted_actions"][0]["job_type"] == "set_entity_status"

    candidate = couchdb_review.review_payload_for(semantic_pass.candidate_paths[0])
    assert candidate["candidate_type"] == "voice_memo_operator_action"
    assert candidate["status"] == "pending_review"
    assert candidate["channel_metadata"]["channel"] == "voice_memo"
    assert candidate["channel_metadata"]["site_id"] == "7030"
    proposed = candidate["approval_metadata"]["proposed_queue_job"]
    assert proposed["job_type"] == "set_entity_status"
    assert proposed["payload"]["entity_type"] == "site"
    assert proposed["payload"]["entity_id"] == "7030"
    assert proposed["payload"]["status"] == "inactive"


def test_employee_status_review_candidate_carries_employee_context(tmp_path: Path, couchdb_review) -> None:
    transcript_path = tmp_path / "employee.webm.whisper.txt"
    transcript_path.write_text("Set Maria inactive.", encoding="utf-8")
    employee_context = VoiceMemoTranscriptContext(
        capture_id="vm-employee",
        routing_flag="employee_tagged",
        raw_text="Set Maria inactive.",
        raw_transcript_path=transcript_path,
        audio_file="employee.webm",
        employees=[{"slug": "hutton-maria", "name": "Maria Hutton"}],
    )

    semantic_pass = run_semantic_pass(employee_context, tmp_path / "runtime", engine=RuleCaptureEngine())

    assert len(semantic_pass.candidate_paths) == 1
    candidate = couchdb_review.review_payload_for(semantic_pass.candidate_paths[0])
    assert candidate["channel_metadata"]["employee_slugs"] == ["hutton-maria"]
    assert candidate["channel_metadata"]["employee_names"] == ["Maria Hutton"]
    proposed = candidate["approval_metadata"]["proposed_queue_job"]
    assert proposed["job_type"] == "set_entity_status"
    assert proposed["payload"]["entity_type"] == "employee"
    assert proposed["payload"]["entity_id"] == "hutton-maria"
    assert candidate["provenance"]["semantic_artifact_path"] == str(semantic_pass.artifact_path.resolve(strict=False))


def test_voice_memo_rule_engine_writes_recruiting_candidate_with_proposed_job(tmp_path: Path, couchdb_review) -> None:
    transcript_path = tmp_path / "recruiting.webm.whisper.txt"
    transcript_path.write_text("Need recruiting coverage for the evening cleaner opening.", encoding="utf-8")
    memo_context = VoiceMemoTranscriptContext(
        capture_id="vm-recruiting",
        routing_flag="site_tagged",
        raw_text="Need recruiting coverage for the evening cleaner opening.",
        raw_transcript_path=transcript_path,
        audio_file="recruiting.webm",
        site_id="7022",
        site="Hartwell Medical Center",
    )

    semantic_pass = run_semantic_pass(memo_context, tmp_path / "runtime", engine=RuleCaptureEngine())

    assert len(semantic_pass.candidate_paths) == 1
    candidate = couchdb_review.review_payload_for(semantic_pass.candidate_paths[0])
    assert candidate["status"] == "pending_review"
    proposed = candidate["approval_metadata"]["proposed_queue_job"]
    assert proposed["job_type"] == "trigger_recruiting"
    assert proposed["payload"]["site"] == "Hartwell Medical Center"


def test_voice_memo_rule_engine_writes_retention_candidate_with_proposed_job(tmp_path: Path, couchdb_review) -> None:
    transcript_path = tmp_path / "retention.webm.whisper.txt"
    transcript_path.write_text("Maria may quit if we keep her on that schedule.", encoding="utf-8")
    memo_context = VoiceMemoTranscriptContext(
        capture_id="vm-retention",
        routing_flag="employee_tagged",
        raw_text="Maria may quit if we keep her on that schedule.",
        raw_transcript_path=transcript_path,
        audio_file="retention.webm",
        site_id="7022",
        site="Hartwell Medical Center",
        employees=[{"slug": "hutton-maria", "name": "Maria Hutton"}],
    )

    semantic_pass = run_semantic_pass(memo_context, tmp_path / "runtime", engine=RuleCaptureEngine())

    assert len(semantic_pass.candidate_paths) == 1
    candidate = couchdb_review.review_payload_for(semantic_pass.candidate_paths[0])
    proposed = candidate["approval_metadata"]["proposed_queue_job"]
    assert proposed["job_type"] == "flag_retention_risk"
    assert proposed["payload"]["employee"] == "Maria Hutton"
    assert candidate["channel_metadata"]["channel"] == "voice_memo"


def test_one_voice_transcript_fans_out_multiple_stable_candidates_without_duplicates(tmp_path: Path, couchdb_review) -> None:
    transcript_path = tmp_path / "multi.webm.whisper.txt"
    transcript = "Maria was late again at Hartwell. Also restock paper towels at Hartwell."
    transcript_path.write_text(transcript, encoding="utf-8")
    memo_context = VoiceMemoTranscriptContext(
        capture_id="vm-multi",
        routing_flag="employee_tagged",
        raw_text=transcript,
        raw_transcript_path=transcript_path,
        audio_file="multi.webm",
        site_id="7022",
        site="Hartwell Medical Center",
        employees=[{"slug": "hutton-maria", "name": "Maria Hutton"}],
    )
    runtime = tmp_path / "runtime"

    first = run_semantic_pass(memo_context, runtime, engine=RuleCaptureEngine())
    second = run_semantic_pass(memo_context, runtime, engine=RuleCaptureEngine())

    candidates = sorted((runtime / "reviews" / "action_candidates" / "field_capture").glob("*.json"))
    assert len(first.candidate_paths) >= 2
    assert first.candidate_paths == second.candidate_paths
    assert len(candidates) == len(first.candidate_paths)
    candidate_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in candidates]
    targets = {(payload["channel_metadata"]["target_type"], payload["channel_metadata"]["target_id"]) for payload in candidate_payloads}
    assert ("employee", "hutton-maria") in targets
    assert ("site", "7022") in targets
    assert len({payload["candidate_id"] for payload in candidate_payloads}) == len(candidate_payloads)


def test_voice_memo_semantic_failure_writes_failed_artifact(tmp_path: Path) -> None:
    class FailingEngine:
        engine_name = "test-failing-engine"
        prompt_version = "test"

        def __call__(self, _capture: CaptureSemanticInput):
            raise ValueError("model returned nonsense")

    semantic_pass = run_semantic_pass(context(tmp_path), tmp_path / "runtime", engine=FailingEngine())

    assert semantic_pass.result is None
    assert semantic_pass.error == "model returned nonsense"
    assert semantic_pass.candidate_paths == ()
    payload = json.loads(semantic_pass.artifact_path.read_text(encoding="utf-8"))
    assert payload["type"] == "voice_memo_semantic_summary"
    assert payload["status"] == "failed"
    assert payload["semantic_engine"] == "test-failing-engine"
    assert payload["error"]["message"] == "model returned nonsense"


def test_capture_semantic_input_preserves_voice_context(tmp_path: Path) -> None:
    capture = capture_semantic_input(context(tmp_path))

    assert capture.source_kind == "voice_memo"
    assert capture.capture_id == "vm-western-gas"
    assert capture.site_id == "7030"
    assert capture.site_label == "Western Gas Transmission"
    assert capture.source_transcript_path == context(tmp_path).raw_transcript_path


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_ollama_text_client_posts_chat_format_json(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[object, float | None]] = []

    def fake_urlopen(req, timeout=None):
        requests.append((req, timeout))
        return FakeHTTPResponse({"message": {"content": '{"intent":"site_note"}'}})

    monkeypatch.setattr(vision_backends.request, "urlopen", fake_urlopen)

    client = vision_backends.OllamaTextClient("qwen3:4b", base_url="http://10.0.0.10:11434", timeout_seconds=12.5)
    result = client.generate_json("Return JSON.")

    assert result == {"intent": "site_note"}
    assert len(requests) == 1
    req, timeout = requests[0]
    assert req.full_url == "http://10.0.0.10:11434/api/chat"
    assert timeout == 12.5
    body = json.loads(req.data.decode("utf-8"))
    assert body["model"] == "qwen3:4b"
    assert body["format"] == "json"
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "Return JSON."}]
    assert "think" not in body


def test_ollama_text_client_can_disable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        return FakeHTTPResponse({"message": {"content": '{"intent":"site_note"}'}})

    monkeypatch.setattr(vision_backends.request, "urlopen", fake_urlopen)

    client = vision_backends.OllamaTextClient(
        "qwen3:4b",
        base_url="http://10.0.0.10:11434",
        disable_thinking=True,
    )
    client.generate_json("Return JSON.")

    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["think"] is False
    assert body["messages"] == [{"role": "user", "content": "Return JSON.\n/no_think\n"}]


def test_ollama_text_client_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req, timeout=None):
        return FakeHTTPResponse({"message": {"content": "not json"}})

    monkeypatch.setattr(vision_backends.request, "urlopen", fake_urlopen)

    client = vision_backends.OllamaTextClient("qwen3:4b", base_url="http://10.0.0.10:11434")

    with pytest.raises(ValueError, match="not valid JSON"):
        client.generate_json("Return JSON.")


def test_build_voice_memo_semantic_engine_ollama_uses_env_url_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeOllamaTextClient:
        provider = "ollama"

        def __init__(
            self,
            model: str,
            base_url: str,
            timeout_seconds: float,
            keep_alive: str,
            disable_thinking: bool,
        ) -> None:
            self.model = model
            created.update(
                {
                    "model": model,
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                    "keep_alive": keep_alive,
                    "disable_thinking": disable_thinking,
                }
            )

        def generate_json(self, _prompt: str) -> dict:
            return {}

    monkeypatch.setattr(vision_backends, "OllamaTextClient", FakeOllamaTextClient)
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "ollama")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_MODEL", "qwen3:4b")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_URL", "http://10.0.0.10:11434")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_TIMEOUT_SECONDS", "45.5")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_KEEP_ALIVE", "30m")

    engine = default_semantic_engine_from_env()

    assert engine is not None
    assert created == {
        "model": "qwen3:4b",
        "base_url": "http://10.0.0.10:11434",
        "timeout_seconds": 45.5,
        "keep_alive": "30m",
        "disable_thinking": True,
    }
    assert engine.engine_name == "ollama:qwen3:4b:capture-semantics"


def test_build_voice_memo_semantic_engine_ollama_can_enable_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeOllamaTextClient:
        provider = "ollama"

        def __init__(
            self,
            model: str,
            base_url: str,
            timeout_seconds: float,
            keep_alive: str,
            disable_thinking: bool,
        ) -> None:
            self.model = model
            created["disable_thinking"] = disable_thinking

        def generate_json(self, _prompt: str) -> dict:
            return {}

    monkeypatch.setattr(vision_backends, "OllamaTextClient", FakeOllamaTextClient)
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "ollama")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_MODEL", "qwen3:4b")
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_DISABLE_THINKING", "0")

    default_semantic_engine_from_env()

    assert created["disable_thinking"] is False


def test_build_voice_memo_semantic_engine_ollama_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTQ_VOICE_MEMO_SEMANTIC_ENGINE", "ollama")
    monkeypatch.delenv("BTQ_VOICE_MEMO_SEMANTIC_MODEL", raising=False)

    with pytest.raises(ValueError, match="BTQ_VOICE_MEMO_SEMANTIC_MODEL is required"):
        default_semantic_engine_from_env()


def test_voice_memo_semantic_eval_reports_expected_case_passes_with_fake_engine(tmp_path: Path) -> None:
    class FakeEngine:
        engine_name = "fake"

        def __call__(self, capture: CaptureSemanticInput):
            result = RuleCaptureEngine()(capture)
            action = ExtractedAction(
                action_key="site_status_change",
                candidate_type="voice_memo_operator_action",
                job_type="set_entity_status",
                target_type="site",
                target_id="7030",
                target_label="Western Gas Transmission",
                summary="Review site status change: Western Gas Transmission -> inactive.",
                rationale="Test.",
                confidence="high",
                source_excerpt="Set site inactive.",
                payload_fields={"status": "inactive"},
            )
            return replace(result, extracted_actions=[action])

    cases = [
        {
            "id": "site-inactive",
            "context": context(tmp_path),
            "expected": {
                "intent": "site_status_change",
                "target_type": "site",
                "target_id": "7030",
                "action": "set_inactive",
                "review_required": True,
            },
        }
    ]

    rows = semantic_eval.evaluate_cases(cases, FakeEngine(), model="fake-model", provider="fake")
    summary = semantic_eval.summary_for_rows(rows)

    assert rows[0]["pass"] is True
    assert rows[0]["validated"] is True
    assert summary["cases"] == 1
    assert summary["passed"] == 1


def test_voice_memo_semantic_eval_writes_jsonl_without_queue_side_effects(tmp_path: Path) -> None:
    fixture_path = tmp_path / "fixtures.json"
    output_path = tmp_path / "eval.jsonl"
    fixture_path.write_text(
        json.dumps(
            [
                {
                    "id": "general",
                    "context": {
                        "capture_id": "eval-general",
                        "routing_flag": "general",
                        "raw_text": "General dashboard note.",
                        "raw_transcript_path": "/tmp/eval-general.webm.whisper.txt",
                        "audio_file": "eval-general.webm",
                        "employees": [],
                    },
                    "expected": {
                        "intent": "unknown",
                        "target_type": "general",
                        "target_id": "",
                        "action": "",
                        "review_required": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    summary = semantic_eval.run(
        Namespace(
            engine="rule",
            url="http://10.0.0.10:11434",
            model="",
            timeout=1.0,
            keep_alive="1m",
            enable_thinking=False,
            fixture=fixture_path,
            output=output_path,
        )
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert summary["cases"] == 1
    assert rows[0]["fixture_id"] == "general"
    assert rows[0]["pass"] is True
    assert not (tmp_path / "queue").exists()
    assert not (tmp_path / "btq_queue").exists()
