"""
Phase 5 — MCP Client tests.

Tests config loading, tool discovery, and the MCPClient interface.
"""
import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from agent.mcp_client import (
    MCPClient,
    MCPServerConfig,
    MCPToolInfo,
    _extract_content,
    load_mcp_config,
)


# ── Config loading ───────────────────────────────────────────────────────────


def test_load_config_empty_file():
    """Empty config yields no servers."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump({"servers": []}, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)
    assert configs == []


def test_load_config_missing_file():
    """Missing config file yields no servers."""
    configs = load_mcp_config("/nonexistent/path.yaml")
    assert configs == []


def test_load_config_valid_server():
    """Valid server entry is parsed correctly."""
    data = {
        "servers": [
            {
                "name": "test-server",
                "command": "npx",
                "args": ["-y", "test-pkg"],
                "trust_level": "external",
                "env": {"API_KEY": "test123"},
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "test-server"
    assert configs[0].command == "npx"
    assert configs[0].args == ["-y", "test-pkg"]
    assert configs[0].trust_level == "external"
    assert configs[0].env == {"API_KEY": "test123"}


def test_load_config_env_interpolation():
    """Environment variables in env values are interpolated."""
    os.environ["TEST_MCP_TOKEN"] = "secret_value"
    data = {
        "servers": [
            {
                "name": "test",
                "command": "test",
                "env": {"TOKEN": "${TEST_MCP_TOKEN}"},
                "trust_level": "local",
            }
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)
    del os.environ["TEST_MCP_TOKEN"]

    assert configs[0].env["TOKEN"] == "secret_value"


def test_load_config_invalid_trust_level():
    """Invalid trust_level raises ValueError."""
    with pytest.raises(ValueError, match="Invalid trust_level"):
        MCPServerConfig(name="bad", command="test", trust_level="invalid")


def test_load_config_default_trust_level():
    """Default trust_level is 'external' (conservative)."""
    data = {
        "servers": [
            {"name": "no-trust", "command": "test"}
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert configs[0].trust_level == "external"


def test_load_config_multiple_servers():
    """Multiple servers are all loaded."""
    data = {
        "servers": [
            {"name": "s1", "command": "cmd1", "trust_level": "local"},
            {"name": "s2", "command": "cmd2", "trust_level": "trusted"},
            {"name": "s3", "command": "cmd3", "trust_level": "external"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 3
    assert {c.trust_level for c in configs} == {"local", "trusted", "external"}


def test_load_config_skips_entry_missing_name():
    """Config entry without 'name' is skipped, not crashed."""
    data = {
        "servers": [
            {"command": "test", "trust_level": "local"},         # missing name
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_skips_entry_missing_command():
    """Config entry without 'command' is skipped, not crashed."""
    data = {
        "servers": [
            {"name": "no-cmd", "trust_level": "local"},          # missing command
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_skips_invalid_trust_level():
    """Config entry with invalid trust_level is skipped gracefully."""
    data = {
        "servers": [
            {"name": "bad-trust", "command": "cmd", "trust_level": "superadmin"},
            {"name": "good", "command": "ok", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].name == "good"


def test_load_config_coerces_args_to_strings():
    """Integer args in YAML are coerced to strings."""
    data = {
        "servers": [
            {"name": "test", "command": "node", "args": ["-p", 8080], "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert configs[0].args == ["-p", "8080"]
    assert all(isinstance(a, str) for a in configs[0].args)


# ── MCPClient interface ─────────────────────────────────────────────────────


def test_client_no_config():
    """Client with no config file has empty tools."""
    client = MCPClient(config_path="/nonexistent.yaml")
    assert client.get_tools() == []


def test_tool_info_lookup():
    """get_tool_info returns correct info for registered tools."""
    client = MCPClient()
    # Manually register a tool
    info = MCPToolInfo(
        name="test_tool",
        description="A test tool",
        input_schema={"type": "object"},
        server_name="test-server",
        trust_level="local",
    )
    client._tool_index["mcp_test-server_test_tool"] = info

    assert client.get_tool_info("mcp_test-server_test_tool") == info
    assert client.get_tool_info("nonexistent") is None


def test_get_tools_format():
    """get_tools returns Anthropic function-calling format with MCP metadata."""
    client = MCPClient()
    info = MCPToolInfo(
        name="my_tool",
        description="Does something",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        server_name="github",
        trust_level="external",
    )
    client._tool_index["mcp_github_my_tool"] = info

    tools = client.get_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "mcp_github_my_tool"
    assert "[MCP/github]" in tool["function"]["description"]
    assert tool["_mcp"] is True
    assert tool["_mcp_server"] == "github"
    assert tool["_mcp_tool"] == "my_tool"
    assert tool["_trust_level"] == "external"


@pytest.mark.asyncio
async def test_call_tool_server_not_found():
    """Calling a tool on a non-existent server returns an error."""
    client = MCPClient()
    result = await client.call_tool("nonexistent", "some_tool", {})
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_server_not_connected():
    """Calling a tool on a server that is not connected returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["offline"] = MCPServerConnection(
        config=MCPServerConfig(name="offline", command="test"),
        status="disconnected",
    )
    result = await client.call_tool("offline", "some_tool", {})
    assert "error" in result
    assert "disconnected" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_server_error_status():
    """Calling a tool on a server in error state returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["broken"] = MCPServerConnection(
        config=MCPServerConfig(name="broken", command="test"),
        status="error",
    )
    result = await client.call_tool("broken", "some_tool", {})
    assert "error" in result
    assert "error" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_no_session():
    """Calling a tool on a server with connected status but no session returns an error."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["nosession"] = MCPServerConnection(
        config=MCPServerConfig(name="nosession", command="test"),
        status="connected",
        session=None,
    )
    result = await client.call_tool("nosession", "some_tool", {})
    assert "error" in result
    assert "no active session" in result["error"]


