import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from adapter import DiscordAdapter, _segments


class DiscordOutboundSegmentTests(unittest.TestCase):
    def test_voice_stream_is_an_event_not_a_complete_wav(self):
        event = {"event": "start", "stream_id": "stream-1"}
        segment = {"type": "voice_stream", "data": event}

        self.assertEqual(list(_segments(segment)), [("voice_stream", event)])
        self.assertNotIn("voice", [kind for kind, _ in _segments(segment)])

    def test_nested_voice_and_stream_keep_order(self):
        segment = {
            "type": "seglist",
            "data": [
                {"type": "voice", "data": "first"},
                {"type": "voice_stream", "data": "second"},
            ],
        }

        self.assertEqual(
            list(_segments(segment)),
            [("voice", "first"), ("voice_stream", "second")],
        )

    def test_stream_lifecycle_validates_sequence_and_format(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        source = SimpleNamespace(
            stream_id="stream-1", sample_rate=24_000, channels=1, sample_width=2
        )
        channel_id = 423456789012345678
        generation = "voice-generation-1"
        session = SimpleNamespace(
            channel_id=channel_id,
            guild_id=623456789012345678,
            generation=generation,
        )
        streams = {}

        def start_tts_stream(ch_id, gen, stream_id, rate, channels, width):
            streams[ch_id] = (gen, source)
            return True

        adapter.bot = SimpleNamespace(
            tts_streams=streams,
            get_voice_session=Mock(return_value=session),
            _session_is_current=Mock(return_value=True),
            start_tts_stream=Mock(side_effect=start_tts_stream),
            feed_tts_stream=Mock(return_value=True),
            end_tts_stream=Mock(return_value=True),
            abort_tts_stream=Mock(return_value=True),
        )
        target = {
            "mode": "voice",
            "channel_id": str(channel_id),
            "voice_generation": generation,
        }
        base = {
            "stream_id": "stream-1",
            "sample_rate": 24_000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
        }
        adapter._handle_voice_stream_event(session, target, {**base, "event": "start"})
        adapter._handle_voice_stream_event(
            session,
            target,
            {**base, "event": "chunk", "seq": 0, "audio_base64": "AQACAA=="},
        )
        adapter._handle_voice_stream_event(session, target, {**base, "event": "end"})
        adapter._handle_voice_stream_event(session, target, {**base, "event": "abort"})
        adapter.bot.start_tts_stream.assert_called_once_with(
            channel_id, generation, "stream-1", 24_000, 1, 2
        )
        adapter.bot.feed_tts_stream.assert_called_once_with(
            channel_id, generation, "stream-1", 0, b"\x01\x00\x02\x00"
        )
        adapter.bot.end_tts_stream.assert_called_once_with(
            channel_id, generation, "stream-1"
        )
        adapter.bot.abort_tts_stream.assert_called_once_with(channel_id, "stream-1")

        with self.assertRaises(ValueError):
            adapter._handle_voice_stream_event(
                session,
                target,
                {**base, "event": "chunk", "seq": 1, "audio_base64": "AQ=="},
            )
        with self.assertRaises(ValueError):
            adapter._handle_voice_stream_event(
                session,
                target,
                {
                    **base,
                    "event": "chunk",
                    "seq": 1,
                    "audio_base64": "AQACAA==",
                    "sample_rate": 16_000,
                },
            )


if __name__ == "__main__":
    unittest.main()
