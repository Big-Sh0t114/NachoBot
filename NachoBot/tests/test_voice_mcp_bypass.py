"""MCP opt-out follows the trigger task, including concurrent text in one chat."""
import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.plugin_system.apis import generator_api  # Initialize the production API import graph first.
from src.chat.replyer import group_generator, private_generator
from src.chat.runtime_capabilities import RuntimeCapabilities, scoped_runtime_capabilities
from src.chat.utils.capability_router import CapabilityDecision


class VoiceMCPBypassTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, module, *, anonymous=False, web=False):
        cls = module.DefaultReplyer if module is group_generator else module.PrivateReplyer
        replyer = object.__new__(cls)
        replyer.chat_stream = SimpleNamespace(
            stream_id="same-chat", platform="qq", context=None,
            user_info=SimpleNamespace(user_id="caller"), group_info=None,
        )
        replyer.url_fetcher = SimpleNamespace(build_url_info=AsyncMock(return_value=""))
        replyer.web_search_manager = SimpleNamespace(is_available=web)
        replyer.tool_executor = SimpleNamespace(execute_from_chat_message=AsyncMock(return_value=([], [], "")))
        replyer.mcp_executor = SimpleNamespace(get_tool_catalog_summary=Mock(return_value="browser catalog"))
        replyer.capability_router = SimpleNamespace(decide=AsyncMock(return_value=CapabilityDecision()))
        mcp = AsyncMock(return_value=([], [], ""))
        search = AsyncMock(return_value=None)
        kwargs = {"chat_history": "", "sender": "", "target": "打开浏览器"}
        if module is group_generator:
            kwargs.update(user_id="", actorless_anonymous=anonymous)
        access = Mock(return_value=SimpleNamespace())
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, "access_context_from_stream", access))
            stack.enter_context(patch.object(module, "sandbox_user_allowed", return_value=False))
            stack.enter_context(patch.object(module, "execute_mcp_after_decision", mcp))
            stack.enter_context(patch.object(module, "build_search_after_decision", search))

            # A shared stream can point at text while the exact voice trigger
            # is scoped to its generation task. No catalog/ACL/MCP use is allowed.
            with scoped_runtime_capabilities("same-chat", RuntimeCapabilities(mcp_enabled=False)):
                await replyer.build_tool_info(**kwargs)
            access.assert_not_called()
            replyer.mcp_executor.get_tool_catalog_summary.assert_not_called()
            mcp.assert_not_awaited()
            if web:
                self.assertFalse(replyer.capability_router.decide.await_args.kwargs["allow_mcp"])
                self.assertEqual(replyer.capability_router.decide.await_args.kwargs["mcp_catalog"], "")
                search.assert_awaited_once()
            else:
                replyer.capability_router.decide.assert_not_awaited()
            self.assertEqual(replyer.tool_executor.execute_from_chat_message.await_count, 0 if anonymous else 1)

            # Concurrent generations on the SAME replyer keep their own opt-out.
            replyer.capability_router.decide.reset_mock()
            async def generate(enabled):
                with scoped_runtime_capabilities("same-chat", RuntimeCapabilities(mcp_enabled=enabled)):
                    await asyncio.sleep(0)
                    await replyer.build_tool_info(**kwargs)
            await asyncio.gather(generate(False), generate(True))
            self.assertEqual(replyer.mcp_executor.get_tool_catalog_summary.call_count, 1)
            self.assertEqual(mcp.await_count, 1)
            if not web:
                self.assertEqual(replyer.capability_router.decide.await_count, 1)
            self.assertTrue(any(call.kwargs["allow_mcp"] for call in replyer.capability_router.decide.await_args_list))

            # Ordinary text after the voice scope returns to default MCP access.
            await replyer.build_tool_info(**kwargs)
            self.assertEqual(replyer.mcp_executor.get_tool_catalog_summary.call_count, 2)
            self.assertEqual(mcp.await_count, 2)

    async def test_private_voice_and_text(self):
        await self.exercise(private_generator)

    async def test_group_voice_and_text(self):
        await self.exercise(group_generator)

    async def test_anonymous_group_preserves_identity_boundary(self):
        await self.exercise(group_generator, anonymous=True)

    async def test_private_web_remains_available_without_mcp(self):
        await self.exercise(private_generator, web=True)

    async def test_group_web_remains_available_without_mcp(self):
        await self.exercise(group_generator, anonymous=True, web=True)

    def test_mapping_requires_boolean_and_defaults_to_existing_behavior(self):
        self.assertFalse(RuntimeCapabilities.from_mapping({"mcp_enabled": False}).mcp_enabled)
        for mapping in ({}, {"mcp_enabled": "false"}, {"mcp_enabled": 0}, {"schema_version": 99, "mcp_enabled": False}):
            self.assertTrue(RuntimeCapabilities.from_mapping(mapping).mcp_enabled)
