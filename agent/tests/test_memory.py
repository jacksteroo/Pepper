import pytest
from unittest.mock import AsyncMock, MagicMock
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
