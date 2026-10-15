from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot"))

from ncnk_message import BaseMessageInfo, GroupInfo, MessageBase, Seg  # noqa: E402
from bili_src.audio.audio_player import AudioPlayer  # noqa: E402
from bili_src.audio.tts_manager import TTSManager  # noqa: E402
from bili_src.live.outgoing_handler import OutgoingHandler  # noqa: E402
from bili_src.live.two_phase_search import BilibiliLiveSearchOrchestrator  # noqa: E402
from bili_src.live2d.remote_controller import PreparedReplyResult  # noqa: E402


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _AudioPlayer:
    def __init__(self, results=()):
        self.results = list(results)
        self.events = []
        self.played = []
        self.interrupt_idle = Mock()

    async def handle_voice_stream_event(self, payload):
        event = dict(payload) if isinstance(payload, dict) else payload
        self.events.append(event)
        accepted = self.results.pop(0) if self.results else True
        if accepted and isinstance(event, dict):
            if event.get("event") == "start":
                self.active_voice_stream_id = event.get("stream_id")
            elif (
                event.get("event") in {"end", "abort"}
                and event.get("stream_id") == getattr(self, "active_voice_stream_id", None)
            ):
                self.active_voice_stream_id = None
        return accepted

    def play(self, audio_data):
        self.played.append(audio_data)


class _FakePCMOutput:
    def __init__(self):
        self.writes = []
        self.finished = False
        self.aborted = False
        self.closed = False

    def write(self, pcm):
        self.writes.append(pcm)
        return True

    def finish(self):
        self.finished = True

    def wait_until_drained(self, _timeout):
        return True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True


class _Live2DManager:
    def __init__(self, prepare=None):
        self.controller = SimpleNamespace(
            on_start_replying=AsyncMock(),
            on_reply_finished=AsyncMock(),
        )
        self.apply_control = AsyncMock(return_value=True)
        self.prepare_reply = AsyncMock(side_effect=prepare or self._prepare)

    @staticmethod
    async def _prepare(raw):
        envelope = OutgoingHandler._reply_envelope(raw)
        if envelope is not None:
            return PreparedReplyResult(
                envelope["reply"],
                bool(envelope.get("web_search")),
                str(envelope.get("search_query") or ""),
                "test-control",
            )
        return PreparedReplyResult(str(raw or ""), False, "", "test-control")


class LiveReplyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.subtitle_path = Path(self.tempdir.name) / "subtitle.txt"
        self.logger = _Logger()
        self.audio_player = _AudioPlayer()
        self.live2d_manager = _Live2DManager()
        self.api = SimpleNamespace(
            send_danmu=AsyncMock(
                return_value={"code": 0, "data": {"dmid": "sent-dmid"}}
            )
        )
        self.tts_manager = TTSManager.__new__(TTSManager)
        self.tts_manager.logger = self.logger
        self.tts_manager.subtitle_path = str(self.subtitle_path)
        self.adapter = SimpleNamespace(
            config=SimpleNamespace(
                live_network_search_enabled=False,
                live_room_prompts={
                    100: {"tts": {"subtitle_path": str(self.subtitle_path)}}
                },
                platform="bilibili.live",
            ),
            api=self.api,
            audio_player=self.audio_player,
            live2d_manager=self.live2d_manager,
            tts_manager=self.tts_manager,
            comment_handler=SimpleNamespace(
                resolve_comment_target=Mock(return_value=None),
                send_comment_reply_from_context=AsyncMock(),
            ),
            private_handler=SimpleNamespace(
                resolve_private_target=Mock(return_value=None),
                send_private_message=AsyncMock(),
            ),
            _danmu_cache={100: {"viewer-dmid": "cached-mid"}},
            _self_danmu_ids={},
            _self_danmu_texts={},
            _filter_outgoing_text=lambda value: value,
        )
        self.tts_manager.config = self.adapter.config
        self.tts_manager._tts_manual_overrides = {}
        self.tts_manager._lang_overrides = {}
        self.adapter.tts_manager = self.tts_manager
        self.handler = OutgoingHandler(self.adapter.config, self.logger, self.adapter)

    def tearDown(self):
        self.tempdir.cleanup()

    def _message(self, message_id, segment, room_id=100):
        return MessageBase(
            message_info=BaseMessageInfo(
                platform="bilibili.live",
                message_id=message_id,
                group_info=GroupInfo(platform="bilibili", group_id=str(room_id)),
            ),
            message_segment=segment,
        ).to_dict()

    def _initial(
        self, message_id="reply-1", *, reply="显示正文", search=False, tts_lang="ja"
    ):
        envelope = {
            "reply": reply,
            "tts_text": {"text": "TTS_SECRET_SPEECH", "lang": tts_lang},
            "emotion": "angry",
            "action": "点头/同意",
            "web_search": search,
            "search_query": "天气",
        }
        return self._message(
            message_id,
            Seg(
                "seglist",
                [
                    Seg("reply", "viewer-dmid"),
                    Seg("text", json.dumps(envelope, ensure_ascii=False)),
                ],
            ),
        )

    def _stream_event(
        self,
        parent_message_id,
        event,
        *,
        stream_id="stream-1",
        room_id=100,
        seq=0,
    ):
        payload = {
            "event": event,
            "stream_id": stream_id,
            "parent_message_id": parent_message_id,
            "sample_rate": 24_000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
        }
        if event == "chunk":
            payload["seq"] = seq
            payload["audio_base64"] = base64.b64encode(b"\x01\x00" * 32).decode("ascii")
        return self._message(
            f"{parent_message_id}-{event}-{stream_id}-{seq}",
            Seg("voice_stream", payload),
            room_id,
        )

    def _subtitle(self):
        return self.subtitle_path.read_text(encoding="utf-8-sig")

    async def _send_stream(self, parent, event, **kwargs):
        await self.handler.handle_from_nachobot(
            self._stream_event(parent, event, **kwargs)
        )

    async def test_stream_success_writes_only_display_reply_and_suppresses_ack(self):
        initial = self._initial(reply="显示正文\u200b")
        await self.handler.handle_from_nachobot(initial)
        self.assertEqual(self._subtitle(), "显示正文")
        self.assertNotIn("TTS_SECRET_SPEECH", self._subtitle())

        self.api.send_danmu.assert_not_awaited()

        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "chunk")
        await self._send_stream("reply-1", "end")
        await self._send_stream("reply-1", "end")
        await self._send_stream("reply-1", "start", stream_id="stream-2")
        second_start_timer = self.handler._reply_states["reply-1"].timer
        await self._send_stream("reply-1", "chunk", stream_id="stream-2")
        second_chunk_timer = self.handler._reply_states["reply-1"].timer
        await asyncio.sleep(0)
        self.assertIsNot(second_start_timer, second_chunk_timer)
        self.assertTrue(second_start_timer.cancelled())
        await self._send_stream("reply-1", "end", stream_id="stream-2")
        await asyncio.sleep(0)
        self.assertTrue(second_chunk_timer.cancelled())
        self.assertIsNone(self.handler._reply_states["reply-1"].timer)
        await self.handler.handle_from_nachobot(initial)

        self.assertEqual(
            [event["event"] for event in self.audio_player.events],
            ["start", "chunk", "end", "start", "chunk", "end"],
        )
        self.api.send_danmu.assert_not_awaited()
        self.live2d_manager.apply_control.assert_awaited_once_with("test-control")
        self.live2d_manager.controller.on_start_replying.assert_awaited_once()
        self.assertEqual(self._subtitle(), "显示正文")
        await self.handler.shutdown()

    async def test_prepare_waits_for_parent_events_and_preserves_arrival_order(self):
        prepare_started = asyncio.Event()
        release_prepare = asyncio.Event()

        async def blocked_prepare(raw):
            prepare_started.set()
            await release_prepare.wait()
            return await _Live2DManager._prepare(raw)

        self.live2d_manager.prepare_reply = AsyncMock(side_effect=blocked_prepare)
        initial_task = asyncio.create_task(
            self.handler.handle_from_nachobot(self._initial())
        )
        await prepare_started.wait()
        self.assertIn("reply-1", self.handler._reply_states)

        event_tasks = [
            asyncio.create_task(self._send_stream("reply-1", event))
            for event in ("start", "chunk", "end")
        ]
        await asyncio.sleep(0)
        self.assertEqual(self.audio_player.events, [])

        release_prepare.set()
        await asyncio.gather(initial_task, *event_tasks)

        self.assertEqual([event["event"] for event in self.audio_player.events], ["start", "chunk", "end"])
        self.assertTrue(self.handler._reply_states["reply-1"].audio_succeeded)
        self.api.send_danmu.assert_not_awaited()
        await self.handler.shutdown()

    async def test_shutdown_during_prepare_does_not_publish_late_reply(self):
        self.adapter.config.live_network_search_enabled = True
        prepare_started = asyncio.Event()
        release_prepare = asyncio.Event()

        async def blocked_prepare(raw):
            prepare_started.set()
            await release_prepare.wait()
            return await _Live2DManager._prepare(raw)

        self.live2d_manager.prepare_reply = AsyncMock(side_effect=blocked_prepare)
        initial_task = asyncio.create_task(
            self.handler.handle_from_nachobot(self._initial(search=True))
        )
        await prepare_started.wait()
        state = self.handler._reply_states["reply-1"]

        await self.handler.shutdown()
        release_prepare.set()
        await initial_task

        self.assertTrue(state.evicted)
        self.assertTrue(state.ready.is_set())
        self.assertFalse(self.subtitle_path.exists())
        self.api.send_danmu.assert_not_awaited()
        self.assertFalse(self.handler.live_search._tasks)

    async def test_timeout_fallback_is_once_targeted_and_does_not_rewrite_subtitle(self):
        await self.handler.handle_from_nachobot(self._initial(reply="字幕与回复"))
        state = self.handler._reply_states["reply-1"]
        subtitle_before = self._subtitle()

        await self.handler._expire_pending_reply(state)
        await self.handler._expire_pending_reply(state)

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="字幕与回复",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual(self._subtitle(), subtitle_before)
        self.assertEqual(state.status, "fallback")
        self.assertEqual(state.timer, None)
        await self.handler.handle_from_nachobot(self._initial(reply="字幕与回复"))
        self.api.send_danmu.assert_awaited_once()
        await self.handler.shutdown()

    async def test_real_initial_timeout_sends_once_without_cancelling_itself(self):
        self.handler.PENDING_REPLY_TIMEOUT_SECONDS = 0.01
        send_started = asyncio.Event()

        async def send_danmu(**_kwargs):
            send_started.set()
            return {"code": 0, "data": {"dmid": "sent-dmid"}}

        self.api.send_danmu.side_effect = send_danmu
        await self.handler.handle_from_nachobot(self._initial())
        timer = self.handler._reply_states["reply-1"].timer
        await asyncio.wait_for(send_started.wait(), timeout=0.2)
        await asyncio.wait_for(timer, timeout=0.2)

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual(self.handler._reply_states["reply-1"].status, "fallback")
        await self._send_stream("reply-1", "start", stream_id="late-after-timeout")
        self.assertEqual(self.audio_player.events, [])
        await self.handler.shutdown()

    async def test_rejected_start_waits_for_buffered_fallback_in_same_room(self):
        self.audio_player.results = [False]
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "abort")

        state = self.handler._reply_states["reply-1"]
        self.assertEqual(state.status, "waiting")
        self.api.send_danmu.assert_not_awaited()

        voice = base64.b64encode(b"buffered-fallback").decode("ascii")
        await self.handler.handle_from_nachobot(
            self._message("buffer-fresh-id", Seg("voice", voice))
        )

        self.assertEqual(self.audio_player.played, [b"buffered-fallback"])
        self.assertTrue(state.audio_succeeded)
        self.assertEqual(self._subtitle(), "显示正文")
        self.api.send_danmu.assert_not_awaited()
        self.live2d_manager.apply_control.assert_awaited_once_with("test-control")
        await self.handler.shutdown()

    async def test_accepted_start_abort_falls_back_once_and_ignores_late_start(self):
        self.audio_player.results = [True, True]
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "abort")
        await self._send_stream("reply-1", "start", stream_id="late-stream")

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual([event["event"] for event in self.audio_player.events], ["start", "abort"])
        self.live2d_manager.apply_control.assert_awaited_once_with("test-control")
        await self.handler.shutdown()

    async def test_new_parent_start_silently_supersedes_old_stream(self):
        await self.handler.handle_from_nachobot(self._initial("reply-a", reply="A"))
        await self.handler.handle_from_nachobot(self._initial("reply-b", reply="B"))
        await self._send_stream("reply-a", "start")
        await self._send_stream("reply-a", "chunk")
        old_timer = self.handler._reply_states["reply-a"].timer

        await self._send_stream("reply-b", "start", stream_id="stream-b")
        old_state = self.handler._reply_states["reply-a"]
        self.assertEqual(old_state.status, "superseded")
        self.assertEqual(old_state.active_stream_id, "")
        self.assertIsNone(old_state.active_stream_metadata)
        await asyncio.sleep(0)
        self.assertTrue(old_timer.cancelled())

        await self._send_stream("reply-a", "chunk")
        await self._send_stream("reply-a", "abort")
        await self.handler._expire_pending_reply(old_state)
        await self._send_stream("reply-b", "end", stream_id="stream-b")

        self.assertTrue(self.handler._reply_states["reply-b"].audio_succeeded)
        self.assertEqual(
            [event["event"] for event in self.audio_player.events],
            ["start", "chunk", "start", "end"],
        )
        self.api.send_danmu.assert_not_awaited()
        await self.handler.shutdown()

    async def test_unassociated_stream_start_supersedes_pending_reply(self):
        await self.handler.handle_from_nachobot(self._initial("reply-a", reply="A"))
        await self._send_stream("reply-a", "start")
        await self._send_stream("reply-a", "chunk")
        old_timer = self.handler._reply_states["reply-a"].timer

        await self._send_stream("unassociated-parent", "start", stream_id="pure-stream")
        old_state = self.handler._reply_states["reply-a"]
        self.assertEqual(old_state.status, "superseded")
        await asyncio.sleep(0)
        self.assertTrue(old_timer.cancelled())

        await self._send_stream("reply-a", "abort")
        await self.handler._expire_pending_reply(old_state)
        self.api.send_danmu.assert_not_awaited()
        self.assertEqual(
            [event["event"] for event in self.audio_player.events],
            ["start", "chunk", "start"],
        )
        await self.handler.shutdown()

    async def test_stream_timeout_rolls_after_chunk_and_aborts_only_its_stream(self):
        self.handler.STREAM_INACTIVITY_TIMEOUT_SECONDS = 0.01
        send_started = asyncio.Event()

        async def send_danmu(**_kwargs):
            send_started.set()
            return {"code": 0, "data": {"dmid": "sent-dmid"}}

        self.api.send_danmu.side_effect = send_danmu
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start")
        start_timer = self.handler._reply_states["reply-1"].timer
        await self._send_stream("reply-1", "chunk")
        chunk_timer = self.handler._reply_states["reply-1"].timer
        self.assertIsNot(start_timer, chunk_timer)
        await asyncio.wait_for(send_started.wait(), timeout=0.2)
        await asyncio.wait_for(chunk_timer, timeout=0.2)

        self.assertEqual(
            [event["event"] for event in self.audio_player.events],
            ["start", "chunk", "abort"],
        )
        abort = self.audio_player.events[-1]
        self.assertEqual(abort["parent_message_id"], "reply-1")
        self.assertEqual(abort["stream_id"], "stream-1")
        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        await self.handler.shutdown()

    async def test_rejected_stream_chunk_falls_back_once(self):
        self.audio_player.results = [True, False]
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "chunk")

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual(self.handler._reply_states["reply-1"].status, "fallback")
        await self.handler.shutdown()

    async def test_real_audio_player_invalid_chunk_aborts_active_stream_before_fallback(self):
        outputs = []

        def make_output(*_args):
            output = _FakePCMOutput()
            outputs.append(output)
            return output

        player = AudioPlayer(self.logger, pcm_output_factory=make_output)
        self.adapter.audio_player = player
        player.start()
        try:
            await self.handler.handle_from_nachobot(self._initial())
            await self._send_stream("reply-1", "start")
            await self._send_stream("reply-1", "chunk")
            self.assertEqual(outputs[0].writes, [b"\x01\x00" * 32])
            self.assertEqual(player.active_voice_stream_id, "stream-1")

            invalid_message = self._stream_event("reply-1", "chunk", seq=1)
            invalid_message["message_segment"]["data"]["audio_base64"] = "%%%"
            await self.handler.handle_from_nachobot(invalid_message)

            self.assertEqual(
                (outputs[0].aborted, player.active_voice_stream_id),
                (True, None),
                "rejected chunk must abort the saved active stream metadata",
            )
            self.assertIsNone(self.handler._reply_states["reply-1"].timer)
            self.api.send_danmu.assert_awaited_once_with(
                room_id=100,
                message="显示正文",
                reply_mid="cached-mid",
                reply_dmid="viewer-dmid",
            )

            await self._send_stream("reply-1", "end")
            await self._send_stream("reply-1", "abort")
            self.api.send_danmu.assert_awaited_once()
        finally:
            await self.handler.shutdown()
            await player.shutdown()

    async def test_post_success_stream_rejections_are_cleaned_without_fallback(self):
        outputs = []

        def make_output(*_args):
            output = _FakePCMOutput()
            outputs.append(output)
            return output

        player = AudioPlayer(self.logger, pcm_output_factory=make_output)
        self.adapter.audio_player = player
        player.start()
        try:
            await self.handler.handle_from_nachobot(self._initial())
            await self._send_stream("reply-1", "start", stream_id="stream-1")
            await self._send_stream("reply-1", "chunk", stream_id="stream-1")
            await self._send_stream("reply-1", "end", stream_id="stream-1")
            self.assertTrue(self.handler._reply_states["reply-1"].audio_succeeded)
            self.api.send_danmu.assert_not_awaited()

            await self._send_stream("reply-1", "start", stream_id="stream-2")
            invalid_chunk = self._stream_event(
                "reply-1", "chunk", stream_id="stream-2", seq=0
            )
            invalid_chunk["message_segment"]["data"]["audio_base64"] = "%%%"
            await self.handler.handle_from_nachobot(invalid_chunk)
            self.assertTrue(outputs[1].aborted)
            self.assertIsNone(player.active_voice_stream_id)
            self.assertIsNone(self.handler._reply_states["reply-1"].timer)

            await self._send_stream("reply-1", "start", stream_id="stream-3")
            invalid_end = self._stream_event("reply-1", "end", stream_id="stream-3")
            invalid_end["message_segment"]["data"]["channels"] = 2
            await self.handler.handle_from_nachobot(invalid_end)
            self.assertTrue(outputs[2].aborted)
            self.assertIsNone(player.active_voice_stream_id)

            await self._send_stream("reply-1", "start", stream_id="stream-4")
            invalid_abort = self._stream_event(
                "reply-1", "abort", stream_id="stream-4"
            )
            invalid_abort["message_segment"]["data"]["channels"] = 2
            await self.handler.handle_from_nachobot(invalid_abort)
            self.assertTrue(outputs[3].aborted)
            self.assertIsNone(player.active_voice_stream_id)

            await self._send_stream("reply-1", "end", stream_id="stream-4")
            await self._send_stream("reply-1", "abort", stream_id="stream-4")
            self.api.send_danmu.assert_not_awaited()
        finally:
            await self.handler.shutdown()
            await player.shutdown()

    async def test_rejected_stream_end_falls_back_once(self):
        self.audio_player.results = [True, True, False]
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "chunk")
        await self._send_stream("reply-1", "end")

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual(self.handler._reply_states["reply-1"].status, "fallback")
        await self.handler.shutdown()

    async def test_invalid_paired_buffered_audio_falls_back_once(self):
        await self.handler.handle_from_nachobot(self._initial())
        await self.handler.handle_from_nachobot(
            self._message("buffer-fresh-id", Seg("voice", "not-valid-base64"))
        )

        self.assertEqual(self.api.send_danmu.await_count, 1)
        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="显示正文",
            reply_mid="cached-mid",
            reply_dmid="viewer-dmid",
        )
        self.assertEqual(self._subtitle(), "显示正文")
        await self.handler.shutdown()

    async def test_buffer_pairing_requires_one_waiting_reply_in_same_room(self):
        await self.handler.handle_from_nachobot(self._initial("reply-a", reply="A"))
        await self.handler.handle_from_nachobot(self._initial("reply-b", reply="B"))
        voice = base64.b64encode(b"unpaired-media").decode("ascii")
        await self.handler.handle_from_nachobot(
            self._message("fresh-buffer", Seg("voice", voice), room_id=100)
        )

        self.assertEqual(self.audio_player.played, [b"unpaired-media"])
        self.assertFalse(self.handler._reply_states["reply-a"].buffer_claimed)
        self.assertFalse(self.handler._reply_states["reply-b"].buffer_claimed)
        self.api.send_danmu.assert_not_awaited()

        await self.handler._expire_pending_reply(self.handler._reply_states["reply-a"])
        await self.handler._expire_pending_reply(self.handler._reply_states["reply-b"])
        self.assertEqual(self.api.send_danmu.await_count, 2)
        await self.handler.shutdown()

    async def test_buffered_voice_from_other_room_does_not_pair(self):
        await self.handler.handle_from_nachobot(self._initial())
        await self._send_stream("reply-1", "start", room_id=200)
        voice = base64.b64encode(b"room-200-media").decode("ascii")
        await self.handler.handle_from_nachobot(
            self._message("other-room-buffer", Seg("voice", voice), room_id=200)
        )

        self.assertEqual(self.audio_player.played, [b"room-200-media"])
        self.assertEqual(self.audio_player.events, [])
        self.assertFalse(self.handler._reply_states["reply-1"].buffer_claimed)
        self.api.send_danmu.assert_not_awaited()
        await self.handler.shutdown()

    async def test_ordinary_text_updates_subtitle_and_removes_escaped_silence(self):
        await self.handler.handle_from_nachobot(
            self._message("ordinary-text", Seg("text", "普通回复"))
        )
        self.assertEqual(self._subtitle(), "普通回复")
        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="普通回复",
            reply_mid=None,
            reply_dmid=None,
        )

        prepared = PreparedReplyResult(r"可见\u200b正文", False, "", None)
        await self.handler._deliver_live_reply(prepared, 100, "", "")
        self.assertEqual(self._subtitle(), "可见正文")
        self.assertEqual(self.api.send_danmu.await_count, 2)
        await self.handler.shutdown()

    async def test_live_search_suppresses_only_initial_ack_and_keeps_pass_two(self):
        self.adapter.config.live_network_search_enabled = True
        core = SimpleNamespace(synthesize_tts=AsyncMock())
        self.adapter.multimodal_client = core

        class SearchClient:
            search = AsyncMock(return_value="可信搜索材料")

        class ModelClient:
            call_replyer = AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "查询结果",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": {"text": "意外的语音字段", "lang": "ja"},
                    },
                    ensure_ascii=False,
                )
            )

        model = ModelClient()
        self.handler.live_search = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=SearchClient(),
            model_client=model,
        )
        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True)
        )
        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True)
        )
        self.api.send_danmu.assert_not_awaited()
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "end")
        await self.handler.live_search.wait_for_pending()

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="查询结果",
            reply_mid=None,
            reply_dmid=None,
        )
        self.assertEqual(self._subtitle(), "查询结果")
        self.assertEqual(SearchClient.search.await_count, 1)
        self.assertEqual(ModelClient.call_replyer.await_count, 1)
        core.synthesize_tts.assert_not_awaited()
        self.assertEqual(self.audio_player.played, [])
        self.assertIn("不要添加 tts_text", model.call_replyer.await_args.args[0])
        await self.handler.shutdown()

    async def test_pass_two_audio_delivery_cannot_expire_to_old_search_ack(self):
        self.adapter.config.live_network_search_enabled = True
        self.tts_manager._tts_manual_overrides[100] = True
        self.tts_manager._lang_overrides[100] = "ja"
        self.adapter.multimodal_client = SimpleNamespace(
            synthesize_tts=AsyncMock(
                return_value={
                    "audio_base64": base64.b64encode(b"pass-two-audio").decode("ascii")
                }
            )
        )
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        model = SimpleNamespace(
            call_replyer=AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "Pass2最终语音",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": "spoken result",
                    },
                    ensure_ascii=False,
                )
            )
        )
        self.handler.live_search = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=search,
            model_client=model,
        )

        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True)
        )
        state = self.handler._reply_states["reply-1"]
        await self.handler.live_search.wait_for_pending()

        self.assertEqual(self.audio_player.played, [b"pass-two-audio"])
        self.assertEqual(self._subtitle(), "Pass2最终语音")
        self.assertTrue(state.final_search_delivered)
        self.api.send_danmu.assert_not_awaited()
        self.assertFalse(state.audio_succeeded)

        await self.handler._expire_pending_reply(state)

        self.api.send_danmu.assert_not_awaited()
        self.assertEqual(self._subtitle(), "Pass2最终语音")
        self.assertEqual(state.status, "success")
        self.assertGreater(state.finished_at, 0)
        self.assertIsNone(state.timer)
        await self.handler.shutdown()

    async def test_pass_two_text_fallback_failed_chunk_does_not_restore_search_ack(self):
        self.adapter.config.live_network_search_enabled = True
        self.tts_manager._tts_manual_overrides[100] = True
        self.tts_manager._lang_overrides[100] = "ja"
        self.audio_player.results = [True, False, True]
        self.adapter.multimodal_client = SimpleNamespace(
            synthesize_tts=AsyncMock(return_value={"audio_base64": "%%%"})
        )
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        model = SimpleNamespace(
            call_replyer=AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "Pass2文本最终",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": "spoken result",
                    },
                    ensure_ascii=False,
                )
            )
        )
        self.handler.live_search = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=search,
            model_client=model,
        )

        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True)
        )
        state = self.handler._reply_states["reply-1"]
        await self._send_stream("reply-1", "start")
        await self.handler.live_search.wait_for_pending()

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="Pass2文本最终",
            reply_mid=None,
            reply_dmid=None,
        )
        self.assertEqual(self._subtitle(), "Pass2文本最终")
        self.assertTrue(state.final_search_delivered)
        await self._send_stream("reply-1", "chunk")
        await self.handler._expire_pending_reply(state)

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="Pass2文本最终",
            reply_mid=None,
            reply_dmid=None,
        )
        self.assertEqual(self._subtitle(), "Pass2文本最终")
        self.assertEqual(
            [event["event"] for event in self.audio_player.events],
            ["start", "chunk", "abort"],
        )
        self.assertEqual(self.audio_player.events[-1]["stream_id"], "stream-1")
        self.assertEqual(state.status, "success")
        self.assertGreater(state.finished_at, 0)
        self.assertIsNone(state.active_stream_metadata)
        self.assertIsNone(state.timer)
        await self.handler.shutdown()
    async def test_live_search_synthesizes_pass_two_through_core_and_plays_audio(self):
        self.adapter.config.live_network_search_enabled = True
        self.tts_manager._tts_manual_overrides[100] = True
        self.tts_manager._lang_overrides[100] = "zh"

        core = SimpleNamespace(
            synthesize_tts=AsyncMock(
                return_value={
                    "audio_base64": base64.b64encode(b"pass-two-audio").decode("ascii")
                }
            )
        )
        self.adapter.multimodal_client = core

        class SearchClient:
            search = AsyncMock(return_value="可信搜索材料")

        class ModelClient:
            call_replyer = AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "查询结果",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": {"text": "検索結果", "lang": "ja"},
                    },
                    ensure_ascii=False,
                )
            )

        model = ModelClient()
        self.handler.live_search = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=SearchClient(),
            model_client=model,
        )

        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True, tts_lang="zh")
        )
        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True, tts_lang="zh")
        )
        self.api.send_danmu.assert_not_awaited()
        await self._send_stream("reply-1", "start")
        await self._send_stream("reply-1", "end")
        await self.handler.live_search.wait_for_pending()

        core.synthesize_tts.assert_awaited_once_with(
            "検索結果", platform="bilibili.live", text_lang="ja"
        )
        self.assertEqual(self.audio_player.played, [b"pass-two-audio"])
        self.api.send_danmu.assert_not_awaited()
        self.assertEqual(self._subtitle(), "查询结果")
        prompt = model.call_replyer.await_args.args[0]
        self.assertIn("本轮语音输出已明确指定为中文", prompt)
        self.assertIn('"lang":"zh"', prompt)
        await self.handler.shutdown()

    async def test_live_search_string_tts_uses_the_current_room_language(self):
        self.tts_manager._tts_manual_overrides[100] = True
        core = SimpleNamespace(
            synthesize_tts=AsyncMock(
                return_value={"audio": base64.b64encode(b"room-language").decode("ascii")}
            )
        )
        self.adapter.multimodal_client = core
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        response = json.dumps(
            {
                "reply": "查询结果",
                "emotion": "normal",
                "action": "一般",
                "tts_text": "spoken result",
            },
            ensure_ascii=False,
        )

        for language, expected_prompt in (
            ("ja", "当前语种为日语"),
            ("zh", "当前语种为中文"),
        ):
            with self.subTest(language=language):
                self.tts_manager._lang_overrides[100] = language
                self.api.send_danmu.reset_mock()
                self.audio_player.played.clear()
                core.synthesize_tts.reset_mock()
                model = SimpleNamespace(call_replyer=AsyncMock(return_value=response))
                orchestrator = BilibiliLiveSearchOrchestrator(
                    self.adapter,
                    self.logger,
                    search_client=search,
                    model_client=model,
                )
                prompt = orchestrator._build_followup_prompt(
                    100, "天气", "正在查询", "可信搜索材料"
                )
                self.assertIn(expected_prompt, prompt)
                self.assertNotIn("不要添加 tts_text", prompt)
                await orchestrator._run_followup(
                    room_id=100,
                    query="天气",
                    initial_reply="正在查询",
                    deliver=self.handler._deliver_live_reply,
                )
                core.synthesize_tts.assert_awaited_once_with(
                    "spoken result", platform="bilibili.live", text_lang=language
                )
                self.assertEqual(self.audio_player.played, [b"room-language"])
                self.api.send_danmu.assert_not_awaited()
                await orchestrator.shutdown()

    async def test_first_pass_language_override_defaults_generated_object_without_lang(self):
        self.adapter.config.live_network_search_enabled = True
        self.tts_manager._tts_manual_overrides[100] = True
        self.tts_manager._lang_overrides[100] = "ja"
        core = SimpleNamespace(
            synthesize_tts=AsyncMock(
                return_value={
                    "audio_base64": base64.b64encode(b"override-audio").decode("ascii")
                }
            )
        )
        self.adapter.multimodal_client = core
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        model = SimpleNamespace(
            call_replyer=AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "查询结果",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": {"text": "指定语种朗读"},
                    },
                    ensure_ascii=False,
                )
            )
        )
        self.handler.live_search = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=search,
            model_client=model,
        )

        await self.handler.handle_from_nachobot(
            self._initial(reply="正在查询", search=True, tts_lang="zh")
        )
        self.assertEqual(self.handler._reply_states["reply-1"].first_pass_tts_language, "zh")
        await self.handler.live_search.wait_for_pending()

        core.synthesize_tts.assert_awaited_once_with(
            "指定语种朗读", platform="bilibili.live", text_lang="zh"
        )
        self.assertEqual(self.audio_player.played, [b"override-audio"])
        self.api.send_danmu.assert_not_awaited()
        self.assertIn("本轮语音输出已明确指定为中文", model.call_replyer.await_args.args[0])
        await self.handler.shutdown()

    async def test_live_search_tts_failures_fall_back_to_text_once(self):
        self.tts_manager._tts_manual_overrides[100] = True
        core = SimpleNamespace(synthesize_tts=AsyncMock())
        self.adapter.multimodal_client = core
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        cases = (
            ("missing", False, None, {"audio_base64": "YXVkaW8="}, None, False),
            ("blank", True, "   ", {"audio_base64": "YXVkaW8="}, None, False),
            ("invalid_language", True, {"text": "朗读", "lang": []}, {"audio_base64": "YXVkaW8="}, None, False),
            ("no_audio", True, "朗读", {}, None, True),
            ("bad_base64", True, "朗读", {"audio_base64": "%%%"}, None, True),
            ("api_error", True, "朗读", None, RuntimeError("offline fake"), True),
        )
        for name, includes_tts, tts_text, synthesis_result, synthesis_error, should_synthesize in cases:
            with self.subTest(case=name):
                self.api.send_danmu.reset_mock()
                self.audio_player.played.clear()
                core.synthesize_tts.reset_mock()
                core.synthesize_tts.side_effect = synthesis_error
                if synthesis_error is None:
                    core.synthesize_tts.return_value = synthesis_result
                envelope = {
                    "reply": "查询结果",
                    "emotion": "normal",
                    "action": "一般",
                }
                if includes_tts:
                    envelope["tts_text"] = tts_text
                model = SimpleNamespace(
                    call_replyer=AsyncMock(
                        return_value=json.dumps(envelope, ensure_ascii=False)
                    )
                )
                orchestrator = BilibiliLiveSearchOrchestrator(
                    self.adapter,
                    self.logger,
                    search_client=search,
                    model_client=model,
                )

                await orchestrator._run_followup(
                    room_id=100,
                    query="天气",
                    initial_reply="正在查询",
                    deliver=self.handler._deliver_live_reply,
                )

                self.api.send_danmu.assert_awaited_once_with(
                    room_id=100,
                    message="查询结果",
                    reply_mid=None,
                    reply_dmid=None,
                )
                self.assertEqual(self.audio_player.played, [])
                self.assertEqual(core.synthesize_tts.await_count, int(should_synthesize))
                await orchestrator.shutdown()

    async def test_live_search_playback_enqueue_failure_falls_back_once(self):
        from bili_src.live.two_phase_search import SearchFollowupResult

        self.tts_manager._tts_manual_overrides[100] = True
        self.audio_player.play = Mock(side_effect=RuntimeError("queue unavailable"))
        result = SearchFollowupResult(
            "可见最终回复",
            False,
            "",
            "test-control",
            base64.b64encode(b"generated-audio").decode("ascii"),
        )

        await self.handler._deliver_live_reply(result, 100, "", "")

        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="可见最终回复",
            reply_mid=None,
            reply_dmid=None,
        )
        self.assertEqual(self._subtitle(), "可见最终回复")
        self.live2d_manager.apply_control.assert_awaited_once_with("test-control")
        self.live2d_manager.controller.on_start_replying.assert_awaited_once()

    async def test_tts_disabled_after_synthesis_falls_back_at_delivery(self):
        self.tts_manager._tts_manual_overrides[100] = True
        self.adapter.multimodal_client = SimpleNamespace(
            synthesize_tts=AsyncMock(
                return_value={"audio_base64": base64.b64encode(b"toggle-audio").decode("ascii")}
            )
        )
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))
        model = SimpleNamespace(
            call_replyer=AsyncMock(
                return_value=json.dumps(
                    {
                        "reply": "查询结果",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": "spoken result",
                    },
                    ensure_ascii=False,
                )
            )
        )
        orchestrator = BilibiliLiveSearchOrchestrator(
            self.adapter,
            self.logger,
            search_client=search,
            model_client=model,
        )

        async def disable_then_deliver(prepared, room_id, reply_mid, reply_dmid):
            self.tts_manager._tts_manual_overrides[100] = False
            await self.handler._deliver_live_reply(prepared, room_id, reply_mid, reply_dmid)

        await orchestrator._run_followup(
            room_id=100,
            query="天气",
            initial_reply="正在查询",
            deliver=disable_then_deliver,
        )

        self.adapter.multimodal_client.synthesize_tts.assert_awaited_once()
        self.assertEqual(self.audio_player.played, [])
        self.api.send_danmu.assert_awaited_once_with(
            room_id=100,
            message="查询结果",
            reply_mid=None,
            reply_dmid=None,
        )
        await orchestrator.shutdown()

    async def test_live_search_shutdown_cancels_model_prepare_and_tts(self):
        self.adapter.config.live_network_search_enabled = True
        self.tts_manager._tts_manual_overrides[100] = True
        search = SimpleNamespace(search=AsyncMock(return_value="可信搜索材料"))

        for blocked_stage in ("model", "prepare", "tts"):
            with self.subTest(stage=blocked_stage):
                self.handler = OutgoingHandler(
                    self.adapter.config, self.logger, self.adapter
                )
                self.adapter.api.send_danmu.reset_mock()
                self.audio_player.played.clear()
                self.live2d_manager = _Live2DManager()
                self.adapter.live2d_manager = self.live2d_manager
                self.handler.adapter = self.adapter
                self.handler.live_search = BilibiliLiveSearchOrchestrator(
                    self.adapter, self.logger, search_client=search
                )
                stage_started = asyncio.Event()
                stage_cancelled = asyncio.Event()

                async def block_stage(*_args, **_kwargs):
                    stage_started.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        stage_cancelled.set()
                        raise

                model_response = json.dumps(
                    {
                        "reply": "查询结果",
                        "emotion": "normal",
                        "action": "一般",
                        "tts_text": {"text": "等待中的语音", "lang": "ja"},
                    },
                    ensure_ascii=False,
                )
                if blocked_stage == "model":
                    model = SimpleNamespace(call_replyer=AsyncMock(side_effect=block_stage))
                else:
                    model = SimpleNamespace(call_replyer=AsyncMock(return_value=model_response))
                self.handler.live_search._model_client = model
                if blocked_stage == "prepare":
                    prepare_calls = 0

                    async def block_second_prepare(raw):
                        nonlocal prepare_calls
                        prepare_calls += 1
                        if prepare_calls == 2:
                            await block_stage()
                        return await _Live2DManager._prepare(raw)

                    self.live2d_manager.prepare_reply = AsyncMock(
                        side_effect=block_second_prepare
                    )
                elif blocked_stage == "tts":
                    self.adapter.multimodal_client = SimpleNamespace(
                        synthesize_tts=AsyncMock(side_effect=block_stage)
                    )
                await self.handler.handle_from_nachobot(
                    self._initial(reply="正在查询", search=True)
                )
                await asyncio.wait_for(stage_started.wait(), timeout=1)
                pending_tasks = tuple(self.handler.live_search._tasks)

                await self.handler.shutdown()

                self.assertTrue(stage_cancelled.is_set())
                self.assertTrue(all(task.done() for task in pending_tasks))
                self.assertFalse(self.handler.live_search._tasks)
                self.assertEqual(self.audio_player.played, [])
                self.adapter.api.send_danmu.assert_not_awaited()

    async def test_shutdown_cancels_pending_timers_without_fallback(self):
        await self.handler.handle_from_nachobot(self._initial())
        timers = tuple(self.handler._timer_tasks)
        self.assertTrue(timers)
        self.assertEqual(self.handler.PENDING_REPLY_TIMEOUT_SECONDS, 120.0)

        await self.handler.shutdown()

        self.assertTrue(all(task.done() for task in timers))
        self.assertFalse(self.handler._timer_tasks)
        self.api.send_danmu.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
