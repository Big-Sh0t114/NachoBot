from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ncnk_message import BaseMessageInfo, Seg, UserInfo

# Follow the Core test import order so plugin APIs do not reverse-import the
# replyer while memory_retrieval is still initializing.
from src.chat.planner_actions.planner import ActionPlanner  # noqa: F401
from src.chat.replyer.group_generator import DefaultReplyer  # noqa: F401
from src.chat.brain_chat.brain_chat import BrainChatting
from src.chat.json_reply_delivery import prepare_json_envelope_delivery
from src.chat.message_receive.message import MessageRecv, MessageSending
from src.chat.message_receive.storage import MessageStorage
from src.common.data_models.message_data_model import ReplyContentType
from src.plugin_system.apis import send_api


def _anchor(*, delivery: str = "json_envelope", tts_language: str = ""):
    trigger_info = BaseMessageInfo(
        platform="webui",
        message_id="source-1",
        time=1.0,
        user_info=UserInfo(
            platform="webui",
            user_id="user-1",
            user_nickname="Tester",
            user_cardname="Tester",
        ),
        additional_config={
            "runtime_capabilities": {
                "schema_version": 1,
                "reply_delivery": delivery,
                "tts_language": tts_language,
            }
        },
    )
    return MessageRecv(
        {
            "message_info": trigger_info.to_dict(),
            "message_segment": Seg(type="text", data="hello").to_dict(),
            "raw_message": "hello",
            "processed_plain_text": "hello",
        }
    )


def _reply_set(text: str):
    return SimpleNamespace(
        reply_data=[SimpleNamespace(content_type=ReplyContentType.TEXT, content=text)]
    )


class _StoreStream:
    stream_id = "stream-1"

    def to_dict(self):
        return {
            "stream_id": self.stream_id,
            "platform": "webui",
            "group_info": None,
            "user_info": None,
            "create_time": 1.0,
            "last_active_time": 2.0,
        }


def _sending_message(*, reply, processed: str, display: str = "") -> MessageSending:
    bot = UserInfo(
        platform="webui",
        user_id="bot",
        user_nickname="Bot",
        user_cardname="Bot",
    )
    source = SimpleNamespace(
        stream_id="stream-1",
        platform="webui",
        user_info=bot,
        group_info=None,
        context=SimpleNamespace(message=reply),
    )
    message = MessageSending(
        message_id="outgoing-1",
        chat_stream=source,
        bot_user_info=bot,
        sender_info=bot,
        message_segment=Seg(type="text", data=processed),
        display_message=display,
        reply=reply,
    )
    message.processed_plain_text = processed
    message.display_message = display
    return message


def _incoming_message(text: str) -> MessageRecv:
    message = _anchor()
    message.processed_plain_text = text
    message.message_info.message_id = "incoming-1"
    message.message_segment = Seg(type="text", data=text)
    return message


