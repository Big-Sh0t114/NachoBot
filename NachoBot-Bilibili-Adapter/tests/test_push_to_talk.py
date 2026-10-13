from __future__ import annotations

import asyncio
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot"))

import bili_src.audio.mic_capture as mic_capture_module  # noqa: E402
from bili_src.audio.mic_capture import MicCaptureWorker, MicConfig, PTTKeyMonitor  # noqa: E402


class _Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def __getattr__(self, name):
        def capture(message, *args, **kwargs):
            del kwargs
            try:
                rendered = str(message).format(*args)
            except (IndexError, KeyError, ValueError):
                rendered = " ".join([str(message), *(str(arg) for arg in args)])
            self.records.append((name, rendered))

        return capture


class _PCMBuffer:
    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm

    def tobytes(self) -> bytes:
        return self.pcm


class _Listener:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class _MonitorStub:
    def __init__(self, held: bool) -> None:
        self.held = held
        self.started = False
        self.stopped = False

    @property
    def is_held(self) -> bool:
        return self.held

    @property
    def listener_alive(self) -> bool:
        return True

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeSender:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.finish_count = 0
        self.abort_count = 0

    def enqueue_chunk(self, pcm: bytes) -> bool:
        self.chunks.append(bytes(pcm))
        return True

    async def finish(self):
        self.finish_count += 1
        return {"text": "recognized", "result_id": "receipt-ptt"}

    async def abort(self):
        self.abort_count += 1


class _FakeStreamClient:
    def __init__(self) -> None:
        self.sender = _FakeSender()
        self.open_count = 0

    async def open_stream(self, **_kwargs):
        self.open_count += 1
        return self.sender

    async def close(self) -> None:
        return None


