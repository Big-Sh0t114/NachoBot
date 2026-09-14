from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from live2d_adapter.chat import (
    ChatReply,
    DesktopPetBackendClient,
    DesktopPetBackendError,
    DesktopPetChat,
    docked_window_position,
    load_chat_history,
    save_chat_history,
)
from live2d_adapter.config import DesktopChatConfig


class StubLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class StubBackend:
    def ask(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "auto",
    ) -> ChatReply:
        audio = b"RIFF-audio" if include_audio else b""
        return ChatReply(f"回答：{text}（{tts_language}）", audio)

    def announce(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "auto",
    ) -> ChatReply:
        audio = b"RIFF-audio" if include_audio else b""
        return ChatReply(text, audio)


class ChatCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands: list[tuple[str, Any]] = []
        self.chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda command, value: self.commands.append((command, value)),
            StubLogger(),
            backend=StubBackend(),
        )

    def test_plain_text_asks_backend_and_queues_speech(self) -> None:
        reply = self.chat.execute_for_test("你好")

        self.assertEqual(reply, "回答：你好（auto）")
        self.assertEqual(self.commands[0], ("state", "start_thinking"))
        self.assertIn(("emotion", "joy"), self.commands)
        self.assertIn(("play_chat_audio", b"RIFF-audio"), self.commands)

    def test_routes_motion_and_window_commands(self) -> None:
        self.assertEqual(self.chat.execute_for_test("/动作 开心"), "已执行动作：开心")
        self.assertEqual(self.chat.execute_for_test("/置顶"), "已切换置顶状态。")

        self.assertEqual(
            self.commands,
            [("desktop_motion", "FlickUp"), ("desktop_toggle_topmost", None)],
        )

    def test_rejects_unknown_app_instead_of_running_shell_text(self) -> None:
        with self.assertRaises(DesktopPetBackendError):
            self.chat.execute_for_test("/打开 powershell")

    def test_mute_mode_keeps_text_reply_and_stops_audio(self) -> None:
        self.assertIn("闭嘴模式", self.chat.execute_for_test("/闭嘴"))
        self.commands.clear()

        reply = self.chat.execute_for_test("继续聊天")

        self.assertEqual(reply, "回答：继续聊天（auto）")
        self.assertNotIn(("play_chat_audio", b"RIFF-audio"), self.commands)
        self.assertIn(("state", "finish_reply"), self.commands)

    def test_tts_language_command_is_forwarded_to_backend(self) -> None:
        self.assertIn("日语", self.chat.execute_for_test("/语言 日语"))
        self.assertEqual(self.chat.execute_for_test("こんにちは"), "回答：こんにちは（ja）")

    def test_dock_is_centered_below_pet_and_kept_on_monitor(self) -> None:
        self.assertEqual(
            docked_window_position(
                (1000, 100, 1520, 860),
                (0, 0, 1920, 1040),
                (440, 120),
            ),
            (1040, 866),
        )
        self.assertEqual(
            docked_window_position(
                (1450, 160, 1970, 920),
                (0, 0, 1920, 1040),
                (440, 140),
            ),
            (1480, 780),
        )


class LocalHostHandler(BaseHTTPRequestHandler):
    replies = 0
    latest_reply = ""
    audio_version = 0
    streamed_audio_version = 0
    last_speak = None

    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self._json(
                {
                    "status": "ok",
                    "core_connected": True,
                    "replies": self.__class__.replies,
                    "latest_reply": self.__class__.latest_reply,
                    "neural_audio_ready": self.__class__.audio_version > 0,
                    "neural_audio_version": self.__class__.audio_version,
                    "live2d_streamed_audio_version": self.__class__.streamed_audio_version,
                    "last_error": "",
                }
            )
            return
        if self.path == "/api/latest-speech.mp3":
            audio = b"RIFF-test-wave"
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.last_speak = payload.get("speak")
        text = str(payload.get("text") or "")
        self.__class__.replies += 1
        self.__class__.latest_reply = f"本机回答：{text}"
        self.__class__.audio_version += 1
        if self.path == "/api/respond":
            self._json({"ok": True, "request_id": "test-request"})
            return
        if self.path == "/api/announce":
            self.__class__.latest_reply = text
            self._json({"ok": True, "text": text})
            return
        self.send_error(404)

    def _json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class BackendClientTests(unittest.TestCase):
    def setUp(self) -> None:
        LocalHostHandler.replies = 0
        LocalHostHandler.latest_reply = ""
        LocalHostHandler.audio_version = 0
        LocalHostHandler.streamed_audio_version = 0
        LocalHostHandler.last_speak = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHostHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = DesktopPetBackendClient(
            DesktopChatConfig(
                enabled=True,
                backend_url=f"http://{host}:{port}",
                poll_interval_seconds=0.01,
                reply_timeout_seconds=2.0,
            )
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_ask_waits_for_reply_and_downloads_audio(self) -> None:
        reply = self.client.ask("今天怎么样")

        self.assertEqual(reply.text, "本机回答：今天怎么样")
        self.assertEqual(reply.audio, b"RIFF-test-wave")

    def test_announce_returns_direct_text_and_audio(self) -> None:
        reply = self.client.announce("你好呀")

        self.assertEqual(reply.text, "你好呀")
        self.assertEqual(reply.audio, b"RIFF-test-wave")

    def test_streamed_audio_is_not_downloaded_and_replayed(self) -> None:
        LocalHostHandler.streamed_audio_version = 1

        reply = self.client.ask("流式回答")

        self.assertEqual(reply.text, "本机回答：流式回答")
        self.assertEqual(reply.audio, b"")

    def test_muted_request_tells_server_to_skip_speech(self) -> None:
        reply = self.client.ask("安静回答", include_audio=False)

        self.assertEqual(reply.text, "本机回答：安静回答")
        self.assertEqual(reply.audio, b"")
        self.assertIs(LocalHostHandler.last_speak, False)


class ChatHistoryTests(unittest.TestCase):
    def test_history_round_trip_keeps_newest_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            messages = [
                {"role": "user", "text": "第一句", "created_at": "10:01"},
                {"role": "bot", "text": "第二句", "created_at": "10:02"},
                {"role": "user", "text": "第三句", "created_at": "10:03"},
            ]

            save_chat_history(path, messages, 2)

            self.assertEqual(load_chat_history(path, 2), messages[-2:])


if __name__ == "__main__":
    unittest.main()
