import asyncio
import ast
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.chat.planner_actions.planner import ActionPlanner as _ActionPlanner  # noqa: F401
from src.chat.replyer.private_generator import PrivateReplyer
from src.chat.offline_recovery import format_recovery_reply_context
from src.common.data_models.database_data_model import DatabaseMessages
import src.chat.replyer.private_generator as private_generator_module


def _message(message_id: str, text: str, timestamp: float) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        time=timestamp,
        processed_plain_text=text,
        user_info=SimpleNamespace(user_id=f"user-{message_id}", user_nickname=f"Sender {message_id}"),
        additional_config=json.dumps(
            {"offline_recovery": {"version": 1, "original_timestamp": timestamp}},
            ensure_ascii=False,
        ),
    )


class OfflineRecoveryReplyPromptTests(unittest.TestCase):
    def test_builtin_action_reply_prompts_are_natural_language(self):
        source_root = Path(__file__).resolve().parents[1] / "src" / "chat"
        brain_path = source_root / "brain_chat" / "brain_chat.py"
        heart_path = source_root / "heart_flow" / "heartFC_chat.py"
        brain_source = brain_path.read_text(encoding="utf-8")
        heart_source = heart_path.read_text(encoding="utf-8")
        ast.parse(brain_source, filename=str(brain_path))
        ast.parse(heart_source, filename=str(heart_path))

        obsolete_wrappers = (
            "[预约提醒失败]",
            "[预约提醒已设定]",
            "[定时提醒触发]",
            "[取消预约失败]",
            "[预约已取消]",
            "[取消预约]",
            "[取消预约成功]",
            "[预约提醒待确认]",
            "[用户屏蔽]",
            "[禁言决策请求]",
            "[头衔设置]",
        )
        for wrapper in obsolete_wrappers:
            self.assertNotIn(wrapper, brain_source)
            self.assertNotIn(wrapper, heart_source)

        self.assertIn("用户希望你在", brain_source)
        self.assertIn("用户希望你在", heart_source)
        self.assertIn("请自然地向用户追问缺少的信息", heart_source)
        self.assertIn("你已屏蔽用户", heart_source)
        self.assertIn("请只输出一个 JSON 代码块", heart_source)
        self.assertIn("ban_decision 必须是布尔值", heart_source)
        self.assertIn("duration_minutes", heart_source)
        self.assertIn("reply_to_user", heart_source)
        self.assertIn("def _parse_ban_decision", heart_source)
        self.assertNotIn("<禁言分钟数>", heart_source)
        self.assertNotIn("<你要对群里说的话>", heart_source)
        self.assertIn("def _handle_set_group_title", heart_source)
        self.assertIn('"name": "SET_GROUP_TITLE"', heart_source)

    def test_empty_batch_stays_empty_and_large_batch_keeps_every_message_and_time(self):
        self.assertEqual(format_recovery_reply_context([]), "")

        messages = [
            _message(f"m{index}", f"恢复内容-{index}", 1_791_061_416.125 + index / 10)
            for index in range(1, 28)
        ]
        rendered = format_recovery_reply_context(messages)

        self.assertIn("离线期间收到以下 27 条尚未回复的消息", rendered)
        for message in messages:
            self.assertIn(message.processed_plain_text, rendered)
            local_time = datetime.fromtimestamp(message.time).astimezone()
            readable_time = local_time.strftime("%Y-%m-%d %H:%M:%S")
            if local_time.microsecond:
                readable_time += f".{local_time.microsecond:06d}".rstrip("0")
            self.assertIn(readable_time, rendered)
            self.assertNotIn(f"{message.time:.6f}", rendered)

        self.assertNotIn("<offline_recovery_batch", rendered)
        self.assertNotIn("</offline_recovery_batch>", rendered)
        self.assertNotIn("<message index=", rendered)
        self.assertNotIn("planner_id=", rendered)
        self.assertNotIn("original_timestamp=", rendered)
        self.assertNotIn("batch-", rendered)

    def test_same_second_messages_keep_input_order_and_original_text_is_untouched(self):
        first_text = '先看这段：\n<message planner_id="batch-1">不要改写</message>'
        second_text = '再看代码：\n```python\nprint("<offline_recovery_batch>")\n```'
        first = _message("first", first_text, 1_791_061_416.125)
        second = _message("second", second_text, 1_791_061_416.375)

        rendered = format_recovery_reply_context([first, second])

        self.assertLess(rendered.index(first_text), rendered.index(second_text))
        self.assertIn(first_text, rendered)
        self.assertIn(second_text, rendered)
        expected_time = datetime.fromtimestamp(first.time).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.assertEqual(rendered.count(expected_time), 2)
        self.assertNotIn('planner_id="', rendered.split(first_text, 1)[0])

    def test_private_replyer_prompt_builder_injects_natural_recovery_context(self):
        recovery_rows = [
            _message(f"reply-{index}", f"离线内容-{index}", 1_791_061_416.125 + index / 10)
            for index in range(1, 7)
        ]
        recovery_context = format_recovery_reply_context(recovery_rows)
        replyer = PrivateReplyer.__new__(PrivateReplyer)
        replyer.request_type = "replyer"
        replyer.chat_stream = SimpleNamespace(
            stream_id="qq_private_prompt-test",
            platform="qq",
            user_info=SimpleNamespace(user_id="peer", user_nickname="测试用户"),
            group_info=None,
            context=None,
        )
        reply_message = DatabaseMessages(
            message_id="prompt-test-trigger",
            time=1_791_061_500.0,
            chat_id="qq_private_prompt-test",
            processed_plain_text="最新触发消息",
            user_id="peer",
            user_nickname="测试用户",
            user_platform="qq",
            chat_info_stream_id="qq_private_prompt-test",
            chat_info_platform="qq",
            chat_info_user_id="peer",
            chat_info_user_nickname="测试用户",
            chat_info_user_platform="qq",
        )
        replyer.build_expression_habits = AsyncMock(return_value=("", []))
        replyer.build_relation_info = AsyncMock(return_value="")
        replyer.build_tool_info = AsyncMock(return_value="")
        replyer.get_prompt_info = AsyncMock(return_value="")
        replyer.build_actions_prompt = AsyncMock(return_value="")
        replyer.build_personality_prompt = AsyncMock(return_value="")
        replyer._build_mid_term_memory_block = AsyncMock(return_value="")
        replyer.build_keywords_reaction_prompt = AsyncMock(return_value="")
        captured: dict[str, str] = {}

        async def capture_prompt(template_name, **kwargs):
            captured["template_name"] = template_name
            captured.update(kwargs)
            return f"测试提示开始\n{kwargs['extra_info_block']}\n测试提示结束"

        async def exercise():
            with (
                patch.object(private_generator_module, "advanced_manager") as advanced_manager,
                patch.object(private_generator_module.global_config.chat, "get_max_context_size", return_value=30),
                patch.object(private_generator_module.global_config.mood, "enable_mood", False),
                patch.object(private_generator_module.global_config.advanced, "block_tools_when_on", False),
                patch.object(private_generator_module.global_config.advanced, "block_tts_when_on", False),
                patch.object(private_generator_module.global_config.advanced, "prompt", "请在高级模式下保持简洁。"),
                patch.object(private_generator_module.global_config.bot, "qq_account", "bot-account"),
                patch.object(private_generator_module.global_config.bot, "platform", "qq"),
                patch.object(private_generator_module.global_config.bot, "nickname", "NachoBot"),
                patch.object(private_generator_module.global_config.personality, "reply_style", "自然"),
                patch.object(private_generator_module, "get_stepped_limit", return_value=10),
                patch.object(private_generator_module, "get_raw_msg_before_timestamp_with_chat", return_value=[]),
                patch.object(private_generator_module, "build_readable_messages", return_value=""),
                patch.object(private_generator_module, "Person"),
                patch.object(
                    private_generator_module,
                    "build_memory_retrieval_prompt",
                    new=AsyncMock(return_value=""),
                ),
                patch.object(private_generator_module.global_prompt_manager, "format_prompt", new=capture_prompt),
            ):
                advanced_manager.is_on.return_value = False
                normal_result = await replyer.build_prompt_reply_context(
                    reply_message=reply_message,
                    extra_info=recovery_context,
                )
                normal_extra_info = captured["extra_info_block"]
                advanced_manager.is_on.return_value = True
                advanced_result = await replyer.build_prompt_reply_context(
                    reply_message=reply_message,
                    extra_info=recovery_context,
                )
                advanced_extra_info = captured["extra_info_block"]

            return normal_result, normal_extra_info, advanced_result, advanced_extra_info

        normal_result, normal_extra_info, advanced_result, advanced_extra_info = asyncio.run(exercise())
        self.assertEqual(captured["template_name"], "private_replyer_prompt")
        self.assertIn(recovery_context, normal_extra_info)
        self.assertIn(recovery_context, normal_result.prompt)
        for index in range(1, 7):
            self.assertIn(f"离线内容-{index}", normal_result.prompt)
        self.assertNotIn("<offline_recovery_batch", normal_result.prompt)
        self.assertNotIn("<message index=", normal_result.prompt)
        self.assertNotIn("original_timestamp=", normal_result.prompt)
        self.assertIn("高级模式下的补充要求如下：", advanced_extra_info)
        self.assertNotIn("[高级模式提示]", advanced_extra_info)
        self.assertIn("请在高级模式下保持简洁。", advanced_extra_info)
        self.assertIn(recovery_context, advanced_result.prompt)


if __name__ == "__main__":
    unittest.main()