class PushToTalkTests(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, native_state, *, platform_name="win32") -> PTTKeyMonitor:
        monitor = PTTKeyMonitor(
            "caps_lock",
            _Logger(),
            native_key_state_getter=native_state,
            platform_name=platform_name,
        )
        monitor._listener = _Listener(alive=True)
        monitor._key_held = True
        return monitor

    def test_caps_lock_toggle_bit_is_not_treated_as_pressed(self) -> None:
        monitor = self._monitor(lambda _vk: 0x0001)

        self.assertFalse(monitor.is_held)

    def test_native_caps_lock_high_bit_is_pressed(self) -> None:
        monitor = self._monitor(lambda _vk: 0x8001)

        self.assertTrue(monitor.is_held)

    def test_native_release_overrides_cached_listener_press(self) -> None:
        monitor = self._monitor(lambda _vk: 0)

        self.assertFalse(monitor.is_held)

    def test_native_read_failure_fails_closed(self) -> None:
        logger = _Logger()

        def fail(_vk):
            raise OSError("GetAsyncKeyState failed")

        monitor = PTTKeyMonitor(
            "caps_lock",
            logger,
            native_key_state_getter=fail,
            platform_name="win32",
        )
        monitor._listener = _Listener(alive=True)
        monitor._key_held = True

        self.assertFalse(monitor.is_held)
        self.assertFalse(monitor.is_held)
        self.assertEqual(sum(level == "warning" for level, _ in logger.records), 1)

    def test_non_windows_uses_callback_only_while_listener_is_alive(self) -> None:
        monitor = self._monitor(None, platform_name="linux")
        monitor._key_held = True
        self.assertTrue(monitor.is_held)

        monitor._listener.alive = False
        self.assertFalse(monitor.is_held)

    def test_unknown_special_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PTTKeyMonitor("f13", _Logger(), platform_name="linux")

    async def test_missing_monitor_never_falls_back_to_continuous_capture(self) -> None:
        logger = _Logger()
        with patch.object(
            mic_capture_module,
            "PTTKeyMonitor",
            side_effect=RuntimeError("monitor init failed"),
        ):
            worker = MicCaptureWorker(
                MicConfig(push_to_talk=True),
                _recognized,
                logger,
            )

        worker._running = True
        worker._audio_callback(_PCMBuffer(struct.pack("<1600h", *([1000] * 1600))), 1600, None, None)

        self.assertFalse(worker._is_speaking)
        self.assertEqual(worker._processing_queue.qsize(), 0)

    async def test_missing_monitor_refuses_to_start_microphone(self) -> None:
        logger = _Logger()
        with patch.object(mic_capture_module, "PTTKeyMonitor", side_effect=RuntimeError("init failed")):
            worker = MicCaptureWorker(MicConfig(enable=True, push_to_talk=True), _recognized, logger)
        fake_sd = SimpleNamespace(
            query_devices=Mock(return_value={"default_samplerate": 16_000}),
            InputStream=Mock(),
        )

        with patch.object(mic_capture_module, "SOUNDDEVICE_AVAILABLE", True), patch.object(
            mic_capture_module, "sd", fake_sd
        ):
            await worker.start()

        fake_sd.InputStream.assert_not_called()
        self.assertFalse(worker._running)
        self.assertTrue(any(level == "error" for level, _ in logger.records))

    async def test_listener_start_failure_refuses_microphone_stream(self) -> None:
        logger = _Logger()
        monitor = _MonitorStub(held=False)
        monitor.start = Mock(side_effect=RuntimeError("listener start failed"))
        fake_sd = SimpleNamespace(
            query_devices=Mock(return_value={"default_samplerate": 16_000}),
            InputStream=Mock(),
        )
        worker = MicCaptureWorker(
            MicConfig(enable=True, push_to_talk=True), _recognized, logger
        )
        worker._ptt_monitor = monitor

        with patch.object(mic_capture_module, "SOUNDDEVICE_AVAILABLE", True), patch.object(
            mic_capture_module, "sd", fake_sd
        ):
            await worker.start()

        fake_sd.InputStream.assert_not_called()
        self.assertTrue(any("PTT" in message and level == "error" for level, message in logger.records))

    async def test_release_finishes_held_utterance_once_and_drops_later_frames(self) -> None:
        monitor = _MonitorStub(held=True)
        sender_client = _FakeStreamClient()
        delivered: list[tuple[bytes, ...]] = []

        async def on_recognized(*args):
            delivered.append(args)

        with patch.object(mic_capture_module, "PTTKeyMonitor", return_value=monitor):
            worker = MicCaptureWorker(
                MicConfig(sample_rate=16_000, channels=1, push_to_talk=True),
                on_recognized,
                _Logger(),
                stream_client=sender_client,
            )
        pressed_pcm = struct.pack("<1600h", *([1000] * 1600))
        released_pcm = struct.pack("<1600h", *([2000] * 1600))

        worker._running = True
        worker._audio_callback(_PCMBuffer(pressed_pcm), 1600, None, None)
        monitor.held = False
        worker._audio_callback(_PCMBuffer(released_pcm), 1600, None, None)
        worker._audio_callback(_PCMBuffer(released_pcm), 1600, None, None)
        worker._running = False
        await worker._process_queue_loop()

        self.assertEqual(sender_client.open_count, 1)
        self.assertEqual(sender_client.sender.finish_count, 1)
        self.assertTrue(sender_client.sender.chunks)
        self.assertTrue(all(chunk == pressed_pcm for chunk in sender_client.sender.chunks))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(delivered[0]), 2)
        self.assertEqual(worker._processing_queue.qsize(), 0)

    async def test_continuous_mode_still_captures_speech(self) -> None:
        delivered: list[bytes] = []

        async def on_recognized(payload):
            delivered.append(payload)

        worker = MicCaptureWorker(MicConfig(sample_rate=16_000), on_recognized, _Logger())
        pcm = struct.pack("<1600h", *([1000] * 1600))
        worker._running = True
        worker._audio_callback(_PCMBuffer(pcm), 1600, None, None)
        worker._finish_capture_stream("PTT released")
        worker._running = False
        await worker._process_queue_loop()

        self.assertEqual(len(delivered), 1)


async def _recognized(*_args) -> None:
    return None


if __name__ == "__main__":
    unittest.main()
