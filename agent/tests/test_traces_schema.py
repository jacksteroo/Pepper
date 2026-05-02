"""Schema-stub tests for `agent.traces.schema`.

Locks in the invariants documented in `docs/trace-schema.md` so a future
edit to the dataclass cannot silently drift from the canonical doc /
ADR-0005. Wider behavioral tests land alongside the repository (#20)
and the emitter (#22).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

from agent.error_classifier import DataSensitivity
from agent.traces import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_DEFAULT,
    PROMPT_VERSION_UNVERSIONED,
    Archetype,
    Trace,
    TraceTier,
    TriggerSource,
)


def _embedding(value: float = 0.1) -> list[float]:
    return [value] * EMBEDDING_DIM


class TestTraceDefaults:
    def test_defaults_match_canonical_initial_state(self) -> None:
        t = Trace()
        # New traces are user-initiated orchestrator turns in the working tier.
        assert t.trigger_source is TriggerSource.USER
        assert t.archetype is Archetype.ORCHESTRATOR
        assert t.tier is TraceTier.WORKING
        # Privacy-conservative default: assume RAW_PERSONAL until proven otherwise.
        assert t.data_sensitivity is DataSensitivity.LOCAL_ONLY
        # Embedding is opt-in.
        assert t.embedding is None
        assert t.embedding_model_version is None
        # User reaction is lazy.
        assert t.user_reaction is None
        # Pre-#48: prompt_version defaults to the documented sentinel.
        assert t.prompt_version == PROMPT_VERSION_UNVERSIONED
        # Timing fields are populated.
        assert isinstance(t.created_at, datetime)
        assert t.created_at.tzinfo is timezone.utc
        # ID is a valid UUID4 string.
        uuid.UUID(t.trace_id)

    def test_each_trace_id_is_unique(self) -> None:
        ids = {Trace().trace_id for _ in range(50)}
        assert len(ids) == 50


class TestTraceImmutability:
    def test_trace_is_frozen(self) -> None:
        t = Trace()
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.data_sensitivity = DataSensitivity.PUBLIC  # type: ignore[misc]

    def test_dataclasses_replace_revalidates(self) -> None:
        # `dataclasses.replace` is the canonical way to derive a new trace.
        # Validation MUST run on the derived instance — otherwise frozen=True
        # is meaningless for invariant enforcement.
        t = Trace()
        with pytest.raises(ValueError, match="embedding dimension"):
            dataclasses.replace(t, embedding=[0.0] * 8, embedding_model_version="x")


class TestTraceInvariants:
    def test_trace_id_must_be_uuid(self) -> None:
        with pytest.raises(ValueError, match="trace_id must be a valid UUID"):
            Trace(trace_id="not-a-uuid")

    def test_embedding_requires_model_version(self) -> None:
        with pytest.raises(ValueError, match="embedding_model_version"):
            Trace(embedding=_embedding())

    def test_embedding_dimension_is_locked(self) -> None:
        with pytest.raises(ValueError, match="embedding dimension"):
            Trace(embedding=[0.0] * 768, embedding_model_version="nomic-embed-text")

    def test_embedding_with_model_version_is_allowed(self) -> None:
        t = Trace(embedding=_embedding(), embedding_model_version=EMBEDDING_MODEL_DEFAULT)
        assert t.embedding_model_version == EMBEDDING_MODEL_DEFAULT
        assert len(t.embedding or []) == EMBEDDING_DIM

    def test_scheduler_job_name_requires_scheduler_trigger(self) -> None:
        with pytest.raises(ValueError, match="scheduler_job_name"):
            Trace(
                trigger_source=TriggerSource.USER,
                scheduler_job_name="morning_brief",
            )

    def test_scheduler_trigger_accepts_job_name(self) -> None:
        t = Trace(
            trigger_source=TriggerSource.SCHEDULER,
            scheduler_job_name="weekly_review",
        )
        assert t.scheduler_job_name == "weekly_review"

    def test_scheduler_trigger_without_job_name_is_allowed(self) -> None:
        # Pre-#23 wiring may emit scheduler traces without a job name; the
        # schema does not require it. #23 makes population mandatory at the
        # call site.
        t = Trace(trigger_source=TriggerSource.SCHEDULER)
        assert t.scheduler_job_name is None


class TestJsonbShapeValidation:
    def test_assembled_context_must_be_dict(self) -> None:
        with pytest.raises(TypeError, match="assembled_context"):
            Trace(assembled_context=["not", "a", "dict"])  # type: ignore[arg-type]

    def test_tools_called_must_be_list(self) -> None:
        with pytest.raises(TypeError, match="tools_called"):
            Trace(tools_called={"not": "a list"})  # type: ignore[arg-type]

    def test_tools_called_element_must_be_dict(self) -> None:
        with pytest.raises(TypeError, match=r"tools_called\[0\]"):
            Trace(tools_called=["not a dict"])  # type: ignore[list-item]

    def test_tools_called_element_must_have_name(self) -> None:
        with pytest.raises(ValueError, match="missing required keys"):
            Trace(tools_called=[{"args": {}, "result_summary": "ok"}])

    def test_tools_called_well_formed_passes(self) -> None:
        t = Trace(
            tools_called=[
                {
                    "name": "send_telegram_message",
                    "args": {"chat_id": "123", "text": "hi"},
                    "result_summary": "ok",
                    "latency_ms": 42,
                    "error": None,
                },
            ],
        )
        assert t.tools_called[0]["name"] == "send_telegram_message"

    def test_unjsonable_payload_is_rejected(self) -> None:
        # A circular reference is the canonical "JSON-impossible" payload.
        bad: dict[str, object] = {}
        bad["self"] = bad
        with pytest.raises(ValueError, match="JSON-serialisable"):
            Trace(assembled_context=bad)


class TestReprRedaction:
    def test_repr_does_not_leak_input_or_output(self) -> None:
        secret_in = "social security 123-45-6789"
        secret_out = "transferred to account 9876543210"
        t = Trace(input=secret_in, output=secret_out)
        r = repr(t)
        assert "123-45-6789" not in r
        assert "9876543210" not in r
        assert "<redacted" in r
        # Metadata that helps debugging is still present.
        assert t.trace_id[:8] in r
        assert "trigger_source" in r

    def test_repr_does_not_leak_assembled_context_values(self) -> None:
        t = Trace(
            assembled_context={"items": [{"summary": "leaked-pii-string"}]},
        )
        assert "leaked-pii-string" not in repr(t)

    def test_repr_does_not_leak_tool_call_args(self) -> None:
        t = Trace(
            tools_called=[
                {"name": "lookup", "args": {"phone": "555-leaked"}, "result_summary": "ok"},
            ],
        )
        assert "555-leaked" not in repr(t)


class TestEnumCoverage:
    def test_trigger_source_members_match_doc(self) -> None:
        assert {m.value for m in TriggerSource} == {"user", "scheduler", "agent"}

    def test_archetype_members_match_adr_0004(self) -> None:
        assert {m.value for m in Archetype} == {
            "orchestrator",
            "reflector",
            "monitor",
            "researcher",
        }

    def test_tier_members_match_compression_policy(self) -> None:
        assert {m.value for m in TraceTier} == {"working", "recall", "archival"}

    def test_data_sensitivity_is_reused_not_duplicated(self) -> None:
        # Schema mirrors the existing enum exactly — no parallel definition.
        from agent.error_classifier import DataSensitivity as Source

        assert DataSensitivity is Source
