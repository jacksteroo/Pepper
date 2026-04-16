"""
Phase 5.1 — MCP Client.

Connects to external MCP servers (stdio-based), discovers their tools,
and makes them callable through the same interface as native Pepper tools.

Each MCP server is configured in config/mcp_servers.yaml with:
  - name: unique identifier
  - command: executable to launch
  - args: command-line arguments
  - env: environment variables (optional)
  - trust_level: local | trusted | external

The client manages server lifecycles: starts them on init, discovers tools,
routes calls, and shuts them down cleanly.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()


# ── MCP result extraction helpers ────────────────────────────────────────────

def _extract_text_content(content_blocks: list) -> str:
    """Extract only text from MCP content blocks (for error messages)."""
    return "\n".join(
        part.text for part in content_blocks if hasattr(part, "text")
    )


def _extract_content(content_blocks: list) -> Any:
    """Extract the most useful representation from MCP content blocks.

    Returns a string when all blocks are text (the common case).
    Returns a list of typed dicts when blocks are mixed (images, resources).
    This preserves information that would otherwise be silently discarded
    when a server returns structured or binary content alongside text.
    """
    if not content_blocks:
        return ""

    parts = []
    for part in content_blocks:
        if hasattr(part, "text"):
            parts.append({"type": "text", "text": part.text})
        elif hasattr(part, "data"):  # ImageContent: base64-encoded bytes
            parts.append({
                "type": "image",
                "mimeType": getattr(part, "mimeType", "application/octet-stream"),
                "data": part.data,
            })
        elif hasattr(part, "resource"):  # EmbeddedResource
            resource = part.resource
            if hasattr(resource, "text"):
                parts.append({"type": "text", "text": resource.text})
            elif hasattr(resource, "blob"):
                parts.append({
                    "type": "resource",
                    "mimeType": getattr(resource, "mimeType", "application/octet-stream"),
                    "uri": getattr(resource, "uri", ""),
                })

    # Simplify: if every block is plain text, return a single string
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


# ── Trust levels ─────────────────────────────────────────────────────────────

TRUST_LEVELS = ("local", "trusted", "external")


@dataclass
class MCPServerConfig:
    """Parsed config for a single MCP server from mcp_servers.yaml."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    trust_level: str = "external"  # conservative default
    # When False (default), tools on external servers that are not annotated
    # as read-only are blocked — they may mutate remote state, and the repo
    # guardrails require explicit user approval for consequential operations.
    # Set to True in mcp_servers.yaml only after reviewing what the server can do.
    allow_side_effects: bool = False

    def __post_init__(self):
        if self.trust_level not in TRUST_LEVELS:
            raise ValueError(
                f"Invalid trust_level '{self.trust_level}' for MCP server '{self.name}'. "
                f"Must be one of: {TRUST_LEVELS}"
            )


@dataclass
class MCPToolInfo:
    """A tool discovered from an MCP server, tagged with provenance."""
    name: str
    description: str
    input_schema: dict
    server_name: str
    trust_level: str
    # True when the MCP server declares readOnlyHint=True for this tool.
    # Used by the approval gate: write tools on servers with allow_side_effects=False
    # are blocked at the router level rather than silently executed.
    read_only: bool = False


@dataclass
class MCPServerConnection:
    """Active connection to an MCP server."""
    config: MCPServerConfig
    session: Any = None        # mcp.ClientSession (set after successful connect)
    tools: list[MCPToolInfo] = field(default_factory=list)
    status: str = "disconnected"  # disconnected | connected | error
    # Internal stream and context manager handles — populated by _connect_server
    _read: Any = None
    _write: Any = None
    _ctx: Any = None           # stdio_client async context manager
    _session_ctx: Any = None   # ClientSession async context manager


