"""
Phase 5 — Tool Router MCP integration tests.

Tests that the ToolRouter correctly:
  - Detects MCP tools by name
  - Parses qualified names
  - Enforces privacy before MCP calls
  - Returns MCP tools for LLM injection
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.tool_router import ToolRouter
from agent.mcp_client import MCPClient, MCPToolInfo, MCPServerConnection, MCPServerConfig
from agent.mcp_audit import MCPPrivacyViolation


@pytest.fixture
def router_with_mcp():
    """Router with a mock MCP client that has a test tool registered."""
    router = ToolRouter()
    client = MCPClient()

    # Register a local tool
    local_info = MCPToolInfo(
        name="read_file",
        description="Read a local file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        server_name="filesystem",
        trust_level="local",
    )
    client._tool_index["mcp_filesystem_read_file"] = local_info
    client._servers["filesystem"] = MCPServerConnection(
        config=MCPServerConfig(name="filesystem", command="test", trust_level="local"),
        status="connected",
    )
    client._servers["filesystem"].tools = [local_info]

    # Register an external tool
    ext_info = MCPToolInfo(
        name="create_issue",
        description="Create a GitHub issue",
        input_schema={"type": "object", "properties": {"title": {"type": "string"}}},
        server_name="github",
        trust_level="external",
    )
    client._tool_index["mcp_github_create_issue"] = ext_info
    client._servers["github"] = MCPServerConnection(
        config=MCPServerConfig(name="github", command="test", trust_level="external"),
        status="connected",
    )
    client._servers["github"].tools = [ext_info]

    router.set_mcp_client(client)
    return router


class TestMCPToolDetection:

    def test_is_mcp_tool_true(self, router_with_mcp):
        assert router_with_mcp.is_mcp_tool("mcp_filesystem_read_file") is True

    def test_is_mcp_tool_false_native(self, router_with_mcp):
        assert router_with_mcp.is_mcp_tool("get_upcoming_events") is False

    def test_is_mcp_tool_false_no_client(self):
        router = ToolRouter()
        assert router.is_mcp_tool("mcp_anything") is False

    def test_parse_mcp_tool_name(self, router_with_mcp):
        result = router_with_mcp.parse_mcp_tool_name("mcp_filesystem_read_file")
        assert result == ("filesystem", "read_file")

    def test_parse_mcp_tool_name_external(self, router_with_mcp):
        result = router_with_mcp.parse_mcp_tool_name("mcp_github_create_issue")
        assert result == ("github", "create_issue")

    def test_parse_native_tool_returns_none(self, router_with_mcp):
        assert router_with_mcp.parse_mcp_tool_name("get_upcoming_events") is None

    def test_is_mcp_read_only_tool_false_by_default(self, router_with_mcp):
        """Tools without readOnlyHint are not read-only (conservative default)."""
        assert router_with_mcp.is_mcp_read_only_tool("mcp_github_create_issue") is False

    def test_is_mcp_read_only_tool_true_when_annotated(self, router_with_mcp):
        """Tools with read_only=True are correctly identified."""
        read_info = MCPToolInfo(
            name="search_issues", description="Search", input_schema={},
            server_name="github", trust_level="external", read_only=True,
        )
        router_with_mcp._mcp_client._tool_index["mcp_github_search_issues"] = read_info
        assert router_with_mcp.is_mcp_read_only_tool("mcp_github_search_issues") is True

    def test_is_mcp_read_only_tool_false_without_client(self):
        router = ToolRouter()
        assert router.is_mcp_read_only_tool("mcp_anything") is False


class TestMCPToolList:

    def test_get_mcp_tools_returns_anthropic_format(self, router_with_mcp):
        tools = router_with_mcp.get_mcp_tools()
        assert len(tools) == 2
        names = {t["function"]["name"] for t in tools}
        assert "mcp_filesystem_read_file" in names
        assert "mcp_github_create_issue" in names

    def test_mcp_tools_have_metadata(self, router_with_mcp):
        tools = router_with_mcp.get_mcp_tools()
        for t in tools:
            assert t["_mcp"] is True
            assert t["_mcp_server"] in ("filesystem", "github")
            assert t["_trust_level"] in ("local", "external")

    def test_no_mcp_tools_without_client(self):
        router = ToolRouter()
        assert router.get_mcp_tools() == []


class TestMCPToolRouting:

    @pytest.mark.asyncio
    async def test_call_mcp_tool_no_client(self):
        router = ToolRouter()
        result = await router.call_mcp_tool("mcp_test_tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_call_mcp_tool_unknown(self, router_with_mcp):
        result = await router_with_mcp.call_mcp_tool("mcp_nonexistent_tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_call_mcp_tool_privacy_violation(self, router_with_mcp):
        """External server cannot call raw personal data tools.

        The call is blocked by whichever gate fires first:
        - the side-effects gate (allow_side_effects=False + not read_only), or
        - the privacy trust-boundary check (RAW_PERSONAL tool on external server).
        Both are correct outcomes; the test asserts on the error presence only.
        To exercise the privacy gate specifically (after the side-effects gate has
        been bypassed), we mark the tool read_only=True so only the trust-boundary
        check applies.
        """
        # read_only=True bypasses the side-effects gate so the privacy check fires.
        info = MCPToolInfo(
            name="get_recent_imessages",
            description="Read iMessages",
            input_schema={},
            server_name="github",
            trust_level="external",
            read_only=True,
        )
        router_with_mcp._mcp_client._tool_index["mcp_github_get_recent_imessages"] = info

        result = await router_with_mcp.call_mcp_tool(
            "mcp_github_get_recent_imessages", {}
        )
        assert "error" in result
        assert "PRIVACY VIOLATION" in result["error"]


class TestMCPToolSuccessPath:

    @pytest.mark.asyncio
    async def test_call_mcp_tool_success(self, router_with_mcp):
        """Successful MCP tool call returns result dict."""
        # Mock the underlying client call_tool to return a successful result
        router_with_mcp._mcp_client.call_tool = AsyncMock(
            return_value={"result": "file contents here"}
        )
        result = await router_with_mcp.call_mcp_tool("mcp_filesystem_read_file", {"path": "/tmp/test.txt"})
        assert "error" not in result
        assert result == {"result": "file contents here"}

    @pytest.mark.asyncio
    async def test_call_mcp_tool_returns_error_from_server(self, router_with_mcp):
        """If the MCP server returns an error, it propagates correctly."""
        router_with_mcp._mcp_client.call_tool = AsyncMock(
            return_value={"error": "permission denied"}
        )
        result = await router_with_mcp.call_mcp_tool("mcp_filesystem_read_file", {"path": "/etc/shadow"})
        assert "error" in result
        assert result["error"] == "permission denied"

    @pytest.mark.asyncio
    async def test_audit_log_called_on_success(self, router_with_mcp):
        """Audit log is called for successful tool calls."""
        from unittest.mock import patch
        router_with_mcp._mcp_client.call_tool = AsyncMock(
            return_value={"result": "ok"}
        )
        with patch("agent.tool_router.log_mcp_call") as mock_log:
            await router_with_mcp.call_mcp_tool("mcp_filesystem_read_file", {})
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args[1]
            assert call_kwargs["success"] is True
            assert call_kwargs["tool_name"] == "read_file"

    @pytest.mark.asyncio
    async def test_audit_log_called_on_error(self, router_with_mcp):
        """Audit log records failure for tool calls that return errors."""
        from unittest.mock import patch
        router_with_mcp._mcp_client.call_tool = AsyncMock(
            return_value={"error": "something went wrong"}
        )
        with patch("agent.tool_router.log_mcp_call") as mock_log:
            await router_with_mcp.call_mcp_tool("mcp_filesystem_read_file", {})
            mock_log.assert_called_once()
            call_kwargs = mock_log.call_args[1]
            assert call_kwargs["success"] is False
            assert call_kwargs["error"] == "something went wrong"


class TestMCPWriteToolGate:
    """Regression tests for the MCP write-tool approval gate (Fix 2 / P1).

    Write tools on non-local servers with allow_side_effects=False must be
    blocked by the router before they reach the MCP client.  The gate must
    NOT apply to local servers, and must NOT apply when allow_side_effects=True
    or when the tool is declared read-only via MCP readOnlyHint.
    """

    @pytest.mark.asyncio
    async def test_write_tool_blocked_on_external_server_by_default(self, router_with_mcp):
        """External server with allow_side_effects=False blocks write tools."""
        result = await router_with_mcp.call_mcp_tool("mcp_github_create_issue", {})
        assert "error" in result
        assert "allow_side_effects" in result["error"]

    @pytest.mark.asyncio
    async def test_write_tool_allowed_when_side_effects_enabled(self, router_with_mcp):
        """Setting allow_side_effects=True on the server config permits write tools."""
        router_with_mcp._mcp_client.servers["github"].config.allow_side_effects = True
        router_with_mcp._mcp_client.call_tool = AsyncMock(return_value={"result": "created"})
        result = await router_with_mcp.call_mcp_tool("mcp_github_create_issue", {"title": "bug"})
        assert "error" not in result
        assert result == {"result": "created"}

    @pytest.mark.asyncio
    async def test_read_only_tool_bypasses_gate_even_without_side_effects(self, router_with_mcp):
        """A tool with read_only=True is allowed even when allow_side_effects=False."""
        # Register a read-only search tool on the external github server
        read_info = MCPToolInfo(
            name="search_issues",
            description="Search GitHub issues",
            input_schema={},
            server_name="github",
            trust_level="external",
            read_only=True,
        )
        router_with_mcp._mcp_client._tool_index["mcp_github_search_issues"] = read_info
        router_with_mcp._mcp_client.call_tool = AsyncMock(return_value={"result": "issues list"})
        result = await router_with_mcp.call_mcp_tool("mcp_github_search_issues", {"q": "bug"})
        assert "error" not in result
        assert result == {"result": "issues list"}

    @pytest.mark.asyncio
    async def test_local_server_bypasses_write_gate(self, router_with_mcp):
        """Local servers are fully trusted — the write gate does not apply."""
        # Register a write tool on the local filesystem server
        write_info = MCPToolInfo(
            name="write_file",
            description="Write a local file",
            input_schema={},
            server_name="filesystem",
            trust_level="local",
            read_only=False,
        )
        router_with_mcp._mcp_client._tool_index["mcp_filesystem_write_file"] = write_info
        router_with_mcp._mcp_client.call_tool = AsyncMock(return_value={"result": "written"})
        result = await router_with_mcp.call_mcp_tool("mcp_filesystem_write_file", {"path": "/tmp/x", "content": "y"})
        assert "error" not in result


class TestRouterStatus:

    def test_status_includes_mcp(self, router_with_mcp):
        status = router_with_mcp.get_status()
        assert "mcp_filesystem" in status
        assert "mcp_github" in status

    def test_status_without_mcp(self):
        router = ToolRouter()
        status = router.get_status()
        assert not any(k.startswith("mcp_") for k in status)
