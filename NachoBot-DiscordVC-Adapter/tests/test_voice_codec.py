import base64
import os
import wave
from io import BytesIO
import unittest

from voice_codec import decode_wav_base64, pcm16_to_wav_base64, write_wav_base64


class DiscordVoiceCodecTests(unittest.TestCase):
    def test_pcm_is_valid_stereo_wav_and_round_trips(self):
        payload = pcm16_to_wav_base64(
            b"\x00\x00\x01\x00" * 4,
            sample_rate=48_000,
            channels=2,
        )
        raw = decode_wav_base64(payload)
        with wave.open(BytesIO(raw), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 2)
            self.assertEqual(wav_file.getframerate(), 48_000)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getnframes(), 4)

    def test_outbound_payload_is_written_without_logging_content(self):
        payload = pcm16_to_wav_base64(b"\x00\x00\x01\x00" * 2, channels=2)
        path = write_wav_base64(payload)
        try:
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as audio_file:
                self.assertEqual(base64.b64decode(payload), audio_file.read())
        finally:
            os.unlink(path)

    def test_invalid_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_wav_base64("not-audio")


if __name__ == "__main__":
    unittest.main()
