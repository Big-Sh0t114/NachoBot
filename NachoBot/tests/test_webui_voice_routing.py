"""Platform-neutral tests for per-message runtime routing capabilities."""

import json
import asyncio
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.chat.planner_actions.planner import ActionPlanner
from src.chat.brain_chat.brain_planner import BrainPlanner
from src.chat.runtime_capabilities import (
    classify_runtime_message_batch,
    planner_target_eligible,
    runtime_capabilities_from_message,
)
from src.common.data_models.database_data_model import DatabaseMessages
import src.chat.brain_chat.brain_planner as brain_planner_module
import src.chat.planner_actions.planner as action_planner_module


def _message(
    message_id: str,
    *,
    platform: str = "local",
    planner_bypass=...,
    source: str | None = None,
    channel: str | None = None,
    user_id: str | None = "caller",
    text: str = "message",
    extra_config: dict | None = None,
) -> DatabaseMessages:
    additional = dict(extra_config or {})
    if source is not None:
        additional["source"] = source
    if channel is not None:
        additional["webui_channel"] = channel
    if planner_bypass is not ...:
        additional["runtime_capabilities"] = {
            "schema_version": 1,
            "planner_bypass": planner_bypass,
        }
    return DatabaseMessages(
        message_id=message_id,
        time=float(len(message_id)),
        chat_id="private-chat",
        processed_plain_text=text,
        display_message=text,
        additional_config=json.dumps(additional, ensure_ascii=False),
        user_id=user_id or "",
        user_nickname="Caller" if user_id else "",
        user_platform=platform if user_id else "",
        chat_info_stream_id="private-chat",
        chat_info_platform=platform,
    )


class RuntimeCapabilityRoutingTests(unittest.TestCase):
    def test_only_strict_per_message_boolean_selects_direct_reply(self):
        voice = _message(
            "voice",
            source="webui-chat",
            channel="voice",
            planner_bypass=True,
        )
        text = _message(
            "text",
            source="webui-chat",
            channel="text",
            planner_bypass=False,
        )
        discord_voice_without_declaration = _message(
            "discord-voice",
            platform="discord",
            channel="voice",
        )
        qq_voice_with_truthy_string = _message(
            "qq-voice",
            platform="qq",
            channel="voice",
            planner_bypass="true",
        )

        batch = classify_runtime_message_batch(
            [voice, text, discord_voice_without_declaration, qq_voice_with_truthy_string]
        )

        self.assertEqual(batch.direct_messages, (voice,))
        self.assertEqual(
            batch.planner_messages,
            (text, discord_voice_without_declaration, qq_voice_with_truthy_string),
        )
        self.assertEqual(batch.explicit_planner_messages, (text,))
        self.assertTrue(runtime_capabilities_from_message(voice).planner_bypass)
        self.assertFalse(runtime_capabilities_from_message(text).planner_bypass)
        self.assertFalse(runtime_capabilities_from_message(qq_voice_with_truthy_string).planner_bypass_declared)
        self.assertFalse(planner_target_eligible(voice))
        self.assertTrue(planner_target_eligible(text))

    def test_source_platform_and_channel_are_opaque_to_core_routing(self):
        misleading_voice = _message(
            "voice-without-capability",
            source="webui-chat",
            channel="voice",
        )
        declared_direct = _message(
            "declared-direct-on-discord",
            platform="discord",
            source="arbitrary-adapter",
            channel="text",
            planner_bypass=True,
        )

        batch = classify_runtime_message_batch([misleading_voice, declared_direct])

        self.assertEqual(batch.planner_messages, (misleading_voice,))
        self.assertEqual(batch.direct_messages, (declared_direct,))

    def test_senderless_platform_event_can_be_direct_without_being_a_user(self):
        event = _message(
            "event",
            user_id=None,
            planner_bypass=True,
            extra_config={"platform_event": {"kind": "support", "amount": 2}},
        )

        batch = classify_runtime_message_batch([event])

        self.assertEqual(batch.direct_messages, (event,))
        self.assertIsNone(event.user_info)

    def test_interruption_feedback_count_is_strictly_typed_and_bounded(self):
        def with_feedback(value):
            message = _message("feedback", planner_bypass=True)
            config = json.loads(message.additional_config)
            config["runtime_capabilities"]["interruption_feedback"] = value
            message.additional_config = json.dumps(config)
            return runtime_capabilities_from_message(message).interruption_feedback_count

        self.assertEqual(with_feedback({"count": 3}), 3)
        self.assertEqual(with_feedback({"count": 500}), 8)
        self.assertEqual(with_feedback({"count": -3}), 0)
        self.assertEqual(with_feedback({"count": True}), 0)
        self.assertEqual(with_feedback({"count": "4"}), 0)
        self.assertEqual(with_feedback([4]), 0)


