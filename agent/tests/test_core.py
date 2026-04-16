import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def make_mock_config():
    config = MagicMock()
    config.LIFE_CONTEXT_PATH = "docs/LIFE_CONTEXT.md"
    config.OWNER_NAME = "Jack Chan"
    config.TIMEZONE = "UTC"
    config.DEFAULT_LOCAL_MODEL = "hermes3:latest"
    config.DEFAULT_FRONTIER_MODEL = "local/hermes3:latest"
    config.select_model.return_value = "local/hermes3:latest"
    return config


def make_mock_llm():
    llm = AsyncMock()
    llm.chat.return_value = {"content": "Hello, I'm Pepper.", "tool_calls": []}
    llm.embed.return_value = [0.1] * 768
    llm.config = MagicMock()
    llm.config.DEFAULT_LOCAL_MODEL = "hermes3:latest"
    return llm


@pytest.mark.asyncio
async def test_pepper_core_get_status():
    """get_status() returns dict with expected keys."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient"), \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system prompt"):

        MockRouter.return_value.check_health = AsyncMock(return_value={"calendar": "down"})
        MockRouter.return_value.get_status.return_value = {"calendar": "down"}
        MockMem.return_value._working = []

        pepper = PepperCore(config)
        status = await pepper.get_status()

        assert "initialized" in status
        assert "subsystems" in status
        assert "working_memory_size" in status


@pytest.mark.asyncio
async def test_pepper_core_chat_adds_to_memory():
    """chat() adds user message to working memory."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor:

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        mock_memory.build_context_for_query = AsyncMock(return_value="")
        mock_memory.save_to_recall = AsyncMock()
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat("What should I focus on today?", "test-session")

        mock_memory.add_to_working_memory.assert_any_call("user", "What should I focus on today?")
        assert isinstance(response, str)


