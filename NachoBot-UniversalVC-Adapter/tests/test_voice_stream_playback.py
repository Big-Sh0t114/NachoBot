import asyncio
import base64
import logging
import os
import tempfile
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

import numpy as np

from adapter import UniversalVCAdapter
from audio_output import AudioOutput


class FakeOutputStream:
    def __init__(self, *, write_gate=None, **kwargs):
        self.kwargs = kwargs
        self.write_gate = write_gate
        self.write_started = threading.Event()
        self.abort_called = False
        self.started = False
        self.stopped = False
        self.closed = False
        self.writes = []

    def start(self):
        self.started = True

    def write(self, samples):
        self.write_started.set()
        if self.write_gate is not None:
            while not self.write_gate.wait(0.01):
                if self.abort_called:
                    return
        if not self.abort_called:
            self.writes.append(np.array(samples, copy=True))

    def stop(self):
        self.stopped = True

    def abort(self):
        self.abort_called = True
        if self.write_gate is not None:
            self.write_gate.set()

    def close(self):
        self.closed = True


class FakeSoundDevice:
    def __init__(self, *, device_rate=48_000, max_channels=2, write_gate=None):
        self.device_info = {
            "name": "CABLE Input",
            "max_output_channels": max_channels,
            "default_samplerate": device_rate,
        }
        self.write_gate = write_gate
        self.output_streams = []
        self.play_calls = []
        self.stream_active = False

    def query_devices(self, device=None, *, kind=None):
        if device is None and kind is None:
            return [self.device_info]
        return self.device_info

    def OutputStream(self, **kwargs):
        stream = FakeOutputStream(write_gate=self.write_gate, **kwargs)
        self.output_streams.append(stream)
        return stream

    def play(self, samples, *, samplerate, device, blocking):
        self.play_calls.append((np.array(samples, copy=True), samplerate, device, blocking))

    def get_stream(self):
        return SimpleNamespace(active=self.stream_active)