class RealPlannerTargetFilteringTests(unittest.TestCase):
    def test_both_planners_keep_full_context_filter_targets_and_preserve_bot_shortcut_boundary(self):
        def make_message(message_id, text, *, bypass=False, bot=False):
            config = (
                {"runtime_capabilities": {"schema_version": 1, "planner_bypass": True}}
                if bypass
                else {}
            )
            return DatabaseMessages(
                message_id=message_id,
                time=float(len(message_id)),
                chat_id="planner-private",
                processed_plain_text=text,
                display_message=text,
                additional_config=json.dumps(config, ensure_ascii=False),
                user_id=str(global_bot_id) if bot else "caller",
                user_nickname="NachoBot" if bot else "Caller",
                user_platform=(action_planner_module.global_config.bot.platform if bot else "local"),
                chat_info_stream_id="planner-private",
                chat_info_platform="local",
            )

        global_bot_id = str(action_planner_module.global_config.bot.qq_account)

        async def check_planner(planner_type, planner_module, *, messages, expect_shortcut=False):
            planner = object.__new__(planner_type)
            planner.chat_id = "planner-private"
            planner.log_prefix = "[planner-private]"
            planner.last_obs_time_mark = 0.0
            planner.get_necessary_info = Mock(return_value=(False, None, {}))
            planner._filter_actions_by_activation_type = Mock(return_value={})
            prompt_calls = []

            async def build_prompt(**kwargs):
                prompt_calls.append(kwargs)
                return "prompt", kwargs["message_id_list"]

            async def execute_main_planner(**kwargs):
                if expect_shortcut:
                    return []
                return planner._parse_single_action(
                    {
                        "action": "reply",
                        "target_message_id": "voice-url",
                    },
                    kwargs["message_id_list"],
                    [],
                    allow_no_reply=kwargs["allow_no_reply"],
                )

            planner.build_planner_prompt = AsyncMock(side_effect=build_prompt)
            execute = AsyncMock(side_effect=execute_main_planner)
            planner._execute_main_planner = execute

            def render(messages, **_kwargs):
                return (
                    "\n".join(message.processed_plain_text or "" for message in messages),
                    [(message.message_id, message) for message in messages],
                )

            with ExitStack() as stack:
                stack.enter_context(patch.object(planner_module, "get_raw_msg_before_timestamp_with_chat", return_value=messages))
                stack.enter_context(patch.object(planner_module, "get_stepped_limit", return_value=30))
                stack.enter_context(patch.object(planner_module, "build_readable_messages_with_id", side_effect=render))
                stack.enter_context(patch.object(planner_module, "can_offer_switch_chat", return_value=False))
                stack.enter_context(patch.object(planner_module.global_config.chat, "get_max_context_size", return_value=30))
                if planner_type is BrainPlanner:
                    stack.enter_context(patch.object(planner_module, "has_active_focus_lease", return_value=False))
                    stack.enter_context(patch.object(planner_module.global_config.bot, "integrated_plan", False))
                    stack.enter_context(patch.object(planner_module.global_config.focus, "mode", "off"))
                actions, _ = await planner.plan(available_actions={}, allow_no_reply=True)

            self.assertEqual(execute.await_count, 1)
            return planner, actions, prompt_calls[0]

        async def exercise():
            for planner_type, planner_module in (
                (BrainPlanner, brain_planner_module),
                (ActionPlanner, action_planner_module),
            ):
                voice_url = make_message("voice-url", "https://voice.example", bypass=True)
                ordinary_text = make_message("text", "ordinary typed request")
                with self.subTest(planner=planner_type.__name__, scenario="direct-url-filter-and-fallback"):
                    _, actions, prompt = await check_planner(
                        planner_type,
                        planner_module,
                        messages=[voice_url, ordinary_text],
                    )
                    self.assertIn("https://voice.example", prompt["chat_content_block"])
                    self.assertIn("ordinary typed request", prompt["chat_content_block"])
                    self.assertEqual([message for _, message in prompt["message_id_list"]], [ordinary_text])
                    self.assertEqual(len(actions), 1)
                    self.assertIs(actions[0].action_message, ordinary_text)

                url = make_message("old-url", "https://old.example")
                reminder = make_message("old-reminder", "5 分钟后提醒我取文件")
                bot = make_message("bot-reply", "已保存提醒和链接", bot=True)
                with self.subTest(planner=planner_type.__name__, scenario="latest-bot-does-not-reexpose-shortcut"):
                    _, actions, prompt = await check_planner(
                        planner_type,
                        planner_module,
                        messages=[url, reminder, bot],
                        expect_shortcut=True,
                    )
                    self.assertEqual(actions, [])
                    self.assertIn("https://old.example", prompt["chat_content_block"])
                    self.assertIn("5 分钟后提醒我取文件", prompt["chat_content_block"])
                    self.assertEqual([message for _, message in prompt["message_id_list"]], [url, reminder])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
