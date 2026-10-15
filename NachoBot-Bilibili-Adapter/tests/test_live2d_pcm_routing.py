from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "NachoBot") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "NachoBot"))

from bili_src.audio.audio_player import AudioPlayer  # noqa: E402
from bili_src.live2d.remote_controller import RemoteLive2DController  # noqa: E402


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class Live2DPCMRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_player_forwards_stream_to_legacy_ready_renderer(self) -> None:
        class FakeWebSocket:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send(self, message: str) -> None:
                self.messages.append(message)

        controller = RemoteLive2DController(
            SimpleNamespace(config=SimpleNamespace()), _Logger()
        )
        websocket = FakeWebSocket()
        controller._connected.set()
        controller._active_websocket = websocket
        await controller._handle_interaction(
            json.dumps(
                {
                    "type": "avatar.interaction",
                    "version": "1.1",
                    "event": "ready",
                    "payload": {
                        "running": True,
                        "protocol_version": "1.1",
                        "capabilities": {
                            "prepare_reply": True,
                            "apply_control": True,
                        },
                    },
                }
            )
        )

        def local_pcm_must_not_start(_sample_rate: int, _channels: int):
            raise AssertionError("legacy-ready PCM unexpectedly used local playback")

        playback_finished = asyncio.Event()
        player = AudioPlayer(
            _Logger(),
            on_stop=playback_finished.set,
            remote_voice_stream_callback=controller.send_voice_stream_event,
            remote_voice_stream_ready_callback=lambda: controller.can_stream_pcm,
            pcm_output_factory=local_pcm_must_not_start,
        )
        player._loop = asyncio.get_running_loop()
        metadata = {
            "stream_id": "stream-1",
            "parent_message_id": "reply-1",
            "sample_rate": 16_000,
            "channels": 1,
            "sample_width": 2,
            "codec": "pcm_s16le",
        }
        pcm = b"\x01\x00\x02\x00"

        try:
            self.assertTrue(await player.handle_voice_stream_event({**metadata, "event": "start"}))
            self.assertTrue(
                await player.handle_voice_stream_event(
                    {
                        **metadata,
                        "event": "chunk",
                        "seq": 0,
                        "audio_base64": base64.b64encode(pcm).decode("ascii"),
                    }
                )
            )
            self.assertTrue(await player.handle_voice_stream_event({**metadata, "event": "end"}))

            messages = [json.loads(message) for message in websocket.messages]
            self.assertEqual([message["event"] for message in messages], ["voice_stream"] * 3)
            payloads = [message["payload"] for message in messages]
            self.assertEqual([payload["event"] for payload in payloads], ["start", "chunk", "end"])
            self.assertEqual(base64.b64decode(payloads[1]["audio_base64"]), pcm)
            self.assertNotIn("pcm", payloads[1])
            await asyncio.wait_for(playback_finished.wait(), timeout=1)
        finally:
            await player.shutdown()


if __name__ == "__main__":
    unittest.main()
