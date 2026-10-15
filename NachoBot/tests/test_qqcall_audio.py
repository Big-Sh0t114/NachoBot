from __future__ import annotations

import asyncio
import base64
import io
import math
import struct
import sys
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QQCALL_ROOT = PROJECT_ROOT.parent / "NachoBot-QQCall"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QQCALL_ROOT) not in sys.path:
    sys.path.insert(0, str(QQCALL_ROOT))

from qqcall_pcm import (
    AudioDeviceInfo,
    QQCallAudioBackend,
    QQCallAudioConfig,
    QQCallAudioDeviceScope,
    QQCallAudioError,
)
from qqcall_pcm.backend import _Pcm16Resampler
from qqcall_client.qqcall_universal_api import QQCallVoiceRuntime


CAPTURE_RENDER = "Incoming Mix (Virtual Audio Cable)"
CAPTURE_LOOPBACK = f"{CAPTURE_RENDER} [Loopback]"
PLAYBACK_RENDER = "Mic Feed (VB-Audio Virtual Cable)"


class FakeStream:
    def __init__(self, owner: FakePyAudio, kwargs: dict[str, object]):
        self.owner = owner
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        self.close_calls = 0
        self.close_entered = threading.Event()
        self.callback = kwargs.get("stream_callback")
        self.block_close: threading.Event | None = None
        self.invoke_callback_on_close = False
        self.last_callback_result: tuple[bytes | None, int] | tuple[None, int] | None = None
        self.frames_per_buffer = int(kwargs.get("frames_per_buffer", 960))
        self.channels = int(kwargs["channels"])
        self.input = bool(kwargs.get("input", False))
        self.output = bool(kwargs.get("output", False))

    def start_stream(self) -> None:
        self.owner.record_thread("start_stream")
        if self.owner.start_error is not None:
            error = self.owner.start_error
            self.owner.start_error = None
            raise error
        self.active = True

    def is_active(self) -> bool:
        self.owner.record_thread("is_active")
        return self.active

    def stop_stream(self) -> None:
        self.owner.record_thread("stop_stream")
        self.active = False

    def close(self) -> None:
        self.owner.record_thread("close")
        self.close_calls += 1
        self.close_entered.set()
        if self.block_close is not None:
            self.block_close.wait()
        if self.invoke_callback_on_close and self.active and callable(self.callback) and self.output:
            self.last_callback_result = self.callback(
                None, self.frames_per_buffer, {}, 0
            )
        self.active = False
        self.closed = True

    def feed_input(self, pcm: bytes, status_flags: int = 0) -> int:
        if not self.active or not callable(self.callback) or not self.input:
            raise AssertionError("input stream is not active")
        _data, flag = self.callback(pcm, len(pcm) // (self.channels * 2), {}, status_flags)
        return int(flag)

    def pump_output(self, frame_count: int) -> tuple[bytes, int]:
        if not self.active or not callable(self.callback) or not self.output:
            raise AssertionError("output stream is not active")
        data, flag = self.callback(None, frame_count, {}, 0)
        self.last_callback_result = (data, flag)
        if flag in (FakeModule.paAbort, FakeModule.paComplete):
            self.active = False
        return data, flag


class FakePyAudio:
    DEVICES = (
        {
            "index": 0, "name": CAPTURE_LOOPBACK, "hostApi": 0, "maxInputChannels": 2,
            "maxOutputChannels": 0, "isLoopbackDevice": True, "defaultSampleRate": 48_000.0,
        },
        {
            "index": 1, "name": CAPTURE_RENDER, "hostApi": 0, "maxInputChannels": 0,
            "maxOutputChannels": 2, "isLoopbackDevice": False, "defaultSampleRate": 48_000.0,
        },
        {
            "index": 2, "name": PLAYBACK_RENDER, "hostApi": 0, "maxInputChannels": 0,
            "maxOutputChannels": 2, "isLoopbackDevice": False, "defaultSampleRate": 48_000.0,
        },
        {
            "index": 3, "name": "Built-in Microphone", "hostApi": 0, "maxInputChannels": 2,
            "maxOutputChannels": 0, "isLoopbackDevice": False, "defaultSampleRate": 48_000.0,
        },
    )

    def __init__(self, module: FakeModule):
        self.module = module
        self.streams: list[FakeStream] = []
        self.threads: list[tuple[str, int]] = []
        self.terminate_calls = 0
        self.open_calls: list[dict[str, object]] = []
        self.block_capture_open: threading.Event | None = None
        self.open_entered = threading.Event()
        self.in_open = False
        self.concurrent_terminate = False
        self.start_error: OSError | None = None

    def record_thread(self, operation: str) -> None:
        self.threads.append((operation, threading.get_ident()))

    def get_device_count(self) -> int:
        self.record_thread("get_device_count")
        return len(self.DEVICES)

    def get_device_info_by_index(self, index: int) -> dict[str, object]:
        self.record_thread("get_device_info_by_index")
        return self.DEVICES[index]

    def get_host_api_info_by_index(self, index: int) -> dict[str, object]:
        self.record_thread("get_host_api_info_by_index")
        if index != 0:
            raise IndexError(index)
        return {"name": "Windows WASAPI"}

    def open(self, **kwargs: object) -> FakeStream:
        self.record_thread("open")
        self.open_calls.append(kwargs)
        is_capture = bool(kwargs.get("input"))
        self.in_open = True
        self.open_entered.set()
        try:
            if is_capture and self.block_capture_open is not None:
                self.block_capture_open.wait()
            stream = FakeStream(self, kwargs)
            self.streams.append(stream)
            return stream
        finally:
            self.in_open = False

    def terminate(self) -> None:
        self.record_thread("terminate")
        if self.in_open:
            self.concurrent_terminate = True
            raise AssertionError("terminate raced an in-flight open")
        self.terminate_calls += 1

    def get_default_input_device_info(self) -> None:
        raise AssertionError("default input device must never be consulted")

    def get_default_output_device_info(self) -> None:
        raise AssertionError("default output device must never be consulted")


class FakeModule:
    paInt16 = 8
    paContinue = 0
    paComplete = 1
    paAbort = 2
    paInputOverflow = 2

    def __init__(self):
        self.instances: list[FakePyAudio] = []
        self.threads: list[tuple[str, int]] = []

    def PyAudio(self) -> FakePyAudio:
        self.threads.append(("PyAudio", threading.get_ident()))
        instance = FakePyAudio(self)
        self.instances.append(instance)
        return instance


def enabled_config(**overrides: object) -> QQCallAudioConfig:
    values: dict[str, object] = {
        "enabled": True,
        "capture_device": CAPTURE_LOOPBACK,
        "capture_mode": "loopback",
        "playback_device": PLAYBACK_RENDER,
        "device_scope": QQCallAudioDeviceScope(CAPTURE_RENDER, PLAYBACK_RENDER),
    }
    values.update(overrides)
    return QQCallAudioConfig(**values)


async def wait_until(predicate: object, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        await asyncio.sleep(0.005)
    return False


def pcm_stereo_silence(frames: int) -> bytes:
    return bytes(frames * 2 * 2)


class ConfigAndResamplerTests(unittest.TestCase):
    def test_disabled_config_is_lazy_and_mapping_is_strict(self) -> None:
        config = QQCallAudioConfig.from_mapping({"enabled": False})
        backend = QQCallAudioBackend(config)
        self.assertFalse(backend.status["ready"])
        self.assertEqual(backend.status["state"], "disabled")
        with self.assertRaises(QQCallAudioError):
            QQCallAudioConfig.from_mapping({"enabled": False, "unknown": True})

    def test_mapping_accepts_nested_scope_and_rejects_insecure_routes(self) -> None:
        config = QQCallAudioConfig.from_mapping(
            {
                "enabled": True,
                "capture_device": CAPTURE_LOOPBACK,
                "playback_device": PLAYBACK_RENDER,
                "device_scope": {
                    "capture_render_name": CAPTURE_RENDER,
                    "playback_render_name": PLAYBACK_RENDER,
                },
            }
        )
        self.assertEqual(config.device_scope.capture_render_name, CAPTURE_RENDER)
        with self.assertRaises(QQCallAudioError):
            enabled_config(capture_device="Built-in Microphone [Loopback]")
        with self.assertRaises(QQCallAudioError):
            enabled_config(device_scope=QQCallAudioDeviceScope(PLAYBACK_RENDER, PLAYBACK_RENDER))

    def test_chunked_resampling_matches_whole_buffer_at_multiple_native_rates(self) -> None:
        for source_rate, target_rate, count in ((48_000, 16_000, 4_800), (44_100, 48_000, 4_410)):
            samples = [round(7_000 * math.sin(index * 0.073)) for index in range(count)]
            pcm = struct.pack(f"<{count}h", *samples)
            whole = _Pcm16Resampler(source_rate, target_rate)
            expected = whole.process(pcm) + whole.flush()
            split = _Pcm16Resampler(source_rate, target_rate)
            split_index = count // 3
            actual = split.process(pcm[: split_index * 2])
            actual += split.process(pcm[split_index * 2 : count * 2])
            actual += split.flush()
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual) // 2, math.ceil(count * target_rate / source_rate))

    def test_downsampling_preserves_speech_band_and_rejects_above_nyquist_tones(self) -> None:
        for source_rate, target_rate, rejected_tone in ((48_000,16_000,12_000), (44_100,16_000,12_000),
                                                       (96_000,16_000,20_000), (96_000,48_000,36_000)):
            with self.subTest(source_rate=source_rate, target_rate=target_rate):
                rms_values = []
                for frequency in (1_000,rejected_tone):
                    count=source_rate//10
                    samples=[round(10_000*math.sin(2*math.pi*frequency*i/source_rate)) for i in range(count)]
                    sampler=_Pcm16Resampler(source_rate,target_rate)
                    pcm=struct.pack(f'<{count}h',*samples)
                    result=sampler.process(pcm)+sampler.flush()
                    values=struct.unpack(f'<{len(result)//2}h',result)[100:-100]
                    rms_values.append(math.sqrt(sum(value*value for value in values)/len(values)))
                    self.assertEqual(len(result)//2,math.ceil(count*target_rate/source_rate))
                self.assertAlmostEqual(rms_values[0]/(10_000/math.sqrt(2)),1,delta=0.02)
                self.assertLess(rms_values[1]/rms_values[0],0.01,'out-of-band energy must be suppressed by at least 40 dB')

    def test_filtered_downsampling_is_independent_of_tiny_chunks_and_flushes_short_inputs(self) -> None:
        for source_rate,count in ((44_100,2205),(48_000,1),(48_000,2),(48_000,32),(96_000,120)):
            with self.subTest(source_rate=source_rate,count=count):
                pcm=struct.pack(f'<{count}h',*[round(7_000*math.sin(i*0.37)) for i in range(count)])
                whole=_Pcm16Resampler(source_rate,16_000)
                expected=whole.process(pcm)+whole.flush()
                split=_Pcm16Resampler(source_rate,16_000)
                actual=b''.join(split.process(pcm[i:i+2]) for i in range(0,len(pcm),2))+split.flush()
                self.assertEqual(actual,expected)
                self.assertEqual(len(actual)//2,math.ceil(count*16_000/source_rate))
                self.assertEqual(split.flush(),b'')
                with self.assertRaises(QQCallAudioError): split.process(b'\0\0')

    def test_wav_decoder_accepts_pcm16_and_rejects_compressed_payload(self) -> None:
        module = FakeModule()
        backend = QQCallAudioBackend(enabled_config(), audio_module=module)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48_000)
            wav.writeframes(struct.pack("<4h", 1, 2, 3, 4))
        pcm, rate, channels = backend._decode_wav(base64.b64encode(buffer.getvalue()).decode("ascii"))
        self.assertEqual((pcm, rate, channels), (struct.pack("<4h", 1, 2, 3, 4), 48_000, 1))
        with self.assertRaises(QQCallAudioError):
            backend._decode_wav("not-base64")


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = FakeModule()
        self.config = enabled_config(cancel_timeout_s=0.08, drain_timeout_s=0.5)
        self.backend = QQCallAudioBackend(self.config, audio_module=self.module)
        self.captured: list[tuple[bytes, dict[str, object]]] = []
        self.failures: list[str] = []
        await self.backend.start(
            lambda pcm, metadata: self.captured.append((pcm, metadata)),
            self.failures.append,
            {"call": "opaque-scope"},
        )
        self.audio = self.module.instances[0]

    async def asyncTearDown(self) -> None:
        if self.backend.status["state"] not in ("stopped", "disabled"):
            await self.backend.stop()
        if self.backend._native_executor is not None:
            self.backend._shutdown_native_executor()

    def _runtime_with_backend(self, turn_id: str) -> tuple[QQCallVoiceRuntime, dict[str, object], str]:
        scope = {
            "serviceInstance": "service-test",
            "hostGeneration": 1,
            "connectionGeneration": 2,
            "callId": "call-test",
            "callGeneration": 3,
        }
        call = {
            "scope": scope,
            "peer_user_id": "12345",
            "peer_uid": "u_abcdefghijklmnopqrstuv",
            "phase": "connected",
            "uncertain": False,
            "inviteCallbackReady": True,
            "inviteCorrelation": "kernel_from_uid_matched",
        }

        async def send_message(_message):
            return True

        runtime = QQCallVoiceRuntime(
            platform="qq",
            send_message=send_message,
            admit_peer=lambda _peer: True,
            identity_is_current=lambda _identity: True,
            universal_api={"url": "http://127.0.0.1:8016", "token_file": "injected"},
            backend=self.backend,
        )
        runtime.update_call(call, {"identity": (1, 2, "99999")})
        desired = runtime._desired
        self.assertIsNotNone(desired)
        runtime._loop = asyncio.get_running_loop()
        runtime._active_scope_key = desired.scope_key
        runtime._route_verified_at = time.monotonic()
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._set_turn_sync(desired.scope_key, turn_id)
        return runtime, scope, call["peer_user_id"]

    async def _queue_runtime_playback(self, runtime, scope, peer: str, turn_id: str, stream_id: str):
        fence = lambda: runtime.fence(scope, peer, turn_id)
        await self.backend.stream_start(
            stream_id,
            {"codec": "pcm_s16le", "sample_rate": 48_000, "channels": 1, "sample_width": 2},
            fence,
        )
        session = self.backend._session
        self.assertIsNotNone(session)
        for sequence in range(4):
            await self.backend.stream_chunk(
                stream_id, sequence, struct.pack("<960h", *([400] * 960)),
            )
            if not session.output_queue.empty():
                break
        self.assertFalse(session.output_queue.empty())
        self.assertTrue(any(session.output_queue.queue[0]), "the callback must have real queued PCM to reject")
        playback = next(stream for stream in reversed(self.audio.streams) if stream.output)
        return session, playback

    async def test_capture_is_canonical_scoped_and_continues_during_playback(self) -> None:
        capture = next(stream for stream in self.audio.streams if stream.input)
        self.assertTrue(capture.active)
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: bool(self.captured)))
        pcm, metadata = self.captured[-1]
        self.assertEqual(metadata["codec"], "pcm_s16le")
        self.assertEqual(metadata["sample_rate"], 16_000)
        self.assertEqual(metadata["channels"], 1)
        self.assertEqual(metadata["scope"], {"call": "opaque-scope"})
        self.assertEqual(metadata["sample_count"], len(pcm) // 2)

        await self.backend.stream_start(
            "tts-1",
            {"codec": "pcm_s16le", "sample_rate": 48_000, "channels": 1, "sample_width": 2},
            lambda: True,
        )
        playback = next(stream for stream in self.audio.streams if stream.output)
        self.assertEqual(playback.kwargs["rate"], 48_000)
        self.assertEqual(playback.kwargs["channels"], 2)
        await self.backend.stream_chunk("tts-1", 0, struct.pack("<960h", *([300] * 960)))
        before = len(self.captured)
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: len(self.captured) > before))
        self.assertTrue(capture.active)
        self.assertTrue(self.backend.status["ready"])

    async def test_public_session_uplink_is_paced_and_checked_inside_pcm_package(self) -> None:
        await self.backend.stop()
        sent: list[tuple[bytes, str]] = []
        failures: list[str] = []

        async def send_pcm(pcm: bytes, source_id: str = "main"):
            sent.append((pcm, source_id))
            return {"accepted": True}

        await self.backend.start(
            None, failures.append, "scope", send_pcm=send_pcm, capture_fence=lambda: True,
        )
        self.audio = self.module.instances[-1]
        capture = next(stream for stream in self.audio.streams if stream.input)
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: bool(sent)))
        first_pcm, source_id = sent[0]
        self.assertEqual(source_id, "main")
        self.assertEqual(first_pcm, bytes(len(first_pcm)))
        self.assertTrue(576 <= len(first_pcm) <= 640, "initial filter lookahead must stay within 2 ms")
        self.assertEqual(self.backend.status["uplink_frames_sent"], 1)
        self.assertEqual(failures, [])

    async def test_diagnostic_snapshot_failure_cannot_change_pcm_receipt_or_open_another_endpoint(self) -> None:
        await self.backend.stop()
        sent, failures = [], []

        async def send_pcm(pcm, source_id="main"):
            sent.append((pcm, source_id))
            return {"accepted": True}

        class BrokenSnapshot:
            def observe(self, *_args):
                raise OSError("synthetic diagnostic write failure")
            def close(self):
                raise OSError("synthetic diagnostic close failure")

        self.backend._diagnostic_snapshot = BrokenSnapshot()
        await self.backend.start(None, failures.append, "scope", send_pcm=send_pcm, capture_fence=lambda: True)
        self.audio = self.module.instances[-1]
        capture = next(stream for stream in self.audio.streams if stream.input)
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: self.backend.status["uplink_frames_sent"] == 1))
        self.assertEqual(len(sent), 1)
        first_pcm, source_id = sent[0]
        self.assertEqual(source_id, "main")
        self.assertEqual(first_pcm, bytes(len(first_pcm)))
        self.assertTrue(576 <= len(first_pcm) <= 640, "snapshot failure cannot change bounded capture startup")
        self.assertEqual(failures, [])
        self.assertEqual(len(self.audio.streams), 1)
        await self.backend.stop()
        self.assertEqual(self.backend.status["state"], "stopped")

    async def test_public_session_uplink_rejects_sample_gap_without_forwarding_later_pcm(self) -> None:
        await self.backend.stop()
        self.backend = QQCallAudioBackend(enabled_config(), audio_module=self.module)
        sent: list[bytes] = []
        failures: list[str] = []

        async def send_pcm(pcm: bytes, source_id: str = "main"):
            self.assertEqual(source_id, "main")
            sent.append(pcm)
            return {"accepted": True}

        await self.backend.start(None, failures.append, "scope", send_pcm=send_pcm, capture_fence=lambda: True)
        self.audio = self.module.instances[-1]
        capture = next(stream for stream in self.audio.streams if stream.input)
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: len(sent) == 1))
        self.assertEqual(self.backend.status["uplink_frames_sent"], 1)

        # Simulate a sample discontinuity from the capture callback's metadata.
        self.backend._capture_sample_counter += 1
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: "universal_api_capture_frame_invalid" in failures))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.backend.status["uplink_frames_sent"], 1)

    async def test_public_session_uplink_overflow_and_route_fence_fail_closed(self) -> None:
        await self.backend.stop()
        gate = asyncio.Event()
        entered = asyncio.Event()
        failures: list[str] = []
        sent: list[bytes] = []

        async def send_pcm(pcm: bytes, source_id: str = "main"):
            self.assertEqual(source_id, "main")
            sent.append(pcm)
            entered.set()
            await gate.wait()
            return {"accepted": True}

        self.backend = QQCallAudioBackend(
            enabled_config(capture_queue_ms=40, frame_ms=20, cancel_timeout_s=0.08),
            audio_module=self.module,
        )
        fence = [True]
        await self.backend.start(None, failures.append, "scope", send_pcm=send_pcm, capture_fence=lambda: fence[0])
        self.audio = self.module.instances[-1]
        capture = next(stream for stream in self.audio.streams if stream.input)
        capture.feed_input(pcm_stereo_silence(960))
        await asyncio.wait_for(entered.wait(), 1)

        # The sender is blocked; two frames fill the bounded queue and the next
        # callback must fail the route instead of dropping or reordering PCM.
        capture.feed_input(pcm_stereo_silence(960))
        capture.feed_input(pcm_stereo_silence(960))
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: "capture_queue_overrun" in failures))
        stopping = asyncio.create_task(self.backend.stop())
        await asyncio.sleep(0.02)
        self.assertFalse(stopping.done(), "stop must wait for the in-flight bounded PCM send to retire")
        gate.set()
        await asyncio.wait_for(stopping, 1)
        self.assertEqual(self.backend.status["uplink_frames_sent"], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.backend.status["state"], "stopped")

        self.backend = QQCallAudioBackend(enabled_config(), audio_module=self.module)
        failures.clear()
        sent.clear()
        fence[0] = True

        async def immediate_send(pcm: bytes, source_id: str = "main"):
            self.assertEqual(source_id, "main")
            sent.append(pcm)
            return {"accepted": True}

        await self.backend.start(None, failures.append, "scope", send_pcm=immediate_send, capture_fence=lambda: fence[0])
        self.audio = self.module.instances[-1]
        capture = next(stream for stream in self.audio.streams if stream.input)
        fence[0] = False
        capture.feed_input(pcm_stereo_silence(960))
        self.assertTrue(await wait_until(lambda: "audio_scope_stale" in failures))
        self.assertEqual(sent, [])
        await asyncio.wait_for(self.backend.stop(), timeout=1)
        self.assertEqual(self.backend.status["state"], "stopped")

    async def test_core_voice_stream_driver_reports_only_real_local_drain(self) -> None:
        fence_current = True
        base = {
            "stream_id": "stream-a", "parent_message_id": "message-a",
            "sample_rate": 16_000, "channels": 1, "sample_width": 2, "codec": "pcm_s16le",
        }
        started = await self.backend.play_core_segment(
            "voice_stream", {"event": "start", **base}, lambda: fence_current,
        )
        self.assertFalse(started["drain_confirmed"])
        encoded = base64.b64encode(struct.pack("<320h", *([10] * 320))).decode("ascii")
        chunk = await self.backend.play_core_segment(
            "voice_stream", {"event": "chunk", **base, "seq": 0, "audio_base64": encoded}, lambda: fence_current,
        )
        self.assertFalse(chunk["drain_confirmed"])
        ending = asyncio.create_task(self.backend.play_core_segment(
            "voice_stream", {"event": "end", **base}, lambda: fence_current,
        ))
        self.assertTrue(await wait_until(lambda: self.backend._session is not None and self.backend._session.producer_eof))
        playback = next(stream for stream in self.audio.streams if stream.output)
        _data, flag = playback.pump_output(960)
        self.assertEqual(flag, FakeModule.paContinue)
        _data, flag = playback.pump_output(960)
        self.assertEqual(flag, FakeModule.paComplete)
        receipt = await asyncio.wait_for(ending, 1)
        self.assertTrue(receipt["render_drained"])
        self.assertTrue(receipt["drain_confirmed"])
        self.assertFalse(receipt["remote_playback_confirmed"])

    async def test_end_waits_for_complete_callback_and_inactive_stream(self) -> None:
        await self.backend.stream_start(
            "tts-drain",
            {"codec": "pcm_s16le", "sample_rate": 48_000, "channels": 1},
            lambda: True,
        )
        playback = next(stream for stream in self.audio.streams if stream.output)
        await self.backend.stream_chunk("tts-drain", 0, struct.pack("<960h", *([200] * 960)))
        ending = asyncio.create_task(self.backend.stream_end("tts-drain"))
        await asyncio.sleep(0.02)
        self.assertFalse(ending.done(), "producer EOF alone must not be reported as a drain")
        _data, first_flag = playback.pump_output(960)
        self.assertEqual(first_flag, FakeModule.paContinue)
        await asyncio.sleep(0.02)
        self.assertFalse(ending.done(), "the callback still needs to return paComplete and become inactive")
        _data, last_flag = playback.pump_output(960)
        self.assertEqual(last_flag, FakeModule.paComplete)
        receipt = await asyncio.wait_for(ending, timeout=0.5)
        self.assertTrue(receipt["render_drained"])
        self.assertTrue(receipt["drain_confirmed"])
        self.assertFalse(receipt["remote_playback_confirmed"])
        self.assertFalse(playback.active)

    async def test_underrun_is_silence_and_stale_fence_aborts_only_playback(self) -> None:
        fence_current = True
        await self.backend.stream_start(
            "tts-fenced",
            {"codec": "pcm_s16le", "sample_rate": 48_000, "channels": 1},
            lambda: fence_current,
        )
        playback = next(stream for stream in self.audio.streams if stream.output)
        session = self.backend._session
        self.assertIsNotNone(session)
        silence, flag = playback.pump_output(960)
        self.assertEqual(flag, FakeModule.paContinue)
        self.assertEqual(silence, bytes(len(silence)))
        self.assertGreater(self.backend.status["playback_underrun_frames"], 0)
        fence_current = False
        with self.assertRaises(QQCallAudioError) as raised:
            await self.backend.stream_chunk("tts-fenced", 0, struct.pack("<960h", *([1] * 960)))
        self.assertEqual(raised.exception.code, "audio_scope_stale")
        self.assertTrue(playback.closed)
        self.assertTrue(session.output_queue.empty())
        capture = next(stream for stream in self.audio.streams if stream.input)
        self.assertTrue(capture.active)

    async def test_output_callback_fences_route_and_turn_before_render(self) -> None:
        runtime, scope, peer = self._runtime_with_backend("turn-a")
        session, playback = await self._queue_runtime_playback(runtime, scope, peer, "turn-a", "turn-a-output")

        runtime._revoke_route_sync("universal_api_route_unverified")
        audio, flag = playback.pump_output(960)
        self.assertEqual(audio, bytes(len(audio)))
        self.assertEqual(flag, FakeModule.paAbort)
        self.assertTrue(session.cancelled)
        self.assertTrue(session.completed.is_set())
        self.assertTrue(session.output_queue.empty())
        with self.assertRaises(QQCallAudioError):
            await self.backend.stream_end("turn-a-output")
        self.assertFalse(self.backend.status["playback_active"])
        self.assertTrue(await wait_until(lambda: session.output_stream.closed and self.backend._session is None))

        # Start a fresh generation and retire it by changing the scoped turn.
        # The PortAudio callback itself must fence before consuming its queued PCM.
        runtime._route_failed = False
        runtime._ready = True
        runtime._capture_enabled = True
        runtime._route_verified_at = time.monotonic()
        runtime._active_scope_key = runtime._desired.scope_key
        runtime._set_turn_sync(runtime._desired.scope_key, "turn-b")
        old_session, old_playback = await self._queue_runtime_playback(
            runtime, scope, peer, "turn-b", "turn-b-output",
        )
        runtime._set_turn_sync(runtime._desired.scope_key, "turn-c")
        runtime._cancel_old_playback()
        audio, flag = old_playback.pump_output(960)
        self.assertEqual(audio, bytes(len(audio)))
        self.assertEqual(flag, FakeModule.paAbort)
        self.assertTrue(old_session.cancelled)
        self.assertTrue(old_session.output_queue.empty())
        with self.assertRaises(QQCallAudioError):
            await self.backend.stream_end("turn-b-output")
        self.assertTrue(await wait_until(lambda: old_session.output_stream.closed and self.backend._session is None))
        await runtime.stop()

    async def test_disconnect_aborts_native_playback_before_remote_interrupt(self) -> None:
        runtime, scope, peer = self._runtime_with_backend("turn-disconnect")
        session, playback = await self._queue_runtime_playback(
            runtime, scope, peer, "turn-disconnect", "disconnect-output",
        )

        class SlowRemoteSession:
            def __init__(self):
                self.interrupt_entered = asyncio.Event()
                self.interrupt_release = asyncio.Event()

            async def interrupt(self):
                self.interrupt_entered.set()
                await self.interrupt_release.wait()

            async def close(self):
                return None

        remote = SlowRemoteSession()
        runtime._session = remote
        close_release = threading.Event()
        playback.block_close = close_release
        stopping = asyncio.create_task(runtime._stop_resources(interrupt=True))
        try:
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(playback.close_entered.wait, 0.5), 0.75))
            self.assertFalse(remote.interrupt_entered.is_set(), "local output cancellation starts before remote interrupt")
            # The disconnect fence is already revoked while native close is in
            # flight. Pump the actual fake PortAudio callback and prove it emits
            # silence/paAbort instead of the queued Core reply.
            audio, flag = playback.pump_output(960)
            self.assertEqual(audio, bytes(len(audio)))
            self.assertEqual(flag, FakeModule.paAbort)
            self.assertTrue(session.cancelled)
            self.assertTrue(session.output_queue.empty())
            close_release.set()
            await asyncio.wait_for(remote.interrupt_entered.wait(), 1)
            self.assertFalse(stopping.done(), "the deliberately slow remote interrupt is still pending")
            remote.interrupt_release.set()
            await asyncio.wait_for(stopping, 2)
        finally:
            close_release.set()
            remote.interrupt_release.set()
            await asyncio.gather(stopping, return_exceptions=True)
        self.assertTrue(playback.closed)
        self.assertEqual(self.backend.status["state"], "stopped")

    async def test_strict_sequence_and_format_validation_close_bad_playback(self) -> None:
        await self.backend.stream_start(
            "tts-seq",
            {"codec": "pcm_s16le", "sample_rate": 16_000, "channels": 1},
            lambda: True,
        )
        with self.assertRaises(QQCallAudioError) as raised:
            await self.backend.stream_chunk("tts-seq", True, struct.pack("<320h", *([1] * 320)))
        self.assertEqual(raised.exception.code, "audio_sequence_invalid")
        self.assertFalse(self.backend.status["playback_active"])
        with self.assertRaises(QQCallAudioError):
            await self.backend.stream_start(
                "bad-format",
                {"codec": "float32", "sample_rate": 16_000, "channels": 1},
                lambda: True,
            )

    async def test_abort_purges_queue_and_uses_callback_abort_then_close(self) -> None:
        await self.backend.stream_start(
            "tts-cancel",
            {"codec": "pcm_s16le", "sample_rate": 48_000, "channels": 1},
            lambda: True,
        )
        playback = next(stream for stream in self.audio.streams if stream.output)
        playback.invoke_callback_on_close = True
        await self.backend.stream_chunk("tts-cancel", 0, struct.pack("<960h", *([500] * 960)))
        await self.backend.cancel_playback()
        self.assertTrue(playback.closed)
        self.assertEqual(playback.last_callback_result[1], FakeModule.paAbort)
        self.assertFalse(hasattr(playback, "abort_stream"))
        self.assertFalse(self.backend.status["playback_active"])
        self.assertTrue(next(stream for stream in self.audio.streams if stream.input).active)

    async def test_stop_is_idempotent_and_all_native_calls_share_owned_thread(self) -> None:
        await self.backend.stop()
        first_terminations = self.audio.terminate_calls
        await self.backend.stop()
        self.assertEqual(self.audio.terminate_calls, first_terminations)
        native_threads = {
            thread_id for name, thread_id in self.audio.threads
            if name in {"get_device_count", "get_device_info_by_index", "get_host_api_info_by_index",
                        "open", "start_stream", "is_active", "stop_stream", "close", "terminate"}
        }
        native_threads.update(thread_id for name, thread_id in self.module.threads if name == "PyAudio")
        self.assertEqual(len(native_threads), 1)

    async def test_active_enumeration_reuses_owned_native_context_and_thread(self) -> None:
        before = len(self.audio.threads)
        devices = await self.backend.enumerate_devices()
        self.assertTrue(devices)
        self.assertEqual(len(self.module.instances), 1)
        operations = self.audio.threads[before:]
        self.assertTrue(operations)
        self.assertEqual({thread_id for _name, thread_id in operations}, {self.audio.threads[0][1]})

    async def test_blocked_close_is_quarantined_without_concurrent_terminate(self) -> None:
        capture = next(stream for stream in self.audio.streams if stream.input)
        release = threading.Event()
        capture.block_close = release
        stopping = asyncio.create_task(self.backend.stop())
        await asyncio.sleep(0.02)
        self.assertTrue(await wait_until(lambda: self.backend.status["state"] == "cleanup_pending", 0.4))
        self.assertFalse(self.backend.status["ready"])
        self.assertTrue(self.backend.status["cleanup_unconfirmed"])
        self.assertEqual(self.audio.terminate_calls, 0)
        self.assertFalse(self.audio.concurrent_terminate)
        await stopping
        release.set()
        self.assertTrue(await wait_until(lambda: self.backend.status["state"] == "stopped", 0.5))
        self.assertEqual(self.audio.terminate_calls, 1)
        self.assertFalse(self.audio.concurrent_terminate)


