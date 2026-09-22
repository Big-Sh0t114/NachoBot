import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ncnk_message import build_system_event
# Match the Core test import order that initializes plugin APIs before the
# replyer module's reverse import through generator_api.
from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401
# Import the replyer module first so its memory-retrieval dependency is fully
# initialized before BrainChatting imports BrainPlanner through plugin APIs.
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
import src.chat.brain_chat.brain_chat as brain_chat_module
from src.common.data_models.database_data_model import DatabaseMessages


class BrainChatSystemEventFinalizerTests(unittest.TestCase):
    @staticmethod
    def _persisted_event_message() -> DatabaseMessages:
        event = build_system_event(
            "qq.poke",
            actor={"user_id": "event-actor", "name": "戳人用户"},
            target={"user_id": "bot", "name": "NachoBot"},
        )
        return DatabaseMessages(
            message_id="persisted-private-event",
            time=123.0,
            chat_id="qq-private-peer",
            processed_plain_text="用鼠标戳了戳你",
            additional_config=json.dumps({"system_event": event}, ensure_ascii=False),
            chat_info_stream_id="qq-private-peer",
            chat_info_platform="qq",
        )

    def test_senderless_system_event_finalizer_stores_environment_reply(self):
        async def exercise():
            runtime = BrainChatting.__new__(BrainChatting)
            runtime.chat_stream = SimpleNamespace(stream_id="qq-private-peer", platform="qq")
            action_message = self._persisted_event_message()
            send_order = []

            async def fake_send_response(**kwargs):
                send_order.append("send")
                return "系统事件回复"

            async def fake_store_action_info(**kwargs):
                send_order.append("store")

            runtime._send_response = AsyncMock(side_effect=fake_send_response)
            with (
                patch.object(
                    brain_chat_module.database_api,
                    "store_action_info",
                    new=AsyncMock(side_effect=fake_store_action_info),
                ) as store_action_info,
                patch.object(brain_chat_module, "Person") as person,
            ):
                result = await runtime._send_and_store_reply(
                    response_set=object(),
                    action_message=action_message,
                    cycle_timers={},
                    thinking_id="thinking-event",
                    actions=[],
                )

            self.assertEqual(send_order, ["send", "store"])
            runtime._send_response.assert_awaited_once()
            store_action_info.assert_awaited_once()
            self.assertEqual(
                store_action_info.await_args.kwargs["action_prompt_display"],
                "你对系统事件进行了回复：系统事件回复",
            )
            person.assert_not_called()
            self.assertEqual(result[1], "系统事件回复")

        asyncio.run(exercise())

    def test_ordinary_private_finalizer_keeps_person_display(self):
        async def exercise():
            runtime = BrainChatting.__new__(BrainChatting)
            runtime.chat_stream = SimpleNamespace(stream_id="qq-private-peer", platform="qq")
            action_message = DatabaseMessages(
                message_id="ordinary-private-message",
                time=123.0,
                chat_id="qq-private-peer",
                processed_plain_text="你好",
                chat_info_stream_id="qq-private-peer",
                chat_info_platform="qq",
                user_id="peer-7",
                user_nickname="私聊用户",
                user_platform="qq",
            )
            runtime._send_response = AsyncMock(return_value="普通回复")
            person = SimpleNamespace(person_name="私聊用户")
            with (
                patch.object(
                    brain_chat_module.database_api,
                    "store_action_info",
                    new=AsyncMock(),
                ) as store_action_info,
                patch.object(brain_chat_module, "Person", return_value=person) as person_ctor,
            ):
                result = await runtime._send_and_store_reply(
                    response_set=object(),
                    action_message=action_message,
                    cycle_timers={},
                    thinking_id="thinking-ordinary",
                    actions=[],
                )

            person_ctor.assert_called_once_with(platform="qq", user_id="peer-7")
            self.assertEqual(
                store_action_info.await_args.kwargs["action_prompt_display"],
                "你对私聊用户进行了回复：普通回复",
            )
            self.assertEqual(result[1], "普通回复")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
