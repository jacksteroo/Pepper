"""LastNTurnsSelector unit tests."""
from __future__ import annotations

import json

from agent.context.selectors import LastNTurnsSelector


class _StubMemory:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.calls: list[int] = []

    def get_working_memory(self, *, limit: int) -> list[dict]:
        self.calls.append(limit)
        return list(self._messages[-limit:])


def test_returns_history_with_limit() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]
    sel = LastNTurnsSelector(memory_manager=_StubMemory(msgs))
    rec = sel.select(limit=2, isolated=False)

    assert rec.name == "last_n_turns"
    assert isinstance(rec.content, list)
    assert len(rec.content) == 2
    assert rec.provenance["n_messages"] == 2
    assert rec.provenance["limit"] == 2
    assert rec.provenance["isolated"] is False
    # Role counts come out of the slice we returned.
    assert rec.provenance["role_counts"]["user"] == 1
    assert rec.provenance["role_counts"]["assistant"] == 1


def test_isolated_returns_empty_history() -> None:
    mem = _StubMemory([{"role": "user", "content": "x"}])
    sel = LastNTurnsSelector(memory_manager=mem)
    rec = sel.select(limit=10, isolated=True)
    assert rec.content == []
    assert mem.calls == []  # isolated path skips memory entirely
    assert rec.provenance["n_messages"] == 0


def test_handles_memory_failure() -> None:
    class _Bad:
        def get_working_memory(self, *, limit: int) -> list[dict]:
            raise RuntimeError("boom")

    sel = LastNTurnsSelector(memory_manager=_Bad())
    rec = sel.select(limit=5, isolated=False)
    assert rec.content == []


def test_provenance_is_json_serializable() -> None:
    sel = LastNTurnsSelector(memory_manager=_StubMemory([]))
    rec = sel.select(limit=10, isolated=False)
    json.dumps(rec.provenance)