@pytest.mark.asyncio
async def test_client_health_returns_server_statuses():
    """check_health returns status for all registered servers."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    client._servers["s1"] = MCPServerConnection(
        config=MCPServerConfig(name="s1", command="test"),
        status="connected",
    )
    client._servers["s2"] = MCPServerConnection(
        config=MCPServerConfig(name="s2", command="test"),
        status="error",
    )
    health = await client.check_health()
    assert health == {"s1": "connected", "s2": "error"}


@pytest.mark.asyncio
async def test_client_health_empty():
    """Health check with no servers returns empty dict."""
    client = MCPClient()
    health = await client.check_health()
    assert health == {}


def test_get_tools_empty_schema_gets_default():
    """get_tools uses a default empty schema when tool has no input_schema."""
    client = MCPClient()
    info = MCPToolInfo(
        name="bare_tool",
        description="No schema",
        input_schema={},
        server_name="local",
        trust_level="local",
    )
    client._tool_index["mcp_local_bare_tool"] = info
    tools = client.get_tools()
    assert len(tools) == 1
    params = tools[0]["function"]["parameters"]
    # Should fall back to {"type": "object", "properties": {}}
    assert params == {"type": "object", "properties": {}}


# ── Rate limiting ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_not_applied_to_local_servers():
    """Local servers bypass rate limiting entirely."""
    from agent.mcp_client import MCPServerConnection, MCPServerConfig
    client = MCPClient()
    # Saturate with calls (well beyond the rate limit)
    for _ in range(100):
        client._call_times.setdefault("localsvr", []).append(0.0)  # old timestamps
    client._servers["localsvr"] = MCPServerConnection(
        config=MCPServerConfig(name="localsvr", command="test", trust_level="local"),
        status="connected",
        session=object(),  # non-None placeholder
    )
    # Rate limit should not trigger for local
    result = client._check_rate_limit("localsvr", "local")
    assert result is None


def test_rate_limit_blocks_external_at_threshold():
    """External servers are blocked when call count reaches the rate limit."""
    client = MCPClient()
    now = time.monotonic()
    # Fill up to exactly the rate limit within the window
    client._call_times["ext"] = [now - 1.0] * client._EXTERNAL_RATE_LIMIT
    result = client._check_rate_limit("ext", "external")
    assert result is not None
    assert "Rate limit exceeded" in result


def test_rate_limit_allows_external_below_threshold():
    """External servers are not blocked below the rate limit."""
    client = MCPClient()
    now = time.monotonic()
    # One below the limit
    client._call_times["ext"] = [now - 1.0] * (client._EXTERNAL_RATE_LIMIT - 1)
    result = client._check_rate_limit("ext", "external")
    assert result is None


def test_rate_limit_expires_old_calls():
    """Calls older than the rate window do not count against the limit."""
    client = MCPClient()
    # All calls are expired (2x the window ago)
    client._call_times["ext"] = [0.0] * 100  # ancient timestamps
    result = client._check_rate_limit("ext", "external")
    assert result is None


# ── _extract_content helper ───────────────────────────────────────────────────


class _FakeText:
    def __init__(self, text): self.text = text

class _FakeImage:
    def __init__(self, data, mime): self.data = data; self.mimeType = mime

class _FakeResourceText:
    def __init__(self, text): self.text = text

class _FakeEmbedded:
    def __init__(self, resource): self.resource = resource


class TestExtractContent:
    """Regression tests for _extract_content (Fix 4 / P2).

    _extract_content must handle text, images, and embedded resources —
    not just TextContent blocks.  All-text results simplify to a plain string;
    mixed results return a list of typed dicts so no information is lost.
    """

    def test_empty_content_returns_empty_string(self):
        assert _extract_content([]) == ""

    def test_single_text_block_returns_string(self):
        result = _extract_content([_FakeText("hello")])
        assert result == "hello"

    def test_multiple_text_blocks_joined(self):
        result = _extract_content([_FakeText("line 1"), _FakeText("line 2")])
        assert result == "line 1\nline 2"

    def test_image_block_preserved_in_mixed_output(self):
        result = _extract_content([_FakeText("caption"), _FakeImage("abc==", "image/png")])
        assert isinstance(result, list)
        types = {p["type"] for p in result}
        assert "text" in types
        assert "image" in types
        img = next(p for p in result if p["type"] == "image")
        assert img["data"] == "abc=="
        assert img["mimeType"] == "image/png"

    def test_embedded_text_resource_extracted(self):
        resource = _FakeResourceText("resource text here")
        result = _extract_content([_FakeEmbedded(resource)])
        # Single embedded text resource simplifies to a plain string
        assert result == "resource text here"

    def test_image_only_returns_list(self):
        result = _extract_content([_FakeImage("data", "image/jpeg")])
        assert isinstance(result, list)
        assert result[0]["type"] == "image"

    def test_all_text_blocks_simplify_to_string(self):
        """When every block is text, the result is a plain string, not a list."""
        blocks = [_FakeText(f"part {i}") for i in range(5)]
        result = _extract_content(blocks)
        assert isinstance(result, str)
        assert "part 0" in result and "part 4" in result


def test_load_config_reads_allow_side_effects():
    """allow_side_effects is parsed from YAML and stored on the config object."""
    import tempfile
    data = {
        "servers": [
            {"name": "gh", "command": "npx", "trust_level": "external",
             "allow_side_effects": True},
            {"name": "local", "command": "python", "trust_level": "local"},
        ]
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    gh = next(c for c in configs if c.name == "gh")
    local = next(c for c in configs if c.name == "local")
    assert gh.allow_side_effects is True
    assert local.allow_side_effects is False  # conservative default


def test_load_config_quoted_false_string_does_not_enable_side_effects():
    """Quoted 'false' string must NOT enable side effects (P2 regression).

    yaml.safe_load keeps allow_side_effects: "false" as the Python string "false".
    bool("false") == True, which would unintentionally enable writes.
    The loader must reject non-bool values and default to False.
    """
    # Simulate what yaml.safe_load produces for a quoted value by writing raw YAML
    raw_yaml = "servers:\n  - name: gh\n    command: npx\n    trust_level: external\n    allow_side_effects: \"false\"\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(raw_yaml)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].allow_side_effects is False, (
        "Quoted 'false' string must be treated as False, not bool('false') == True"
    )


def test_load_config_quoted_true_string_does_not_enable_side_effects():
    """Quoted 'true' string must NOT enable side effects — only real YAML booleans do."""
    raw_yaml = "servers:\n  - name: gh\n    command: npx\n    trust_level: external\n    allow_side_effects: \"true\"\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(raw_yaml)
        f.flush()
        configs = load_mcp_config(f.name)
    os.unlink(f.name)

    assert len(configs) == 1
    assert configs[0].allow_side_effects is False, (
        "Quoted 'true' string must be rejected and default to False"
    )


def test_get_tools_side_effects_flag_reflects_read_only():
    """get_tools sets side_effects=False for read-only tools, True for others."""
    client = MCPClient()

    read_info = MCPToolInfo(
        name="search", description="Search", input_schema={},
        server_name="gh", trust_level="external", read_only=True,
    )
    write_info = MCPToolInfo(
        name="create", description="Create", input_schema={},
        server_name="gh", trust_level="external", read_only=False,
    )
    client._tool_index["mcp_gh_search"] = read_info
    client._tool_index["mcp_gh_create"] = write_info

    tools = {t["function"]["name"]: t for t in client.get_tools()}
    assert tools["mcp_gh_search"]["side_effects"] is False
    assert tools["mcp_gh_create"]["side_effects"] is True
