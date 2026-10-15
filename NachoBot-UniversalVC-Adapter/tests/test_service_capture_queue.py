"""Service capture queues fail closed when frame continuity is lost."""

from __future__ import annotations

import asyncio
import logging
import unittest

from universalvc_service.audio_capture import AudioCapture, MicrophoneCapture
from universalvc_service.config import AudioCaptureConfig, MicrophoneConfig


class ServiceCaptureQueueTests(unittest.IsolatedAsyncioTestCase):
    async def _assert_service_queue_overflow_fails(self, capture):
        errors = []

        async def on_error(reason):
            errors.append(reason)

        capture.on_error = on_error
        capture._loop = asyncio.get_running_loop()
        capture._queue = asyncio.Queue(maxsize=2)
        capture._running = True
        capture._queue.put_nowait((b"first", 48_000, 2))
        capture._queue.put_nowait((b"second", 48_000, 2))

        capture._put_nowait_safe((b"lost", 48_000, 2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(capture._queue.qsize(), 2)
        self.assertFalse(capture._running)
        self.assertEqual(len(errors), 1)
        self.assertIn("continuity", errors[0])

    async def test_process_and_microphone_service_queues_report_discontinuity(self):
        logger = logging.getLogger("service-capture-queue-test")
        process = AudioCapture(
            AudioCaptureConfig(target_process_name="fake.exe"),
            logger,
            strict_device_selection=True,
            queue_size=2,
        )
        microphone = MicrophoneCapture(
            MicrophoneConfig(enabled=True, device_name="fake microphone"),
            logger,
            strict_device_selection=True,
            queue_size=2,
        )
        self.assertEqual(process.queue_size, 2)
        self.assertEqual(microphone.queue_size, 2)
        await self._assert_service_queue_overflow_fails(process)
        await self._assert_service_queue_overflow_fails(microphone)

    async def test_legacy_queue_keeps_default_size_and_does_not_fail_session(self):
        capture = AudioCapture(
            AudioCaptureConfig(target_process_name="legacy.exe"),
            logging.getLogger("legacy-capture-queue-test"),
        )
        self.assertEqual(capture.queue_size, 500)
        capture._loop = asyncio.get_running_loop()
        capture._queue = asyncio.Queue(maxsize=1)
        capture._running = True
        capture._queue.put_nowait((b"first", 48_000, 2))
        capture._put_nowait_safe((b"lost", 48_000, 2))
        await asyncio.sleep(0)
        self.assertTrue(capture._running)
        self.assertFalse(capture._error_notified)


if __name__ == "__main__":
    unittest.main()
