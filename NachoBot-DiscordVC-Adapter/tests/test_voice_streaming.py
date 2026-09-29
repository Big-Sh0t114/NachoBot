import asyncio
import base64
import logging
import wave
from io import BytesIO
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import numpy as np

from core_audio_stream import DiscordCoreAudioStreamBridge
from voice_handler import (
    MAX_STREAM_CONTROL_EVENTS,
    SilenceDetectingSink,
    VoiceHandler,
)


class _FailingStreamClient:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.counter = 0
        self.aborted = []

    async def start_stream(self):
        if self.fail_at == "start":
            raise RuntimeError("start unavailable")
        self.counter += 1
        return f"core-{self.counter}"

    async def send_chunk(self, stream_id, seq, pcm):
        if self.fail_at == "chunk":
            raise RuntimeError("chunk unavailable")

    async def finish_stream(self, stream_id):
        if self.fail_at == "finish":
            raise RuntimeError("finish unavailable")
        return {"text": "recognized", "result_id": f"receipt-{stream_id}"}

    async def abort_stream(self, stream_id):
        self.aborted.append(stream_id)


class DiscordVoiceStreamLifecycleTests(IsolatedAsyncioTestCase):
    def _build_sink(self, *, fail_at=None, block_start=None):
        logger = Mock()
        voice_handler = VoiceHandler(
            SimpleNamespace(
                voice=SimpleNamespace(enabled=True, sample_rate=48_000)
            ),
            logger,
        )
        stream_client = _FailingStreamClient(fail_at=fail_at)
        bridge = DiscordCoreAudioStreamBridge(stream_client, logging.getLogger("test"))
        captured_owner = {}
        results = {}
        completed = asyncio.Event()

        def key(capture_id):
            return f"discord:test:{capture_id}"

        async def stream_start(capture_id):
            if block_start is not None:
                await block_start.wait()
            return await bridge.start(key(capture_id))

        async def stream_audio(capture_id, pcm_data):
            return await bridge.send_pcm(key(capture_id), pcm_data)

        async def stream_finish(capture_id):
            return await bridge.finish(key(capture_id))

        async def stream_abort(capture_id):
            await bridge.abort(key(capture_id))

        async def capture_finish(capture_id):
            return await asyncio.to_thread(
                voice_handler.finish_stream, key(capture_id)
            )

        async def capture_abort(capture_id):
            await asyncio.to_thread(voice_handler.abort_stream, key(capture_id))

        def capture_audio(capture_id, pcm_data):
            captured_owner.setdefault(
                capture_id, int(np.frombuffer(pcm_data[:2], dtype=np.int16)[0])
            )
            voice_handler.accept_pcm(key(capture_id), pcm_data)

        async def on_result(user_id, voice_data, result_id):
            results[user_id] = (voice_data, result_id)
            if len(results) >= 1:
                completed.set()

        sink = SilenceDetectingSink(
            callback=on_result,
            on_stream_start_callback=stream_start,
            on_stream_audio_callback=stream_audio,
            on_stream_finish_callback=stream_finish,
            on_stream_abort_callback=stream_abort,
            on_capture_start_callback=lambda capture_id: voice_handler.start_stream(
                key(capture_id)
            ),
            on_capture_audio_callback=capture_audio,
            on_capture_finish_callback=capture_finish,
            on_capture_abort_callback=capture_abort,
            config=SimpleNamespace(vad_threshold=100, silence_threshold=0.04),
        )
        sink._ensure_tasks()
        return sink, voice_handler, stream_client, captured_owner, results, completed

    @staticmethod
    def _speech_packet(amplitude):
        return np.full((960, 2), amplitude, dtype=np.int16).tobytes()

    async def test_start_chunk_and_finish_failures_still_publish_full_wav(self):
        for fail_at in ("start", "chunk", "finish"):
            with self.subTest(fail_at=fail_at):
                sink, voice_handler, client, _, results, completed = self._build_sink(
                    fail_at=fail_at
                )
                packet = self._speech_packet(1500)
                for _ in range(15):
                    sink.write(packet, 11)

                await asyncio.wait_for(completed.wait(), timeout=2.0)
                voice_data, result_id = results[11]
                self.assertIsNotNone(voice_data)
                self.assertIsNone(result_id)
                with wave.open(BytesIO(base64.b64decode(voice_data)), "rb") as wav:
                    self.assertEqual(wav.getframerate(), 48_000)
                    self.assertEqual(wav.getnchannels(), 2)
                    self.assertEqual(wav.readframes(wav.getnframes()), packet * 15)
                await sink.aclose()
                self.assertFalse(voice_handler._streams)
                if fail_at != "start":
                    self.assertTrue(client.aborted)

    async def test_overlapping_users_keep_their_wav_and_receipt_pairs(self):
        sink, _, _, captured_owner, results, completed = self._build_sink()
        packets = {11: self._speech_packet(1200), 22: self._speech_packet(2400)}
        for _ in range(15):
            sink.write(packets[11], 11)
            sink.write(packets[22], 22)

        async def wait_for_both():
            while len(results) < 2:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_for_both(), timeout=2.0)
        expected = {11: (1200, "receipt-core-1"), 22: (2400, "receipt-core-2")}
        for user_id, (voice_data, result_id) in results.items():
            self.assertIsNotNone(voice_data)
            expected_amplitude, expected_receipt = expected[user_id]
            self.assertEqual(result_id, expected_receipt)
            with wave.open(BytesIO(base64.b64decode(voice_data)), "rb") as wav:
                self.assertEqual(wav.readframes(wav.getnframes()), packets[user_id] * 15)
        self.assertEqual(
            sorted(captured_owner.values()), [1200, 2400]
        )
        await sink.aclose()

    async def test_stream_queue_is_bounded_and_overflow_keeps_fallback_wav(self):
        start_gate = asyncio.Event()
        sink, _, _, _, results, _ = self._build_sink(block_start=start_gate)
        packet = self._speech_packet(1800)
        for _ in range(15):
            sink.write(packet, 33)

        await asyncio.sleep(0)
        capture_id = sink._capture_ids[33]
        with patch("voice_handler.MAX_STREAM_AUDIO_EVENTS", 8):
            for _ in range(16):
                sink._queue_from_audio_thread(
                    ("audio", 33, capture_id, b"stream-only")
                )

            self.assertLessEqual(sink._queued_audio_events, 8)
            self.assertLessEqual(
                sink._event_queue.qsize(),
                8 + MAX_STREAM_CONTROL_EVENTS,
            )
            self.assertTrue(sink._stream_failed(capture_id))
            start_gate.set()

            async def wait_for_result():
                while 33 not in results:
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_result(), timeout=3.0)
        voice_data, result_id = results[33]
        self.assertIsNone(result_id)
        with wave.open(BytesIO(base64.b64decode(voice_data)), "rb") as wav:
            self.assertEqual(wav.readframes(wav.getnframes()), packet * 15)
        await sink.aclose()


if __name__ == "__main__":
    import unittest

    unittest.main()
