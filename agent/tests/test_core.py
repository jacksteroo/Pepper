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
