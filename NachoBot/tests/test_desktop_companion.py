"""Desktop private prompt and typed delivery; no model/service calls."""
from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ncnk_message import Seg

from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401 - Core import order
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.heart_flow.heartFC_chat import HeartFChatting
from src.chat.message_receive.message import MessageSending
from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.runtime_capabilities import RuntimeCapabilities, runtime_capabilities_from_message
from src.chat.structured_reply_delivery import normalize_reply
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.data_models.message_data_model import ReplySetModel
from src.llm_models.prompt_layout import validated_segments
from src.chat.replyer.companion_prompt import desktop_visual_context
from src.config.config import global_config
from src.plugin_system.apis import generator_api, send_api
import src.chat.replyer.private_generator as private_module


def trigger():
    return DatabaseMessages(
        message_id="turn-1", time=123.0, chat_id="pet-private", processed_plain_text="你好",
        user_id="host", user_nickname="用户", user_platform="local.live2d",
        chat_info_stream_id="pet-private", chat_info_platform="local.live2d",
        additional_config=json.dumps({"runtime_capabilities": {
            "schema_version": 1, "planner_bypass": True, "prompt_profile": "desktop_companion",
            "reply_delivery": "structured_reply", "reply_controls": True,
            "control_emotions": ["normal", "joy"], "control_actions": ["NOD"],
        }}),
    )


