"""Core-owned MCP connection pool, catalog, authorization, and invocation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from src.common.logger import get_logger
from src.mcp.config import MCPConfigError, load_runtime_config
from src.mcp.policy import MCPPermissionPolicy
from src.mcp.types import (
    MCPAccessContext,
    MCPInvocationResult,
    MCPRuntimeConfig,
    MCPServerConfig,
    MCPServerStatus,
    MCPToolDescriptor,
)


logger = get_logger("mcp_service")

ClientFactory = Callable[[MCPServerConfig, float], Any]
ConfigLoader = Callable[[], MCPRuntimeConfig | Awaitable[MCPRuntimeConfig]]


@dataclass(frozen=True, slots=True)
class _RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class _ServerConnection:
    """Own exactly one SDK client and serialize its lifecycle operations."""

    def __init__(self, config: MCPServerConfig, client_factory: ClientFactory, runtime: MCPRuntimeConfig) -> None:
        self.config = config
        self._client_factory = client_factory
        self._connect_timeout = runtime.connect_timeout
        self._call_timeout = runtime.call_timeout
        self._reconnect_interval = runtime.reconnect_interval
        self._client: Any = None
        self._lock = asyncio.Lock()
        self.tools: tuple[_RemoteTool, ...] = ()
        self.last_error = ""
        self.failure_count = 0
        self.next_retry_at = 0.0

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def retry_in_seconds(self) -> float:
        return max(0.0, self.next_retry_at - time.monotonic())

    @property
    def can_retry(self) -> bool:
        return self._client is None and self.retry_in_seconds <= 0

    async def connect(self) -> bool:
        async with self._lock:
            if self._client is not None:
                return True
            candidate: Any = None
            try:
                candidate = self._client_factory(self.config, self._call_timeout)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
                entered = await asyncio.wait_for(candidate.__aenter__(), timeout=self._connect_timeout)
                tools = await asyncio.wait_for(self._fetch_tools(entered), timeout=self._connect_timeout)
                self._client = entered
                self.tools = tools
                self.last_error = ""
                self.failure_count = 0
                self.next_retry_at = 0.0
                logger.info(f"MCP 服务器 {self.config.name} 已连接，发现 {len(tools)} 个工具")
                return True
            except Exception as exc:
                self.last_error = _safe_error(exc)
                self.tools = ()
                self.failure_count += 1
                if self._reconnect_interval > 0:
                    delay = min(
                        self._reconnect_interval * (2 ** min(self.failure_count - 1, 5)),
                        900.0,
                    )
                    self.next_retry_at = time.monotonic() + delay
                logger.warning(f"MCP 服务器 {self.config.name} 连接失败: {self.last_error}")
                if candidate is not None:
                    await _close_client(candidate)
                return False

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_unlocked()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with self._lock:
            if self._client is None:
                raise ConnectionError(f"MCP server {self.config.name} is not connected")
            try:
                return await asyncio.wait_for(
                    self._client.call_tool(name, arguments),
                    timeout=self._call_timeout,
                )
            except Exception as exc:
                self.last_error = _safe_error(exc)
                # Calls are never retried automatically: a lost response may
                # still have committed a side effect on the remote system.
                await self._disconnect_unlocked()
                raise

    async def _disconnect_unlocked(self) -> None:
        client, self._client = self._client, None
        self.tools = ()
        if client is not None:
            await _close_client(client)

    @staticmethod
    async def _fetch_tools(client: Any) -> tuple[_RemoteTool, ...]:
        tools: list[_RemoteTool] = []
        cursor: Optional[str] = None
        for _page in range(100):
            try:
                result = await client.list_tools(cursor=cursor, cache_mode="refresh")
            except TypeError:
                result = await client.list_tools(cursor=cursor)
            for tool in getattr(result, "tools", ()):
                name = str(getattr(tool, "name", "") or "").strip()
                if not name:
                    continue
                schema = getattr(tool, "input_schema", None)
                if not isinstance(schema, dict):
                    schema = {}
                tools.append(
                    _RemoteTool(
                        name=name,
                        description=str(getattr(tool, "description", "") or f"MCP tool: {name}"),
                        input_schema=_normalize_input_schema(schema),
                    )
                )
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break
        else:
            raise RuntimeError("MCP tools/list exceeded 100 pages")
        return tuple(tools)


class MCPService:
    """The single core authority for MCP sessions and tools."""

    core_managed = True

    def __init__(
        self,
        *,
        config_loader: ConfigLoader = load_runtime_config,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config_loader = config_loader
        self._client_factory = client_factory or _build_sdk_client
        self._lifecycle_lock = asyncio.Lock()
        self._runtime = MCPRuntimeConfig(enabled=False, source="not-started")
        self._policy = MCPPermissionPolicy(self._runtime.permissions)
        self._connections: dict[str, _ServerConnection] = {}
        self._tools: dict[str, tuple[MCPToolDescriptor, _ServerConnection]] = {}
        self._started = False
        self._initial_connect_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._total_calls = 0
        self._successful_calls = 0
        self._failed_calls = 0

    @property
    def started(self) -> bool:
        return self._started

    @property
    def config_source(self) -> str:
        return self._runtime.source

    async def start(self, *, wait_for_connections: bool = True) -> None:
        initial_task: asyncio.Task[None] | None = None
        async with self._lifecycle_lock:
            if self._started:
                initial_task = self._initial_connect_task
            else:
                try:
                    runtime = self._config_loader()
                    if inspect.isawaitable(runtime):
                        runtime = await runtime
                except MCPConfigError as exc:
                    logger.error(f"MCP 核心配置无效，运行时保持禁用: {exc}")
                    runtime = MCPRuntimeConfig(enabled=False, source="invalid")
                except Exception as exc:
                    logger.error(f"MCP 核心配置加载失败，运行时保持禁用: {_safe_error(exc)}")
                    runtime = MCPRuntimeConfig(enabled=False, source="error")

                self._runtime = runtime
                self._policy = MCPPermissionPolicy(runtime.permissions)
                self._connections = {
                    config.name: _ServerConnection(config, self._client_factory, runtime)
                    for config in runtime.servers
                    if config.enabled
                }
                self._started = True

                if not runtime.enabled:
                    logger.info(f"MCP 核心运行时未启用（配置来源: {runtime.source}）")
                elif not self._connections:
                    logger.info(f"MCP 核心运行时没有已启用服务器（配置来源: {runtime.source}）")
                else:
                    initial_task = asyncio.create_task(
                        self._connect_initial_servers(),
                        name="mcp-core-initial-connect",
                    )
                    self._initial_connect_task = initial_task

        if wait_for_connections and initial_task is not None:
            await asyncio.shield(initial_task)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            initial_task, self._initial_connect_task = self._initial_connect_task, None
            maintenance_task, self._maintenance_task = self._maintenance_task, None
            self._started = False
            for task in (initial_task, maintenance_task):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await asyncio.gather(
                *(connection.disconnect() for connection in self._connections.values()),
                return_exceptions=True,
            )
            self._connections.clear()
            self._tools.clear()
            logger.info("MCP 核心运行时已关闭")

    async def reload(self) -> None:
        await self.shutdown()
        await self.start()

    def get_tool_definitions(self, context: MCPAccessContext | None = None) -> list[dict[str, Any]]:
        access = context or MCPAccessContext()
        definitions: list[dict[str, Any]] = []
        for name in sorted(self._tools):
            descriptor, _connection = self._tools[name]
            if not self._policy.allows(name, access):
                continue
            definitions.append(
                {
                    "name": descriptor.exposed_name,
                    "description": descriptor.description,
                    "parameters": [],
                    "input_schema": descriptor.input_schema,
                    "mcp_server": descriptor.server_name,
                }
            )
        return definitions

    async def invoke(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        context: MCPAccessContext | None = None,
    ) -> MCPInvocationResult:
        started_at = time.monotonic()
        access = context or MCPAccessContext()
        entry = self._tools.get(str(tool_name or ""))
        if entry is None:
            return self._error_result(tool_name, "MCP tool is unavailable", started_at)

        descriptor, connection = entry
        if not self._policy.allows(descriptor.exposed_name, access):
            return self._error_result(tool_name, "MCP tool access denied", started_at, descriptor.server_name)
        if arguments is not None and not isinstance(arguments, Mapping):
            return self._error_result(
                tool_name, "MCP tool arguments must be an object", started_at, descriptor.server_name
            )

        self._total_calls += 1
        try:
            raw_result = await connection.call_tool(descriptor.remote_name, dict(arguments or {}))
            normalized = _normalize_call_result(raw_result)
            duration = (time.monotonic() - started_at) * 1000
            if normalized[0]:
                self._successful_calls += 1
            else:
                self._failed_calls += 1
            return MCPInvocationResult(
                success=normalized[0],
                content=normalized[1],
                error=normalized[2],
                duration_ms=duration,
                server_name=descriptor.server_name,
                tool_name=descriptor.exposed_name,
            )
        except Exception as exc:
            self._failed_calls += 1
            self._rebuild_catalog()
            return self._error_result(
                tool_name,
                f"MCP call failed: {_safe_error(exc)}",
                started_at,
                descriptor.server_name,
            )

    def get_status(self) -> dict[str, Any]:
        statuses = [
            MCPServerStatus(
                name=name,
                transport=connection.config.transport,
                connected=connection.connected,
                tools_count=len(connection.tools),
                last_error=connection.last_error,
                failure_count=connection.failure_count,
                retry_in_seconds=connection.retry_in_seconds,
            )
            for name, connection in sorted(self._connections.items())
        ]
        return {
            "started": self._started,
            "initializing": bool(self._initial_connect_task and not self._initial_connect_task.done()),
            "enabled": self._runtime.enabled,
            "config_source": self._runtime.source,
            "total_servers": len(statuses),
            "connected_servers": sum(status.connected for status in statuses),
            "total_tools": len(self._tools),
            "total_calls": self._total_calls,
            "successful_calls": self._successful_calls,
            "failed_calls": self._failed_calls,
            "servers": [
                {
                    "name": status.name,
                    "transport": status.transport,
                    "connected": status.connected,
                    "tools_count": status.tools_count,
                    "last_error": status.last_error,
                    "failure_count": status.failure_count,
                    "retry_in_seconds": round(status.retry_in_seconds, 1),
                }
                for status in statuses
            ],
        }

    async def _connect_initial_servers(self) -> None:
        try:
            await asyncio.gather(*(connection.connect() for connection in self._connections.values()))
            self._rebuild_catalog()
            if self._started and self._runtime.reconnect_interval > 0 and self._maintenance_task is None:
                self._maintenance_task = asyncio.create_task(
                    self._maintenance_loop(),
                    name="mcp-core-reconnect",
                )
            logger.info(
                f"MCP 核心运行时已就绪: source={self._runtime.source}, "
                f"servers={len(self._connections)}, tools={len(self._tools)}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"MCP 初始连接任务异常: {_safe_error(exc)}")

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._runtime.reconnect_interval)
                disconnected = [connection for connection in self._connections.values() if connection.can_retry]
                if disconnected:
                    await asyncio.gather(*(connection.connect() for connection in disconnected))
                    self._rebuild_catalog()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"MCP 重连维护异常: {_safe_error(exc)}")

    def _rebuild_catalog(self) -> None:
        tools: dict[str, tuple[MCPToolDescriptor, _ServerConnection]] = {}
        prefix = _safe_identifier(self._runtime.tool_prefix, fallback="mcp")
        for server_name, connection in sorted(self._connections.items()):
            if not connection.connected:
                continue
            for remote_tool in sorted(connection.tools, key=lambda item: item.name):
                base = _exposed_tool_name(prefix, server_name, remote_tool.name)
                exposed_name = base
                if exposed_name in tools:
                    suffix = hashlib.sha256(f"{server_name}\0{remote_tool.name}".encode()).hexdigest()[:8]
                    exposed_name = f"{base[:55]}_{suffix}"
                if _is_disabled(
                    self._runtime.disabled_tools,
                    exposed_name,
                    remote_tool.name,
                ):
                    continue
                descriptor = MCPToolDescriptor(
                    exposed_name=exposed_name,
                    server_name=server_name,
                    remote_name=remote_tool.name,
                    description=f"[{server_name}] {remote_tool.description}"[:1000],
                    input_schema=remote_tool.input_schema,
                )
                tools[exposed_name] = (descriptor, connection)
        self._tools = tools

    def _error_result(
        self,
        tool_name: str,
        error: str,
        started_at: float,
        server_name: str = "",
    ) -> MCPInvocationResult:
        return MCPInvocationResult(
            success=False,
            error=error,
            content=error,
            duration_ms=(time.monotonic() - started_at) * 1000,
            server_name=server_name,
            tool_name=str(tool_name or ""),
        )


def _build_sdk_client(config: MCPServerConfig, call_timeout: float) -> Any:
    from mcp import StdioServerParameters
    from mcp.client import Client
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client

    if config.transport == "stdio":
        transport = stdio_client(
            StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=config.env or None,
                cwd=config.cwd or None,
            )
        )
    elif config.transport == "sse":
        transport = sse_client(
            config.url,
            headers=config.headers or None,
            timeout=min(call_timeout, 60.0),
            sse_read_timeout=max(call_timeout, 60.0),
        )
    else:
        transport = _streamable_transport(config.url, config.headers)
    return Client(transport, read_timeout_seconds=call_timeout, mode="auto", cache=None)


@asynccontextmanager
async def _streamable_transport(url: str, headers: Mapping[str, str]):
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers=dict(headers) or None) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams


async def _close_client(client: Any) -> None:
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=10.0)
    except Exception as exc:
        logger.debug(f"关闭 MCP 客户端时出现异常: {_safe_error(exc)}")


def _normalize_call_result(result: Any) -> tuple[bool, Any, str]:
    is_error = bool(getattr(result, "is_error", False))
    structured = getattr(result, "structured_content", None)
    blocks = [_normalize_content_block(block) for block in getattr(result, "content", ())]
    blocks = [block for block in blocks if block is not None]

    if structured is not None and blocks:
        content: Any = {"structured_content": structured, "content": blocks}
    elif structured is not None:
        content = structured
    elif blocks and all(isinstance(block, str) for block in blocks):
        content = "\n".join(blocks)
    else:
        content = blocks

    error = ""
    if is_error:
        error = content if isinstance(content, str) else "MCP server returned isError=true"
    return not is_error, content, str(error)


def _normalize_content_block(block: Any) -> Any:
    block_type = str(getattr(block, "type", "") or "")
    if block_type == "text":
        return str(getattr(block, "text", "") or "")
    if block_type in {"image", "audio"}:
        data = str(getattr(block, "data", "") or "")
        return {
            "type": block_type,
            "mime_type": str(getattr(block, "mime_type", "") or ""),
            "base64_chars": len(data),
        }
    if block_type == "resource_link":
        return {
            "type": block_type,
            "name": str(getattr(block, "name", "") or ""),
            "uri": str(getattr(block, "uri", "") or ""),
            "description": str(getattr(block, "description", "") or ""),
        }
    if block_type == "resource":
        resource = getattr(block, "resource", None)
        text = getattr(resource, "text", None)
        return {
            "type": block_type,
            "uri": str(getattr(resource, "uri", "") or ""),
            "mime_type": str(getattr(resource, "mime_type", "") or ""),
            "text": str(text) if text is not None else "[binary resource omitted]",
        }
    if hasattr(block, "model_dump"):
        dumped = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(dumped, dict) and "data" in dumped:
            dumped["base64_chars"] = len(str(dumped.pop("data") or ""))
        return dumped
    return str(block)


def _normalize_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(schema)
    if not normalized:
        return {"type": "object", "properties": {}}
    normalized.setdefault("type", "object")
    if normalized.get("type") == "object":
        normalized.setdefault("properties", {})
    return normalized


def _exposed_tool_name(prefix: str, server_name: str, tool_name: str) -> str:
    raw = "_".join(
        (
            prefix,
            _safe_identifier(server_name, fallback="server"),
            _safe_identifier(tool_name, fallback="tool"),
        )
    )
    if len(raw) <= 64:
        return raw
    suffix = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"{raw[:55]}_{suffix}"


def _safe_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_")
    if not normalized:
        normalized = fallback
    if not re.match(r"^[A-Za-z_]", normalized):
        normalized = f"_{normalized}"
    return normalized


def _is_disabled(disabled: frozenset[str], exposed_name: str, remote_name: str) -> bool:
    normalized_remote = remote_name.replace("-", "_").replace(".", "_")
    return bool({exposed_name, remote_name, normalized_remote} & disabled)


def _safe_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(authorization|api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[:500] or exc.__class__.__name__


mcp_service = MCPService()
