"""Central permission evaluation for MCP discovery and execution."""

from __future__ import annotations

import fnmatch
from typing import Any, Iterable

from src.mcp.types import MCPAccessContext, MCPPermissionConfig


class MCPPermissionPolicy:
    """Evaluate the legacy-compatible policy without trusting chat callers."""

    def __init__(self, config: MCPPermissionConfig) -> None:
        self.config = config

    def allows(self, tool_name: str, context: MCPAccessContext) -> bool:
        if context.is_admin:
            return True
        if not self.config.enabled:
            return True

        user_id = str(context.user_id or "")
        chat_id = str(context.chat_id or "")
        if user_id and user_id in self.config.quick_allow_users:
            return True
        if context.is_group and chat_id and chat_id in self.config.quick_deny_groups:
            return False

        context_ids = self._context_ids(context)
        for rule in self.config.rules:
            if not isinstance(rule, dict) or not fnmatch.fnmatch(tool_name, str(rule.get("tool", "") or "")):
                continue

            denied = self._string_items(rule.get("denied"))
            if any(self._matches_any(denied, value) for value in context_ids):
                return False

            allowed = self._string_items(rule.get("allowed"))
            if any(self._matches_any(allowed, value) for value in context_ids):
                return True
            if allowed and str(rule.get("mode", "")) == "whitelist":
                return False

        return self.config.default_mode == "allow_all"

    @staticmethod
    def _context_ids(context: MCPAccessContext) -> tuple[str, ...]:
        values: list[str] = []
        if context.user_id:
            values.append(f"qq:{context.user_id}:user")
        if context.chat_id:
            suffix = "group" if context.is_group else "private"
            values.append(f"qq:{context.chat_id}:{suffix}")
        return tuple(values)

    @staticmethod
    def _string_items(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return ()
        return tuple(str(item) for item in value if str(item))

    @staticmethod
    def _matches_any(patterns: Iterable[str], value: str) -> bool:
        return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)
