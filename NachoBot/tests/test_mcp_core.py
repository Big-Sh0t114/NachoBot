from __future__ import annotations

import json
import asyncio
import types
import unittest
from pathlib import Path

import tomlkit

from src.mcp.config import load_runtime_config, parse_server_config
from src.mcp.policy import MCPPermissionPolicy
from src.config.official_configs import MCPConfig, ToolConfig
from src.mcp.service import MCPService, _safe_error
from src.mcp.types import (
    MCPAccessContext,
    MCPPermissionConfig,
    MCPRuntimeConfig,
    MCPServerConfig,
)


class _Tool:
    def __init__(self, name: str, description: str, input_schema: dict):
        self.name = name
        self.description = description
        self.input_schema = input_schema


class _Text:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeClient:
    def __init__(self, tools, results=None):
        self.tools = tools
        self.results = list(results or [])
        self.entered = False
        self.closed = False
        self.calls = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def list_tools(self, **_kwargs):
        return types.SimpleNamespace(tools=self.tools, next_cursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FailingClient(_FakeClient):
    async def __aenter__(self):
        raise ConnectionError("offline")


class _BlockingClient(_FakeClient):
    def __init__(self, tools, ready: asyncio.Event):
        super().__init__(tools)
        self.ready = ready

    async def __aenter__(self):
        await self.ready.wait()
        return await super().__aenter__()


class MCPConfigTests(unittest.TestCase):
    def test_mcp_template_is_independent_from_tool_config(self):
        template_path = Path(__file__).resolve().parents[1] / "template" / "mcp_config_template.toml"
        bot_template_path = Path(__file__).resolve().parents[1] / "template" / "bot_config_template.toml"
        mcp_document = tomlkit.parse(template_path.read_text(encoding="utf-8"))
        bot_document = tomlkit.parse(bot_template_path.read_text(encoding="utf-8"))

        config = MCPConfig.from_dict(mcp_document)
        self.assertTrue(config.mcp.enabled)
        self.assertEqual(config.mcp.max_calls, 5)
        self.assertFalse(any(name.startswith("mcp") for name in ToolConfig.__dataclass_fields__))
        self.assertFalse(any(name.startswith("mcp") for name in bot_document["tool"]))

    def test_invalid_core_permission_mode_fails_closed(self):
        mcp_config = types.SimpleNamespace(
            mcp=types.SimpleNamespace(
                enabled=True,
                servers_json='{"mcpServers":{}}',
                permission_default_mode="typo",
            )
        )
        runtime = load_runtime_config(mcp_config=mcp_config)
        self.assertEqual(runtime.permissions.default_mode, "deny_all")

    def test_parse_server_config_preserves_nested_json_schema_inputs(self):
        servers = parse_server_config(
            json.dumps(
                {
                    "mcpServers": {
                        "calendar": {
                            "command": "calendar-server",
                            "args": ["--stdio"],
                            "env": {"TOKEN": "secret"},
                        },
                        "remote": {
                            "url": "https://example.test/mcp",
                            "transport": "streamable-http",
                            "headers": {"Authorization": "Bearer secret"},
                        },
                    }
                }
            )
        )
        self.assertEqual([server.name for server in servers], ["calendar", "remote"])
        self.assertEqual(servers[0].transport, "stdio")
        self.assertEqual(servers[1].transport, "streamable_http")

    def test_parse_server_config_preserves_windows_root_arguments(self):
        servers = parse_server_config(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {
                            "command": "cmd",
                            "args": [
                                "/c",
                                "npx",
                                "-y",
                                "@modelcontextprotocol/server-filesystem",
                                "C:\\",
                                "D:\\",
                            ],
                        }
                    }
                }
            )
        )
        self.assertEqual(servers[0].args[-2:], ("C:\\", "D:\\"))

    def test_empty_core_catalog_still_applies_core_policy_and_limits(self):
        mcp_config = types.SimpleNamespace(
            mcp=types.SimpleNamespace(
                enabled=True,
                servers_json="",
                tool_prefix="remote",
                disabled_tools=["blocked_tool"],
                connect_timeout_seconds=33,
                call_timeout_seconds=44,
                reconnect_interval_seconds=55,
                permissions_enabled=True,
                permission_default_mode="deny_all",
                quick_allow_users=["42"],
                quick_deny_groups=["100"],
                permission_rules_json="[]",
            )
        )
        runtime = load_runtime_config(mcp_config=mcp_config)
        self.assertEqual(runtime.source, "core")
        self.assertEqual(runtime.servers, ())
        self.assertEqual(runtime.tool_prefix, "remote")
        self.assertEqual(runtime.disabled_tools, frozenset({"blocked_tool"}))
        self.assertEqual(runtime.connect_timeout, 33)
        self.assertEqual(runtime.call_timeout, 44)
        self.assertEqual(runtime.reconnect_interval, 55)
        self.assertEqual(runtime.permissions.quick_allow_users, frozenset({"42"}))


