from __future__ import annotations

import ast
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, Mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NACHOBOT_ROOT = PROJECT_ROOT / "NachoBot"
if str(NACHOBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(NACHOBOT_ROOT))

from bili_src.chat.comment_handler import CommentHandler
from bili_src.live.outgoing_handler import OutgoingHandler
from bili_src.live2d.remote_controller import PreparedReplyResult
from ncnk_message import BaseMessageInfo, GroupInfo, MessageBase, Seg


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _CoreMessageBase:
    def __init__(self, *, message_info, message_segment=None, raw_message=None):
        self.message_info = message_info
        self.message_segment = message_segment
        self.raw_message = raw_message


class _CoreBaseMessageInfo:
    def __init__(self, **values):
        self.__dict__.update(values)


def _extract_isolated_core_message_constructor():
    delivery_path = NACHOBOT_ROOT / "src/chat/message_receive/delivery_context.py"
    message_path = NACHOBOT_ROOT / "src/chat/message_receive/message.py"
    delivery_tree = ast.parse(delivery_path.read_text(encoding="utf-8"))
    message_tree = ast.parse(message_path.read_text(encoding="utf-8"))
    delivery_helper = next(
        node
        for node in delivery_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "delivery_target_additional_config"
    )
    message_class = next(
        node
        for node in message_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Message"
    )
    constructor = next(
        node
        for node in message_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    isolated_class = ast.ClassDef(
        name="IsolatedCoreMessage",
        bases=[ast.Name(id="MessageBaseStub", ctx=ast.Load())],
        keywords=[],
        body=[copy.deepcopy(constructor)],
        decorator_list=[],
    )
    isolated_module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            copy.deepcopy(delivery_helper),
            isolated_class,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(isolated_module)

    def additional_config_from_message(source):
        config = getattr(source, "additional_config", None)
        return config if isinstance(config, dict) else {}

    namespace = {
        "copy": copy,
        "time": __import__("time"),
        "Any": Any,
        "Optional": Optional,
        "DELIVERY_TARGET_KEY": "delivery_target",
        "MessageBaseStub": _CoreMessageBase,
        "BaseMessageInfo": _CoreBaseMessageInfo,
        "additional_config_from_message": additional_config_from_message,
    }
    exec(  # noqa: S102 - execute only the AST-extracted Core constructor with isolated stubs
        compile(isolated_module, str(message_path), "exec"), namespace
    )
    return namespace["IsolatedCoreMessage"]


class CommentReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger = _Logger()
        self.inbound_messages = []
        self.api = SimpleNamespace(
            send_comment_reply=AsyncMock(return_value={"code": 0}),
            send_danmu=AsyncMock(return_value={"code": 0}),
        )
        self.audio_player = SimpleNamespace(
            handle_voice_stream_event=AsyncMock(return_value=True),
            interrupt_idle=Mock(),
            play=Mock(),
            play_idle=Mock(),
        )
        self.controller = SimpleNamespace(
            on_start_replying=AsyncMock(),
            on_reply_finished=AsyncMock(),
        )
        self.config = SimpleNamespace(
            platform="bilibili",
            dede_user_id="9000",
            comment_resolve_user_nickname=False,
            comment_force_mention=False,
            live_network_search_enabled=False,
        )

        async def capture_inbound(message):
            self.inbound_messages.append(message)

        self.adapter = SimpleNamespace(
            config=self.config,
            api=self.api,
            audio_player=self.audio_player,
            _send_to_nachobot=capture_inbound,
            _filter_outgoing_text=lambda text: text,
            live2d_manager=SimpleNamespace(
                controller=self.controller,
                prepare_reply=AsyncMock(
                    side_effect=lambda text: PreparedReplyResult(
                        str(text), False, "", "test-control"
                    )
                ),
                apply_control=AsyncMock(return_value=True),
            ),
            private_handler=SimpleNamespace(
                resolve_private_target=Mock(return_value=(42, 1)),
                send_private_message=AsyncMock(),
                send_private_image=AsyncMock(),
            ),
            _danmu_cache={},
            _self_danmu_ids={},
            _self_danmu_texts={},
        )
        self.comment_handler = CommentHandler(self.config, self.logger, self.adapter)
        self.adapter.comment_handler = self.comment_handler
        self.outgoing = OutgoingHandler(self.config, self.logger, self.adapter)
        self.core_message_type = _extract_isolated_core_message_constructor()

    def _notice(self, notice_id, subject_id, root_id, source_id):
        return {
            "id": notice_id,
            "user": {"mid": "123", "nickname": "viewer"},
            "item": {
                "business_id": 1,
                "subject_id": subject_id,
                "root_id": root_id,
                "source_id": source_id,
                "target_id": source_id + 1,
                "source_content": f"comment {notice_id}",
            },
        }

    async def _ingest(self, notice):
        await self.comment_handler._handle_reply_notifications([notice], source="reply")
        return self.inbound_messages[-1]

    def _core_reply(self, source_message, message_id):
        group_info = source_message.message_info.group_info
        stream = SimpleNamespace(
            platform=self.config.platform,
            group_info=group_info,
            context=None,
        )
        return self.core_message_type(
            message_id=message_id,
            chat_stream=stream,
            user_info=None,
            message_segment=Seg("text", "reply"),
            reply=source_message,
        )

    def _outgoing_payload(self, source_message, message_id):
        core_reply = self._core_reply(source_message, message_id)
        return MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id=message_id,
                group_info=source_message.message_info.group_info,
                additional_config=core_reply.message_info.additional_config,
            ),
            message_segment=Seg("text", f"reply from {message_id}"),
        ).to_dict()

    def _comment_payload_with_segment(self, source_message, message_id, segment):
        payload = self._outgoing_payload(source_message, message_id)
        payload["message_segment"] = segment.to_dict()
        # Comment routing must keep its delivery target even if a stale or
        # conflicting live-room hint is also present.
        payload["message_info"]["additional_config"]["room_id"] = 555
        return payload

    async def test_overlapping_same_video_replies_keep_their_own_comment_target(self):
        notice_a = await self._ingest(self._notice("notice-a", 777, 100, 101))
        notice_b = await self._ingest(self._notice("notice-b", 777, 200, 201))

        payload_a = self._outgoing_payload(notice_a, "core-reply-a")
        payload_b = self._outgoing_payload(notice_b, "core-reply-b")
        await self.outgoing.handle_from_nachobot(payload_a)
        await self.outgoing.handle_from_nachobot(payload_b)

        self.assertEqual(
            [call.kwargs for call in self.api.send_comment_reply.await_args_list],
            [
                {
                    "comment_type": 1,
                    "oid": 777,
                    "message": "reply from core-reply-a",
                    "root": 100,
                    "parent": 101,
                },
                {
                    "comment_type": 1,
                    "oid": 777,
                    "message": "reply from core-reply-b",
                    "root": 200,
                    "parent": 201,
                },
            ],
        )
        self.adapter.private_handler.send_private_message.assert_not_awaited()
        self.api.send_danmu.assert_not_awaited()

    async def test_comment_target_snapshot_is_copied_through_core_constructor(self):
        notice = await self._ingest(self._notice("notice-copy", 777, 300, 301))
        source_target = notice.message_info.additional_config["delivery_target"]

        core_reply = self._core_reply(notice, "core-reply-copy")
        copied_target = core_reply.message_info.additional_config["delivery_target"]
        source_target["root"] = 999

        self.assertEqual(copied_target["root"], 300)
        self.assertEqual(copied_target["source_message_id"], "notice-copy")

    async def test_unknown_or_mismatched_comment_target_fails_closed(self):
        self.comment_handler.remember_comment_context(
            group_id="comment:1:777",
            comment_type=1,
            comment_oid=777,
            root_id=12,
            source_id=13,
            target_id=14,
        )
        unknown = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id="unknown-reply",
                group_info=GroupInfo(
                    platform=self.config.platform, group_id="comment:1:777"
                ),
            ),
            message_segment=Seg("text", "unknown"),
        ).to_dict()
        mismatched = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id="mismatched-reply",
                group_info=GroupInfo(
                    platform=self.config.platform, group_id="comment:1:777"
                ),
                additional_config={
                    "room_id": 555,
                    "delivery_target": {
                        "kind": "bilibili.comment",
                        "platform": "bilibili",
                        "group_id": "comment:1:888",
                        "scope": {"type": "group", "group_id": "comment:1:888"},
                        "comment_type": 1,
                        "oid": 888,
                        "root": 7,
                        "parent": 8,
                        "source_message_id": "notice-other-video",
                    },
                },
            ),
            message_segment=Seg("text", "mismatched"),
        ).to_dict()

        await self.outgoing.handle_from_nachobot(unknown)
        await self.outgoing.handle_from_nachobot(mismatched)

        self.api.send_comment_reply.assert_not_awaited()
        self.api.send_danmu.assert_not_awaited()
        self.adapter.private_handler.resolve_private_target.assert_not_called()
        self.adapter.private_handler.send_private_message.assert_not_awaited()

    async def test_stale_explicit_reply_reference_fails_closed(self):
        notice = await self._ingest(self._notice("notice-current", 777, 250, 251))
        payload = self._outgoing_payload(notice, "stale-source-reply")
        payload["message_info"]["additional_config"]["reply_to_message_id"] = (
            "notice-stale"
        )

        await self.outgoing.handle_from_nachobot(payload)

        self.api.send_comment_reply.assert_not_awaited()
        self.api.send_danmu.assert_not_awaited()
        self.adapter.private_handler.send_private_message.assert_not_awaited()

    async def test_comment_target_marker_without_group_cannot_fall_through(self):
        payload = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id="comment-no-group",
                additional_config={
                    "room_id": 999,
                    "delivery_target": {"kind": "bilibili.comment"},
                },
            ),
            message_segment=Seg("text", "should be dropped"),
        ).to_dict()

        await self.outgoing.handle_from_nachobot(payload)

        self.api.send_comment_reply.assert_not_awaited()
        self.api.send_danmu.assert_not_awaited()
        self.adapter.private_handler.resolve_private_target.assert_not_called()
        self.adapter.private_handler.send_private_message.assert_not_awaited()

    async def test_comment_replies_on_different_videos_keep_scope(self):
        notice_a = await self._ingest(self._notice("notice-video-a", 777, 400, 401))
        notice_b = await self._ingest(self._notice("notice-video-b", 888, 500, 501))

        await self.outgoing.handle_from_nachobot(
            self._outgoing_payload(notice_a, "reply-video-a")
        )
        await self.outgoing.handle_from_nachobot(
            self._outgoing_payload(notice_b, "reply-video-b")
        )

        calls = self.api.send_comment_reply.await_args_list
        self.assertEqual((calls[0].kwargs["oid"], calls[0].kwargs["root"]), (777, 400))
        self.assertEqual((calls[1].kwargs["oid"], calls[1].kwargs["root"]), (888, 500))

    async def test_explicit_comment_reply_command_remains_independent(self):
        command = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id="explicit-command",
            ),
            message_segment=Seg(
                "command",
                {
                    "name": "BILI_COMMENT_REPLY",
                    "args": {
                        "type": "1",
                        "oid": "777",
                        "root": "600",
                        "parent": "601",
                        "message": "explicit reply",
                    },
                },
            ),
        ).to_dict()

        await self.outgoing.handle_from_nachobot(command)

        self.api.send_comment_reply.assert_awaited_once_with(
            comment_type=1,
            oid=777,
            message="explicit reply",
            root=600,
            parent=601,
        )

    async def test_comment_voice_stream_lifecycle_never_reaches_live_player(self):
        notice = await self._ingest(self._notice("notice-stream", 777, 700, 701))
        events = [
            {
                "event": "start",
                "stream_id": "comment-stream",
                "parent_message_id": "untracked",
            },
            {
                "event": "chunk",
                "stream_id": "comment-stream",
                "parent_message_id": "untracked",
                "seq": 0,
                "audio_base64": "AA==",
            },
            {
                "event": "end",
                "stream_id": "comment-stream",
                "parent_message_id": "untracked",
            },
        ]

        for index, event in enumerate(events):
            await self.outgoing.handle_from_nachobot(
                self._comment_payload_with_segment(
                    notice, f"comment-stream-{index}", Seg("voice_stream", event)
                )
            )

        self.audio_player.handle_voice_stream_event.assert_not_awaited()
        self.controller.on_start_replying.assert_not_awaited()
        self.controller.on_reply_finished.assert_not_awaited()
        self.api.send_comment_reply.assert_not_awaited()

    async def test_buffered_voice_only_comment_reply_is_ignored(self):
        notice = await self._ingest(self._notice("notice-voice-only", 777, 710, 711))
        payload = self._comment_payload_with_segment(
            notice,
            "comment-voice-only",
            Seg("voice", {"audio_base64": "AQID"}),
        )
        play_core_voice = AsyncMock()
        self.outgoing._play_core_voice = play_core_voice

        await self.outgoing.handle_from_nachobot(payload)

        play_core_voice.assert_not_awaited()
        self.audio_player.interrupt_idle.assert_not_called()
        self.audio_player.play.assert_not_called()
        self.controller.on_start_replying.assert_not_awaited()
        self.controller.on_reply_finished.assert_not_awaited()
        self.adapter.live2d_manager.apply_control.assert_not_awaited()
        self.api.send_comment_reply.assert_not_awaited()

    async def test_mixed_comment_text_and_voice_sends_text_without_playback(self):
        notice = await self._ingest(self._notice("notice-mixed", 777, 720, 721))
        payload = self._comment_payload_with_segment(
            notice,
            "comment-mixed",
            Seg(
                "seglist",
                [
                    Seg("text", "text-only comment reply"),
                    Seg("voice", {"audio_base64": "AQID"}),
                ],
            ),
        )
        play_core_voice = AsyncMock()
        self.outgoing._play_core_voice = play_core_voice

        await self.outgoing.handle_from_nachobot(payload)

        self.api.send_comment_reply.assert_awaited_once_with(
            comment_type=1,
            oid=777,
            message="text-only comment reply",
            root=720,
            parent=721,
        )
        play_core_voice.assert_not_awaited()
        self.audio_player.interrupt_idle.assert_not_called()
        self.audio_player.play.assert_not_called()
        self.controller.on_start_replying.assert_not_awaited()
        self.controller.on_reply_finished.assert_not_awaited()

    async def test_live_voice_stream_still_reaches_live_player(self):
        event = {
            "event": "start",
            "stream_id": "live-stream",
            "parent_message_id": "untracked",
        }
        live_payload = MessageBase(
            message_info=BaseMessageInfo(
                platform=self.config.platform,
                message_id="live-stream-start",
                group_info=GroupInfo(platform=self.config.platform, group_id="1234"),
            ),
            message_segment=Seg("voice_stream", event),
        ).to_dict()

        await self.outgoing.handle_from_nachobot(live_payload)

        self.audio_player.handle_voice_stream_event.assert_awaited_once_with(event)
        self.controller.on_start_replying.assert_awaited_once_with()
        self.api.send_comment_reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
