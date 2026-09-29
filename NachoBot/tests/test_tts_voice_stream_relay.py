import asyncio
import base64
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ncnk_message import Seg, UserInfo

from src.chat.message_receive.message import MessageSending
from src.chat.message_receive.uni_message_sender import (
    UniversalMessageSender,
    _relay_tts_text,
    _send_message,
)
from src.chat.runtime_capabilities import RuntimeCapabilities
from src.multimodal.contracts import (
    TTSStreamChunk,
    TTSStreamError,
    TTSStreamSpec,
    TTSResult,
)
from src.plugin_system.base.component_types import EventType
from src.plugin_system.core.events_manager import events_manager


def _chunk(pcm: bytes = b"\x01\x00\x02\x00") -> TTSStreamChunk:
    return TTSStreamChunk(
        pcm_s16le=pcm,
        spec=TTSStreamSpec(sample_rate=16_000, channels=1, sample_width=2),
    )


class _FakeRouter:
    def __init__(self, chunks=None, error=None, *, allows_tts=True, buffered_audio="buffered-wav"):
        self.chunks = list(chunks or [])
        self.error = error
        self.profile = SimpleNamespace(allows_tts=allows_tts)
        self.buffered_audio = buffered_audio
        self.stream_calls = []
        self.buffer_calls = []
        self.materialize_calls = []
        self.after_first_chunk = None

    def should_materialize_reply(self, segment):
        return segment.type == "tts_text"

    async def materialize_reply(self, segment, **kwargs):
        self.materialize_calls.append((segment, kwargs))
        return Seg(
            type="seglist",
            data=[segment, Seg(type="voice", data="buffered-wav")],
        )

    def synthesize_tts_stream(self, text, *, platform="core", text_lang=None):
        self.stream_calls.append((text, platform, text_lang))

        async def generate():
            for index, chunk in enumerate(self.chunks):
                yield chunk
                if index == 0 and self.after_first_chunk is not None:
                    await self.after_first_chunk()
            if self.error is not None:
                raise self.error

        return generate()

    async def synthesize_tts(self, text, *, platform="core", text_lang=None):
        self.buffer_calls.append((text, platform, text_lang))
        return TTSResult(text=text, audio_base64=self.buffered_audio)


class _FakeApi:
    def __init__(self, *, reject_event=None):
        self.sent = []
        self.reject_event = reject_event
        self.chunk_sent = asyncio.Event()

    async def send_message(self, message):
        segment = message.message_segment
        if isinstance(segment.data, list):
            data = [child.to_dict() for child in segment.data]
        else:
            data = copy.deepcopy(segment.data)
        record = {"type": segment.type, "data": data}
        self.sent.append(record)
        if segment.type == "voice_stream" and isinstance(data, dict):
            if data.get("event") == "chunk":
                self.chunk_sent.set()
            if data.get("event") == self.reject_event:
                return False
        return True


def _message(*, voice_stream=True, segment=None):
    bot = UserInfo(
        platform="universal_vc",
        user_id="bot",
        user_nickname="Bot",
        user_cardname="Bot",
    )
    trigger = SimpleNamespace(
        message_info=SimpleNamespace(
            additional_config={
                "tts_language": "zh",
                "runtime_capabilities": {"voice_stream": voice_stream},
            }
        )
    )
    stream = SimpleNamespace(
        stream_id="stream-1",
        platform="universal_vc",
        user_info=bot,
        group_info=None,
        context=SimpleNamespace(message=trigger),
    )
    return MessageSending(
        message_id="parent-1",
        chat_stream=stream,
        bot_user_info=bot,
        sender_info=bot,
        message_segment=segment or Seg(
            type="tts_text",
            data={"text": "initial speech", "display_text": "initial display"},
        ),
    )


class TTSVoiceStreamRelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sender = UniversalMessageSender.__new__(UniversalMessageSender)
        self.sender.storage = SimpleNamespace(store_message=AsyncMock())
        self.api = _FakeApi()
        self.router = _FakeRouter(chunks=[_chunk()])
        self.logical_messages = []
        self.event_calls = []

        async def send_logical(message, show_log=True):
            self.logical_messages.append(copy.deepcopy(message.message_segment.to_dict()))
            return True

        async def handle_event(event, *, message, stream_id):
            self.event_calls.append(event)
            if event == EventType.POST_SEND:
                message.message_segment = Seg(
                    type="seglist",
                    data=[
                        Seg(type="text", data="normal reply"),
                        Seg(
                            type="tts_text",
                            data={
                                "text": "final speech",
                                "display_text": "final display",
                                "lang": "ja",
                            },
                        ),
                    ],
                )
            return True, None

        self.patchers = [
            patch("src.chat.message_receive.uni_message_sender._send_message", side_effect=send_logical),
            patch("src.chat.message_receive.uni_message_sender.get_global_api", return_value=self.api),
            patch("src.multimodal.get_multimodal_router", side_effect=lambda: self.router),
            patch.object(events_manager, "handle_nacho_events", side_effect=handle_event),
            patch(
                "src.chat.message_receive.uni_message_sender._should_suppress_text_reply",
                return_value=False,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_stream_uses_final_plugin_text_and_sends_bounded_ordered_events_once(self):
        self.router.chunks = [_chunk(b"\x01\x00" * 1_600), _chunk(b"\x03\x00" * 1_600)]
        message = _message()

        result = await self.sender.send_message(message)

        self.assertTrue(result)
        self.assertEqual(self.router.stream_calls, [("final speech", "universal_vc", "ja")])
        self.assertEqual(self.router.buffer_calls, [])
        self.assertEqual(
            self.logical_messages[0],
            {
                "type": "seglist",
                "data": [
                    {"type": "text", "data": "normal reply"},
                    {"type": "text", "data": "final display"},
                ],
            },
        )
        self.assertEqual(len(self.logical_messages), 1)
        stream_records = [item["data"] for item in self.api.sent]
        self.assertEqual([item["event"] for item in stream_records], ["start", "chunk", "chunk", "end"])
        self.assertTrue(all(item["stream_id"] == stream_records[0]["stream_id"] for item in stream_records))
        self.assertTrue(all(item["parent_message_id"] == "parent-1" for item in stream_records))
        chunks = [item for item in stream_records if item["event"] == "chunk"]
        self.assertEqual([item["seq"] for item in chunks], [0, 1])
        for item in chunks:
            self.assertEqual(
                (item["sample_rate"], item["channels"], item["sample_width"], item["codec"]),
                (16_000, 1, 2, "pcm_s16le"),
            )
            self.assertLessEqual(len(base64.b64decode(item["audio_base64"])), 64 * 1024)
        self.assertEqual(
            self.event_calls,
            [EventType.POST_SEND_PRE_PROCESS, EventType.POST_SEND, EventType.AFTER_SEND],
        )
        self.sender.storage.store_message.assert_awaited_once()
        self.assertEqual(message.message_segment.type, "seglist")
        self.assertEqual(message.message_segment.data[1].type, "tts_text")

    async def test_fast_synthesis_is_paced_below_playback_buffer_limit(self):
        clock = [100.0]
        sent_at = []
        original_send = self.api.send_message

        async def timed_send(message):
            sent_at.append(clock[0])
            return await original_send(message)

        async def advance(seconds):
            clock[0] += seconds

        self.api.send_message = timed_send
        self.router.chunks = [_chunk(b"\x01\x00" * 16_000) for _ in range(4)]
        with (
            patch("src.chat.message_receive.uni_message_sender._tts_relay_now", side_effect=lambda: clock[0]),
            patch("src.chat.message_receive.uni_message_sender._tts_relay_wait", side_effect=advance),
        ):
            self.assertTrue(await _relay_tts_text(_message(), self.router, "fast", None))

        records = [entry["data"] for entry in self.api.sent]
        self.assertEqual(records[0]["event"], "start")
        self.assertEqual(records[-1]["event"], "end")
        chunks = [(record, at) for record, at in zip(records, sent_at, strict=True) if record["event"] == "chunk"]
        self.assertEqual(len(chunks), 40)
        self.assertEqual([record["seq"] for record, _ in chunks], list(range(40)))
        playback_until = None
        for record, sent_time in chunks:
            pcm = base64.b64decode(record["audio_base64"])
            self.assertLessEqual(len(pcm), 3_200)
            playback_until = max(playback_until or sent_time, sent_time) + len(pcm) / 32_000
            self.assertLessEqual(playback_until - sent_time, 0.501)
        self.assertGreaterEqual(clock[0], 104.1)

    async def test_fragmented_transport_pcm_is_coalesced_for_bounded_playback_queues(self):
        clock = [100.0]
        sent_at = []
        original_send = self.api.send_message

        async def timed_send(message):
            sent_at.append(clock[0])
            return await original_send(message)

        async def advance(seconds):
            clock[0] += seconds

        self.api.send_message = timed_send
        self.router.chunks = [_chunk(b"\x01\x00" * 320) for _ in range(100)]
        with (
            patch("src.chat.message_receive.uni_message_sender._tts_relay_now", side_effect=lambda: clock[0]),
            patch("src.chat.message_receive.uni_message_sender._tts_relay_wait", side_effect=advance),
        ):
            self.assertTrue(await _relay_tts_text(_message(), self.router, "fragmented", None))

        records = [entry["data"] for entry in self.api.sent]
        chunks = [(record, at) for record, at in zip(records, sent_at, strict=True) if record["event"] == "chunk"]
        self.assertEqual(len(chunks), 20)
        self.assertTrue(all(len(base64.b64decode(record["audio_base64"])) == 3_200 for record, _ in chunks))
        playback_until = None
        for record, sent_time in chunks:
            playback_until = max(playback_until or sent_time, sent_time) + 0.1
            self.assertLessEqual(playback_until - sent_time, 0.501)

    async def test_next_tts_field_waits_for_previous_audio_to_drain(self):
        clock = [100.0]
        sent_at = []
        original_send = self.api.send_message

        async def timed_send(message):
            sent_at.append(clock[0])
            return await original_send(message)

        async def advance(seconds):
            clock[0] += seconds

        self.api.send_message = timed_send
        self.router.chunks = [_chunk(b"\x01\x00" * 16_000)]
        with (
            patch("src.chat.message_receive.uni_message_sender._tts_relay_now", side_effect=lambda: clock[0]),
            patch("src.chat.message_receive.uni_message_sender._tts_relay_wait", side_effect=advance),
        ):
            self.assertTrue(await _relay_tts_text(_message(), self.router, "first", None))
            self.assertTrue(await _relay_tts_text(_message(), self.router, "second", None))

        records = [entry["data"] for entry in self.api.sent]
        starts = [sent_at[index] for index, entry in enumerate(records) if entry["event"] == "start"]
        ends = [sent_at[index] for index, entry in enumerate(records) if entry["event"] == "end"]
        self.assertEqual(len(starts), 2)
        self.assertGreater(starts[1] - starts[0], 1.19)
        self.assertGreater(starts[1], ends[0])

    async def test_post_send_cancellation_and_filter_replacement_do_not_synthesize(self):
        message = _message()

        async def cancel_post_send(event, *, message, stream_id):
            self.event_calls.append(event)
            if event == EventType.POST_SEND:
                return False, None
            return True, None

        with patch.object(events_manager, "handle_nacho_events", side_effect=cancel_post_send):
            self.assertFalse(await self.sender.send_message(message))
        self.assertEqual(self.router.stream_calls, [])
        self.assertEqual(self.router.buffer_calls, [])
        self.assertEqual(self.logical_messages, [])
        self.sender.storage.store_message.assert_not_awaited()

        self.event_calls.clear()
        with patch(
            "src.chat.message_receive.uni_message_sender._should_suppress_text_reply",
            return_value=True,
        ):
            self.assertTrue(await self.sender.send_message(_message()))
        self.assertEqual(self.router.stream_calls, [])
        self.assertEqual(self.router.buffer_calls, [])
        self.assertEqual(self.logical_messages[-1], {"type": "text", "data": "Filtered"})
        self.sender.storage.store_message.assert_awaited_once()

    async def test_buffered_capability_keeps_existing_complete_voice_path(self):
        message = _message(voice_stream=False)
        self.assertFalse(RuntimeCapabilities.from_mapping({}).voice_stream)
        self.assertFalse(RuntimeCapabilities.from_mapping({"voice_stream": "true"}).voice_stream)

        await message.process()
        self.assertEqual(len(self.router.materialize_calls), 1)
        self.assertEqual(message.message_segment.data[1].type, "voice")

    async def test_potato_profile_keeps_display_text_without_tts(self):
        self.router.profile = SimpleNamespace(allows_tts=False)

        self.assertTrue(await self.sender.send_message(_message()))

        self.assertEqual(self.router.stream_calls, [])
        self.assertEqual(self.router.buffer_calls, [])
        self.assertEqual(self.logical_messages[0]["data"][1]["type"], "text")

    async def test_no_audio_relay_when_logical_ncnk_send_returns_false(self):
        message = _message()
        with patch(
            "src.chat.message_receive.uni_message_sender._send_message",
            new=AsyncMock(return_value=False),
        ):
            self.assertFalse(await self.sender.send_message(message))
        self.assertEqual(self.router.stream_calls, [])
        self.assertEqual(self.router.buffer_calls, [])
        self.sender.storage.store_message.assert_not_awaited()

    async def test_explicit_false_ncnk_result_is_not_reported_as_success(self):
        api = SimpleNamespace(send_message=AsyncMock(return_value=False))
        message = _message()
        with patch("src.chat.message_receive.uni_message_sender.get_global_api", return_value=api):
            self.assertFalse(await _send_message(message))

    async def test_false_chunk_send_aborts_without_buffered_replay(self):
        self.api.reject_event = "chunk"

        await self.sender.send_message(_message())

        self.assertEqual(
            [item["data"]["event"] for item in self.api.sent],
            ["start", "chunk", "abort"],
        )
        self.assertEqual(self.router.buffer_calls, [])

    async def test_malformed_first_chunk_uses_buffered_fallback(self):
        self.router.chunks = [
            SimpleNamespace(
                pcm_s16le=b"\x01",
                spec=TTSStreamSpec(sample_rate=16_000, channels=1, sample_width=2),
            )
        ]

        await self.sender.send_message(_message())

        self.assertEqual(self.router.buffer_calls, [("final speech", "universal_vc", "ja")])
        self.assertEqual([item["type"] for item in self.api.sent], ["voice"])

    async def test_pre_audio_stream_failure_falls_back_to_one_buffered_voice_segment(self):
        self.router = _FakeRouter(
            error=TTSStreamError("unavailable", audio_started=False),
            buffered_audio="buffered-wav",
        )
        with patch("src.multimodal.get_multimodal_router", return_value=self.router):
            await self.sender.send_message(_message())

        self.assertEqual(self.router.buffer_calls, [("final speech", "universal_vc", "ja")])
        self.assertEqual([item["type"] for item in self.api.sent], ["voice"])
        self.assertEqual(self.api.sent[0]["data"], "buffered-wav")

    async def test_failure_after_chunk_aborts_without_buffered_replay(self):
        self.router = _FakeRouter(
            chunks=[_chunk(b"\x01\x00" * 1_600)],
            error=TTSStreamError("backend_failed", audio_started=True),
        )
        with patch("src.multimodal.get_multimodal_router", return_value=self.router):
            await self.sender.send_message(_message())

        self.assertEqual(self.router.buffer_calls, [])
        self.assertEqual(
            [item["data"]["event"] for item in self.api.sent],
            ["start", "chunk", "abort"],
        )

    async def test_cancel_after_start_closes_generator_and_sends_abort(self):
        waiting_for_next = asyncio.Event()
        never = asyncio.Event()

        async def wait_after_first():
            waiting_for_next.set()
            await never.wait()

        self.router.after_first_chunk = wait_after_first
        self.router.chunks = [_chunk(b"\x01\x00" * 1_600)]
        message = _message()
        task = asyncio.create_task(
            _relay_tts_text(message, self.router, "speech", "zh")
        )
        await asyncio.wait_for(waiting_for_next.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            [item["data"]["event"] for item in self.api.sent],
            ["start", "chunk", "abort"],
        )
        self.assertEqual(self.router.buffer_calls, [])


if __name__ == "__main__":
    unittest.main()
