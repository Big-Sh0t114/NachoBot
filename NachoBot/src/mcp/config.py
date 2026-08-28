"""Core-owned MCP configuration."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from src.mcp.types import MCPPermissionConfig, MCPRuntimeConfig, MCPServerConfig


class MCPConfigError(ValueError):
    """Raised when MCP configuration is present but structurally invalid."""


def load_runtime_config(
    *,
    mcp_config: Any = None,
) -> MCPRuntimeConfig:
    """Load the complete core MCP runtime configuration."""
    if mcp_config is None:
        from src.config.config import mcp_config as loaded_mcp_config

        mcp_config = loaded_mcp_config

    settings = getattr(mcp_config, "mcp", mcp_config)

    enabled = bool(getattr(settings, "enabled", True))
    core_json = str(getattr(settings, "servers_json", "") or "").strip()
    return MCPRuntimeConfig(
        enabled=enabled,
        servers=tuple(parse_server_config(core_json)) if core_json else (),
        tool_prefix=_tool_prefix(getattr(settings, "tool_prefix", "mcp")),
        disabled_tools=frozenset(_string_items(getattr(settings, "disabled_tools", ()))),
        connect_timeout=_bounded_float(getattr(settings, "connect_timeout_seconds", 20), 20.0, 1.0, 120.0),
        call_timeout=_bounded_float(getattr(settings, "call_timeout_seconds", 60), 60.0, 1.0, 600.0),
        reconnect_interval=_bounded_float(
            getattr(settings, "reconnect_interval_seconds", 30), 30.0, 0.0, 3600.0
        ),
        permissions=_core_permissions(settings),
        source="core",
    )


def parse_server_config(config_json: str) -> list[MCPServerConfig]:
    """Parse Claude Desktop-style `mcpServers` JSON."""
    try:
        raw = json.loads(str(config_json or ""))
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"invalid mcpServers JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise MCPConfigError("MCP server configuration must be a JSON object")

    mapping = raw.get("mcpServers", raw)
    if not isinstance(mapping, dict):
        raise MCPConfigError("mcpServers must be an object")

    servers: list[MCPServerConfig] = []
    for raw_name, raw_config in mapping.items():
        name = str(raw_name or "").strip()
        if not name:
            raise MCPConfigError("MCP server name must be non-empty")
        if not isinstance(raw_config, dict):
            raise MCPConfigError(f"MCP server '{name}' must be an object")

        command = str(raw_config.get("command", "") or "").strip()
        url = str(raw_config.get("url", "") or "").strip()
        if command:
            transport = "stdio"
        elif url:
            transport = _normalize_transport(raw_config.get("transport", raw_config.get("type")))
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise MCPConfigError(f"MCP server '{name}' has an invalid HTTP URL")
        else:
            raise MCPConfigError(f"MCP server '{name}' requires either command or url")

        servers.append(
            MCPServerConfig(
                name=name,
                enabled=bool(raw_config.get("enabled", True)),
                transport=transport,
                command=command,
                args=tuple(_string_list(raw_config.get("args"), f"{name}.args")),
                env=_string_dict(raw_config.get("env"), f"{name}.env"),
                cwd=str(raw_config.get("cwd", "") or ""),
                url=url,
                headers=_string_dict(raw_config.get("headers"), f"{name}.headers"),
            )
        )
    return servers


def _core_permissions(tool_config: Any) -> MCPPermissionConfig:
    return MCPPermissionConfig(
        enabled=bool(getattr(tool_config, "permissions_enabled", True)),
        default_mode=_default_mode(
            getattr(tool_config, "permission_default_mode", "deny_all"),
            fallback="deny_all",
        ),
        quick_allow_users=frozenset(_string_items(getattr(tool_config, "quick_allow_users", ()))),
        quick_deny_groups=frozenset(_string_items(getattr(tool_config, "quick_deny_groups", ()))),
        rules=tuple(_parse_rules(getattr(tool_config, "permission_rules_json", "[]"))),
    )


def _parse_rules(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except json.JSONDecodeError as exc:
            raise MCPConfigError(f"invalid MCP permission rules JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MCPConfigError("MCP permission rules must be a JSON array of objects")
    return [dict(item) for item in value]


def _normalize_transport(value: Any) -> str:
    normalized = str(value or "streamable_http").strip().lower().replace("-", "_")
    aliases = {
        "streamablehttp": "streamable_http",
        "streamable": "streamable_http",
        "streamable_http": "streamable_http",
        "http": "http",
        "sse": "sse",
    }
    if normalized not in aliases:
        raise MCPConfigError(f"unsupported MCP transport: {value}")
    return aliases[normalized]


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MCPConfigError(f"{field_name} must be an array")
    return [str(item) for item in value]


def _string_dict(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MCPConfigError(f"{field_name} must be an object")
    return {str(key): str(item) for key, item in value.items()}


def _string_items(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.splitlines() if item.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _tool_prefix(value: Any) -> str:
    normalized = str(value or "mcp").strip()
    return normalized or "mcp"


def _default_mode(value: Any, *, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in {"allow_all", "deny_all"} else fallback


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))
