import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import numpy as np

from audio_pipeline import AudioPipeline


class ScriptedVAD:
    def __init__(self, steps):
        self.steps = list(steps)
        self._is_speaking = False

    @property
    def is_speaking(self):
        return self._is_speaking

    def feed(self, _samples):
        if not self.steps:
            self._is_speaking = False
            return []
        self._is_speaking, segments = self.steps.pop(0)
        return segments


def segment(samples=None):
    if samples is None:
        samples = np.full(3200, 0.5, dtype=np.float32)
    return SimpleNamespace(samples=samples)


class FakeCoreStreamClient:
    def __init__(
        self,
        *,
        finish_response=None,
        fail_start=False,
        fail_chunk=False,
        start_gate=None,
        finish_wait_for=None,
    ):
        self.finish_response = finish_response or {
            "text": "recognized words",
            "result_id": "receipt-1",
        }
        self.fail_start = fail_start
        self.fail_chunk = fail_chunk
        self.start_gate = start_gate
        self.finish_wait_for = finish_wait_for
        self.started_event = asyncio.Event()
        self.finish_complete_event = asyncio.Event()
        self.starts = []
        self.chunks = []
        self.finishes = []
        self.aborts = []

    async def start_stream(self, *, sample_rate, channels):
        self.starts.append((sample_rate, channels))
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("streaming unavailable")
        self.started_event.set()
        return f"stream-{len(self.starts)}"

    async def send_chunk(self, stream_id, seq, pcm):
        self.chunks.append((stream_id, seq, bytes(pcm)))
        if self.fail_chunk:
            raise RuntimeError("chunk transport failed")

    async def finish_stream(self, stream_id):
        if self.finish_wait_for is not None:
            await asyncio.to_thread(self.finish_wait_for.wait, 2)
        self.finishes.append(stream_id)
        self.finish_complete_event.set()
        return self.finish_response

    async def abort_stream(self, stream_id):
        self.aborts.append(stream_id)