class JsonReplyDeliveryTests(unittest.TestCase):
    def test_helper_projects_fenced_unicode_and_preserves_explicit_tts_sidecar(self):
        raw = '```json\n{"reply":"你好 🌙","emotion":"happy","action":"wave","tts_text":{"text":"你好","lang":"zh"}}\n```'

        display, tts = prepare_json_envelope_delivery(raw)

        self.assertEqual(display, "你好 🌙")
        self.assertEqual(
            tts,
            {"text": "你好", "display_text": raw, "lang": "zh"},
        )

    def test_helper_fails_closed_for_empty_bad_and_truncated_envelopes(self):
        for raw in (
            '{"reply":""}',
            '{"reply":"broken" garbage}',
            '{"reply":"unfinished"',
            '```json\n{"reply":"unfinished"\n```',
        ):
            with self.subTest(raw=raw):
                self.assertEqual(prepare_json_envelope_delivery(raw), ("", None))

    def test_helper_leaves_ordinary_text_untouched(self):
        raw = "This is ordinary text with an unfinished { example"
        self.assertEqual(prepare_json_envelope_delivery(raw), (raw, None))

    def test_brain_send_returns_clean_text_but_transports_raw_envelope_and_finalizes_clean_history(self):
        raw = '{"reply":"你好 🌙","emotion":"happy","action":"wave"}'
        trigger = _anchor()
        brain = BrainChatting.__new__(BrainChatting)
        brain.chat_stream = SimpleNamespace(stream_id="stream-1")
        brain.last_read_time = 1.0
        brain.log_prefix = "[test]"
        delivered = send_api.SendReceipt(send_api.SendStatus.DELIVERED, "stream-1")
        send_text = AsyncMock(return_value=delivered)

        async def send_for_finalizer(**kwargs):
            reply_text, _ = await brain._send_response_permitted(
                reply_set=kwargs["reply_set"],
                message_data=kwargs["message_data"],
                selected_expressions=kwargs["selected_expressions"],
                receipts=[],
            )
            return reply_text

        brain._send_response = AsyncMock(side_effect=send_for_finalizer)
        action_store = AsyncMock()

        async def scenario():
            with (
                patch("src.chat.brain_chat.brain_chat.message_api.count_new_messages", return_value=0),
                patch("src.chat.brain_chat.brain_chat.send_api._should_suppress_reply_set", return_value=False),
                patch("src.chat.brain_chat.brain_chat.send_api.text_to_stream_receipt", new=send_text),
                patch("src.chat.brain_chat.brain_chat.database_api.store_action_info", new=action_store),
            ):
                result, receipts = await brain._send_response_permitted(
                    reply_set=_reply_set(raw),
                    message_data=trigger,
                    selected_expressions=None,
                    receipts=[],
                )
                self.assertEqual(result, "你好 🌙")
                self.assertEqual(len(receipts), 1)
                send_text.assert_awaited_once()
                self.assertEqual(send_text.await_args.kwargs["text"], raw)
                self.assertEqual(send_text.await_args.kwargs["display_message"], "你好 🌙")

                loop_info, finalized, _ = await brain._send_and_store_reply(
                    response_set=_reply_set(raw),
                    action_message=trigger,
                    cycle_timers={},
                    thinking_id="think-1",
                    actions=[],
                )
                self.assertEqual(finalized, "你好 🌙")
                self.assertEqual(loop_info["loop_action_info"]["reply_text"], "你好 🌙")
                self.assertEqual(action_store.await_args.kwargs["action_data"], {"reply_text": "你好 🌙"})

        asyncio.run(scenario())

    def test_brain_plain_text_and_undeclared_json_keep_existing_delivery(self):
        brain = BrainChatting.__new__(BrainChatting)
        brain.chat_stream = SimpleNamespace(stream_id="stream-1")
        brain.last_read_time = 1.0
        brain.log_prefix = "[test]"
        delivered = send_api.SendReceipt(send_api.SendStatus.DELIVERED, "stream-1")

        async def run_case(raw: str, trigger, expected: str):
            send_text = AsyncMock(return_value=delivered)
            with (
                patch("src.chat.brain_chat.brain_chat.message_api.count_new_messages", return_value=0),
                patch("src.chat.brain_chat.brain_chat.send_api._should_suppress_reply_set", return_value=False),
                patch("src.chat.brain_chat.brain_chat.send_api.text_to_stream_receipt", new=send_text),
            ):
                result, _ = await brain._send_response_permitted(
                    reply_set=_reply_set(raw),
                    message_data=trigger,
                    selected_expressions=None,
                    receipts=[],
                )
            self.assertEqual(result, expected)
            self.assertEqual(send_text.await_args.kwargs["text"], raw)

        async def scenario():
            await run_case("ordinary reply", _anchor(), "ordinary reply")
            raw_json = '{"reply":"not projected"}'
            await run_case(raw_json, _anchor(delivery="chunked"), raw_json)

        asyncio.run(scenario())

    def test_storage_projects_only_outgoing_json_replies_and_preserves_precleaned_or_tts_text(self):
        raw = '{"reply":"{\\"foo\\":1}","emotion":"happy"}'
        nested_json_reply = '{"foo":1}'
        cases = (
            (
                _sending_message(reply=_anchor(), processed=raw, display=nested_json_reply),
                nested_json_reply,
                nested_json_reply,
            ),
            (
                _sending_message(reply=_anchor(), processed="[voice:buffered-audio]", display="こんにちは"),
                "こんにちは",
                "こんにちは",
            ),
            (_sending_message(reply=_anchor(), processed="ordinary reply"), "ordinary reply", "ordinary reply"),
            (
                _sending_message(reply=_anchor(delivery="chunked"), processed=raw, display=raw),
                raw,
                raw,
            ),
        )

        async def store(message):
            captured = {}

            def create(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(id=7)

            with patch("src.chat.message_receive.storage.Messages.create", side_effect=create):
                await MessageStorage.store_message(message, _StoreStream())
            return captured

        for message, expected_processed, expected_display in cases:
            with self.subTest(processed=message.processed_plain_text):
                if message is cases[0][0]:
                    message.build_reply()
                    asyncio.run(message.process())
                    self.assertIn("[回复<Tester:user-1> 的消息：hello]", message.processed_plain_text)
                    self.assertIn(raw, message.processed_plain_text)
                original_processed = message.processed_plain_text
                row = asyncio.run(store(message))
                self.assertEqual(row["processed_plain_text"], expected_processed)
                self.assertEqual(row["display_message"], expected_display)
                # Storage is a projection; transport state on the message remains unchanged.
                self.assertEqual(message.processed_plain_text, original_processed)

        malformed = _sending_message(reply=_anchor(), processed='{"reply":"unfinished"')
        malformed.build_reply()
        asyncio.run(malformed.process())
        self.assertIn("[回复<Tester:user-1> 的消息：hello]", malformed.processed_plain_text)
        self.assertTrue(malformed.processed_plain_text.endswith('{"reply":"unfinished"'))
        malformed_row = asyncio.run(store(malformed))
        self.assertEqual(malformed_row["processed_plain_text"], "")
        self.assertEqual(malformed_row["display_message"], "")

        incoming = _incoming_message(raw)
        incoming_row = asyncio.run(store(incoming))
        self.assertEqual(incoming_row["processed_plain_text"], raw)
        self.assertEqual(incoming_row["display_message"], "")


if __name__ == "__main__":
    unittest.main()
