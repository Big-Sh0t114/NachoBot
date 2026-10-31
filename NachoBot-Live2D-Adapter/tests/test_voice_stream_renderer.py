from __future__ import annotations

import base64
import queue
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live2d_adapter.config import AdapterConfig, RendererConfig, ServerConfig
from live2d_adapter.protocol import ProtocolError
from live2d_adapter.renderer import Live2DRenderer, _PCM16Resampler
from live2d_adapter.runtime import AvatarRuntime


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _FakeChannel:
    def __init__(self):
        self.busy = False
        self.current = None
        self.queued = None
        self.stopped = False

    def get_busy(self):
        return self.busy

    def get_sound(self):
        return self.current

    def play(self, sound):
        self.current = sound
        self.busy = True

    def queue(self, sound):
        self.queued = sound

    def advance(self):
        if self.queued is not None:
            self.current = self.queued
            self.queued = None
        else:
            self.current = None
            self.busy = False

    def stop(self):
        self.stopped = True
        self.current = None
        self.queued = None
        self.busy = False


class _FakeMixer:
    def __init__(self):
        self.format = (48000, -16, 2)
        self.channel = _FakeChannel()
        self.sounds = []

    def get_init(self):
        return self.format

    def quit(self):
        self.format = None

    def init(self, *, frequency, size, channels, buffer):
        self.format = (frequency, size, channels)

    def set_num_channels(self, _count):
        pass

    def get_num_channels(self):
        return 8

    def Channel(self, _index):
        return self.channel

    def Sound(self, *, buffer):
        sound = bytes(buffer)
        self.sounds.append(sound)
        return sound


def _renderer() -> Live2DRenderer:
    renderer = Live2DRenderer(
        model_path="unused.model3.json",
        logger=_Logger(),
        command_queue=queue.Queue(),
        model_adapter=SimpleNamespace(),
    )
    renderer._pcm_output_rate = 48000
    renderer._pcm_output_channels = 2
    return renderer


def _event(operation, *, seq=None, pcm=None, rate=48000, channels=2):
    result = {
        "event": operation,
        "stream_id": "stream-1",
        "parent_message_id": "message-1",
        "sample_rate": rate,
        "channels": channels,
        "sample_width": 2,
        "codec": "pcm_s16le",
    }
    if seq is not None:
        result["seq"] = seq
    if pcm is not None:
        result["pcm"] = pcm
    return result


