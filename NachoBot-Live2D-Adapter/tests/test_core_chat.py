from __future__ import annotations

import asyncio
import json
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from websockets.sync.server import serve

from live2d_adapter.config import DesktopChatConfig
from live2d_adapter.core_chat import CoreChatClient, extract_reply
from live2d_adapter.voice import DesktopVoice, pcm_format, speech_segments


class Logger:
    def __getattr__(self, _name):
        return lambda *args: None


def response(request_id, text='{"reply":"你好。","emotion":"joy","action":"nod"}'):
    return {
        "message_info": {
            "user_info": {"user_id": "host"},
            "additional_config": {"reply_to_message_id": request_id},
        },
        "message_segment": {"type": "text", "data": text},
    }


class CoreChatTests(unittest.TestCase):
    def test_real_wire_ignores_late_and_unattributed_replies(self):
        received = []

        def handler(socket):
            received.append(json.loads(socket.recv()))
            current = received[0]["message_info"]["message_id"]
            for reply in (
                response("expired", "旧回答"),
                response(None, "无归属回答"),
                response(current),
            ):
                socket.send(json.dumps(reply, ensure_ascii=False))

        server = serve(handler, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        client = CoreChatClient(
            DesktopChatConfig(
                transport="core",
                core_url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}",
                play_audio=False,
                reply_timeout_seconds=2,
            ),
            lambda *args: None,
            Logger(),
            self.fail,
        )
        try:
            reply = client.ask("你好", include_audio=False)
            self.assertEqual((reply.text, reply.emotion, reply.action), ("你好。", "joy", "NOD"))
            self.assertFalse(reply.audio_pending)
            self.assertEqual(received[0]["message_info"]["platform"], "local.live2d")
        finally:
            client.close()
            server.shutdown()
            worker.join(2)

    def test_other_user_and_custom_messages_cannot_speak(self):
        value = response("current")
        value["message_info"]["user_info"]["user_id"] = "other"
        self.assertIsNone(extract_reply(value, "current"))
        value = response("current")
        value["is_custom_message"] = True
        self.assertIsNone(extract_reply(value, "current"))

    def test_mute_then_unmute_does_not_revive_a_pending_reply(self):
        commands = []
        voice = DesktopVoice(
            DesktopChatConfig(play_audio=True),
            lambda *args: commands.append(args),
            Logger(),
            self.fail,
        )
        old = voice.token()
        try:
            voice.set_enabled(False)
            voice.set_enabled(True)
            self.assertFalse(voice.speak("迟到的回答", "zh", old))
            self.assertFalse(any(command == "queue_audio" for command, _ in commands))
        finally:
            voice.close()


class VoiceTests(unittest.TestCase):
    def test_cancelled_synthesis_cannot_enqueue_audio_after_mute(self):
        began = threading.Event()
        released = threading.Event()
        finished = threading.Event()
        commands = []

        class SlowSpeech:
            def __init__(self, *args, **kwargs):
                pass

            async def stream(self):
                began.set()
                try:
                    while not released.is_set():
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    # Simulate an external implementation that finishes despite cancel.
                    pass
                yield {"type": "audio", "data": b"late-audio"}
                finished.set()

        voice = DesktopVoice(
            DesktopChatConfig(play_audio=True),
            lambda *args: commands.append(args),
            Logger(),
            self.fail,
        )
        try:
            with patch("live2d_adapter.voice.edge_tts.Communicate", SlowSpeech):
                self.assertTrue(voice.speak("你好。", "zh", voice.token()))
                self.assertTrue(began.wait(2))
                voice.set_enabled(False)
                released.set()
                self.assertTrue(finished.wait(2))
                self.assertFalse(any(command == "queue_audio" for command, _ in commands))
        finally:
            voice.close()

    def test_segments_preserve_text_and_bound_long_requests(self):
        text = "你好。请慢慢说，保证每一句都能听清！" + "今天可以聊喜欢的游戏，" * 20
        segments = speech_segments(text)
        self.assertEqual("".join(segments), text)
        self.assertLessEqual(max(map(len, segments)), 80)

    def test_format_rejects_missing_or_unsupported_pcm_metadata(self):
        for headers in (
            {},
            {"X-TTS-Stream-Version": "2"},
            {"X-TTS-Stream-Version": "1", "X-Audio-Codec": "mp3"},
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                pcm_format(headers)
        result = pcm_format(
            {
                "X-TTS-Stream-Version": "1",
                "X-Audio-Codec": "pcm_s16le",
                "X-Audio-Sample-Rate": "22050",
                "X-Audio-Channels": "1",
                "X-Audio-Sample-Width": "2",
            }
        )
        self.assertEqual(result["sample_rate"], 22050)

    def test_current_http_pcm_contract_and_frame_alignment(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/api/tts/stream":
                    self.send_error(404)
                    return
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                for key, value in {
                    "X-TTS-Stream-Version": "1",
                    "X-Audio-Codec": "pcm_s16le",
                    "X-Audio-Sample-Rate": "22050",
                    "X-Audio-Channels": "1",
                    "X-Audio-Sample-Width": "2",
                    "Content-Length": "12",
                }.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(b"\1\0" * 6)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        events = []
        config = replace(
            DesktopChatConfig(play_audio=True),
            tts_provider="multimodal",
            tts_url=f"http://127.0.0.1:{server.server_port}",
        )
        voice = DesktopVoice(
            config, lambda command, value: events.append((command, value)), Logger(), self.fail
        )
        try:
            asyncio.run(voice._multimodal("你好", "zh", 0, 0))
            packets = [value for command, value in events if command == "voice_stream"]
            self.assertEqual([packet["event"] for packet in packets], ["start", "chunk", "end"])
            self.assertEqual(packets[1]["pcm"], b"\1\0" * 6)
            self.assertTrue(all(packet["sample_rate"] == 22050 for packet in packets))
        finally:
            voice.close()
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
