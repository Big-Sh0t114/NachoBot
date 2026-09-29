from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np

import bili_src.audio.audio_player as audio_player_module
from bili_src.audio.audio_player import (
    AudioPlayer,
    _PCMLinearResampler,
    _SoundDevicePCMOutput,
)
from bili_src.core.runtime_profile import build_live_additional_config


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _FakeOutputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = False
        self.callback = kwargs["callback"]

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.active = False


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


class _FakeSoundDevice:
    def __init__(self, rate=48000, channels=2):
        self.device = {
            "default_samplerate": rate,
            "max_output_channels": channels,
        }
        self.settings = []
        self.streams = []

    def query_devices(self, *, kind):
        assert kind == "output"
        return self.device

    def check_output_settings(self, **kwargs):
        self.settings.append(kwargs)

    def RawOutputStream(self, **kwargs):
        stream = _FakeOutputStream(**kwargs)
        self.streams.append(stream)
        return stream


def _start_event(stream_id="stream-1", *, sample_rate=24000, channels=1):
    return {
        "event": "start",
        "stream_id": stream_id,
        "parent_message_id": "message-1",
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": 2,
        "codec": "pcm_s16le",
    }


def _chunk_event(seq, pcm, *, stream_id="stream-1", sample_rate=24000, channels=1):
    import base64

    event = _start_event(stream_id, sample_rate=sample_rate, channels=channels)
    event.update(
        event="chunk",
        seq=seq,
        audio_base64=base64.b64encode(pcm).decode("ascii"),
    )
    return event


class VoiceStreamPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_capability_requires_playback_readiness(self):
        base = {
            "search_enabled": False,
            "person_profile_enabled": False,
            "tts_enabled": True,
        }
        self.assertFalse(
            build_live_additional_config(**base)["runtime_capabilities"]["voice_stream"]
        )
        self.assertTrue(
            build_live_additional_config(
                **base,
                voice_stream_playback_ready=True,
            )["runtime_capabilities"]["voice_stream"]
        )
        self.assertFalse(
            build_live_additional_config(
                **{**base, "tts_enabled": False},
                voice_stream_playback_ready=True,
            )["runtime_capabilities"]["voice_stream"]
        )

    def test_stateful_resampler_is_chunk_boundary_independent_at_24k_and_22k(self):
        for rate, frames in ((24000, 2400), (22050, 2205)):
            with self.subTest(rate=rate):
                samples = np.arange(frames, dtype="<i2")
                pcm = samples.tobytes()
                one_shot = _PCMLinearResampler(rate, 1, 48000, 2)
                expected = one_shot.convert(pcm) + one_shot.finish()

                chunked = _PCMLinearResampler(rate, 1, 48000, 2)
                first = chunked.convert(pcm[: len(pcm) // 3])
                second = chunked.convert(pcm[len(pcm) // 3 : 2 * len(pcm) // 3])
                third = chunked.convert(pcm[2 * len(pcm) // 3 :])
                actual = first + second + third + chunked.finish()

                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), round(frames * 48000 / rate) * 4)

    def test_local_output_opens_at_device_rate_and_drains_after_last_callback(self):
        device = _FakeSoundDevice(rate=24000, channels=1)
        with patch.object(audio_player_module, "_sounddevice", device):
            output = _SoundDevicePCMOutput(24000, 1)
            stream = device.streams[-1]
            self.assertEqual(stream.kwargs["samplerate"], 24000)
            self.assertEqual(stream.kwargs["channels"], 1)

            output.write(np.arange(4, dtype="<i2").tobytes())
            final_audio_block = bytearray(8)
            stream.callback(final_audio_block, 4, None, None)
            output.finish()
            self.assertFalse(output.wait_until_drained(0))

            trailing_silence = bytearray(8)
            stream.callback(trailing_silence, 4, None, None)
            self.assertFalse(output.wait_until_drained(0))
            final_drain = bytearray(8)
            stream.callback(final_drain, 4, None, None)
            self.assertTrue(output.wait_until_drained(0))
            output.close()

    def test_local_output_resamples_24k_and_non_24k_to_default_device(self):
        device = _FakeSoundDevice(rate=48000, channels=2)
        with patch.object(audio_player_module, "_sounddevice", device):
            for rate, frames in ((24000, 2400), (22050, 2205)):
                with self.subTest(rate=rate):
                    output = _SoundDevicePCMOutput(rate, 1)
                    samples = np.arange(frames, dtype="<i2").tobytes()
                    split = len(samples) // 2
                    split -= split % 2
                    output.write(samples[:split])
                    output.write(samples[split:])
                    output.finish()
                    self.assertEqual(output._stream.kwargs["samplerate"], 48000)
                    self.assertEqual(output._stream.kwargs["channels"], 2)
                    self.assertEqual(
                        len(output._buffer),
                        round(frames * 48000 / rate) * 2 * 2,
                    )
                    output.abort()

    async def test_remote_end_waits_for_audio_queued_after_synthesis_pause(self):
        remote = AsyncMock(return_value=True)
        player = AudioPlayer(
            _Logger(),
            remote_voice_stream_callback=remote,
            remote_voice_stream_ready_callback=lambda: True,
        )
        player.start()
        pcm = np.zeros(24000, dtype="<i2").tobytes()  # 0.5 seconds at 48 kHz mono
        try:
            self.assertTrue(await player.handle_voice_stream_event(_start_event(sample_rate=48000)))
            self.assertTrue(
                await player.handle_voice_stream_event(
                    _chunk_event(0, pcm, sample_rate=48000)
                )
            )
            await asyncio.sleep(0.65)  # Longer than the first chunk's audio duration.
            self.assertTrue(
                await player.handle_voice_stream_event(
                    _chunk_event(1, pcm, sample_rate=48000)
                )
            )
            end = _start_event(sample_rate=48000)
            end["event"] = "end"
            self.assertTrue(await player.handle_voice_stream_event(end))
            await asyncio.sleep(0.5)
            self.assertTrue(player.is_playing)
            self.assertEqual(player.active_voice_stream_id, "stream-1")
        finally:
            await player.shutdown()

    async def test_invalid_sequence_aborts_and_cleans_stream(self):
        output = _FakePCMOutput()
        player = AudioPlayer(_Logger(), pcm_output_factory=lambda *_args: output)
        player.start()
        try:
            self.assertTrue(await player.handle_voice_stream_event(_start_event()))
            self.assertFalse(
                await player.handle_voice_stream_event(
                    _chunk_event(1, b"\0\0")
                )
            )
            self.assertIsNone(player.active_voice_stream_id)
            self.assertTrue(output.aborted)
        finally:
            await player.shutdown()


if __name__ == "__main__":
    unittest.main()
