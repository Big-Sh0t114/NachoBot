from __future__ import annotations

import threading
import unittest

import numpy as np

from nachobot_multimodal.asr.streaming import (
    StreamingASR,
    StreamingASRChunkError,
    StreamingASRFinalizationError,
)


class FakeStream:
    def __init__(self):
        self.result = ""
        self.fail_accept = False
        self.fail_finish = False
        self.finish_result = None

    def accept_waveform(self, _sample_rate, samples):
        if self.fail_accept:
            raise RuntimeError("accept failed")
        if samples.size:
            self.result = "partial transcript"

    def input_finished(self):
        if self.fail_finish:
            raise RuntimeError("finish failed")
        if self.finish_result is not None:
            self.result = self.finish_result


class FakeRecognizer:
    def __init__(self):
        self.created = []

    def create_stream(self):
        stream = FakeStream()
        self.created.append(stream)
        return stream

    def is_ready(self, _stream):
        return False

    def decode_stream(self, _stream):
        raise AssertionError("decode should not be called")

    def get_result(self, stream):
        return stream.result


def make_asr():
    asr = object.__new__(StreamingASR)
    asr.mode = "local_streaming"
    asr._recognizer = FakeRecognizer()
    asr._streams = {}
    asr._partial_text = {}
    asr._lock = threading.RLock()
    asr.logger = __import__("logging").getLogger(__name__)
    return asr


class StreamingASRStrictTests(unittest.TestCase):
    def test_unknown_chunk_and_finish_do_not_create_streams(self):
        asr = make_asr()
        samples = np.ones(8, dtype=np.float32)

        with self.assertRaises(KeyError):
            asr.accept_stream_audio("missing", samples)
        with self.assertRaises(KeyError):
            asr.finish_stream("missing")
        self.assertEqual(asr._recognizer.created, [])
        self.assertEqual(asr._streams, {})

    def test_duplicate_start_does_not_replace_live_stream(self):
        asr = make_asr()
        asr.start_stream("one")
        original = asr._streams["one"]

        with self.assertRaises(ValueError):
            asr.start_stream("one")
        self.assertIs(asr._streams["one"], original)
        self.assertEqual(len(asr._recognizer.created), 1)

    def test_chunk_failure_is_raised_and_clears_partial_state(self):
        asr = make_asr()
        asr.start_stream("one")
        stream = asr._streams["one"]
        stream.fail_accept = True

        with self.assertRaises(StreamingASRChunkError):
            asr.accept_stream_audio("one", np.ones(8, dtype=np.float32))
        self.assertNotIn("one", asr._streams)
        self.assertNotIn("one", asr._partial_text)
        with self.assertRaises(KeyError):
            asr.accept_stream_audio("one", np.ones(8, dtype=np.float32))

    def test_finalize_failure_is_not_converted_to_last_partial_text(self):
        asr = make_asr()
        asr.start_stream("one")
        stream = asr._streams["one"]
        asr.accept_stream_audio("one", np.ones(8, dtype=np.float32))
        stream.fail_finish = True

        with self.assertRaises(StreamingASRFinalizationError):
            asr.finish_stream("one")
        self.assertNotIn("one", asr._streams)
        self.assertNotIn("one", asr._partial_text)

    def test_empty_final_result_does_not_return_a_prior_partial(self):
        asr = make_asr()
        asr.start_stream("one")
        stream = asr._streams["one"]
        self.assertEqual(
            asr.accept_stream_audio("one", np.ones(8, dtype=np.float32)),
            "partial transcript",
        )
        stream.finish_result = ""

        self.assertIsNone(asr.finish_stream("one"))


if __name__ == "__main__":
    unittest.main()
