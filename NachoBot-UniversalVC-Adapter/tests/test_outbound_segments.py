import unittest

from adapter import UniversalVCAdapter


class UniversalOutboundSegmentTests(unittest.TestCase):
    def test_voice_stream_is_not_a_buffered_wav_alias(self):
        event = {"event": "start", "stream_id": "stream-1"}
        segment = {"type": "voice_stream", "data": event}

        self.assertEqual(list(UniversalVCAdapter._voice_segments(segment)), [])
        self.assertEqual(
            list(UniversalVCAdapter._audio_segments(segment)),
            [("voice_stream", event)],
        )

    def test_nested_buffered_voice_and_stream_events_keep_order(self):
        event = {"event": "chunk", "stream_id": "stream-1", "seq": 0}
        segment = {
            "type": "seglist",
            "data": [
                {"type": "voice", "data": "first"},
                {"type": "voice_stream", "data": event},
            ],
        }

        self.assertEqual(list(UniversalVCAdapter._voice_segments(segment)), ["first"])
        self.assertEqual(
            list(UniversalVCAdapter._audio_segments(segment)),
            [("voice", "first"), ("voice_stream", event)],
        )


if __name__ == "__main__":
    unittest.main()
