"""Disconnect must close the owner-thread iterator before another request."""

import asyncio
import importlib.util
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from starlette.requests import ClientDisconnect  # noqa: E402

# The raw engine runs as a script, without importing the public Vox backend.
path = Path(__file__).resolve().parents[1] / "src/tts/backends/Vox/vox_api_server.py"
spec = importlib.util.spec_from_file_location("vox_test_raw_server", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
InferenceWorker = module.InferenceWorker
OwnedStreamingResponse = module.OwnedStreamingResponse


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def check_disconnect(self, spec):
        worker = InferenceWorker()
        self.addCleanup(worker.shutdown)
        order = []
        first_body = asyncio.Event()

        def factory():
            try:
                while True:
                    yield b"pcm"
            finally:
                order.append(("closed", threading.get_ident()))

        async def receive():
            await first_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                first_body.set()
                if spec == "2.4":
                    raise OSError("client disconnected")
                await asyncio.Event().wait()

        response = OwnedStreamingResponse(worker.stream(factory))
        scope = {"type": "http", "asgi": {"spec_version": spec}}
        if spec == "2.4":
            with self.assertRaises(ClientDisconnect):
                await asyncio.wait_for(response(scope, receive, send), 2)
        else:
            await asyncio.wait_for(response(scope, receive, send), 2)
        await worker.generate(lambda: order.append(("next", threading.get_ident())))
        self.assertEqual([entry[0] for entry in order], ["closed", "next"])
        self.assertEqual(order[0][1], order[1][1])

    async def test_disconnect_listener_closes_producer(self):
        await self.check_disconnect("2.0")

    async def test_send_failure_closes_producer(self):
        await self.check_disconnect("2.4")