class AudioStreamingTests(IsolatedAsyncioTestCase):
    def _make_pipeline(
        self,
        vad,
        client,
        *,
        microphone_enabled=False,
        on_mic_speech_start=None,
        on_mic_speech_end=None,
        speaker_tracker=None,
    ):
        config = SimpleNamespace(
            denoise=SimpleNamespace(enabled=False),
            vad=SimpleNamespace(
                model_path="unused",
                threshold=0.5,
                min_silence_duration=0.25,
                min_speech_duration=0.3,
            ),
            microphone=SimpleNamespace(
                enabled=microphone_enabled,
                owner_speaker_id="owner-1",
                owner_speaker_name="主人",
            ),
            speaker=SimpleNamespace(
                enabled=True,
                embedding_model_path="unused",
                similarity_threshold=0.5,
                max_speakers=8,
                db_path="unused",
            ),
        )
        results = []
        result_event = asyncio.Event()

        async def on_result(
            speaker_id, speaker_name, voice_data, result_id, confirmed_text
        ):
            results.append(
                (speaker_id, speaker_name, voice_data, result_id, confirmed_text)
            )
            result_event.set()

        vad_instances = iter(vad if isinstance(vad, list) else [vad])
        wav_patcher = patch(
            "audio_pipeline.samples_to_wav_base64", return_value="full-wav-payload"
        )
        wav_patcher.start()
        self.addCleanup(wav_patcher.stop)
        with (
            patch("audio_pipeline.DenoiseProcessor", return_value=SimpleNamespace(enabled=False)),
            patch("audio_pipeline.VADProcessor", side_effect=lambda **_kwargs: next(vad_instances)),
            patch(
                "audio_pipeline.SpeakerTracker",
                return_value=speaker_tracker
                or SimpleNamespace(identify=lambda _samples: ("speaker-1", "听众")),
            ),
        ):
            pipeline = AudioPipeline(
                config=config,
                logger=logging.getLogger("test-universalvc-stream"),
                on_result=on_result,
                on_mic_speech_start=on_mic_speech_start,
                on_mic_speech_end=on_mic_speech_end,
                stream_client=client,
            )
        pipeline.set_loop(asyncio.get_running_loop())
        self.addAsyncCleanup(pipeline.stop_streaming)
        return pipeline, results, result_event

    def _frame(self, pipeline, value=0.0, *, mic=False):
        frame = np.full(1600, value, dtype=np.float32)
        method = pipeline.process_mic_frame if mic else pipeline.process_frame
        method(frame, 16000, 1)

    async def test_stream_sends_400ms_preroll_ordered_pcm_and_attaches_receipt(self):
        vad = ScriptedVAD(
            [(False, [])] * 4
            + [(True, []), (True, []), (False, [segment()])]
        )
        client = FakeCoreStreamClient()
        pipeline, results, result_event = self._make_pipeline(vad, client)

        for _ in range(4):
            self._frame(pipeline, 0.1)
        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.0)

        await asyncio.wait_for(result_event.wait(), timeout=2)
        await asyncio.sleep(0)

        self.assertEqual(client.starts, [(16000, 1)])
        self.assertEqual(client.finishes, ["stream-1"])
        self.assertEqual(client.aborts, [])
        self.assertEqual([seq for _, seq, _ in client.chunks], list(range(5)))
        pcm = b"".join(chunk for _, _, chunk in client.chunks)
        self.assertEqual(len(pcm), 600 * 16 * 2)
        decoded = np.frombuffer(pcm, dtype="<i2")
        self.assertTrue(np.all(decoded[: 400 * 16] == 3277))
        self.assertTrue(np.all(decoded[400 * 16 :] == 16384))
        self.assertEqual(
            results,
            [
                (
                    "speaker-1",
                    "听众",
                    "full-wav-payload",
                    "receipt-1",
                    "recognized words",
                )
            ],
        )

    async def test_each_completed_segment_gets_its_own_stream_receipt_and_text(self):
        vad = ScriptedVAD(
            [(True, []), (False, [segment()]), (True, []), (False, [segment()])]
        )

        class DistinctFinishClient(FakeCoreStreamClient):
            async def finish_stream(self, stream_id):
                self.finishes.append(stream_id)
                return {
                    "text": f"recognized {stream_id}",
                    "result_id": f"receipt-{stream_id}",
                }

        client = DistinctFinishClient()
        pipeline, results, _result_event = self._make_pipeline(vad, client)

        for value in (0.5, 0.0, 0.5, 0.0):
            self._frame(pipeline, value)

        async def wait_for_two_results():
            deadline = asyncio.get_running_loop().time() + 2
            while len(results) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.005)
            self.assertEqual(len(results), 2)

        await wait_for_two_results()
        self.assertEqual(client.starts, [(16000, 1), (16000, 1)])
        self.assertEqual(client.finishes, ["stream-1", "stream-2"])
        self.assertEqual(
            [result[3:] for result in results],
            [
                ("receipt-stream-1", "recognized stream-1"),
                ("receipt-stream-2", "recognized stream-2"),
            ],
        )

    async def test_stream_api_failure_falls_back_to_full_wav_without_receipt(self):
        vad = ScriptedVAD([(True, []), (False, [segment()])])
        client = FakeCoreStreamClient(fail_start=True)
        pipeline, results, result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.5)
        await asyncio.wait_for(result_event.wait(), timeout=2)

        self.assertEqual(client.starts, [(16000, 1)])
        self.assertEqual(client.finishes, [])
        self.assertEqual(client.aborts, [])
        self.assertEqual(results[0][2:], ("full-wav-payload", None, None))

    async def test_empty_final_text_uses_full_wav_path_without_receipt(self):
        vad = ScriptedVAD([(True, []), (True, []), (False, [segment()])])
        client = FakeCoreStreamClient(finish_response={"text": "", "result_id": None})
        pipeline, results, result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.0)
        await asyncio.wait_for(result_event.wait(), timeout=2)

        self.assertEqual(client.finishes, ["stream-1"])
        self.assertEqual(client.aborts, [])
        self.assertEqual(results[0][2:], ("full-wav-payload", None, None))
        self.assertEqual(pipeline._stream_contexts, {})

    async def test_vad_end_without_segment_aborts_stream(self):
        vad = ScriptedVAD([(True, []), (False, [])])
        client = FakeCoreStreamClient()
        pipeline, results, _result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.0)
        await asyncio.sleep(0.02)

        self.assertEqual(client.finishes, [])
        self.assertEqual(client.aborts, ["stream-1"])
        self.assertEqual(results, [])
        self.assertEqual(pipeline._stream_contexts, {})

    async def test_chunk_failure_aborts_and_uses_full_wav_fallback(self):
        vad = ScriptedVAD([(True, []), (True, []), (False, [segment()])])
        client = FakeCoreStreamClient(fail_chunk=True)
        pipeline, results, result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.0)
        await asyncio.wait_for(result_event.wait(), timeout=2)

        self.assertEqual(client.aborts, ["stream-1"])
        self.assertEqual(client.finishes, [])
        self.assertEqual(results[0][2:], ("full-wav-payload", None, None))

    async def test_adapter_stream_shutdown_aborts_open_core_stream(self):
        vad = ScriptedVAD([(True, [])])
        client = FakeCoreStreamClient()
        pipeline, results, _result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        await asyncio.wait_for(client.started_event.wait(), timeout=2)
        await pipeline.stop_streaming()

        self.assertEqual(client.aborts, ["stream-1"])
        self.assertEqual(results, [])
        self.assertEqual(pipeline._stream_contexts, {})

    async def test_core_finish_progresses_while_speaker_identification_blocks(self):
        vad = ScriptedVAD([(True, []), (True, []), (False, [segment()])])

        class BlockingSpeakerTracker:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()

            def identify(self, _samples):
                self.started.set()
                if not self.release.wait(timeout=3):
                    raise TimeoutError("test did not release speaker identification")
                return "speaker-1", "听众"

        tracker = BlockingSpeakerTracker()
        self.addCleanup(tracker.release.set)
        client = FakeCoreStreamClient(finish_wait_for=tracker.started)
        pipeline, results, result_event = self._make_pipeline(
            vad, client, speaker_tracker=tracker
        )

        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.5)
        self._frame(pipeline, 0.0)
        await asyncio.wait_for(client.finish_complete_event.wait(), timeout=2)

        self.assertTrue(tracker.started.is_set())
        self.assertFalse(tracker.release.is_set())
        self.assertEqual(client.finishes, ["stream-1"])
        tracker.release.set()
        await asyncio.wait_for(result_event.wait(), timeout=2)
        self.assertEqual(results[0][3:], ("receipt-1", "recognized words"))

    async def test_microphone_keeps_owner_identity_and_pause_resume_callbacks(self):
        main_vad = ScriptedVAD([])
        mic_vad = ScriptedVAD([(True, []), (False, [segment()])])
        client = FakeCoreStreamClient()
        mic_started = asyncio.Event()
        mic_ended = asyncio.Event()

        async def mark_mic_started():
            mic_started.set()

        async def mark_mic_ended():
            mic_ended.set()

        pipeline, results, result_event = self._make_pipeline(
            [main_vad, mic_vad],
            client,
            microphone_enabled=True,
            on_mic_speech_start=mark_mic_started,
            on_mic_speech_end=mark_mic_ended,
        )

        self._frame(pipeline, 0.5, mic=True)
        self._frame(pipeline, 0.0, mic=True)
        await asyncio.wait_for(result_event.wait(), timeout=2)
        await asyncio.wait_for(mic_started.wait(), timeout=2)
        await asyncio.wait_for(mic_ended.wait(), timeout=2)

        self.assertEqual(results[0][:2], ("owner-1", "主人"))
        self.assertEqual(results[0][3:], ("receipt-1", "recognized words"))
        self.assertEqual(client.finishes, ["stream-1"])

    async def test_capture_queue_is_bounded_and_overflow_aborts_stream(self):
        start_gate = asyncio.Event()
        vad = ScriptedVAD([(True, [])] * 80 + [(False, [segment()])])
        client = FakeCoreStreamClient(start_gate=start_gate)
        pipeline, results, result_event = self._make_pipeline(vad, client)

        self._frame(pipeline, 0.5)
        for _ in range(20):
            if client.starts:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(client.starts, [(16000, 1)])

        def fill_capture_queue():
            for _ in range(79):
                self._frame(pipeline, 0.5)
            self._frame(pipeline, 0.0)

        await asyncio.to_thread(fill_capture_queue)
        self.assertLessEqual(
            pipeline._stream_queue.qsize(), pipeline.MAX_STREAM_QUEUE_EVENTS
        )
        self.assertTrue(pipeline._main_stream_state.active is None)

        await asyncio.wait_for(result_event.wait(), timeout=2)
        start_gate.set()
        await asyncio.sleep(0.02)

        self.assertEqual(results[0][2:], ("full-wav-payload", None, None))
        self.assertEqual(client.aborts, ["stream-1"])
        self.assertEqual(pipeline._stream_contexts, {})


if __name__ == "__main__":
    import unittest

    unittest.main()
