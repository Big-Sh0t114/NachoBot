"""Shared contracts for the core MCP runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


MCPTransport = Literal["stdio", "sse", "http", "streamable_http"]


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Validated connection settings for one MCP server."""

    name: str
    transport: MCPTransport
    enabled: bool = True
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MCPPermissionConfig:
    """Permission policy loaded from core or legacy configuration."""

    enabled: bool = True
    default_mode: Literal["allow_all", "deny_all"] = "deny_all"
    quick_allow_users: frozenset[str] = frozenset()
    quick_deny_groups: frozenset[str] = frozenset()
    rules: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class MCPRuntimeConfig:
    """Complete immutable runtime configuration snapshot."""

    enabled: bool = True
    servers: tuple[MCPServerConfig, ...] = ()
    tool_prefix: str = "mcp"
    disabled_tools: frozenset[str] = frozenset()
    connect_timeout: float = 20.0
    call_timeout: float = 60.0
    reconnect_interval: float = 30.0
    permissions: MCPPermissionConfig = MCPPermissionConfig()
    source: str = "core"


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    """A server tool exposed through a stable, model-safe name."""

    exposed_name: str
    server_name: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MCPAccessContext:
    """Identity used for catalog filtering and invocation authorization."""

    user_id: str = ""
    chat_id: str = ""
    is_group: bool = False
    is_admin: bool = False


@dataclass(slots=True)
class MCPInvocationResult:
    """Normalized result returned by the core service."""

    success: bool
    content: Any = None
    error: str = ""
    duration_ms: float = 0.0
    server_name: str = ""
    tool_name: str = ""


@dataclass(frozen=True, slots=True)
class MCPServerStatus:
    """Safe status data; connection secrets are intentionally absent."""

    name: str
    transport: str
    connected: bool
    tools_count: int
    last_error: str = ""
    failure_count: int = 0
    retry_in_seconds: float = 0.0
