from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main as main_module
from adapter import DiscordAdapter
from core_audio_stream import DiscordCoreAudioStreamBridge
import numpy as np


class _FakeStdin:
    def __init__(self):
        self.closed = False

    def write(self, _data):
        return None

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _FakeStdout:
    def __init__(self, exited: asyncio.Event):
        self.exited = exited

    async def read(self, _size):
        await self.exited.wait()
        return b""


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.exited = asyncio.Event()
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(self.exited)
        self.killed = False
        self.wait_calls = 0

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.exited.set()

    async def wait(self):
        self.wait_calls += 1
        await self.exited.wait()
        return self.returncode


class _BlockingCoreClient:
    def __init__(self):
        self.start_entered = asyncio.Event()
        self.start_release = asyncio.Event()
        self.chunk_entered = asyncio.Event()
        self.finish_entered = asyncio.Event()
        self.operation_release = asyncio.Event()
        self.aborted: list[str] = []

    async def start_stream(self, _scope):
        self.start_entered.set()
        await self.start_release.wait()
        return "remote-stream"

    async def send_chunk(self, _stream_id, _seq, _pcm):
        self.chunk_entered.set()
        await self.operation_release.wait()

    async def finish_stream(self, _stream_id):
        self.finish_entered.set()
        await self.operation_release.wait()
        return {"text": "done"}

    async def abort_stream(self, stream_id):
        self.aborted.append(stream_id)


class DiscordRuntimeCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_transcode_cancellation_kills_and_drains_ffmpeg(self):
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        adapter.config = SimpleNamespace(max_attachment_bytes=1024)
        process = _FakeProcess()
        create_process = AsyncMock(return_value=process)

        with (
            patch("adapter.resolve_ffmpeg_executable", return_value="ffmpeg"),
            patch("adapter.asyncio.create_subprocess_exec", create_process),
        ):
            task = asyncio.create_task(adapter._transcode_audio_attachment(b"audio"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.killed)
        self.assertTrue(process.stdin.closed)
        self.assertGreaterEqual(process.wait_calls, 1)
        create_process.assert_awaited_once()

    async def test_start_cancellation_waits_for_remote_id_then_aborts(self):
        client = _BlockingCoreClient()
        bridge = DiscordCoreAudioStreamBridge(client, SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None))
        task = asyncio.create_task(bridge.start("capture-1", "scope-1"))
        await client.start_entered.wait()
        task.cancel()
        client.start_release.set()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.aborted, ["remote-stream"])
        self.assertNotIn("capture-1", bridge._streams)

    async def test_chunk_and_finish_cancellation_drop_and_abort_stream_state(self):
        client = _BlockingCoreClient()
        client.start_release.set()
        bridge = DiscordCoreAudioStreamBridge(client, SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None))
        self.assertTrue(await bridge.start("capture-1", "scope-1"))
        pcm = np.full((7_680, 2), 800, dtype=np.int16).tobytes()
        send_task = asyncio.create_task(bridge.send_pcm("capture-1", pcm))
        await client.chunk_entered.wait()
        send_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await send_task
        self.assertNotIn("capture-1", bridge._streams)
        self.assertEqual(client.aborted, ["remote-stream"])

        client2 = _BlockingCoreClient()
        client2.start_release.set()
        bridge2 = DiscordCoreAudioStreamBridge(client2, SimpleNamespace(warning=lambda *a: None, debug=lambda *a: None))
        self.assertTrue(await bridge2.start("capture-2", "scope-2"))
        finish_task = asyncio.create_task(bridge2.finish("capture-2"))
        await client2.finish_entered.wait()
        finish_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await finish_task
        self.assertNotIn("capture-2", bridge2._streams)
        self.assertEqual(client2.aborted, ["remote-stream"])

    async def test_missing_config_exits_nonzero_without_reading_workspace_config(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter_dir = Path(temp) / "adapter"
            adapter_dir.mkdir()
            entrypoint = adapter_dir / "main.py"
            with (
                patch.object(main_module, "__file__", str(entrypoint)),
                patch.object(main_module, "_migrate_before_start") as migrate,
            ):
                with self.assertRaises(SystemExit) as raised:
                    await main_module.main()

        migrate.assert_called_once()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
