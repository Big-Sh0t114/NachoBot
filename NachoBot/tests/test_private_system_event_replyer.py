import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner as _ActionPlanner
from src.chat.replyer.private_generator import PrivateReplyer
from src.common.data_models.database_data_model import DatabaseMessages
from src.plugin_system.base.base_action import BaseAction
import src.chat.replyer.private_generator as private_generator_module


class _PermissionProbeAction(BaseAction):
    action_name = "permission_probe"

    async def execute(self):
        return True, "ok"


class PrivateSystemEventReplyerTests(unittest.TestCase):
    def test_private_system_event_action_keeps_route_peer_out_of_user_principal(self):
        chat_stream = SimpleNamespace(
            stream_id="qq_private_route-peer",
            platform="qq",
            user_info=SimpleNamespace(
                platform="qq",
                user_id="route-peer",
                user_nickname="私聊路由用户",
            ),
            group_info=None,
        )
        action_message = DatabaseMessages(
            message_id="private-action-event",
            time=123.0,
            chat_id="qq_private_route-peer",
            processed_plain_text="用鼠标戳了戳你",
            additional_config=json.dumps(
                {
                    "system_event": {
                        "version": 1,
                        "type": "qq.poke",
                        "actor": {"user_id": "event-actor", "name": "戳人用户"},
                        "target": {"user_id": "bot", "name": "NachoBot"},
                        "data": {},
                    }
                },
                ensure_ascii=False,
            ),
            chat_info_stream_id="qq_private_route-peer",
            chat_info_platform="qq",
            chat_info_user_id="route-peer",
            chat_info_user_nickname="私聊路由用户",
            chat_info_user_platform="qq",
        )

        action = _PermissionProbeAction(
            action_data={},
            reasoning="system event test",
            cycle_timers={},
            thinking_id="thinking-action-event",
            chat_stream=chat_stream,
            action_message=action_message,
        )

        self.assertFalse(action.is_group)
        self.assertEqual(action.target_id, "route-peer")
        self.assertIsNone(action.user_id)
        self.assertIsNone(action.user_nickname)

    def test_private_system_event_has_environment_prompt_without_peer_personalization(self):
        async def exercise():
            replyer = PrivateReplyer.__new__(PrivateReplyer)
            replyer.request_type = "replyer"
            replyer.chat_stream = SimpleNamespace(
                stream_id="qq_private_route-peer",
                platform="qq",
                user_info=SimpleNamespace(
                    platform="qq",
                    user_id="route-peer",
                    user_nickname="私聊路由用户",
                ),
                group_info=None,
            )
            reply_message = DatabaseMessages(
                message_id="private-poke-event",
                time=123.0,
                chat_id="qq_private_route-peer",
                processed_plain_text="用鼠标戳了戳你",
                additional_config=json.dumps(
                    {
                        "system_event": {
                            "version": 1,
                            "type": "qq.poke",
                            "actor": {"user_id": "event-actor", "name": "戳人用户"},
                            "target": {"user_id": "bot", "name": "NachoBot"},
                            "data": {},
                        }
                    },
                    ensure_ascii=False,
                ),
                chat_info_stream_id="qq_private_route-peer",
                chat_info_platform="qq",
                chat_info_user_id="route-peer",
                chat_info_user_nickname="私聊路由用户",
                chat_info_user_platform="qq",
            )

            captured_prompt = {}

            async def fake_format_prompt(template_name, **kwargs):
                captured_prompt["template_name"] = template_name
                captured_prompt.update(kwargs)
                return "prompt"

            replyer.build_expression_habits = AsyncMock(return_value=("", []))
            replyer.build_relation_info = AsyncMock(return_value="peer relation")
            replyer.build_tool_info = AsyncMock(return_value="")
            replyer.get_prompt_info = AsyncMock(return_value="")
            replyer.build_actions_prompt = AsyncMock(return_value="")
            replyer.build_personality_prompt = AsyncMock(return_value="")
            replyer._build_mid_term_memory_block = AsyncMock(return_value="peer memory")
            replyer.build_keywords_reaction_prompt = AsyncMock(return_value="")

            with (
                patch.object(private_generator_module, "advanced_manager") as advanced_manager,
                patch.object(private_generator_module.global_config.chat, "get_max_context_size", return_value=30),
                patch.object(private_generator_module.global_config.mood, "enable_mood", False),
                patch.object(private_generator_module.global_config.advanced, "block_tools_when_on", False),
                patch.object(private_generator_module.global_config.bot, "qq_account", "bot-account"),
                patch.object(private_generator_module.global_config.bot, "platform", "qq"),
                patch.object(private_generator_module.global_config.bot, "nickname", "NachoBot"),
                patch.object(private_generator_module.global_config.personality, "reply_style", "自然"),
                patch.object(private_generator_module, "get_stepped_limit", return_value=10),
                patch.object(private_generator_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]),
                patch.object(private_generator_module, "build_readable_messages", return_value="历史对话"),
                patch.object(private_generator_module, "resolve_sender_name", wraps=private_generator_module.resolve_sender_name) as resolve_name,
                patch.object(private_generator_module, "Person") as person,
                patch.object(private_generator_module, "build_memory_retrieval_prompt", new=AsyncMock(return_value="peer memory")) as memory_retrieval,
                patch.object(private_generator_module.global_prompt_manager, "format_prompt", new=fake_format_prompt),
            ):
                advanced_manager.is_on.return_value = False
                result = await replyer.build_prompt_reply_context(
                    reply_message=reply_message,
                    person_profile_block="私聊路由用户的资料",
                )

            self.assertEqual(result.prompt, "prompt")
            reply_target_block = captured_prompt["reply_target_block"]
            self.assertIn("平台系统事件（qq.poke）", reply_target_block)
            self.assertIn("事件执行者：戳人用户", reply_target_block)
            self.assertIn("事件内容：用鼠标戳了戳你", reply_target_block)
            self.assertIn("环境信息", reply_target_block)
            self.assertNotIn("对方说的:", reply_target_block)
            self.assertNotIn("私聊路由用户", reply_target_block)
            self.assertEqual(captured_prompt["person_profile_block"], "")
            self.assertEqual(captured_prompt["template_name"], "private_replyer_prompt")

            replyer.build_relation_info.assert_not_awaited()
            replyer._build_mid_term_memory_block.assert_not_awaited()
            memory_retrieval.assert_not_awaited()
            replyer.build_tool_info.assert_awaited_once()
            tool_kwargs = replyer.build_tool_info.await_args.kwargs
            self.assertTrue(tool_kwargs["system_event"])
            self.assertEqual(tool_kwargs["sandbox_actor_id"], "")
            resolve_name.assert_not_called()
            person.assert_not_called()

        asyncio.run(exercise())

    def test_system_event_tool_build_never_uses_private_peer_permissions(self):
        async def exercise():
            replyer = PrivateReplyer.__new__(PrivateReplyer)
            replyer.chat_stream = SimpleNamespace(
                stream_id="qq_private_admin-peer",
                platform="qq",
                user_info=SimpleNamespace(user_id="admin-peer", user_nickname="管理员私聊"),
                group_info=None,
                context=None,
            )
            replyer.url_fetcher = SimpleNamespace(build_url_info=AsyncMock())
            replyer.mcp_executor = SimpleNamespace(get_tool_catalog_summary=Mock(return_value="mcp catalog"))
            replyer.web_search_manager = SimpleNamespace(is_available=False)
            replyer.capability_router = SimpleNamespace(decide=AsyncMock())
            replyer.tool_executor = SimpleNamespace(execute_from_chat_message=AsyncMock(return_value=([], [], "")))

            with (
                patch.object(private_generator_module, "access_context_from_stream") as access_context,
                patch.object(private_generator_module, "sandbox_user_allowed", return_value=True) as sandbox_allowed,
            ):
                result = await replyer.build_tool_info(
                    chat_history="",
                    sender="系统事件",
                    target="qq.poke",
                    system_event=True,
                    sandbox_actor_id="admin-peer",
                )

            self.assertEqual(str(result), "")
            access_context.assert_not_called()
            replyer.mcp_executor.get_tool_catalog_summary.assert_not_called()
            sandbox_allowed.assert_not_called()
            replyer.capability_router.decide.assert_not_awaited()

        asyncio.run(exercise())

    def test_ordinary_private_tool_build_keeps_peer_authorization(self):
        async def exercise():
            replyer = PrivateReplyer.__new__(PrivateReplyer)
            replyer.chat_stream = SimpleNamespace(
                stream_id="qq_private_regular-peer",
                platform="qq",
                user_info=SimpleNamespace(user_id="regular-peer", user_nickname="普通私聊用户"),
                group_info=None,
                context=None,
            )
            replyer.url_fetcher = SimpleNamespace(build_url_info=AsyncMock())
            replyer.mcp_executor = SimpleNamespace(get_tool_catalog_summary=Mock(return_value=""))
            replyer.web_search_manager = SimpleNamespace(is_available=False)
            replyer.capability_router = SimpleNamespace(decide=AsyncMock())
            replyer.tool_executor = SimpleNamespace(execute_from_chat_message=AsyncMock(return_value=([], [], "")))
            access_context_result = object()

            with (
                patch.object(
                    private_generator_module,
                    "access_context_from_stream",
                    return_value=access_context_result,
                ) as access_context,
                patch.object(private_generator_module, "sandbox_user_allowed", return_value=False) as sandbox_allowed,
            ):
                result = await replyer.build_tool_info(
                    chat_history="",
                    sender="普通私聊用户",
                    target="你好",
                )

            self.assertEqual(str(result), "")
            access_context.assert_called_once_with(replyer.chat_stream)
            replyer.mcp_executor.get_tool_catalog_summary.assert_called_once_with(
                access_context=access_context_result
            )
            sandbox_allowed.assert_called_once_with("regular-peer")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
