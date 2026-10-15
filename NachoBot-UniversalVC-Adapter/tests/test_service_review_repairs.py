"""Regression coverage for fixes requested by the final service review."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from universalvc_service.audio_capture import MicrophoneCapture
from universalvc_service.audio_output import AudioOutput
from universalvc_service.audio_pipeline import AudioPipeline
from universalvc_service.config import AudioCaptureConfig, AudioOutputConfig, MicrophoneConfig
from universalvc_service.engine import UniversalVCSessionEngine
from universalvc_service.service_config import load_service_settings


class SplitVAD:
    available = True
    is_speaking = False

    def feed(self, samples):
        self.is_speaking = False
        return [
            SimpleNamespace(samples=np.full(3_200, 0.2, dtype=np.float32)),
            SimpleNamespace(samples=np.full(2_400, -0.2, dtype=np.float32)),
        ]

    def reset(self):
        self.is_speaking = False


class ReceiptCore:
    async def start_stream(self, **_kwargs):
        raise AssertionError("the scripted VAD has no active stream receipt")

    async def close(self):
        pass


class GatedVAD:
    available = True

    def __init__(self):
        self.feed_entered = threading.Event()
        self.feed_release = threading.Event()
        self.feed_active = threading.Event()
        self.reset_count = 0
        self.reset_during_feed = False
        self.is_speaking = False

    def feed(self, _samples):
        self.feed_active.set()
        self.feed_entered.set()
        try:
            if not self.feed_release.wait(3):
                raise TimeoutError("test did not release VAD feed")
        finally:
            self.feed_active.clear()
        self.is_speaking = True
        return []

    def reset(self):
        if self.feed_active.is_set():
            self.reset_during_feed = True
        self.reset_count += 1
        self.is_speaking = False


class QuantizedSilenceVAD:
    """Fake Silero-style VAD that completes on rounded 512-sample windows."""

    def __init__(self, min_silence_duration: float):
        self.is_speaking = False
        self.minimum_silence = math.ceil(min_silence_duration * 16_000 / 512) * 512
        self.silence_samples = 0
        self.speech_samples: list[np.ndarray] = []
        self.fed_samples: list[np.ndarray] = []

    def feed(self, samples):
        values = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
        self.fed_samples.append(values)
        if values.size and float(np.max(np.abs(values))) > 0.1:
            if not self.is_speaking:
                self.speech_samples.clear()
                self.silence_samples = 0
            self.is_speaking = True
            self.speech_samples.append(values)
            return []
        if self.is_speaking:
            self.silence_samples += len(values)
            if self.silence_samples >= self.minimum_silence + 512:
                self.is_speaking = False
                audio = np.concatenate(self.speech_samples)
                self.speech_samples.clear()
                return [SimpleNamespace(samples=audio)]
        return []

    def reset(self):
        self.is_speaking = False
        self.speech_samples.clear()
        self.silence_samples = 0


class ReviewRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        settings = load_service_settings(environ={"NACHOBOT_UNIVERSALVC_TOKEN": "repair-test-token"})
        vad_model = root / "vad.onnx"
        vad_model.write_bytes(b"test placeholder")
        self.settings = dataclasses.replace(
            settings,
            vad_model_path=vad_model,
            speaker_db_root=root / "speaker-db",
        )

    async def test_full_wav_multi_segment_results_get_distinct_scopes_and_turn_ids(self):
        results = []
        complete = asyncio.Event()
        vad = SplitVAD()

        with (
            patch("universalvc_service.audio_pipeline.DenoiseProcessor", return_value=SimpleNamespace(enabled=False)),
            patch("universalvc_service.audio_pipeline.VADProcessor", side_effect=lambda **_kwargs: vad),
            patch("universalvc_service.audio_pipeline.SpeakerTracker", return_value=SimpleNamespace(enabled=False)),
            patch("universalvc_service.audio_pipeline.samples_to_wav_base64", return_value="fake-wav"),
        ):
            def pipeline_factory(config, logger, **callbacks):
                return AudioPipeline(config, logger, **callbacks)

            def emit(event, source_id, **fields):
                if event == "speech_result":
                    results.append({"source_id": source_id, **fields})
                    if len(results) == 2:
                        complete.set()

            options = {
                "platform": "review-test",
                "scope": "multi-segment-fallback",
                "mode": "pipeline",
                "sources": [{
                    "id": "app-audio",
                    "input": "external",
                    "format": "f32le",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "capture": {},
                    "identity": {"mode": "fixed", "id": "app", "name": "App"},
                }],
                "output": {"mode": "none"},
                "denoise": False,
                "speaker": False,
                "speaker_log": False,
                "max_duration_seconds": 5,
            }
            engine = UniversalVCSessionEngine(
                self.settings,
                options,
                service_instance_id="review-service",
                emit=emit,
                pipeline_factory=pipeline_factory,
                core_factory=lambda *_args: ReceiptCore(),
            )
            await engine.start()
            try:
                await engine.process_audio(
                    "send_pcm",
                    "app-audio",
                    0,
                    np.zeros(1_600, dtype=np.float32).tobytes(),
                )
                await asyncio.wait_for(complete.wait(), timeout=2)
                scopes = [item["asr_scope"] for item in results]
                turns = [item["turn_id"] for item in results]
                self.assertTrue(all(isinstance(scope, str) and scope.startswith("uvc1:") for scope in scopes))
                self.assertEqual(len(set(scopes)), 2)
                self.assertEqual(len(set(turns)), 2)
                self.assertTrue(all(item["precomputed_asr_result_id"] is None for item in results))
                self.assertTrue(all(item["voice_base64"] == "fake-wav" for item in results))
                self.assertEqual(len(engine._scope_turns), 0)
            finally:
                await engine.close()

    async def test_interrupt_waits_for_inflight_vad_feed_before_reset(self):
        vad = GatedVAD()
        options = {
            "platform": "review-test",
            "scope": "vad-interrupt-lock",
            "mode": "vad",
            "sources": [{
                "id": "main",
                "input": "external",
                "format": "s16le",
                "sample_rate": 16_000,
                "channels": 1,
                "capture": {},
                "identity": {"mode": "fixed", "id": "owner", "name": "Owner"},
            }],
            "output": {"mode": "none"},
            "denoise": False,
            "speaker": False,
            "speaker_log": False,
            "max_duration_seconds": 5,
        }
        engine = UniversalVCSessionEngine(
            self.settings,
            options,
            service_instance_id="review-service",
            vad_factory=lambda **_kwargs: vad,
        )
        await engine.start()
        feed = asyncio.create_task(engine.process_audio("feed_vad", "main", 0, b"\x00\x00" * 320))
        try:
            self.assertTrue(await asyncio.to_thread(vad.feed_entered.wait, 2))
            interrupt = asyncio.create_task(engine.interrupt())
            await asyncio.sleep(0.03)
            self.assertEqual(vad.reset_count, 0)
            vad.feed_release.set()
            await asyncio.wait_for(asyncio.gather(feed, interrupt), timeout=2)
            self.assertFalse(vad.reset_during_feed)
            self.assertEqual(vad.reset_count, 1)
        finally:
            vad.feed_release.set()
            await engine.close()

    async def test_service_ptt_release_inserts_silence_and_splits_two_presses(self):
        minimum_silence = 0.25
        vad = QuantizedSilenceVAD(minimum_silence)
        segments = []
        encoded_audio = []
        config = SimpleNamespace(
            denoise=SimpleNamespace(enabled=False),
            vad=SimpleNamespace(
                model_path="unused-by-fake",
                threshold=0.5,
                min_silence_duration=minimum_silence,
                min_speech_duration=0.05,
            ),
            microphone=SimpleNamespace(
                enabled=True,
                owner_speaker_id="owner",
                owner_speaker_name="Owner",
            ),
            speaker=SimpleNamespace(
                enabled=False,
                embedding_model_path="unused-by-fake",
                similarity_threshold=0.5,
                max_speakers=8,
                db_path="unused-by-fake",
            ),
        )

        async def on_result(*args):
            segments.append(args)

        with (
            patch("universalvc_service.audio_pipeline.DenoiseProcessor", return_value=SimpleNamespace(enabled=False)),
            patch("universalvc_service.audio_pipeline.VADProcessor", side_effect=lambda **_kwargs: vad),
            patch("universalvc_service.audio_pipeline.SpeakerTracker", return_value=SimpleNamespace(enabled=False)),
            patch(
                "universalvc_service.audio_pipeline.samples_to_wav_base64",
                side_effect=lambda samples, **_kwargs: encoded_audio.append(np.array(samples, copy=True)) or f"wav-{len(encoded_audio)}",
            ),
        ):
            pipeline = AudioPipeline(
                config,
                logging.getLogger("service-ptt-review-test"),
                on_result=on_result,
                fixed_identity=("owner", "Owner"),
                speaker_log=False,
            )
            pipeline.set_loop(asyncio.get_running_loop())
            capture = MicrophoneCapture(
                MicrophoneConfig(enabled=True, push_to_talk=False),
                logging.getLogger("service-ptt-review-test"),
                on_frame=pipeline.process_mic_frame,
                strict_device_selection=True,
                queue_size=32,
                ptt_release_silence_duration=minimum_silence,
            )
            capture._loop = asyncio.get_running_loop()
            capture._queue = asyncio.Queue(maxsize=32)
            capture._running = True
            capture._samplerate = 16_000
            capture._channels = 1
            capture._ptt_monitor = SimpleNamespace(is_held=False)
            capture._worker_task = asyncio.create_task(capture._queue_worker_loop())

            async def callback(samples: np.ndarray):
                capture._audio_callback(samples, len(samples), None, None)
                await asyncio.sleep(0)
                await asyncio.wait_for(capture._queue.join(), timeout=2)
                deadline = asyncio.get_running_loop().time() + 2
                while True:
                    with pipeline._segment_jobs_lock:
                        if not pipeline._segment_jobs:
                            break
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("microphone segment job did not finish")
                    await asyncio.sleep(0.001)

            try:
                for value in (0.4, 0.7):
                    capture._ptt_monitor.is_held = True
                    await callback(np.full(320, value, dtype=np.float32))
                    capture._ptt_monitor.is_held = False
                    # Deliberately nonzero while released: it must never reach VAD.
                    await callback(np.full(320, 0.95, dtype=np.float32))
                    self.assertEqual(len(segments), 1 if value == 0.4 else 2)
                self.assertEqual(len(encoded_audio), 2)
                self.assertAlmostEqual(float(np.mean(encoded_audio[0])), 0.4, places=5)
                self.assertAlmostEqual(float(np.mean(encoded_audio[1])), 0.7, places=5)
                self.assertFalse(any(np.any(np.abs(frame) > 0.9) for frame in vad.fed_samples))
                silence_frame = next(frame for frame in vad.fed_samples if len(frame) > 320)
                expected_samples = math.ceil(16_000 * minimum_silence) + 2 * 512
                self.assertEqual(len(silence_frame), expected_samples)
            finally:
                capture._running = False
                capture._worker_task.cancel()
                await asyncio.gather(capture._worker_task, return_exceptions=True)
                await pipeline.stop_streaming()

    async def test_playback_queue_eviction_and_shutdown_remove_owned_wav_files(self):
        output = AudioOutput(AudioOutputConfig(), logging.getLogger("service-output-review-test"))
        output._is_paused = True
        paths = []
        for index in range(7):
            path = Path(self.temp.name) / f"queued-{index}.wav"
            path.write_bytes(b"temporary WAV owned by output queue")
            paths.append(path)
            await output.play(str(path))

        self.assertEqual(len(output._queue), 5)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(all(path.exists() for path in paths[2:]))
        await output.stop()
        self.assertTrue(all(not path.exists() for path in paths))

        repeated = Path(self.temp.name) / "repeated-queue-path.wav"
        repeated.write_bytes(b"still referenced by later queue entries")
        output = AudioOutput(AudioOutputConfig(), logging.getLogger("service-output-review-test"))
        output._is_paused = True
        for _ in range(6):
            await output.play(str(repeated))
        self.assertTrue(repeated.exists(), "evicting one duplicate reference must not delete another")
        await output.stop()
        self.assertFalse(repeated.exists())


if __name__ == "__main__":
    unittest.main()