def load_mcp_config(config_path: str | None = None) -> list[MCPServerConfig]:
    """Load MCP server configurations from YAML.

    Environment variable interpolation: ${VAR_NAME} in env values
    is replaced with the corresponding environment variable.
    """
    if config_path is None:
        config_path = str(Path(__file__).parent.parent / "config" / "mcp_servers.yaml")

    path = Path(config_path)
    if not path.exists():
        logger.info("mcp_config_not_found", path=config_path)
        return []

    with open(path) as f:
        data = yaml.safe_load(f)

    if not data or not data.get("servers"):
        logger.info("mcp_no_servers_configured", path=config_path)
        return []

    configs = []
    for i, entry in enumerate(data["servers"]):
        if not isinstance(entry, dict):
            logger.warning("mcp_config_invalid_entry", index=i, reason="not a dict")
            continue

        # Validate required fields
        if "name" not in entry:
            logger.warning("mcp_config_invalid_entry", index=i, reason="missing 'name'")
            continue
        if "command" not in entry:
            logger.warning("mcp_config_invalid_entry", index=i,
                           name=entry.get("name"), reason="missing 'command'")
            continue

        # Interpolate env vars; warn on unresolved references
        env = {}
        for k, v in (entry.get("env") or {}).items():
            if isinstance(v, str) and "${" in v:
                def _make_replacer(key: str):
                    def _replace(m: re.Match) -> str:
                        var_name = m.group(1)
                        val = os.environ.get(var_name)
                        if val is None:
                            logger.warning(
                                "mcp_config_env_var_missing",
                                server=entry.get("name"),
                                env_key=key,
                                missing_var=var_name,
                            )
                            return ""
                        return val
                    return _replace

                env[k] = re.sub(r"\$\{(\w+)\}", _make_replacer(k), v)
            else:
                env[k] = str(v)

        # Coerce args to strings in case YAML parsed numbers
        args = [str(a) for a in (entry.get("args") or [])]

        # Strict boolean validation for allow_side_effects.
        # yaml.safe_load keeps quoted values like "false" as strings, and
        # bool("false") == True — a silent footgun in the security control
        # that defaults writes to disabled.  Only accept actual Python booleans.
        raw_ase = entry.get("allow_side_effects", False)
        if not isinstance(raw_ase, bool):
            logger.warning(
                "mcp_config_allow_side_effects_not_bool",
                server=entry.get("name"),
                value=raw_ase,
                note=(
                    "allow_side_effects must be a YAML boolean (true/false without quotes). "
                    "Defaulting to False (writes disabled) to keep the safe default."
                ),
            )
            raw_ase = False

        try:
            configs.append(MCPServerConfig(
                name=entry["name"],
                command=entry["command"],
                args=args,
                env=env,
                trust_level=entry.get("trust_level", "external"),
                allow_side_effects=raw_ase,
            ))
        except ValueError as e:
            logger.warning("mcp_config_invalid_server",
                           name=entry.get("name"), error=str(e))
            continue

    logger.info("mcp_config_loaded", server_count=len(configs),
                names=[c.name for c in configs])
    return configs


