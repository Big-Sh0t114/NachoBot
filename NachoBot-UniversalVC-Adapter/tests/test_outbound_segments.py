import unittest

from adapter import UniversalVCAdapter


class UniversalOutboundSegmentTests(unittest.TestCase):
    def test_pure_voice_stream_is_preserved_for_playback(self):
        segment = {"type": "voice_stream", "data": {"audio_base64": "stream-audio"}}

        self.assertEqual(list(UniversalVCAdapter._voice_segments(segment)), ["stream-audio"])

    def test_nested_voice_and_stream_keep_order(self):
        segment = {
            "type": "seglist",
            "data": [
                {"type": "voice", "data": "first"},
                {"type": "voice_stream", "data": "second"},
            ],
        }

        self.assertEqual(list(UniversalVCAdapter._voice_segments(segment)), ["first", "second"])


if __name__ == "__main__":
    unittest.main()
