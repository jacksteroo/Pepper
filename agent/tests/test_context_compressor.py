"""
Tests for Phase 3.2 — Context Compression.

Covers:
- Token estimation
- Compression threshold detection
- Compress() preserves anchor turns verbatim
- Compress() produces a summary system message
- Privacy invariant: summarization never calls a frontier model
- local_only flag in ModelClient.chat() overrides frontier model to local
- Pre-compression turns saved to recall memory
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_mock_config(context_tokens: int = 8192):
    cfg = MagicMock()
    cfg.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"
    cfg.MODEL_CONTEXT_TOKENS = context_tokens
    return cfg


def make_mock_llm(summary: str = "Summary of earlier turns."):
    llm = AsyncMock()
    llm.chat.return_value = {"content": summary, "tool_calls": []}
    return llm


def make_mock_memory():
    mem = AsyncMock()
    mem.save_to_recall = AsyncMock()
    return mem


def _conversation(n_turns: int) -> list[dict]:
    """Build n_turns of user/assistant pairs (no system message)."""
    msgs = []
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"User turn {i}"})
        msgs.append({"role": "assistant", "content": f"Assistant reply {i}"})
    return msgs


def _with_system(msgs: list[dict]) -> list[dict]:
    return [{"role": "system", "content": "You are Pepper."}] + msgs


# ── Token estimation ──────────────────────────────────────────────────────────

def test_estimate_tokens_empty():
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(None, None, make_mock_config())
    assert compressor.estimate_tokens([]) == 0


def test_estimate_tokens_basic():
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(None, None, make_mock_config())
    messages = [{"role": "user", "content": "Hello"}]  # 5 chars → 1 token
    assert compressor.estimate_tokens(messages) == 1


def test_estimate_tokens_sums_all_roles():
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(None, None, make_mock_config())
    messages = [
        {"role": "system", "content": "A" * 400},   # 100 tokens
        {"role": "user", "content": "B" * 400},      # 100 tokens
        {"role": "assistant", "content": "C" * 400}, # 100 tokens
    ]
    assert compressor.estimate_tokens(messages) == 300


def test_estimate_tokens_handles_none_content():
    from agent.context_compressor import ContextCompressor

    compressor = ContextCompressor(None, None, make_mock_config())
    messages = [{"role": "user", "content": None}, {"role": "assistant"}]
    assert compressor.estimate_tokens(messages) == 0


# ── Needs compression ─────────────────────────────────────────────────────────

def test_needs_compression_below_threshold():
    from agent.context_compressor import ContextCompressor

    # context_tokens=8192, threshold=80% → 6553 tokens
    # 100 chars = 25 tokens → no compression
    cfg = make_mock_config(context_tokens=8192)
    compressor = ContextCompressor(None, None, cfg)
    messages = [{"role": "user", "content": "A" * 100}]
    assert compressor.needs_compression(messages) is False


def test_needs_compression_above_threshold():
    from agent.context_compressor import ContextCompressor

    # context_tokens=100, threshold=80 tokens → 320 chars triggers compression
    cfg = make_mock_config(context_tokens=100)
    compressor = ContextCompressor(None, None, cfg)
    # 400 chars = 100 tokens → exceeds 80-token threshold
    messages = [{"role": "user", "content": "A" * 400}]
    assert compressor.needs_compression(messages) is True


def test_needs_compression_exactly_at_threshold():
    from agent.context_compressor import ContextCompressor

    # 80 tokens = 320 chars → at-threshold means NOT triggered (> not >=)
    cfg = make_mock_config(context_tokens=100)
    compressor = ContextCompressor(None, None, cfg)
    messages = [{"role": "user", "content": "A" * 320}]
    assert compressor.needs_compression(messages) is False


# ── compress() behaviour ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_skips_when_few_turns():
    """compress() returns the original list when there is nothing to compress."""
    from agent.context_compressor import ContextCompressor

    llm = make_mock_llm()
    cfg = make_mock_config()
    compressor = ContextCompressor(llm, make_mock_memory(), cfg)

    # Only 3 turns — less than default anchor of 6
    messages = _with_system(_conversation(3))
    result = await compressor.compress(messages, anchor_turns=6)

    assert result == messages
    llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_compress_preserves_anchor_turns():
    """The most recent anchor_turns*2 conversation messages are unchanged."""
    from agent.context_compressor import ContextCompressor

    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), make_mock_memory(), cfg)

    # 10 turns total, anchor=4 → keep last 8 conv messages
    conv = _conversation(10)
    messages = _with_system(conv)
    result = await compressor.compress(messages, anchor_turns=4)

    # All anchor messages must appear at the end of the result
    anchor = conv[-8:]
    result_conv = [m for m in result if m.get("role") != "system"]
    assert result_conv == anchor


@pytest.mark.asyncio
async def test_compress_inserts_summary_system_message():
    """Compressed result contains a summary system message."""
    from agent.context_compressor import ContextCompressor

    cfg = make_mock_config()
    compressor = ContextCompressor(
        make_mock_llm("Earlier: user asked about the meeting."),
        make_mock_memory(),
        cfg,
    )

    messages = _with_system(_conversation(10))
    result = await compressor.compress(messages, anchor_turns=4)

    summary_msgs = [
        m for m in result
        if m.get("role") == "system" and m.get("content", "").startswith("[Summary")
    ]
    assert len(summary_msgs) == 1
    assert "Earlier: user asked about the meeting." in summary_msgs[0]["content"]


@pytest.mark.asyncio
async def test_compress_preserves_original_system_message():
    """The original system prompt is kept in the compressed result."""
    from agent.context_compressor import ContextCompressor

    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), make_mock_memory(), cfg)

    messages = _with_system(_conversation(10))
    result = await compressor.compress(messages, anchor_turns=4)

    system_msgs = [m for m in result if m.get("role") == "system"]
    originals = [m for m in system_msgs if m["content"] == "You are Pepper."]
    assert len(originals) == 1


@pytest.mark.asyncio
async def test_compress_result_shorter_than_original():
    """Compressed message list is shorter than the input."""
    from agent.context_compressor import ContextCompressor

    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), make_mock_memory(), cfg)

    messages = _with_system(_conversation(15))
    result = await compressor.compress(messages, anchor_turns=4)

    assert len(result) < len(messages)


# ── Privacy invariant ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summarize_always_uses_local_model():
    """_summarize() must call llm.chat with a local/ model string."""
    from agent.context_compressor import ContextCompressor

    llm = make_mock_llm()
    cfg = make_mock_config()
    compressor = ContextCompressor(llm, make_mock_memory(), cfg)

    await compressor._summarize([
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ])

    llm.chat.assert_called_once()
    call_kwargs = llm.chat.call_args
    model_used = call_kwargs.kwargs.get("model") or call_kwargs.args[0] if call_kwargs.args else None
    # model is passed as keyword argument
    model_used = llm.chat.call_args.kwargs.get("model", "")
    assert model_used.startswith("local/"), (
        f"Privacy invariant violated: summarization called with model={model_used!r}"
    )


@pytest.mark.asyncio
async def test_summarize_passes_local_only_flag():
    """_summarize() must pass local_only=True to llm.chat()."""
    from agent.context_compressor import ContextCompressor

    llm = make_mock_llm()
    cfg = make_mock_config()
    compressor = ContextCompressor(llm, make_mock_memory(), cfg)

    await compressor._summarize([{"role": "user", "content": "Test"}])

    llm.chat.assert_called_once()
    local_only = llm.chat.call_args.kwargs.get("local_only", False)
    assert local_only is True, (
        "Privacy invariant violated: local_only=True not passed to llm.chat()"
    )


# ── local_only enforcement in ModelClient ────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_local_only_overrides_frontier_model():
    """ModelClient.chat(local_only=True) must route to local model even if a
    frontier model string is passed."""
    from agent.llm import ModelClient

    cfg = MagicMock()
    cfg.ANTHROPIC_API_KEY = None  # no Anthropic client
    cfg.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"
    cfg.OLLAMA_BASE_URL = "http://localhost:11434"

    client = ModelClient(cfg)

    with patch.object(client, "_ollama_chat", new_callable=AsyncMock) as mock_ollama:
        mock_ollama.return_value = {"content": "ok", "tool_calls": []}

        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-opus-4-6",   # frontier — should be overridden
            local_only=True,
        )

        mock_ollama.assert_called_once()
        actual_model = mock_ollama.call_args.args[0]
        assert actual_model == "hermes-4.3-36b-tools:latest", (
            f"local_only=True did not override to local model, got: {actual_model!r}"
        )


@pytest.mark.asyncio
async def test_llm_local_only_false_allows_frontier():
    """ModelClient.chat(local_only=False) with a local model still uses local."""
    from agent.llm import ModelClient

    cfg = MagicMock()
    cfg.ANTHROPIC_API_KEY = None
    cfg.DEFAULT_LOCAL_MODEL = "hermes-4.3-36b-tools:latest"
    cfg.OLLAMA_BASE_URL = "http://localhost:11434"

    client = ModelClient(cfg)

    with patch.object(client, "_ollama_chat", new_callable=AsyncMock) as mock_ollama:
        mock_ollama.return_value = {"content": "ok", "tool_calls": []}

        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="local/hermes-4.3-36b-tools:latest",
            local_only=False,
        )

        mock_ollama.assert_called_once()
        assert mock_ollama.call_args.args[0] == "hermes-4.3-36b-tools:latest"


# ── Recall memory preservation ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_saves_old_turns_to_recall():
    """Compressed-out messages are saved to recall memory."""
    from agent.context_compressor import ContextCompressor

    mem = make_mock_memory()
    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), mem, cfg)

    # 10 turns, keep 4 → 6 turns (12 messages) compressed out
    messages = _with_system(_conversation(10))
    await compressor.compress(messages, anchor_turns=4)

    # save_to_recall should be called for each user + assistant message compressed
    assert mem.save_to_recall.call_count == 12  # 6 turns × 2 messages each


@pytest.mark.asyncio
async def test_save_to_recall_skips_non_conversation_roles():
    """_save_to_recall() only saves user and assistant messages."""
    from agent.context_compressor import ContextCompressor

    mem = make_mock_memory()
    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), mem, cfg)

    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "User message"},
        {"role": "tool", "content": "Tool result"},
        {"role": "assistant", "content": "Assistant reply"},
    ]
    await compressor._save_to_recall(messages)

    # Only user + assistant should be saved
    assert mem.save_to_recall.call_count == 2
    saved_contents = [str(c) for c in mem.save_to_recall.call_args_list]
    assert all("system" not in s.lower() for s in saved_contents)
    assert all("tool" not in s.lower() for s in saved_contents)


@pytest.mark.asyncio
async def test_compress_handles_recall_save_failure():
    """A recall-save failure doesn't abort compression."""
    from agent.context_compressor import ContextCompressor

    mem = make_mock_memory()
    mem.save_to_recall = AsyncMock(side_effect=Exception("DB down"))
    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), mem, cfg)

    messages = _with_system(_conversation(10))
    # Should not raise
    result = await compressor.compress(messages, anchor_turns=4)
    assert result is not None


# ── Summarization fallback ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summarize_fallback_on_llm_failure():
    """_summarize() returns a truncated concatenation when the LLM fails."""
    from agent.context_compressor import ContextCompressor

    llm = AsyncMock()
    llm.chat = AsyncMock(side_effect=Exception("Ollama unreachable"))
    cfg = make_mock_config()
    compressor = ContextCompressor(llm, make_mock_memory(), cfg)

    summary = await compressor._summarize([
        {"role": "user", "content": "What should I focus on?"},
        {"role": "assistant", "content": "Focus on the quarterly review."},
    ])

    # Fallback: non-empty string containing some content from the messages
    assert isinstance(summary, str)
    assert len(summary) > 0


@pytest.mark.asyncio
async def test_summarize_empty_messages():
    """_summarize() handles an empty message list gracefully."""
    from agent.context_compressor import ContextCompressor

    cfg = make_mock_config()
    compressor = ContextCompressor(make_mock_llm(), make_mock_memory(), cfg)

    summary = await compressor._summarize([])
    assert "no conversation content" in summary.lower()