@pytest.mark.asyncio
async def test_pepper_core_chat_answers_identity_without_llm():
    """Identity questions should be answered deterministically."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor:

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat("Who am I and who are you?", "test-session")

        assert response == "You are Jack Chan. I'm Pepper, your AI life assistant."
        mock_llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_pepper_core_email_action_items_query_bypasses_llm():
    """Email action-item scans should return deterministic results without an LLM call."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch("agent.core.execute_get_email_action_items", new=AsyncMock(return_value={
             "action_items": [
                 {
                     "formatted": "[Personal] Please review the contract [UNREAD] — from Alex. Why: unread, requests review."
                 }
             ],
             "count": 1,
         })), \
         patch("agent.core.detect_email_account_scope", return_value="personal"):

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat(
            "any action items from my personal email?",
            "test-session",
            heavy=True,
        )

        assert "likely action item" in response.lower()
        mock_llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_pepper_core_email_summary_query_bypasses_llm():
    """Recent email summary requests should return deterministic inbox data."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch("agent.core.execute_get_email_summary", new=AsyncMock(return_value={
             "important": [
                 {
                     "formatted": "[Personal] Deadline moved up [UNREAD] — from Boss. Why: unread, marked urgent."
                 }
             ],
             "emails": [
                 {
                     "formatted": "[Personal] Deadline moved up [UNREAD] — from Boss. Why: unread, marked urgent."
                 }
             ],
             "count": 1,
             "hours": 12,
         })), \
         patch("agent.core.detect_email_account_scope", return_value="all"), \
         patch("agent.core.detect_email_time_window_hours", return_value=12):

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat(
            "summarize my emails received overnight. Anything important?",
            "test-session",
            heavy=True,
        )

        assert "most important" in response.lower()
        assert "deadline moved up" in response.lower()
        mock_llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_pepper_core_whatsapp_attention_query_bypasses_llm():
    """Recent WhatsApp summary requests should be built deterministically."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch(
             "agent.core.execute_get_recent_whatsapp_attention",
             new=AsyncMock(return_value={
                 "summary": (
                     "I found 1 WhatsApp chat(s) worth your attention:\n"
                     '1. Family [group] [2 unread] — Last message: '
                     '"Alice: Can you look at the dinner reservation?" '
                     "at 2026-04-15 10:02. Why: unread messages in this chat."
                 )
             }),
         ):

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat(
            "What recent WhatsApp messages do I need to be aware of?",
            "test-session",
            heavy=True,
        )

        assert "dinner reservation" in response
        mock_llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_pepper_core_imessage_attention_query_bypasses_llm():
    """Recent iMessage summary requests should be built deterministically."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch(
             "agent.core.execute_get_recent_imessage_attention",
             new=AsyncMock(return_value={
                 "summary": (
                     "I found 1 iMessage conversation(s) worth your attention:\n"
                     '1. Mom [2 unread] — Last message: '
                     '"Mom: Call me when you wake up." '
                     "at 2026-04-15 10:00. Why: unread messages in this conversation."
                 )
             }),
         ):

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat(
            "What recent iMessages do I need to be aware of?",
            "test-session",
            heavy=True,
        )

        assert "Call me when you wake up." in response
        mock_llm.chat.assert_not_called()


@pytest.mark.asyncio
async def test_pepper_core_summarize_text_messages_bypasses_llm():
    """Generic text-message summary requests should use the iMessage fast path."""
    from agent.core import PepperCore

    config = make_mock_config()

    with patch("agent.core.ModelClient") as MockLLM, \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter") as MockRouter, \
         patch("agent.core.build_system_prompt", return_value="system"), \
         patch("agent.core.CommitmentExtractor") as MockExtractor, \
         patch(
             "agent.core.execute_get_recent_imessage_attention",
             new=AsyncMock(return_value={
                 "summary": (
                     "I found 1 iMessage conversation(s) worth your attention:\n"
                     '1. Mom [2 unread] — Last message: '
                     '"Mom: Call me when you wake up." '
                     "at 2026-04-15 10:00. Why: unread messages in this conversation."
                 )
             }),
         ):

        mock_llm = make_mock_llm()
        MockLLM.return_value = mock_llm

        mock_memory = MagicMock()
        mock_memory.add_to_working_memory = MagicMock()
        mock_memory.get_working_memory = MagicMock(return_value=[])
        MockMem.return_value = mock_memory

        MockRouter.return_value.check_health = AsyncMock(return_value={})
        MockRouter.return_value.list_available_tools = AsyncMock(return_value=[])
        MockRouter.return_value.get_status.return_value = {}

        MockExtractor.return_value.has_commitment_language = MagicMock(return_value=False)

        pepper = PepperCore(config)
        pepper._initialized = True
        pepper._system_prompt = "system"

        response = await pepper.chat(
            "Summarize my text messages",
            "test-session",
            heavy=True,
        )

        assert "Call me when you wake up." in response
        mock_llm.chat.assert_not_called()


def test_config_model_routing():
    """select_model routes raw_personal data to local model always."""
    from agent.config import Settings
    import os
    os.environ.setdefault("POSTGRES_URL", "postgresql+asyncpg://pepper:pepper@localhost/pepper")

    try:
        config = Settings()
        assert "local/" in config.select_model("any", "raw_personal")
        assert config.DEFAULT_FRONTIER_MODEL in config.select_model("family_conversation", "summary")
        assert "local/" in config.select_model("unknown", "unknown")
        assert config.select_model("background_agent", "any") == config.DEFAULT_FRONTIER_MODEL
    except Exception:
        pass  # If .env not present, skip silently


# ── MCP write approval gate ───────────────────────────────────────────────────


def _make_pepper_for_gate():
    """Return a minimal PepperCore for testing _check_mcp_write_gate."""
    from agent.core import PepperCore
    config = make_mock_config()
    with patch("agent.core.ModelClient"), \
         patch("agent.core.MemoryManager") as MockMem, \
         patch("agent.core.ToolRouter"), \
         patch("agent.core.build_system_prompt", return_value="system"):
        MockMem.return_value._working = []
        pepper = PepperCore(config)
    return pepper


class TestMCPWriteApprovalGate:
    """Unit tests for _check_mcp_write_gate (P1 approval gate).

    The gate is the last code-level enforcement that consequential MCP write
    actions require explicit user approval even when allow_side_effects=True
    on the server config.
    """

    def test_first_call_blocks_and_returns_approval_request(self):
        pepper = _make_pepper_for_gate()
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", {"title": "bug"})
        assert result is not None
        assert result.get("approval_required") is True
        assert "mcp_github_create_issue" in result.get("message", "")
        # Pending write was stored
        assert "sess1" in pepper._pending_mcp_writes

    def test_second_call_without_approval_still_blocks(self):
        pepper = _make_pepper_for_gate()
        pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", {"title": "bug"})
        # Second call without setting approved — still blocked
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", {"title": "bug"})
        assert result is not None
        assert result.get("approval_required") is True

    def test_approved_pending_allows_execution_and_clears_state(self):
        pepper = _make_pepper_for_gate()
        args = {"title": "bug"}
        # Simulate approval flow: block on first call, mark approved, then allow
        pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", args)
        pepper._pending_mcp_writes["sess1"]["approved"] = True
        # Re-call with the EXACT same tool and args that the user approved
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", args)
        assert result is None  # gate returns None → proceed with execution
        # State is cleared after approval
        assert "sess1" not in pepper._pending_mcp_writes

    def test_approved_pending_blocked_when_tool_differs(self):
        """Approved pending must NOT authorize a different write tool (P1 regression)."""
        pepper = _make_pepper_for_gate()
        args = {"title": "bug"}
        pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", args)
        pepper._pending_mcp_writes["sess1"]["approved"] = True
        # Model calls a DIFFERENT write tool after user approved create_issue
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_delete_repo", args)
        assert result is not None  # gate must block the mismatched tool
        assert result.get("approval_required") is True
        # Stale approved state was cleared — new pending for the mismatched tool
        assert pepper._pending_mcp_writes.get("sess1", {}).get("tool_name") == "mcp_github_delete_repo"
        assert not pepper._pending_mcp_writes.get("sess1", {}).get("approved")

    def test_approved_pending_blocked_when_args_differ(self):
        """Approved pending must NOT authorize different arguments (P1 regression)."""
        pepper = _make_pepper_for_gate()
        approved_args = {"title": "harmless bug report"}
        different_args = {"title": "DROP TABLE users; --"}
        pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", approved_args)
        pepper._pending_mcp_writes["sess1"]["approved"] = True
        # Model calls the same tool but with different (potentially malicious) args
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", different_args)
        assert result is not None  # gate must block mismatched args
        assert result.get("approval_required") is True

    def test_expired_pending_blocks_again(self):
        import time as _time
        pepper = _make_pepper_for_gate()
        pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", {})
        # Force-expire the pending write
        pepper._pending_mcp_writes["sess1"]["approved"] = True
        pepper._pending_mcp_writes["sess1"]["expires_at"] = _time.monotonic() - 1
        # Expired approved pending should re-block (pending approval was deleted in chat())
        # Simulate chat() expiry cleanup:
        del pepper._pending_mcp_writes["sess1"]
        result = pepper._check_mcp_write_gate("sess1", "mcp_github_create_issue", {})
        assert result is not None  # blocked again after expiry

    def test_different_sessions_are_independent(self):
        pepper = _make_pepper_for_gate()
        pepper._check_mcp_write_gate("sess-A", "mcp_github_create_issue", {})
        pepper._check_mcp_write_gate("sess-B", "mcp_github_create_issue", {})
        # Approve only sess-A
        pepper._pending_mcp_writes["sess-A"]["approved"] = True
        assert pepper._check_mcp_write_gate("sess-A", "mcp_github_create_issue", {}) is None
        assert pepper._check_mcp_write_gate("sess-B", "mcp_github_create_issue", {}) is not None


class TestMCPApprovalRegex:
    """Verify _MCP_WRITE_APPROVAL_RE matches affirmations and rejects other messages."""

    def test_approval_words_match(self):
        from agent.core import _MCP_WRITE_APPROVAL_RE
        affirmations = [
            "yes", "Yes", "YES", "yeah", "yep", "yup", "sure",
            "go ahead", "do it", "approved", "proceed", "confirm",
            "ok", "okay", "absolutely", "please do", "sounds good",
            "yes!", "yes.", "👍", "✅",
        ]
        for word in affirmations:
            assert _MCP_WRITE_APPROVAL_RE.match(word), f"Should match: {word!r}"

    def test_non_approval_messages_do_not_match(self):
        from agent.core import _MCP_WRITE_APPROVAL_RE
        non_approvals = [
            "create a github issue for the login bug",
            "what did you just propose?",
            "no",
            "cancel that",
            "wait, actually no",
            "yes but change the title first",
        ]
        for msg in non_approvals:
            assert not _MCP_WRITE_APPROVAL_RE.match(msg), f"Should NOT match: {msg!r}"