class MCPClient:
    """Manages connections to all configured MCP servers.

    Lifecycle:
    1. initialize() — connect to all servers, discover tools
    2. get_tools() — return tool definitions for injection into LLM
    3. call_tool(server_name, tool_name, arguments) — execute a tool
    4. shutdown() — close all connections

    Rate limiting:
    External servers (trust_level="external") are limited to 30 calls per
    minute per server. Trusted and local servers have no rate limit.
    """

    # Max calls per minute for external MCP servers
    _EXTERNAL_RATE_LIMIT = 30
    _RATE_WINDOW_SECS = 60.0

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path
        self._servers: dict[str, MCPServerConnection] = {}
        self._tool_index: dict[str, MCPToolInfo] = {}  # tool_name → info
        # Per-server call timestamps for rate limiting external servers
        self._call_times: dict[str, list[float]] = {}

    @property
    def servers(self) -> dict[str, MCPServerConnection]:
        return self._servers

    async def initialize(self) -> None:
        """Connect to all configured MCP servers and discover tools."""
        configs = load_mcp_config(self._config_path)
        if not configs:
            logger.info("mcp_client_no_servers")
            return

        for config in configs:
            conn = MCPServerConnection(config=config)
            self._servers[config.name] = conn
            try:
                await self._connect_server(conn)
            except Exception as e:
                logger.error("mcp_server_connect_failed",
                             server=config.name, error=str(e))
                conn.status = "error"

        logger.info(
            "mcp_client_initialized",
            servers={n: s.status for n, s in self._servers.items()},
            total_tools=len(self._tool_index),
        )

    async def _connect_server(self, conn: MCPServerConnection) -> None:
        """Start an MCP server process and perform tool discovery."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        config = conn.config

        # Build environment: inherit current env + server-specific overrides
        server_env = dict(os.environ)
        server_env.update(config.env)

        params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=server_env,
        )

        # stdio_client is an async context manager that yields (read, write) streams.
        # We need to keep it alive for the lifetime of the connection, so we store
        # the context manager and enter it.
        ctx = stdio_client(params)
        streams = await ctx.__aenter__()
        conn._read, conn._write = streams
        conn._ctx = ctx  # keep reference to prevent GC

        # Create and initialize the MCP session.
        # If anything after ctx.__aenter__() fails, clean up the process.
        # Timeouts prevent hanging indefinitely on unresponsive servers.
        INIT_TIMEOUT = 30.0   # seconds to wait for session initialization
        TOOLS_TIMEOUT = 10.0  # seconds to wait for tool discovery
        try:
            session = ClientSession(conn._read, conn._write)
            await session.__aenter__()
            conn._session_ctx = session  # keep reference

            await asyncio.wait_for(session.initialize(), timeout=INIT_TIMEOUT)
            conn.session = session
            conn.status = "connected"

            # Discover tools
            tools_result = await asyncio.wait_for(
                session.list_tools(), timeout=TOOLS_TIMEOUT
            )
            for tool in tools_result.tools:
                # Extract readOnlyHint from MCP tool annotations if present.
                # Default False (conservative): unknown tools may have side effects.
                annotations = getattr(tool, "annotations", None) or {}
                if hasattr(annotations, "readOnlyHint"):
                    read_only = bool(annotations.readOnlyHint)
                elif isinstance(annotations, dict):
                    read_only = bool(annotations.get("readOnlyHint", False))
                else:
                    read_only = False

                info = MCPToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    server_name=config.name,
                    trust_level=config.trust_level,
                    read_only=read_only,
                )
                conn.tools.append(info)
                # Namespace to avoid collisions: mcp_{server}_{tool}
                qualified_name = f"mcp_{config.name}_{tool.name}"
                if qualified_name in self._tool_index:
                    existing = self._tool_index[qualified_name]
                    logger.warning(
                        "mcp_tool_name_collision",
                        qualified_name=qualified_name,
                        existing_server=existing.server_name,
                        new_server=config.name,
                        note="New registration overwrites existing — check server names for underscores",
                    )
                self._tool_index[qualified_name] = info

        except Exception:
            # Clean up the stdio process so we don't leave zombies
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
            conn._ctx = None
            raise

        logger.info(
            "mcp_server_connected",
            server=config.name,
            trust_level=config.trust_level,
            tool_count=len(conn.tools),
            tool_names=[t.name for t in conn.tools],
        )

    def get_tools(self) -> list[dict]:
        """Return MCP tools in Anthropic function-calling format.

        Tool names are qualified as mcp_{server}_{original_name} to avoid
        collisions with native Pepper tools.
        """
        tools = []
        for qualified_name, info in self._tool_index.items():
            tools.append({
                "type": "function",
                # side_effects drives parallel vs sequential dispatch in _handle_tool_calls.
                # read_only=True (from MCP readOnlyHint annotation) means no side effects;
                # unknown or False means conservative: treat as a side-effect call.
                "side_effects": not info.read_only,
                "function": {
                    "name": qualified_name,
                    "description": (
                        f"[MCP/{info.server_name}] {info.description}"
                    ),
                    "parameters": info.input_schema or {
                        "type": "object", "properties": {}
                    },
                },
                # Internal metadata for routing
                "_mcp": True,
                "_mcp_server": info.server_name,
                "_mcp_tool": info.name,
                "_trust_level": info.trust_level,
            })
        return tools

    def _check_rate_limit(self, server_name: str, trust_level: str) -> str | None:
        """Return an error string if rate limit is exceeded, else None."""
        if trust_level != "external":
            return None  # only rate-limit external servers

        now = time.monotonic()
        window_start = now - self._RATE_WINDOW_SECS
        times = self._call_times.setdefault(server_name, [])

        # Evict expired timestamps
        self._call_times[server_name] = [t for t in times if t > window_start]

        if len(self._call_times[server_name]) >= self._EXTERNAL_RATE_LIMIT:
            logger.warning(
                "mcp_rate_limit_exceeded",
                server=server_name,
                calls_in_window=len(self._call_times[server_name]),
                limit=self._EXTERNAL_RATE_LIMIT,
            )
            return (
                f"Rate limit exceeded for MCP server '{server_name}': "
                f"{self._EXTERNAL_RATE_LIMIT} calls per minute allowed."
            )

        self._call_times[server_name].append(now)
        return None

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict
    ) -> dict:
        """Execute a tool on an MCP server. Returns result dict."""
        conn = self._servers.get(server_name)
        if not conn:
            return {"error": f"MCP server '{server_name}' not found"}
        if conn.status != "connected":
            return {"error": f"MCP server '{server_name}' is {conn.status}"}
        if not conn.session:
            return {"error": f"MCP server '{server_name}' has no active session"}

        # Rate limiting for external servers
        rate_error = self._check_rate_limit(server_name, conn.config.trust_level)
        if rate_error:
            return {"error": rate_error}

        CALL_TIMEOUT = 60.0  # seconds — generous for slow external APIs
        started_at = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(tool_name, arguments),
                timeout=CALL_TIMEOUT,
            )

            duration_ms = round((time.perf_counter() - started_at) * 1000)
            is_error = bool(getattr(result, 'isError', False))
            logger.info(
                "mcp_tool_call_completed",
                server=server_name,
                tool=tool_name,
                duration_ms=duration_ms,
                is_error=is_error,
            )

            if is_error:
                error_text = _extract_text_content(result.content or [])
                return {"error": error_text or "MCP tool returned an error"}

            # Prefer structuredContent (newer MCP servers) — richer and already
            # parsed; the model can use it directly without text extraction.
            structured = getattr(result, 'structuredContent', None)
            if structured is not None:
                return {"result": structured}

            # Fall back to content blocks: text, images, embedded resources.
            return {"result": _extract_content(result.content or [])}

        except Exception as e:
            duration_ms = round((time.perf_counter() - started_at) * 1000)
            logger.error(
                "mcp_tool_call_failed",
                server=server_name,
                tool=tool_name,
                error=str(e),
                duration_ms=duration_ms,
            )
            # If the MCP session raised an error, mark the server as errored so
            # future calls fail fast instead of trying an unusable session.
            conn.status = "error"
            return {"error": f"MCP tool call failed: {e}"}

    def get_tool_info(self, qualified_name: str) -> MCPToolInfo | None:
        """Look up tool info by qualified name (mcp_{server}_{tool})."""
        return self._tool_index.get(qualified_name)

    async def check_health(self) -> dict[str, str]:
        """Return health status of all MCP servers."""
        return {name: conn.status for name, conn in self._servers.items()}

    async def shutdown(self) -> None:
        """Gracefully close all MCP server connections."""
        # Snapshot keys to avoid mutation-during-iteration issues if shutdown
        # is somehow called concurrently (though it should not be).
        servers_snapshot = list(self._servers.items())
        for name, conn in servers_snapshot:
            try:
                # _session_ctx and _ctx are declared dataclass fields (may be None)
                if conn._session_ctx is not None:
                    await conn._session_ctx.__aexit__(None, None, None)
                    conn._session_ctx = None
                if conn._ctx is not None:
                    await conn._ctx.__aexit__(None, None, None)
                    conn._ctx = None
                conn.status = "disconnected"
                logger.info("mcp_server_disconnected", server=name)
            except Exception as e:
                logger.warning("mcp_server_shutdown_error", server=name, error=str(e))
        self._servers.clear()
        self._tool_index.clear()
