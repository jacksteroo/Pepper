"""
Phase 5 — Unified Tool Router.

Routes tool calls to either:
  1. Native in-process Pepper tools (calendar, email, etc.)
  2. MCP servers (external tools discovered at startup)

MCP tools are namespaced as mcp_{server}_{tool} to avoid collisions.
Privacy enforcement via trust levels is applied before every MCP call.

The ToolRouter also maintains the legacy HTTP subsystem routing
for forward compatibility (currently unused — all native tools
run in-process via core.py._execute_tool).
"""
from __future__ import annotations

import time
import httpx
import structlog
from dataclasses import dataclass, field

from agent.mcp_audit import (
    check_trust_boundary,
    log_mcp_call,
    MCPPrivacyViolation,
)

logger = structlog.get_logger()

SUBSYSTEM_PORTS = {
    "calendar": 8100,
    "communications": 8101,
    "knowledge": 8102,
    "health": 8103,
    "finance": 8104,
    "people": 8001,
}


@dataclass
class SubsystemInfo:
    name: str
    base_url: str
    status: str = "unknown"  # "ok", "degraded", "down"
    tools: list = field(default_factory=list)


class ToolRouter:
    def __init__(self):
        self._subsystems: dict[str, SubsystemInfo] = {}
        self._mcp_client = None  # set by core.py after init

        # Register defaults
        for name, port in SUBSYSTEM_PORTS.items():
            self.register_subsystem(name, f"http://localhost:{port}")

    def set_mcp_client(self, mcp_client) -> None:
        """Attach the MCP client for routing MCP tool calls."""
        self._mcp_client = mcp_client

    def register_subsystem(self, name: str, base_url: str) -> None:
        self._subsystems[name] = SubsystemInfo(name=name, base_url=base_url)

    def is_mcp_tool(self, tool_name: str) -> bool:
        """Check if a tool name is an MCP-qualified name (mcp_{server}_{tool})."""
        return tool_name.startswith("mcp_") and self._mcp_client is not None

    def is_mcp_read_only_tool(self, tool_name: str) -> bool:
        """Return True if the MCP tool declared readOnlyHint=True at discovery time."""
        if not self._mcp_client:
            return False
        info = self._mcp_client.get_tool_info(tool_name)
        return info.read_only if info else False

    def parse_mcp_tool_name(self, qualified_name: str) -> tuple[str, str] | None:
        """Parse mcp_{server}_{tool} into (server_name, tool_name).

        Returns None if the name doesn't match the expected pattern.
        """
        if not qualified_name.startswith("mcp_"):
            return None
        if not self._mcp_client:
            return None

        info = self._mcp_client.get_tool_info(qualified_name)
        if info:
            return (info.server_name, info.name)
        return None

    async def call_mcp_tool(
        self, qualified_name: str, arguments: dict
    ) -> dict:
        """Route a tool call to the appropriate MCP server.

        Enforces trust boundaries before execution and logs to audit trail.
        """
        if not self._mcp_client:
            return {"error": "MCP client not initialized"}

        parsed = self.parse_mcp_tool_name(qualified_name)
        if not parsed:
            return {"error": f"Unknown MCP tool: {qualified_name}"}

        server_name, tool_name = parsed
        info = self._mcp_client.get_tool_info(qualified_name)
        if not info:
            return {"error": f"MCP tool info not found: {qualified_name}"}

        # Side-effects gate: block write tools on servers that haven't opted in.
        # This enforces the repo guardrail that consequential external actions
        # (creating GitHub issues, writing to Notion, mutating the filesystem, etc.)
        # require explicit user approval — not just the model deciding to call a tool.
        # local servers are always allowed; the gate only applies to external/trusted.
        conn = self._mcp_client.servers.get(server_name)
        if conn and not conn.config.allow_side_effects and not info.read_only:
            if info.trust_level != "local":
                msg = (
                    f"MCP tool '{tool_name}' on server '{server_name}' has side effects "
                    f"and is blocked: allow_side_effects is not enabled for this server. "
                    f"Review the server's tool list, then set allow_side_effects: true "
                    f"in config/mcp_servers.yaml to permit write operations."
                )
                logger.warning(
                    "mcp_write_tool_blocked",
                    server=server_name,
                    tool=tool_name,
                    trust_level=info.trust_level,
                )
                log_mcp_call(
                    server_name=server_name,
                    trust_level=info.trust_level,
                    tool_name=tool_name,
                    duration_ms=0,
                    success=False,
                    error=msg,
                )
                return {"error": msg}

        # Privacy enforcement: check trust boundary BEFORE calling.
        # Pass arguments so the oversized-payload scan runs on the actual outbound data.
        try:
            check_trust_boundary(server_name, info.trust_level, tool_name, arguments)
        except MCPPrivacyViolation as violation:
            log_mcp_call(
                server_name=server_name,
                trust_level=info.trust_level,
                tool_name=tool_name,
                duration_ms=0,
                success=False,
                error=str(violation),
            )
            return {"error": str(violation)}

        # Execute the MCP tool call
        started_at = time.perf_counter()
        result = await self._mcp_client.call_tool(server_name, tool_name, arguments)
        duration_ms = round((time.perf_counter() - started_at) * 1000)

        # Audit log
        log_mcp_call(
            server_name=server_name,
            trust_level=info.trust_level,
            tool_name=tool_name,
            duration_ms=duration_ms,
            success="error" not in result,
            error=result.get("error"),
        )

        return result

    def get_mcp_tools(self) -> list[dict]:
        """Return MCP tool definitions for injection into the LLM."""
        if not self._mcp_client:
            return []
        return self._mcp_client.get_tools()

    async def check_health(self) -> dict[str, str]:
        """Ping all subsystems and MCP servers. Returns {name: status}."""
        results = {}

        # Legacy HTTP subsystem health
        async with httpx.AsyncClient(timeout=3.0) as client:
            for name, info in self._subsystems.items():
                try:
                    resp = await client.get(f"{info.base_url}/health")
                    info.status = "ok" if resp.status_code == 200 else "degraded"
                except Exception:
                    info.status = "down"
                results[name] = info.status

        # MCP server health
        if self._mcp_client:
            mcp_health = await self._mcp_client.check_health()
            for name, status in mcp_health.items():
                results[f"mcp_{name}"] = status

        return results

    async def list_available_tools(self) -> list[dict]:
        """Fetch tool definitions from all healthy subsystems + MCP servers."""
        tools = []

        # Legacy HTTP subsystem tools (for future standalone subsystem services)
        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, info in self._subsystems.items():
                if info.status == "down":
                    continue
                try:
                    resp = await client.get(f"{info.base_url}/tools")
                    if resp.status_code == 200:
                        subsystem_tools = resp.json()
                        if not isinstance(subsystem_tools, list):
                            logger.warning("tool_fetch_bad_format", subsystem=name)
                            continue
                        valid = []
                        for tool in subsystem_tools:
                            if isinstance(tool, dict):
                                tool["_subsystem"] = name
                                valid.append(tool)
                        tools.extend(valid)
                        info.tools = valid
                except Exception as e:
                    logger.warning("tool_fetch_failed", subsystem=name, error=str(e))

        # MCP tools
        tools.extend(self.get_mcp_tools())

        return tools

    async def call_tool(self, subsystem: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a subsystem. Returns result dict."""
        info = self._subsystems.get(subsystem)
        if not info or info.status == "down":
            return {"error": f"Subsystem '{subsystem}' is unavailable", "subsystem": subsystem}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{info.base_url}/tools/{tool_name}",
                    json={"arguments": arguments},
                )
                resp.raise_for_status()
                result = resp.json()
                if not isinstance(result, dict):
                    return {"error": f"Subsystem returned unexpected type: {type(result).__name__}"}
                return result
        except httpx.HTTPStatusError as e:
            logger.error(
                "tool_call_failed",
                subsystem=subsystem,
                tool=tool_name,
                status=e.response.status_code,
            )
            return {
                "error": f"Tool call failed: {e.response.status_code}",
                "subsystem": subsystem,
            }
        except Exception as e:
            logger.error(
                "tool_call_error", subsystem=subsystem, tool=tool_name, error=str(e)
            )
            return {"error": str(e), "subsystem": subsystem}

    def get_status(self) -> dict:
        status = {name: info.status for name, info in self._subsystems.items()}
        if self._mcp_client:
            for name, conn in self._mcp_client.servers.items():
                status[f"mcp_{name}"] = conn.status
        return status