def stream(message):
    return SimpleNamespace(stream_id="pet-private", platform="local.live2d", group_info=None,
                           user_info=message.user_info, context=SimpleNamespace(message=message))


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def test_proactive_senderless_event_uses_companion_prompt_without_user_impersonation(self):
        import time
        from ncnk_message import get_system_event, get_system_event_route
        from src.chat.runtime_capabilities import classify_runtime_message_batch
        extra = json.loads(trigger().additional_config)
        extra.update({
            "system_event": {"version": 1, "type": "desktop.idle", "actor": None, "target": None,
                             "data": {"idle_seconds": 300}},
            "system_event_route": {"platform": "local.live2d", "kind": "private", "peer": {"user_id": "host", "nickname": "用户"}},
            "desktop_context": {"schema_version": 1, "captured_at": time.time(), "summary": "可见代码", "window_title": "Editor"},
        })
        message = DatabaseMessages(message_id="idle-1", time=time.time(), chat_id="pet-private",
                                   processed_plain_text="[系统事件: desktop.idle]", additional_config=json.dumps(extra),
                                   chat_info_stream_id="pet-private", chat_info_platform="local.live2d",
                                   chat_info_user_id="host", chat_info_user_nickname="用户", chat_info_user_platform="local.live2d")
        self.assertIsNone(message.user_info)
        self.assertEqual(get_system_event_route(message)["peer"]["user_id"], "host")
        batch = classify_runtime_message_batch([message], is_system_event=get_system_event)
        self.assertEqual(batch.direct_messages, (message,))
        replyer = PrivateReplyer.__new__(PrivateReplyer)
        replyer.chat_stream = stream(message)
        replyer.chat_stream.user_info = SimpleNamespace(user_id="host", user_nickname="用户")
        replyer.request_type = "replyer"
        for name, value in {
            "build_expression_habits": ("", []), "build_relation_info": "RELATION",
            "build_tool_info": "", "get_prompt_info": "", "build_actions_prompt": "",
            "build_personality_prompt": "EXISTING_PERSONA", "_build_mid_term_memory_block": "",
            "build_keywords_reaction_prompt": "",
        }.items():
            setattr(replyer, name, AsyncMock(return_value=value))
        with ExitStack() as stack:
            stack.enter_context(patch.object(private_module.advanced_manager, "is_on", return_value=False))
            stack.enter_context(patch.object(private_module.global_config.chat, "get_max_context_size", return_value=30))
            stack.enter_context(patch.object(private_module.global_config.mood, "enable_mood", False))
            stack.enter_context(patch.object(private_module, "get_stepped_limit", return_value=10))
            stack.enter_context(patch.object(private_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]))
            stack.enter_context(patch.object(private_module, "build_readable_messages", return_value="真实对话历史"))
            templates = stack.enter_context(patch.object(private_module.global_prompt_manager, "format_prompt", AsyncMock()))
            result = await replyer.build_prompt_reply_context(reply_message=message)
        self.assertIn("EXISTING_PERSONA", result.prompt)
        self.assertIn("[桌宠主动对话触发，非用户发言]", result.prompt)
        self.assertIn("用户约300秒未发言", result.prompt)
        self.assertIn("画面：可见代码", result.prompt)
        self.assertIn("真实对话历史", result.prompt)
        self.assertIn('"controls"', result.prompt)
        self.assertNotIn("用户本轮消息：", result.prompt)
        replyer.build_relation_info.assert_not_awaited()
        self.assertTrue(replyer.build_tool_info.await_args.kwargs["system_event"])
        templates.assert_not_awaited()

    def test_normalization_rejects_malformed_json_and_unlisted_controls(self):
        caps = runtime_capabilities_from_message(trigger())
        self.assertTrue(caps.memory_retrieval and caps.mid_term_memory and caps.mcp_enabled)
        self.assertEqual(caps.tool_mode, "standard")
        for raw in ('{"text":', '```json\n{}\n```', '[]', '"scalar"', '{"reply":"old protocol"}'):
            with self.subTest(raw=raw):
                result = normalize_reply(raw, "turn-1", caps)
                self.assertEqual(result["status"], "error")
                self.assertNotIn("text", result)
        result = normalize_reply('{"text":"你好","controls":{"emotion":"JOY","action":"nod"}}', "turn-1", caps)
        self.assertEqual(result["controls"], {"emotion": "joy", "action": "NOD"})
        result = normalize_reply('{"text":"你好","controls":{"emotion":{},"action":"execute"}}', "turn-1", caps)
        self.assertEqual(result["controls"], {"emotion": "", "action": ""})
        self.assertEqual(normalize_reply("普通文字", "turn-1", caps)["text"], "普通文字")

    async def test_private_prompt_keeps_actual_memory_and_tool_results(self):
        message = trigger()
        import time
        additional = json.loads(message.additional_config)
        additional["desktop_context"] = {"schema_version": 1, "captured_at": time.time(),
                                         "summary": "VLM_RESULT", "window_title": "Editor"}
        message.additional_config = json.dumps(additional)
        replyer = PrivateReplyer.__new__(PrivateReplyer)
        replyer.chat_stream = stream(message)
        replyer.request_type = "replyer"
        values = {
            "build_expression_habits": ("", []), "build_relation_info": "RELATION_RESULT",
            "build_tool_info": "TOOL_RESULT", "get_prompt_info": "KNOWLEDGE_RESULT",
            "build_actions_prompt": "", "build_personality_prompt": "EXISTING_PERSONA",
            "_build_mid_term_memory_block": "MID_TERM_RESULT", "build_keywords_reaction_prompt": "",
        }
        for name, value in values.items():
            setattr(replyer, name, AsyncMock(return_value=value))
        with ExitStack() as stack:
            stack.enter_context(patch.object(private_module.advanced_manager, "is_on", return_value=False))
            stack.enter_context(patch.object(private_module.global_config.chat, "get_max_context_size", return_value=30))
            stack.enter_context(patch.object(private_module.global_config.mood, "enable_mood", True))
            stack.enter_context(patch.object(private_module.mood_manager, "get_mood_by_chat_id", return_value=SimpleNamespace(mood_state="ROLE_CALM_STATE")))
            stack.enter_context(patch.object(private_module, "get_stepped_limit", return_value=10))
            stack.enter_context(patch.object(private_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]))
            stack.enter_context(patch.object(private_module, "build_readable_messages", return_value="HISTORY_RESULT"))
            stack.enter_context(patch.object(private_module, "Person", return_value=SimpleNamespace(person_name="用户")))
            memory = stack.enter_context(patch.object(private_module, "build_memory_retrieval_prompt", AsyncMock(return_value="MEMORY_RESULT")))
            templates = stack.enter_context(patch.object(private_module.global_prompt_manager, "format_prompt", AsyncMock()))
            result = await replyer.build_prompt_reply_context(reply_message=message, enable_tool=True)
        chunks = validated_segments(result.prompt)
        self.assertIsNotNone(chunks)
        self.assertIn("EXISTING_PERSONA", chunks[0])
        self.assertIn("一对一", chunks[0])
        self.assertIn("HISTORY_RESULT", chunks[1])
        for expected in ("MEMORY_RESULT", "MID_TERM_RESULT", "TOOL_RESULT", "KNOWLEDGE_RESULT", "用户本轮消息：你好",
                         "[角色内部情绪，不属于用户发言]\nROLE_CALM_STATE", "画面：VLM_RESULT", "不是用户发言"):
            self.assertIn(expected, chunks[2])
        self.assertNotIn("你正在qq群", result.prompt)
        templates.assert_not_awaited()
        memory.assert_awaited_once()
        self.assertTrue(replyer.build_tool_info.await_args.kwargs["enable_tool"])
        self.assertFalse(replyer.build_tool_info.await_args.kwargs["system_event"])

    def test_visual_context_has_strict_version_expiry_and_trigger_message_scope(self):
        message = trigger()
        extra = json.loads(message.additional_config)
        value = {"schema_version": 1, "captured_at": 990, "summary": "可见代码", "window_title": "Editor"}
        extra["desktop_context"] = value
        message.additional_config = json.dumps(extra)
        self.assertIn("约10秒前", desktop_visual_context(message, now=1000))
        self.assertIn("不是用户发言", desktop_visual_context(message, now=1000))
        self.assertEqual(desktop_visual_context(message, now=1200), "")
        self.assertEqual(desktop_visual_context(message, now=900), "")
        for version in (2, True, "1"):
            value["schema_version"] = version
            message.additional_config = json.dumps(extra)
            self.assertEqual(desktop_visual_context(message, now=1000), "")

    async def test_generator_enables_tools_and_preserves_exact_model_payload(self):
        message = trigger()
        target = stream(message)
        raw = '{"text":"回答。","controls":{"emotion":"joy","action":"NOD"}}'
        generated = SimpleNamespace(content=raw, sandbox_edit_handoff=None, context_refs=[])
        replyer = SimpleNamespace(chat_stream=target, generate_reply_with_context=AsyncMock(return_value=(True, generated)))
        with patch.object(global_config.tool, "enable_tool", True), \
             patch.object(generator_api, "get_replyer", return_value=replyer), \
             patch.object(generator_api, "assemble_reply_context", AsyncMock(return_value=SimpleNamespace(context_refs=()))), \
             patch.object(generator_api, "process_human_text", side_effect=AssertionError("structured data must not be split")):
            success, result = await generator_api.generate_reply(chat_stream=target, reply_message=message)
        self.assertTrue(success)
        self.assertTrue(replyer.generate_reply_with_context.await_args.kwargs["enable_tool"])
        self.assertNotIn("Return exactly one JSON object", replyer.generate_reply_with_context.await_args.kwargs["extra_info"])
        self.assertEqual(result.reply_set.reply_data[0].content, raw)

    async def test_both_chat_pipelines_use_typed_delivery_and_human_only_receipts(self):
        import src.chat.message_receive.uni_message_sender as sender
        for cls, module_name in ((BrainChatting, "src.chat.brain_chat.brain_chat"),
                                 (HeartFChatting, "src.chat.heart_flow.heartFC_chat")):
            for delivered in (True, False):
                with self.subTest(cls=cls.__name__, delivered=delivered):
                    message = trigger()
                    chat = cls.__new__(cls)
                    chat.chat_stream = stream(message)
                    chat.last_read_time = 0
                    chat.log_prefix = "[test]"
                    replies = ReplySetModel()
                    replies.add_text_content('{"text":"普通正文","controls":{"emotion":"joy","action":"nod"}}')
                    receipts = []
                    receipt = SimpleNamespace(delivered=delivered)
                    with patch(module_name + ".message_api.count_new_messages", return_value=0), \
                         patch.object(sender, "_should_suppress_text_reply", return_value=False), \
                         patch.object(send_api, "structured_reply_to_stream_receipt", AsyncMock(return_value=receipt)) as send:
                        text, returned = await chat._send_response_permitted(replies, message, [7], receipts)
                    self.assertIs(returned, receipts)
                    self.assertEqual(receipts, [receipt])
                    self.assertEqual(text, "普通正文" if delivered else "")
                    kwargs = send.await_args.kwargs
                    self.assertEqual(kwargs["content"]["request_id"], "turn-1")
                    self.assertEqual(kwargs["content"]["controls"]["action"], "NOD")
                    self.assertEqual(kwargs["display_message"], "普通正文")

    async def test_transport_uses_normal_receipt_path_and_history_projects_only_text(self):
        payload = normalize_reply('{"text":"正文","controls":{"emotion":"joy"}}', "turn-1", RuntimeCapabilities())
        with patch.object(send_api, "_send_to_target_receipt", AsyncMock()) as send:
            await send_api.structured_reply_to_stream_receipt(payload, "pet-private", reply_message=trigger(), display_message="正文")
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["message_segment"].type, "structured_reply")
        self.assertTrue(kwargs["storage_message"])
        self.assertNotIn("require_platform_ack", kwargs)
        sending = MessageSending.__new__(MessageSending)
        text = await sending._process_message_segments(Seg(type="structured_reply", data=payload))
        self.assertEqual(text, "正文")
        self.assertNotIn("controls", text)
