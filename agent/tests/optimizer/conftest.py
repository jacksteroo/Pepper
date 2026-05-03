"""Fixtures shared across optimizer tests.

These fixtures intentionally do NOT touch the database or the real
``data/optimizer/`` directory. Each test that needs a store gets one
backed by a ``tmp_path`` directory.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.error_classifier import DataSensitivity
from agent.optimizer.schema import TraceExample
from agent.traces import Trace, TraceTier, TriggerSource
from agent.traces.schema import Archetype


@pytest.fixture
def fixture_traces() -> list[Trace]:
    """A small set of traces with two distinct prompt_versions and one
    archetype, for filtering tests.
    """
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    return [
        Trace(
            trace_id="00000000-0000-0000-0000-00000000000{}".format(i),
            created_at=base + timedelta(minutes=i),
            trigger_source=TriggerSource.USER,
            archetype=Archetype.ORCHESTRATOR,
            input=f"input {i}",
            output=f"output {i} apple banana cherry",
            assembled_context={"k": "v"},
            tools_called=[],
            model_selected="claude-sonnet",
            model_version="x",
            prompt_version="v3" if i % 2 == 0 else "v2",
            latency_ms=10 * i,
            data_sensitivity=DataSensitivity.PUBLIC,
            tier=TraceTier.WORKING,
        )
        for i in range(6)
    ]


@pytest.fixture
def fixture_examples(fixture_traces) -> list[TraceExample]:
    from agent.optimizer.datasets import build_from_traces
    return build_from_traces(fixture_traces, prompt_version="v3")