class EnumerationAndQuarantineTests(unittest.IsolatedAsyncioTestCase):
    async def test_enumeration_is_metadata_only_and_never_reads_defaults(self) -> None:
        module = FakeModule()
        backend = QQCallAudioBackend(QQCallAudioConfig(), audio_module=module)
        devices = await backend.enumerate_devices()
        self.assertTrue(all(isinstance(item, AudioDeviceInfo) for item in devices))
        self.assertEqual(len(devices), 4)
        self.assertEqual(module.instances[0].streams, [])
        self.assertEqual(module.instances[0].terminate_calls, 1)
        self.assertIsNone(backend.status["native_errno"])

    async def test_native_error_exposes_only_bounded_numeric_code(self) -> None:
        module = FakeModule()
        audio = module.PyAudio()
        audio.start_error = OSError(-9999, "private device path and diagnostic")
        module.PyAudio = lambda: audio
        backend = QQCallAudioBackend(enabled_config(), audio_module=module)
        with self.assertRaises(QQCallAudioError) as raised:
            await backend.start(lambda _pcm, _meta: None, lambda _code: None, {"scope": "test"})
        self.assertEqual(raised.exception.code, "audio_start_failed")
        self.assertEqual(backend.status["last_error"], "audio_start_failed")
        self.assertEqual(backend.status["native_errno"], -9999)
        self.assertNotIn("private device path", repr(backend.status))
        self.assertEqual(audio.terminate_calls, 1)

    async def test_blocked_open_keeps_loop_responsive_and_disposes_late_stream(self) -> None:
        module = FakeModule()
        audio = module.PyAudio()
        release = threading.Event()
        audio.block_capture_open = release
        module.PyAudio = lambda: audio
        backend = QQCallAudioBackend(enabled_config(cancel_timeout_s=0.06), audio_module=module)
        starting: asyncio.Task[None] | None = None
        heartbeat: asyncio.Task[None] | None = None
        loop_responsive = asyncio.Event()
        try:
            # Keep a generous blocked window so the rendezvous is independent of
            # suite load; the native call still reaches its real bounded timeout.
            with patch("qqcall_pcm.backend._NATIVE_OPERATION_TIMEOUT_S", 0.5):
                starting = asyncio.create_task(
                    backend.start(lambda _pcm, _meta: None, lambda _code: None, {"scope": "test"})
                )
                self.assertTrue(await asyncio.to_thread(audio.open_entered.wait, 0.4))

                async def event_loop_heartbeat() -> None:
                    await asyncio.sleep(0)
                    loop_responsive.set()

                heartbeat = asyncio.create_task(event_loop_heartbeat())
                await asyncio.wait_for(loop_responsive.wait(), timeout=0.2)
                self.assertFalse(release.is_set(), "the fake native open is still blocked")
                self.assertFalse(starting.done(), "the native operation has not timed out yet")
                with self.assertRaises(QQCallAudioError) as raised:
                    await asyncio.wait_for(starting, timeout=0.7)
                self.assertEqual(raised.exception.code, "audio_start_timeout")
                await heartbeat
                self.assertEqual(backend.status["state"], "cleanup_pending")
                self.assertTrue(backend.status["cleanup_unconfirmed"])
                self.assertEqual(audio.terminate_calls, 0)
        finally:
            # Always release the native fake, including when an assertion fails,
            # so the executor thread and late-cleanup callback cannot leak.
            release.set()
            if starting is not None:
                await asyncio.gather(starting, return_exceptions=True)
            if heartbeat is not None:
                await asyncio.gather(heartbeat, return_exceptions=True)

        self.assertTrue(await wait_until(lambda: backend.status["state"] == "stopped", 0.6))
        self.assertEqual(audio.terminate_calls, 1)
        self.assertEqual(len(audio.streams), 1)
        self.assertTrue(audio.streams[0].closed)
        self.assertFalse(audio.concurrent_terminate)

    async def test_cancelled_start_retains_native_open_until_late_cleanup(self) -> None:
        module = FakeModule()
        audio = module.PyAudio()
        release = threading.Event()
        audio.block_capture_open = release
        module.PyAudio = lambda: audio
        backend = QQCallAudioBackend(enabled_config(cancel_timeout_s=0.5), audio_module=module)
        starting = asyncio.create_task(
            backend.start(lambda _pcm, _meta: None, lambda _code: None, {"scope": "cancel-test"})
        )
        stopping: asyncio.Task[None] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(audio.open_entered.wait, 0.5))
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starting
            self.assertEqual(backend.status["state"], "cleanup_pending")

            stopping = asyncio.create_task(backend.stop())
            await asyncio.sleep(0.02)
            self.assertFalse(stopping.done(), "stop must retain ownership while native open is still running")
            self.assertNotEqual(backend.status["state"], "stopped")
            self.assertEqual(audio.terminate_calls, 0)
            release.set()
            await asyncio.wait_for(stopping, 1)
            self.assertEqual(backend.status["state"], "stopped")
            self.assertEqual(audio.terminate_calls, 1)
            self.assertEqual(len(audio.streams), 1)
            self.assertTrue(audio.streams[0].closed)
            self.assertFalse(audio.concurrent_terminate)
        finally:
            release.set()
            await asyncio.gather(starting, return_exceptions=True)
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            if backend._cleanup_task is not None:
                await asyncio.gather(backend._cleanup_task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
