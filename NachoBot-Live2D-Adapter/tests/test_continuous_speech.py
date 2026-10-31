from __future__ import annotations

import io
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live2d_adapter.config import DesktopChatConfig, PerceptionConfig
from live2d_adapter.perception import DesktopPerception
from live2d_adapter.speech_input import SpeechSegmenter


class ContinuousSpeechTests(unittest.TestCase):
    def test_pause_segmentation_ignores_silence_clicks_and_retains_two_sentences(self):
        segmenter = SpeechSegmenter(16000)
        voice = b"\xe8\x03" * 320
        quiet = bytes(640)
        for _ in range(200):
            self.assertIsNone(segmenter.feed(quiet))
        self.assertLessEqual(segmenter._prefix_size, 8000)
        # A short noise impulse is not a sentence.
        self.assertIsNone(segmenter.feed(voice))
        self.assertFalse(any(segmenter.feed(quiet) for _ in range(45)))
        packets = []
        for _ in range(2):
            for pcm in [voice] * 20 + [quiet] * 45:
                result = segmenter.feed(pcm)
                if result:
                    packets.append(result)
        self.assertEqual(len(packets), 2)
        self.assertTrue(all(voice in packet for packet in packets))

    def service(self, perceive):
        events = []
        entered, exited = threading.Event(), threading.Event()
        transcribed = threading.Condition()
        streams = []

        def on_event(name, value):
            with transcribed:
                events.append((name, value))
                transcribed.notify_all()

        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]
                streams.append(self)

            def __enter__(self):
                entered.set()
                return self

            def __exit__(self, *_args):
                exited.set()

        service = DesktopPerception(
            DesktopChatConfig(transport="core", perception=PerceptionConfig(speech=True)),
            Mock(), on_event,
        )
        self.addCleanup(service.close)
        service.perceive = perceive
        device = SimpleNamespace(RawInputStream=Stream, check_input_settings=lambda **_: None)
        return service, events, entered, exited, transcribed, streams, device

    def test_one_microphone_stream_transcribes_multiple_segments_beyond_thirty_seconds(self):
        perceive = Mock(side_effect=["第一段", "第二段"])
        service, events, entered, exited, condition, streams, device = self.service(perceive)
        with patch.dict("sys.modules", {"sounddevice": device}):
            self.assertTrue(service.start_recording(continuous=True))
            self.assertTrue(entered.wait(2))
            chunk = b"\xe8\x03" * 1600  # 0.1 second of synthetic voiced PCM.
            for expected in (1, 2):
                for _ in range(155):
                    streams[0].callback(chunk, 1600, None, None)
                with condition:
                    self.assertTrue(condition.wait_for(
                        lambda: sum(name == "transcript" for name, _ in events) >= expected, timeout=3
                    ))
                self.assertTrue(service.recording)
                self.assertFalse(exited.is_set())
            self.assertEqual(len(streams), 1)
            self.assertEqual([value["text"] for name, value in events if name == "transcript"], ["第一段", "第二段"])
            for call in perceive.call_args_list:
                with wave.open(io.BytesIO(call.args[1]), "rb") as wav:
                    self.assertLessEqual(wav.getnframes(), 16000 * 15)
            packets = [value for name, value in events if name == "transcript"]
            self.assertEqual(service.transcript_text(packets[0]), "第一段")
            service.stop_recording(cancel=True)
            service._record_thread.join(2)
            service._asr_thread.join(2)
            self.assertTrue(exited.is_set())
            self.assertFalse(service.recording)
            self.assertIsNone(service.transcript_text(packets[0]))

    def test_close_stops_device_before_inflight_asr_finishes_and_discards_late_text(self):
        asr_started, release_asr = threading.Event(), threading.Event()

        def perceive(*_args, **_kwargs):
            asr_started.set()
            release_asr.wait(3)
            return "关闭后不能发送"

        service, events, entered, exited, _condition, streams, device = self.service(perceive)
        with patch.dict("sys.modules", {"sounddevice": device}):
            self.assertTrue(service.start_recording(continuous=True))
            self.assertTrue(entered.wait(2))
            streams[0].callback(b"\xe8\x03" * 6400, 6400, None, None)
            streams[0].callback(bytes(32000), 16000, None, None)
            self.assertTrue(asr_started.wait(2))
            service.stop_recording(cancel=True)
            service._record_thread.join(2)
            self.assertTrue(exited.is_set())
            release_asr.set()
            service._asr_thread.join(2)
            self.assertFalse(any(name == "transcript" for name, _ in events))

    def test_pet_output_audio_is_not_transcribed(self):
        perceive = Mock(return_value="用户说话")
        service, events, entered, _exited, condition, streams, device = self.service(perceive)
        with patch.dict("sys.modules", {"sounddevice": device}):
            self.assertTrue(service.start_recording(continuous=True))
            self.assertTrue(entered.wait(2))
            service.set_output_speaking(True)
            streams[0].callback(b"\xe8\x03" * 6400, 6400, None, None)
            streams[0].callback(bytes(32000), 16000, None, None)
            service.set_output_speaking(False)
            with patch("live2d_adapter.perception.time.monotonic", return_value=service._output_quiet_after + 1):
                streams[0].callback(b"\xe8\x03" * 6400, 6400, None, None)
                streams[0].callback(bytes(32000), 16000, None, None)
            with condition:
                self.assertTrue(condition.wait_for(lambda: any(name == "transcript" for name, _ in events), timeout=2))
            service.stop_recording(cancel=True)
            service._record_thread.join(2)
            service._asr_thread.join(2)
            perceive.assert_called_once()
