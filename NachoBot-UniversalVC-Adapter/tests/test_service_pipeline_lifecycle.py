"""Service-only interrupt fencing tests for the real AudioPipeline class."""

from __future__ import annotations

import asyncio
import logging
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from universalvc_service.audio_pipeline import AudioPipeline


def _segment(value: float = 0.4):
    return SimpleNamespace(samples=np.full(3_200, value, dtype=np.float32))


class ScriptedVAD:
    def __init__(self, steps):
        self.steps = deque(steps)
        self.is_speaking = False
        self.reset_count = 0

    def feed(self, _samples):
        speaking, segments = self.steps.popleft()
        self.is_speaking = speaking
        return segments

    def reset(self):
        self.reset_count += 1
        self.is_speaking = False


class FrameGateVAD:
    """Blocks the first frame so interrupt reset ordering is observable."""

    def __init__(self):
        self.feed_entered = threading.Event()
        self.feed_release = threading.Event()
        self.feed_active = threading.Event()
        self.is_speaking = False
        self.reset_count = 0
        self.reset_during_feed = False
        self.calls = 0

    def feed(self, _samples):
        self.calls += 1
        if self.calls == 1:
            self.feed_active.set()
            self.feed_entered.set()
            try:
                if not self.feed_release.wait(timeout=3):
                    raise TimeoutError("test did not release gated VAD feed")
            finally:
                self.feed_active.clear()
            self.is_speaking = True
            return []
        self.is_speaking = False
        return [_segment()]

    def reset(self):
        if self.feed_active.is_set():
            self.reset_during_feed = True
        self.reset_count += 1
        self.is_speaking = False


class DelayedCore:
    def __init__(self):
        self.start_count = 0
        self.finish_started = asyncio.Event()
        self.release_first_finish = asyncio.Event()
        self.finish_done = asyncio.Event()
        self.aborts = []

    async def start_stream(self, *, sample_rate, channels, **_kwargs):
        self.start_count += 1
        return f"stream-{self.start_count}"

    async def send_chunk(self, _stream_id, _seq, _pcm):
        pass

    async def finish_stream(self, stream_id):
        if stream_id == "stream-1":
            self.finish_started.set()
            await self.release_first_finish.wait()
        self.finish_done.set()
        return {"result_id": f"receipt-{stream_id}", "text": f"text-{stream_id}"}

    async def abort_stream(self, stream_id):
        self.aborts.append(stream_id)


class DelayedSpeakerTracker:
    def __init__(self):
        self.started = threading.Event()
        self.release_first = threading.Event()
        self.first_done = threading.Event()
        self.calls = 0

    def identify(self, _samples):
        self.calls += 1
        call = self.calls
        if call == 1:
            self.started.set()
            if not self.release_first.wait(timeout=3):
                raise TimeoutError("test did not release gated speaker identification")
            self.first_done.set()
        return f"speaker-{call}", f"Speaker {call}"


class PipelineLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _make_pipeline(self, vad, *, stream_client=None, tracker=None, on_result=None):
        config = SimpleNamespace(
            denoise=SimpleNamespace(enabled=False),
            vad=SimpleNamespace(
                model_path="unused-by-fake",
                threshold=0.5,
                min_silence_duration=0.25,
                min_speech_duration=0.3,
            ),
            microphone=SimpleNamespace(
                enabled=False,
                owner_speaker_id="owner",
                owner_speaker_name="Owner",
            ),
            speaker=SimpleNamespace(
                enabled=True,
                embedding_model_path="unused-by-fake",
                similarity_threshold=0.5,
                max_speakers=8,
                db_path="unused-by-fake",
            ),
        )
        tracker = tracker or SimpleNamespace(
            identify=lambda _samples: ("speaker", "Speaker")
        )
        patchers = [
            patch(
                "universalvc_service.audio_pipeline.DenoiseProcessor",
                return_value=SimpleNamespace(enabled=False),
            ),
            patch(
                "universalvc_service.audio_pipeline.VADProcessor",
                side_effect=lambda **_kwargs: vad,
            ),
            patch(
                "universalvc_service.audio_pipeline.SpeakerTracker",
                return_value=tracker,
            ),
            patch(
                "universalvc_service.audio_pipeline.samples_to_wav_base64",
                return_value="fake-voice-wav",
            ),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        pipeline = AudioPipeline(
            config=config,
            logger=logging.getLogger("test-service-pipeline-lifecycle"),
            on_result=on_result,
            stream_client=stream_client,
            stream_platform="universal_vc",
            speaker_log=False,
            interrupt_fences_segments=True,
        )
        pipeline.set_loop(asyncio.get_running_loop())
        self.addAsyncCleanup(pipeline.stop_streaming)
        return pipeline

    @staticmethod
    def _frame(pipeline, value=0.5):
        pipeline.process_frame(np.full(1_600, value, dtype=np.float32), 16_000, 1)

    async def _wait_for_segment_jobs(self, pipeline, timeout=2):
        async def wait_until_empty():
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                with pipeline._segment_jobs_lock:
                    pending = bool(pipeline._segment_jobs)
                if not pending:
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("pipeline segment jobs did not settle")
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_until_empty(), timeout=timeout + 0.1)

    async def test_service_interrupt_fences_delayed_speaker_and_core_receipt_then_allows_new_turn(self):
        vad = ScriptedVAD([(True, []), (False, [_segment()])])
        core = DelayedCore()
        tracker = DelayedSpeakerTracker()
        results = []
        result_ready = asyncio.Event()

        async def on_result(*args):
            results.append(args)
            result_ready.set()

        pipeline = self._make_pipeline(
            vad, stream_client=core, tracker=tracker, on_result=on_result
        )
        self._frame(pipeline)
        self._frame(pipeline, 0.0)

        try:
            await asyncio.wait_for(core.finish_started.wait(), timeout=2)
            self.assertTrue(await asyncio.wait_for(
                asyncio.to_thread(tracker.started.wait, 2), timeout=2.1
            ))

            await pipeline.interrupt()
            tracker.release_first.set()
            core.release_first_finish.set()
            self.assertTrue(await asyncio.wait_for(
                asyncio.to_thread(tracker.first_done.wait, 2), timeout=2.1
            ))
            await self._wait_for_segment_jobs(pipeline)
            self.assertEqual(results, [], "the pre-interrupt segment must not publish late")

            vad.steps.extend([(True, []), (False, [_segment(0.6)])])
            self._frame(pipeline)
            self._frame(pipeline, 0.0)
            await asyncio.wait_for(result_ready.wait(), timeout=2)
            await self._wait_for_segment_jobs(pipeline)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "speaker-2")
            self.assertEqual(results[0][3:], ("receipt-stream-2", "text-stream-2"))
        finally:
            tracker.release_first.set()
            core.release_first_finish.set()

    async def test_service_interrupt_waits_for_inflight_vad_feed_and_next_turn_still_publishes(self):
        vad = FrameGateVAD()
        results = []
        result_ready = asyncio.Event()

        async def on_result(*args):
            results.append(args)
            result_ready.set()

        pipeline = self._make_pipeline(vad, on_result=on_result)
        frame_task = asyncio.create_task(
            asyncio.to_thread(
                pipeline.process_frame,
                np.full(1_600, 0.5, dtype=np.float32),
                16_000,
                1,
            )
        )
        timer = threading.Timer(2, vad.feed_release.set)
        timer.start()
        try:
            self.assertTrue(await asyncio.wait_for(
                asyncio.to_thread(vad.feed_entered.wait, 1), timeout=1.1
            ))
            interrupt_task = asyncio.create_task(pipeline.interrupt())
            await asyncio.sleep(0.05)
            reset_before_feed_completed = vad.reset_count
            feed_active_before_release = vad.feed_active.is_set()
            vad.feed_release.set()
            await asyncio.wait_for(frame_task, timeout=2)
            await asyncio.wait_for(interrupt_task, timeout=2)

            self.assertEqual(reset_before_feed_completed, 0)
            self.assertTrue(feed_active_before_release)
            self.assertFalse(vad.reset_during_feed)
            self.assertEqual(vad.reset_count, 1)
            self.assertFalse(vad.is_speaking)

            pipeline.process_frame(np.full(1_600, 0.0, dtype=np.float32), 16_000, 1)
            await asyncio.wait_for(result_ready.wait(), timeout=2)
            await self._wait_for_segment_jobs(pipeline)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "speaker")
        finally:
            vad.feed_release.set()
            timer.cancel()
            await asyncio.gather(frame_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
