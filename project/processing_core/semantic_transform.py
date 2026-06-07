from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from processing_core.semantics import semantic_base_payload, semantic_failed_payload, semantic_success_payload


EngineInput = TypeVar("EngineInput")
EngineResult = TypeVar("EngineResult")


@dataclass(frozen=True)
class SemanticTransformOutcome:
    payload: dict[str, object]
    error: Exception | None = None


def semantic_engine_name(engine: object) -> str:
    explicit = getattr(engine, "engine_name", None)
    if explicit:
        return str(explicit)
    return type(engine).__name__ if hasattr(engine, "__class__") else "semantic-engine"


@dataclass(frozen=True)
class SemanticTransformSpec(Generic[EngineInput, EngineResult]):
    artifact_type: str
    artifact_id_field: str
    artifact_id: str
    source_transcript_path: str
    engine_field: str
    engine_name: str
    raw_text: str
    provenance: dict[str, object]
    engine_input: EngineInput
    engine: Callable[[EngineInput], EngineResult]
    result_fields: Callable[[EngineResult], dict[str, object]]
    failure_fields: dict[str, object]
    validate_result: Callable[[EngineResult], None] | None = None


def transform_semantic_payload(spec: SemanticTransformSpec[EngineInput, EngineResult]) -> SemanticTransformOutcome:
    base_payload = semantic_base_payload(
        artifact_type=spec.artifact_type,
        artifact_id_field=spec.artifact_id_field,
        artifact_id=spec.artifact_id,
        source_transcript_path=spec.source_transcript_path,
        engine_field=spec.engine_field,
        engine_name=spec.engine_name,
        raw_text=spec.raw_text,
        provenance=spec.provenance,
    )
    try:
        result = spec.engine(spec.engine_input)
        if spec.validate_result is not None:
            spec.validate_result(result)
        return SemanticTransformOutcome(semantic_success_payload(base_payload, spec.result_fields(result)))
    except Exception as exc:  # noqa: BLE001
        return SemanticTransformOutcome(
            semantic_failed_payload(base_payload, error=exc, failure_fields=spec.failure_fields),
            error=exc,
        )

