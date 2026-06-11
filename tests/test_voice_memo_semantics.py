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


def test_voice_memo_routes_status_change_through_shared_engine_and_collector(tmp_path: Path, couchdb_review, couchdb_job_drafts) -> None:
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

    # 334b: run_semantic_pass now routes through collect_job_drafts. The
    # reviewable artifact is a pending_approval job_draft carrying the SAME
    # proposed queue job the legacy candidate's
    # approval_metadata.proposed_queue_job did (set_entity_status -> site 7030
    # inactive), with the voice_memo source context preserved.
    drafts = list(couchdb_job_drafts.drafts.values())
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["type"] == "job_draft"
    assert draft["review_status"] == "pending_approval"
    assert draft["source_kind"] == "voice_memo"
    assert draft["site_id"] == "7030"
    assert draft["job_type"] == "set_entity_status"
    assert draft["payload"]["entity_type"] == "site"
    assert draft["payload"]["entity_id"] == "7030"
    assert draft["payload"]["status"] == "inactive"


def test_employee_status_review_candidate_carries_employee_context(tmp_path: Path, couchdb_review, couchdb_job_drafts) -> None:
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
    # 334b: the employee context the legacy candidate carried in its
    # channel_metadata (employee_slugs/names) is now carried by the emitted
    # job_draft's proposed-job payload: an employee-targeted set_entity_status
    # whose entity_id is the resolved employee slug.
    drafts = list(couchdb_job_drafts.drafts.values())
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["review_status"] == "pending_approval"
    assert draft["source_kind"] == "voice_memo"
    assert draft["job_type"] == "set_entity_status"
    assert draft["payload"]["entity_type"] == "employee"
    assert draft["payload"]["entity_id"] == "hutton-maria"
    assert draft["payload"]["status"] == "inactive"
    # The draft's group_id ties it back to this capture (the lineage the legacy
    # provenance.semantic_artifact_path assertion protected).
    assert draft["group_id"] == "vm-employee"
    assert draft["draft_id"] == "vm-employee-set_entity_status-0"


def test_voice_memo_rule_engine_writes_recruiting_candidate_with_proposed_job(tmp_path: Path, couchdb_review, couchdb_job_drafts) -> None:
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
    # 334b: the pending_approval job_draft carries the trigger_recruiting proposed
    # job (site Hartwell) the legacy candidate's proposed_queue_job did.
    drafts = list(couchdb_job_drafts.drafts.values())
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["review_status"] == "pending_approval"
    assert draft["job_type"] == "trigger_recruiting"
    assert draft["payload"]["site"] == "Hartwell Medical Center"


def test_voice_memo_rule_engine_writes_retention_candidate_with_proposed_job(tmp_path: Path, couchdb_review, couchdb_job_drafts) -> None:
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
    # 334b: the pending_approval job_draft carries the flag_retention_risk proposed
    # job (employee Maria Hutton) the legacy candidate's proposed_queue_job did,
    # with the voice_memo source preserved (legacy channel_metadata.channel).
    drafts = list(couchdb_job_drafts.drafts.values())
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["review_status"] == "pending_approval"
    assert draft["job_type"] == "flag_retention_risk"
    assert draft["payload"]["employee"] == "Maria Hutton"
    assert draft["source_kind"] == "voice_memo"


def test_one_voice_transcript_fans_out_multiple_stable_drafts_without_duplicates(tmp_path: Path, couchdb_review, couchdb_job_drafts) -> None:
    # 334b retarget: the legacy test asserted one transcript fans out to >=2
    # review CANDIDATES (an employee/attendance + a site/supply candidate) with
    # distinct candidate_ids and no duplicates across re-runs. At the job_draft
    # layer the fan-out is over PROPOSABLE queue jobs: the attendance candidate
    # yields no proposable job, so its draft never materializes (a real,
    # pre-existing property of proposed_queue_jobs -- NOT a 334b regression). To
    # preserve the SAME structural guarantee -- one transcript -> N>=2 drafts
    # sharing one group_id, distinct draft_ids, idempotent re-walk -- this uses a
    # transcript that genuinely yields two proposable equipment requests from a
    # single voice memo.
    transcript_path = tmp_path / "multi.webm.whisper.txt"
    transcript = "The mop sink is broken and leaking. Also we need to order more towels and a new vacuum."
    transcript_path.write_text(transcript, encoding="utf-8")
    memo_context = VoiceMemoTranscriptContext(
        capture_id="vm-multi",
        routing_flag="site_tagged",
        raw_text=transcript,
        raw_transcript_path=transcript_path,
        audio_file="multi.webm",
        site_id="7022",
        site="Hartwell Medical Center",
        employees=[],
    )
    runtime = tmp_path / "runtime"

    first = run_semantic_pass(memo_context, runtime, engine=RuleCaptureEngine())
    drafts_after_first = couchdb_job_drafts.draft_ids()
    second = run_semantic_pass(memo_context, runtime, engine=RuleCaptureEngine())

    assert first.error == ""
    assert second.error == ""
    drafts = list(couchdb_job_drafts.drafts.values())

    # Fan-out: one transcript -> N>=2 drafts.
    assert len(drafts) >= 2

    # One shared group_id (the single source capture), distinct draft_ids.
    group_ids = {str(d["group_id"]) for d in drafts}
    assert group_ids == {"vm-multi"}
    draft_ids = [str(d["draft_id"]) for d in drafts]
    assert len(set(draft_ids)) == len(draft_ids)

    # The re-walk (second run over the same source) duplicates NOTHING: every
    # draft already exists, so the exists->skip guard fires and the stored
    # draft_id set is byte-for-byte unchanged.
    assert sorted(draft_ids) == drafts_after_first
    assert couchdb_job_drafts.draft_ids() == drafts_after_first

    # Each draft is a real pending_approval job_draft with its own proposed job.
    for draft in drafts:
        assert draft["type"] == "job_draft"
        assert draft["review_status"] == "pending_approval"
        assert draft["job_type"]
        assert isinstance(draft["payload"], dict) and draft["payload"]


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