class VoiceStreamRendererTests(unittest.TestCase):
    def test_disconnect_after_end_preserves_accepted_audio_for_drain(self):
        config = AdapterConfig(
            renderer=RendererConfig(model_path=Path("unused")), server=ServerConfig(),
        )
        runtime = AvatarRuntime(config, _Logger())
        runtime.renderer = SimpleNamespace(pcm_audio_ready=True)
        common = {
            "stream_id": "s", "parent_message_id": "m", "sample_rate": 24000,
            "channels": 1, "sample_width": 2, "codec": "pcm_s16le",
        }
        runtime._dispatch_voice_stream(dict(common, event="start"), "owner")
        runtime._dispatch_voice_stream(dict(common, event="chunk", seq=0,
            audio_base64=base64.b64encode(b"\0\0").decode()), "owner")
        runtime._dispatch_voice_stream(dict(common, event="end"), "owner")
        runtime.discard_client_controls("owner")
        self.assertEqual([e["event"] for e in runtime.voice_stream_queue.queue],
                         ["start", "chunk", "end"])

    def test_disconnect_during_generation_aborts_owner_stream(self):
        config = AdapterConfig(
            renderer=RendererConfig(model_path=Path("unused")), server=ServerConfig(),
        )
        runtime = AvatarRuntime(config, _Logger())
        runtime.renderer = SimpleNamespace(pcm_audio_ready=True)
        common = {
            "stream_id": "s", "parent_message_id": "m", "sample_rate": 24000,
            "channels": 1, "sample_width": 2, "codec": "pcm_s16le",
        }
        runtime._dispatch_voice_stream(dict(common, event="start"), "owner")
        runtime.discard_client_controls("other")
        self.assertIsNotNone(runtime._voice_stream_id)
        runtime.discard_client_controls("owner")
        self.assertEqual([e["event"] for e in runtime.voice_stream_queue.queue], ["abort"])
        self.assertIsNone(runtime._voice_stream_id)

    def test_end_waits_for_the_queued_final_sound_to_finish(self):
        mixer = _FakeMixer()
        renderer = _renderer()
        renderer.pcm_audio_ready = True
        with patch("live2d_adapter.renderer.pygame.mixer", mixer):
            renderer._handle_voice_stream_event(_event("start"))
            renderer._handle_voice_stream_event(_event("chunk", seq=0, pcm=b"\0" * 16))
            renderer._pump_pcm_stream()
            renderer._handle_voice_stream_event(_event("chunk", seq=1, pcm=b"\1" * 16))
            renderer._pump_pcm_stream()
            self.assertIsNotNone(mixer.channel.queued)

            renderer._handle_voice_stream_event(_event("end"))
            renderer._pump_pcm_stream()
            self.assertTrue(renderer.is_speaking)

            mixer.channel.advance()  # First sound ended; second sound is now current.
            renderer._pump_pcm_stream()
            self.assertTrue(renderer.is_speaking)
            self.assertIsNotNone(mixer.channel.queued)

            mixer.channel.advance()  # Second sound ended; resampler tail is current.
            renderer._pump_pcm_stream()
            self.assertTrue(renderer.is_speaking)

            mixer.channel.advance()  # The final sound has now drained.
            renderer._pump_pcm_stream()
            self.assertFalse(renderer.is_speaking)
            self.assertIsNone(renderer._pcm_stream_id)

    def test_abort_stops_current_audio_immediately(self):
        mixer = _FakeMixer()
        renderer = _renderer()
        renderer.pcm_audio_ready = True
        with patch("live2d_adapter.renderer.pygame.mixer", mixer):
            renderer._handle_voice_stream_event(_event("start"))
            renderer._handle_voice_stream_event(_event("chunk", seq=0, pcm=b"\0" * 16))
            renderer._pump_pcm_stream()
            self.assertTrue(renderer.is_speaking)

            renderer._handle_voice_stream_event(_event("abort"))

            self.assertTrue(mixer.channel.stopped)
            self.assertFalse(renderer.is_speaking)
            self.assertIsNone(renderer._pcm_stream_id)

    def test_unavailable_mixer_disables_stream_readiness(self):
        mixer = _FakeMixer()
        mixer.format = None
        renderer = _renderer()
        with patch("live2d_adapter.renderer.pygame.mixer", mixer):
            self.assertFalse(renderer._refresh_pcm_audio_readiness())
            self.assertFalse(renderer.pcm_audio_ready)
            self.assertIsNone(renderer._pcm_stream_id)
            self.assertFalse(renderer.is_speaking)

    def test_non_default_source_rate_is_resampled_to_ready_mixer_format(self):
        mixer = _FakeMixer()
        renderer = _renderer()
        renderer.pcm_audio_ready = True
        pcm = b"\0\0" * 2205
        with patch("live2d_adapter.renderer.pygame.mixer", mixer):
            renderer._handle_voice_stream_event(
                _event("start", rate=22050, channels=1)
            )
            renderer._handle_voice_stream_event(
                _event("chunk", seq=0, pcm=pcm, rate=22050, channels=1)
            )
            renderer._handle_voice_stream_event(
                _event("end", rate=22050, channels=1)
            )
            renderer._pump_pcm_stream()
            renderer._pump_pcm_stream()

            self.assertEqual(
                len(mixer.channel.current) + len(mixer.channel.queued),
                4800 * 2 * 2,
            )
            self.assertEqual(mixer.format, (48000, -16, 2))

    def test_renderer_resampler_keeps_chunk_boundaries_continuous(self):
        for rate, frames in ((24000, 2400), (22050, 2205)):
            with self.subTest(rate=rate):
                pcm = b"\0\0" * frames
                one_shot = _PCM16Resampler(rate, 1, 48000, 2)
                expected = one_shot.convert(pcm) + one_shot.finish()

                split_resampler = _PCM16Resampler(rate, 1, 48000, 2)
                split = len(pcm) // 3
                split -= split % 2
                actual = (
                    split_resampler.convert(pcm[:split])
                    + split_resampler.convert(pcm[split : split * 2])
                    + split_resampler.convert(pcm[split * 2 :])
                    + split_resampler.finish()
                )
                self.assertEqual(actual, expected)
                self.assertEqual(len(actual), round(frames * 48000 / rate) * 4)

    def test_runtime_preserves_chunk_order_before_end_and_rejects_unready_start(self):
        config = AdapterConfig(
            renderer=RendererConfig(model_path=Path("unused")),
            server=ServerConfig(),
        )
        runtime = AvatarRuntime(config, _Logger())
        runtime.renderer = SimpleNamespace(pcm_audio_ready=True)
        common = {
            "stream_id": "stream-1",
            "parent_message_id": "message-1",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
        }
        runtime._dispatch_voice_stream({**common, "event": "start"}, "client")
        runtime._dispatch_voice_stream(
            {
                **common,
                "event": "chunk",
                "seq": 0,
                "audio_base64": base64.b64encode(b"\0\0").decode("ascii"),
            },
            "client",
        )
        runtime._dispatch_voice_stream({**common, "event": "end"}, "client")
        events = list(runtime.voice_stream_queue.queue)
        self.assertEqual([event["event"] for event in events], ["start", "chunk", "end"])
        self.assertEqual(events[1]["pcm"], b"\0\0")

        unready = AvatarRuntime(config, _Logger())
        unready.renderer = SimpleNamespace(pcm_audio_ready=False)
        with self.assertRaisesRegex(ProtocolError, "not ready"):
            unready._dispatch_voice_stream({**common, "event": "start"}, "client")


if __name__ == "__main__":
    unittest.main()
