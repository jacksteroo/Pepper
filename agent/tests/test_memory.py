from datetime import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.memory import MemoryManager


# ─── Working Memory ────────────────────────────────────────────────────────

def test_working_memory_add_get():
    mm = MemoryManager()
    mm.add_to_working_memory("user", "Hello")
    mm.add_to_working_memory("assistant", "Hi there")
    mm.add_to_working_memory("user", "How are you?")
    result = mm.get_working_memory()
    assert len(result) == 3
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "Hello"
    assert result[2]["content"] == "How are you?"


def test_working_memory_limit_parameter():
    mm = MemoryManager()
    for i in range(10):
        mm.add_to_working_memory("user", f"message {i}")
    result = mm.get_working_memory(limit=3)
    assert len(result) == 3
    assert result[-1]["content"] == "message 9"


def test_working_memory_maxlen():
    mm = MemoryManager()
    for i in range(55):
        mm.add_to_working_memory("user", f"message {i}")
    result = mm.get_working_memory(limit=100)
    assert len(result) == 50  # deque maxlen
    assert result[-1]["content"] == "message 54"


def test_working_memory_clear():
    mm = MemoryManager()
    mm.add_to_working_memory("user", "test")
    mm.clear_working_memory()
    assert mm.get_working_memory() == []


def test_working_memory_returns_role_and_content_only():
    """Timestamps should not appear in get_working_memory output."""
    mm = MemoryManager()
    mm.add_to_working_memory("user", "test")
    result = mm.get_working_memory()
    assert set(result[0].keys()) == {"role", "content"}


# ─── Importance Scoring ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_importance_uses_local_model():
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = {"content": "0.8"}
    mock_llm.config = MagicMock()
    mock_llm.config.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"

    mm = MemoryManager(llm_client=mock_llm)
    score = await mm._score_importance("My father was diagnosed with cancer today.")
    assert 0.0 <= score <= 1.0
    mock_llm.chat.assert_called_once()


@pytest.mark.asyncio
async def test_score_importance_fallback_on_error():
    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = Exception("LLM unavailable")
    mock_llm.config = MagicMock()
    mock_llm.config.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"

    mm = MemoryManager(llm_client=mock_llm)
    score = await mm._score_importance("some content")
    assert score == 0.5


@pytest.mark.asyncio
async def test_score_importance_no_llm_fallback():
    mm = MemoryManager(llm_client=None)
    score = await mm._score_importance("anything")
    assert score == 0.5


# ─── Context Building ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_context_empty_when_no_results():
    mm = MemoryManager()
    mm.search_recall = AsyncMock(return_value=[])
    mm.search_archival = AsyncMock(return_value=[])
    result = await mm.build_context_for_query("anything")
    assert result == ""


@pytest.mark.asyncio
async def test_build_context_formats_memories():
    mm = MemoryManager()
    mm.search_recall = AsyncMock(return_value=[{
        "id": 1,
        "content": "User's daughter is named Emma",
        "importance_score": 0.8,
        "created_at": "2026-01-01T00:00:00",
    }])
    mm.search_archival = AsyncMock(return_value=[])
    result = await mm.build_context_for_query("family")
    assert "Emma" in result
    assert "[Relevant memories" in result


@pytest.mark.asyncio
async def test_build_context_no_db_returns_empty():
    mm = MemoryManager(llm_client=None, db_session_factory=None)
    result = await mm.build_context_for_query("test")
    assert result == ""


# ─── BM25 keyword search (#27) ────────────────────────────────────────────


def _make_session_factory_returning(rows: list[dict]):
    """Build a fake async-context-manager session factory that records the
    SQL it was given and returns the supplied mapping rows."""
    captured: dict = {}

    class _FakeMappings:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def mappings(self):
            return _FakeMappings(self._rows)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, sql, params=None):
            captured["sql"] = str(sql)
            captured["params"] = params or {}
            return _FakeResult(rows)

    def factory():
        return _FakeSession()

    return factory, captured


@pytest.mark.asyncio
async def test_bm25_search_no_db_returns_empty():
    mm = MemoryManager(db_session_factory=None)
    assert await mm.search_bm25("anything") == []


@pytest.mark.asyncio
async def test_bm25_search_empty_query_returns_empty():
    factory, _ = _make_session_factory_returning([])
    mm = MemoryManager(db_session_factory=factory)
    assert await mm.search_bm25("") == []
    assert await mm.search_bm25("   ") == []


@pytest.mark.asyncio
async def test_bm25_search_uses_tsvector_and_ts_rank_cd():
    """SQL must hit `content_tsv` with `plainto_tsquery` and rank by
    `ts_rank_cd` so the GIN index is exercised and the scoring is
    cover-density (proximity-aware), not plain ts_rank."""
    factory, captured = _make_session_factory_returning([])
    mm = MemoryManager(db_session_factory=factory)
    await mm.search_bm25("matthew design studio", limit=5)
    sql = captured["sql"].lower()
    assert "content_tsv" in sql
    assert "plainto_tsquery" in sql
    assert "ts_rank_cd" in sql
    assert "@@" in sql
    assert "type = 'recall'" in sql
    assert captured["params"] == {"q": "matthew design studio", "k": 5}


@pytest.mark.asyncio
async def test_bm25_search_normalises_rows_to_dicts():
    rows = [
        {
            "id": 4,
            "content": "Matthew started a new role at the design studio.",
            "importance_score": 0.7,
            "created_at": _dt(2026, 4, 25, 12, 0, 0),
            "score": 0.42,
        },
        {
            "id": 5,
            "content": "Matthew shipped the onboarding redesign last week.",
            "importance_score": 0.6,
            "created_at": _dt(2026, 4, 30, 9, 0, 0),
            "score": 0.21,
        },
    ]
    factory, _ = _make_session_factory_returning(rows)
    mm = MemoryManager(db_session_factory=factory)
    out = await mm.search_bm25("matthew", limit=2)
    assert [r["id"] for r in out] == [4, 5]
    assert out[0]["score"] == pytest.approx(0.42)
    # ISO-8601 string makes results JSON-serialisable for traces / inspector.
    assert out[0]["created_at"] == "2026-04-25T12:00:00"
    assert all(isinstance(r["importance_score"], float) for r in out)


@pytest.mark.asyncio
async def test_bm25_search_clamps_invalid_limit():
    """A non-positive limit reaches Postgres as `LIMIT -1` which errors;
    clamp it to the default rather than fault."""
    factory, captured = _make_session_factory_returning([])
    mm = MemoryManager(db_session_factory=factory)
    await mm.search_bm25("matthew", limit=-1)
    assert captured["params"]["k"] == 10
    await mm.search_bm25("matthew", limit=0)
    assert captured["params"]["k"] == 10


@pytest.mark.asyncio
async def test_bm25_search_swallows_db_errors():
    """Tools must return [] on transient DB errors rather than raising —
    matches the contract used by every other MemoryManager search method."""

    class _BoomSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, sql, params=None):
            raise RuntimeError("postgres exploded")

    mm = MemoryManager(db_session_factory=lambda: _BoomSession())
    assert await mm.search_bm25("matthew") == []