class VoiceStreamPlaybackTests(IsolatedAsyncioTestCase):
    def _make_output(self, sounddevice=None):
        output = AudioOutput(
            SimpleNamespace(device_name="CABLE Input", sample_rate=44_100),
            logging.getLogger("test-universalvc-stream-playback"),
        )
        output._sd = sounddevice or FakeSoundDevice()
        output.initialize()
        return output

    @staticmethod
    def _event(event, *, stream_id="stream-1", parent_message_id="parent-1", **extra):
        return {
            "event": event,
            "stream_id": stream_id,
            "parent_message_id": parent_message_id,
            "sample_rate": 16_000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
            **extra,
        }

    @staticmethod
    def _chunk(seq, samples, **extra):
        return VoiceStreamPlaybackTests._event(
            "chunk",
            seq=seq,
            audio_base64=base64.b64encode(np.asarray(samples, dtype="<i2").tobytes()).decode(
                "ascii"
            ),
            **extra,
        )

    async def test_one_output_stream_drains_and_resamples_across_chunk_boundaries(self):
        output = self._make_output(FakeSoundDevice(device_rate=48_000))
        await output.start_voice_stream(self._event("start"))
        await output.write_voice_stream_chunk(self._chunk(0, [0, 1000]))
        await output.write_voice_stream_chunk(self._chunk(1, [-1000, 0]))
        await output.end_voice_stream(self._event("end"))

        self.assertEqual(len(output._sd.output_streams), 1)
        stream = output._sd.output_streams[0]
        self.assertTrue(stream.started)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertFalse(stream.abort_called)
        self.assertEqual(sum(len(call) for call in stream.writes), 12)
        self.assertTrue(all(call.shape[1] == 2 for call in stream.writes))
        self.assertEqual(output._voice_streams, {})

    async def test_out_of_order_chunk_aborts_stream_and_releases_device(self):
        output = self._make_output()
        await output.start_voice_stream(self._event("start"))

        with self.assertRaisesRegex(ValueError, "sequence"):
            await output.write_voice_stream_chunk(self._chunk(1, [1, 2]))

        stream = output._sd.output_streams[0]
        self.assertTrue(stream.abort_called)
        self.assertTrue(stream.closed)
        self.assertEqual(output._voice_streams, {})

    async def test_format_and_frame_alignment_mismatches_abort_stream(self):
        output = self._make_output()
        await output.start_voice_stream(self._event("start"))
        mismatched = self._chunk(0, [1, 2], sample_rate=22_050)

        with self.assertRaisesRegex(ValueError, "format"):
            await output.write_voice_stream_chunk(mismatched)
        self.assertTrue(output._sd.output_streams[0].closed)

        second = self._make_output()
        await second.start_voice_stream(self._event("start", stream_id="stream-2"))
        malformed = self._event(
            "chunk",
            stream_id="stream-2",
            seq=0,
            audio_base64=base64.b64encode(b"\x01").decode("ascii"),
        )
        with self.assertRaisesRegex(ValueError, "complete PCM frames"):
            await second.write_voice_stream_chunk(malformed)
        self.assertTrue(second._sd.output_streams[0].closed)

    async def test_chunk_size_is_bounded(self):
        output = self._make_output()
        await output.start_voice_stream(self._event("start"))
        too_large = b"\x00\x00" * (output.MAX_STREAM_CHUNK_BYTES // 2 + 1)
        event = self._event(
            "chunk",
            seq=0,
            audio_base64=base64.b64encode(too_large).decode("ascii"),
        )

        with self.assertRaisesRegex(ValueError, "too large"):
            await output.write_voice_stream_chunk(event)
        self.assertTrue(output._sd.output_streams[0].closed)

    async def test_full_pcm_queue_aborts_instead_of_blocking_the_receiver(self):
        gate = threading.Event()
        sounddevice = FakeSoundDevice(write_gate=gate)
        output = self._make_output(sounddevice)
        output.MAX_STREAM_QUEUE_CHUNKS = 1
        await output.start_voice_stream(self._event("start"))
        await output.write_voice_stream_chunk(self._chunk(0, [1, 2, 3, 4]))
        stream = sounddevice.output_streams[0]
        await asyncio.wait_for(asyncio.to_thread(stream.write_started.wait, 1), timeout=2)

        await output.write_voice_stream_chunk(self._chunk(1, [5, 6, 7, 8]))
        with self.assertRaisesRegex(BufferError, "queue is full"):
            await output.write_voice_stream_chunk(self._chunk(2, [9, 10, 11, 12]))

        self.assertTrue(stream.abort_called)
        self.assertTrue(stream.closed)
        self.assertEqual(output._voice_streams, {})

    async def test_local_speech_interrupt_aborts_a_blocked_stream_writer(self):
        gate = threading.Event()
        sounddevice = FakeSoundDevice(write_gate=gate)
        output = self._make_output(sounddevice)
        await output.start_voice_stream(self._event("start"))
        await output.write_voice_stream_chunk(self._chunk(0, [1, 2, 3, 4]))
        stream = sounddevice.output_streams[0]
        await asyncio.wait_for(asyncio.to_thread(stream.write_started.wait, 1), timeout=2)

        await asyncio.wait_for(output.stop_current(), timeout=1)

        self.assertTrue(stream.abort_called)
        self.assertTrue(stream.closed)
        self.assertEqual(output._voice_streams, {})

    async def test_abort_event_discards_pcm_and_closes_the_stream(self):
        output = self._make_output()
        await output.start_voice_stream(self._event("start"))
        await output.abort_voice_stream(self._event("abort"))

        stream = output._sd.output_streams[0]
        self.assertTrue(stream.abort_called)
        self.assertTrue(stream.closed)
        self.assertEqual(output._voice_streams, {})

    async def test_malformed_abort_still_frees_resources_and_is_rejected(self):
        output = self._make_output()
        await output.start_voice_stream(self._event("start"))
        malformed_abort = self._event("abort", parent_message_id="wrong-parent")

        with self.assertRaisesRegex(ValueError, "abort format"):
            await output.abort_voice_stream(malformed_abort)

        stream = output._sd.output_streams[0]
        self.assertTrue(stream.abort_called)
        self.assertTrue(stream.closed)
        self.assertEqual(output._voice_streams, {})

    async def test_buffered_voice_still_uses_the_wav_playback_path(self):
        output = self._make_output()
        handle, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(handle)
        try:
            with wave.open(wav_path, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16_000)
                wav_file.writeframes(np.array([0, 1000, -1000], dtype="<i2").tobytes())

            await output._play_wav(wav_path)

            self.assertEqual(len(output._sd.play_calls), 1)
            samples, rate, device, blocking = output._sd.play_calls[0]
            self.assertEqual(samples.shape[1], 2)
            self.assertEqual(rate, 48_000)
            self.assertEqual(device, 0)
            self.assertFalse(blocking)
            self.assertEqual(output._sd.output_streams, [])
        finally:
            Path(wav_path).unlink(missing_ok=True)

    async def test_adapter_routes_stream_events_and_advertises_the_capability(self):
        adapter = UniversalVCAdapter.__new__(UniversalVCAdapter)
        adapter.audio_output = SimpleNamespace(
            start_voice_stream=AsyncMock(),
            write_voice_stream_chunk=AsyncMock(),
            end_voice_stream=AsyncMock(),
            abort_voice_stream=AsyncMock(),
            play=AsyncMock(),
        )
        adapter.logger = Mock()

        async def deliver(event):
            await adapter._handle_from_nachobot(
                {"message_segment": {"type": "voice_stream", "data": event}}
            )

        for action, method in (
            ("start", adapter.audio_output.start_voice_stream),
            ("chunk", adapter.audio_output.write_voice_stream_chunk),
            ("end", adapter.audio_output.end_voice_stream),
            ("abort", adapter.audio_output.abort_voice_stream),
        ):
            event = self._event(action)
            await deliver(event)
            method.assert_awaited_with(event)
            method.reset_mock()

        adapter.config = SimpleNamespace(
            prompts=SimpleNamespace(planner_prompt="", replyer_prompt="", variables={})
        )
        adapter.router = SimpleNamespace(send_message=AsyncMock())
        adapter.pipeline = SimpleNamespace(TARGET_SR=16_000)
        adapter.audio_capture = SimpleNamespace(get_application_name=lambda: "test-app")
        adapter._session_id = "uvc-test"
        await adapter._on_speech_result(
            speaker_id="speaker-1",
            speaker_name="listener",
            voice_data="input-wav",
        )
        message = adapter.router.send_message.await_args.args[0]
        self.assertIs(
            message.message_info.additional_config["runtime_capabilities"]["voice_stream"],
            True,
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
