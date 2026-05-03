"""Tests for ``agent/optimizer/datasets.py``.

In-memory only; the ``build_from_repo`` path is exercised via the
integration test that mocks ``TraceRepository.query``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from agent.optimizer.datasets import (
    build_from_repo,
    build_from_traces,
    dataset_hash,
    trace_to_example,
)
from agent.optimizer.schema import TraceExample
from agent.traces.schema import Archetype


def test_build_from_traces_filters_by_prompt_version(fixture_traces):
    examples = build_from_traces(fixture_traces, prompt_version="v3")
    assert all(e.prompt_version == "v3" for e in examples)
    assert len(examples) == 3  # i=0,2,4


def test_build_from_traces_no_filter_returns_all(fixture_traces):
    assert len(build_from_traces(fixture_traces)) == len(fixture_traces)


def test_trace_to_example_drops_irrelevant_fields(fixture_traces):
    ex = trace_to_example(fixture_traces[0])
    assert isinstance(ex, TraceExample)
    assert ex.trace_id == fixture_traces[0].trace_id
    assert ex.input == fixture_traces[0].input
    assert ex.output == fixture_traces[0].output


def test_dataset_hash_is_stable_under_reordering(fixture_examples):
    h1 = dataset_hash(fixture_examples)
    h2 = dataset_hash(list(reversed(fixture_examples)))
    assert h1 == h2


def test_dataset_hash_changes_with_membership(fixture_examples):
    h1 = dataset_hash(fixture_examples)
    h2 = dataset_hash(fixture_examples[:-1])
    assert h1 != h2


@pytest.mark.asyncio
async def test_build_from_repo_passes_filters(fixture_traces):
    repo = AsyncMock()
    repo.query.return_value = fixture_traces
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    until = since + timedelta(days=1)
    examples = await build_from_repo(
        repo,
        archetype=Archetype.ORCHESTRATOR,
        prompt_version="v2",
        since=since,
        until=until,
    )
    repo.query.assert_awaited_once()
    kwargs = repo.query.call_args.kwargs
    assert kwargs["archetype"] == Archetype.ORCHESTRATOR
    assert kwargs["since"] == since
    assert kwargs["until"] == until
    # The prompt_version filter is applied client-side after the query.
    assert all(e.prompt_version == "v2" for e in examples)
    assert len(examples) == 3
