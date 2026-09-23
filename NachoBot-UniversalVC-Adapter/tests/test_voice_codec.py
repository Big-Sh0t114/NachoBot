import os
import unittest
import wave
from io import BytesIO

import numpy as np

from voice_codec import decode_wav_base64, samples_to_wav_base64, write_wav_base64


class UniversalVoiceCodecTests(unittest.TestCase):
    def test_float_samples_are_valid_mono_wav(self):
        payload = samples_to_wav_base64(np.array([-1.0, 0.0, 1.0], dtype=np.float32))
        raw = decode_wav_base64(payload)
        with wave.open(BytesIO(raw), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getframerate(), 16_000)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getnframes(), 3)

    def test_outbound_payload_is_written_for_playback(self):
        payload = samples_to_wav_base64(np.zeros(8, dtype=np.float32))
        path = write_wav_base64(payload)
        try:
            self.assertTrue(os.path.isfile(path))
            with wave.open(path, "rb") as wav_file:
                self.assertEqual(wav_file.getnframes(), 8)
        finally:
            os.unlink(path)

    def test_invalid_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_wav_base64("not-audio")


if __name__ == "__main__":
    unittest.main()