class MCPPermissionPolicyTests(unittest.TestCase):
    def test_policy_filters_by_user_group_and_tool_rule(self):
        policy = MCPPermissionPolicy(
            MCPPermissionConfig(
                enabled=True,
                default_mode="deny_all",
                quick_allow_users=frozenset({"admin"}),
                quick_deny_groups=frozenset({"blocked-group"}),
                rules=(
                    {
                        "tool": "mcp_calendar_*",
                        "mode": "whitelist",
                        "allowed": ["qq:42:user"],
                        "denied": [],
                    },
                ),
            )
        )
        self.assertTrue(policy.allows("mcp_anything", MCPAccessContext(user_id="admin")))
        self.assertTrue(policy.allows("mcp_calendar_list", MCPAccessContext(user_id="42")))
        self.assertFalse(policy.allows("mcp_calendar_list", MCPAccessContext(user_id="43")))
        self.assertFalse(
            policy.allows(
                "mcp_calendar_list",
                MCPAccessContext(user_id="42", chat_id="blocked-group", is_group=True),
            )
        )


class MCPServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_status_errors_redact_common_credentials(self):
        error = _safe_error(
            RuntimeError("Authorization=Bearer-raw token=abc password:123 https://user:pass@example.test")
        )
        self.assertNotIn("abc", error)
        self.assertNotIn("password:123", error)
        self.assertNotIn("user:pass", error)

    async def test_service_owns_catalog_schema_permission_and_invocation(self):
        input_schema = {
            "type": "object",
            "properties": {
                "filters": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"field": {"type": "string"}}},
                }
            },
            "required": ["filters"],
        }
        client = _FakeClient(
            [_Tool("list-events", "List events", input_schema)],
            [
                types.SimpleNamespace(
                    is_error=False,
                    structured_content={"event_ids": ["evt-1"]},
                    content=[_Text("one event")],
                ),
                types.SimpleNamespace(
                    is_error=True,
                    structured_content=None,
                    content=[_Text("remote denied")],
                ),
            ],
        )
        runtime = MCPRuntimeConfig(
            enabled=True,
            servers=(MCPServerConfig(name="calendar", transport="stdio", command="demo"),),
            reconnect_interval=0,
            permissions=MCPPermissionConfig(
                enabled=True,
                default_mode="deny_all",
                quick_allow_users=frozenset({"42"}),
            ),
        )
        service = MCPService(config_loader=lambda: runtime, client_factory=lambda _config, _timeout: client)
        await service.start()
        self.addAsyncCleanup(service.shutdown)

        denied = service.get_tool_definitions(MCPAccessContext(user_id="7"))
        self.assertEqual(denied, [])
        allowed = service.get_tool_definitions(MCPAccessContext(user_id="42"))
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["input_schema"], input_schema)
        exposed_name = allowed[0]["name"]

        result = await service.invoke(
            exposed_name,
            {"filters": [{"field": "date"}]},
            context=MCPAccessContext(user_id="42"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.content["structured_content"], {"event_ids": ["evt-1"]})
        self.assertEqual(client.calls[0][0], "list-events")

        error = await service.invoke(exposed_name, {}, context=MCPAccessContext(user_id="42"))
        self.assertFalse(error.success)
        self.assertEqual(error.error, "remote denied")

    async def test_failed_side_effecting_call_is_not_retried(self):
        client = _FakeClient(
            [_Tool("send", "Send a message", {"type": "object", "properties": {}})],
            [ConnectionError("response lost")],
        )
        runtime = MCPRuntimeConfig(
            enabled=True,
            servers=(MCPServerConfig(name="chat", transport="stdio", command="demo"),),
            reconnect_interval=0,
            permissions=MCPPermissionConfig(enabled=False, default_mode="allow_all"),
        )
        service = MCPService(config_loader=lambda: runtime, client_factory=lambda _config, _timeout: client)
        await service.start()
        self.addAsyncCleanup(service.shutdown)
        name = service.get_tool_definitions()[0]["name"]

        result = await service.invoke(name, {"text": "hello"})
        self.assertFalse(result.success)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(service.get_tool_definitions(), [])

    async def test_failed_server_uses_bounded_reconnect_backoff(self):
        client = _FailingClient([])
        runtime = MCPRuntimeConfig(
            enabled=True,
            servers=(MCPServerConfig(name="offline", transport="stdio", command="demo"),),
            reconnect_interval=5,
            permissions=MCPPermissionConfig(enabled=False, default_mode="allow_all"),
        )
        service = MCPService(config_loader=lambda: runtime, client_factory=lambda _config, _timeout: client)
        await service.start()
        self.addAsyncCleanup(service.shutdown)

        status = service.get_status()["servers"][0]
        self.assertEqual(status["failure_count"], 1)
        self.assertGreater(status["retry_in_seconds"], 0)

    async def test_main_start_can_initialize_connections_in_background(self):
        ready = asyncio.Event()
        client = _BlockingClient([], ready)
        runtime = MCPRuntimeConfig(
            enabled=True,
            servers=(MCPServerConfig(name="slow", transport="stdio", command="demo"),),
            reconnect_interval=0,
            permissions=MCPPermissionConfig(enabled=False, default_mode="allow_all"),
        )
        service = MCPService(config_loader=lambda: runtime, client_factory=lambda _config, _timeout: client)
        await service.start(wait_for_connections=False)
        self.addAsyncCleanup(service.shutdown)
        self.assertTrue(service.get_status()["initializing"])

        ready.set()
        await service.start()
        self.assertFalse(service.get_status()["initializing"])


if __name__ == "__main__":
    unittest.main()
