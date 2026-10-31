from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import time
import unittest
import wave
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live2d_adapter.config import (
    ConfigError,
    DesktopChatConfig,
    PerceptionConfig,
    load_config,
    model_display_name,
)
from live2d_adapter.core_chat import incoming_message
from live2d_adapter.perception import DesktopPerception, ScreenCapture, core_http_url
from live2d_adapter.chat import DesktopPetChat


class PerceptionTests(unittest.TestCase):
    def service(self, **flags):
        events = []
        config = replace(DesktopChatConfig(transport="core"), perception=PerceptionConfig(**flags))
        service = DesktopPerception(config, Mock(), lambda *args: events.append(args))
        self.addCleanup(service.close)
        return service, events

    def test_names_come_from_selected_metadata_then_directory_and_preserve_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "猫猫" / "avatar.model3.json"
            model.parent.mkdir()
            model.write_text("{}", encoding="utf-8")
            self.assertEqual(model_display_name(model), "猫猫")
            metadata = model.with_name("avatar.vtube.json")
            metadata.write_text('{"Name":"模型内名称"}', encoding="utf-8")
            self.assertEqual(model_display_name(model), "模型内名称")
            config = root / "config.toml"
            config.write_text(
                '[renderer]\nmodel_path="猫猫/avatar.model3.json"\n[desktop_pet]\ncharacter_name=""',
                encoding="utf-8",
            )
            value = load_config(config)
            self.assertEqual(value.desktop_pet.character_name, "模型内名称")
            self.assertIn("模型内名称", value.desktop_pet.title)
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'character_name=""', 'character_name="手动名称"'
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(config).desktop_pet.character_name, "手动名称")
            metadata.write_text("invalid", encoding="utf-8")
            self.assertEqual(model_display_name(model), "猫猫")

    def test_feature_switches_are_strict_and_require_core_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            prefix = '[renderer]\nmodel_path="avatar.model3.json"\n'
            for settings in (
                'vision="true"',
                "capture_interval_seconds=0",
                "microphone_device=true",
                "vision=true",
            ):
                path.write_text(prefix + "[desktop_pet.perception]\n" + settings, encoding="utf-8")
                with self.subTest(settings=settings), self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text(
                prefix
                + '[desktop_pet.chat]\ntransport="core"\n[desktop_pet.perception]\nvision=true\nspeech=true',
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertTrue(config.desktop_pet.chat.perception.vision)
            self.assertTrue(config.desktop_pet.chat.perception.speech)

    def test_disabled_features_do_not_capture_or_start_threads(self):
        service, events = self.service()
        with patch.object(service.capture, "grab") as grab:
            service.start()
            self.assertFalse(service.start_recording())
        grab.assert_not_called()
        self.assertIsNone(service.context())
        self.assertIsNone(service._vision_thread)
        self.assertEqual(events, [])

    def test_visual_snapshot_expiry_failure_and_request_scoping(self):
        service, _ = self.service(vision=True)
        with (
            patch.object(
                service.capture, "grab", return_value=(b"jpeg", {"window_title": "Editor"})
            ),
            patch.object(service, "perceive", return_value="有一段代码") as perceive,
        ):
            service.refresh_screen()
        self.assertEqual(perceive.call_args.args[:2], ("image.describe.v1", b"jpeg"))
        self.assertEqual(perceive.call_args.kwargs["task"], "vlm_fast")
        context = service.context()
        message = incoming_message("看看这里", "turn-1", desktop_context=context)
        self.assertEqual(
            message["message_info"]["additional_config"]["desktop_context"]["summary"], "有一段代码"
        )
        context["summary"] = "mutated"
        self.assertEqual(service.context()["summary"], "有一段代码")
        with patch("live2d_adapter.perception.time.time", return_value=time.time() + 121):
            self.assertIsNone(service.context())
        with patch.object(service.capture, "grab", side_effect=RuntimeError("capture failed")):
            with self.assertRaises(RuntimeError):
                service.refresh_screen()
        self.assertIsNone(service.context())

    def test_unavailable_active_window_never_falls_back_to_full_screen(self):
        capture = ScreenCapture(PerceptionConfig(vision=True))
        with (
            patch.object(capture, "target", return_value=None),
            patch("PIL.ImageGrab.grab") as grab,
        ):
            self.assertIsNone(capture.grab())
        grab.assert_not_called()

    def test_actual_http_contract_rejects_degraded_results(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            degraded = False

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, data, self.headers.get("Authorization")))
                result = json.dumps(
                    dict(
                        operation=data["operation"],
                        text="转写结果",
                        degraded=self.degraded,
                        error="",
                    ),
                    ensure_ascii=False,
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        service, _ = self.service(speech=True)
        service.base_url = f"http://127.0.0.1:{server.server_port}"
        with patch.dict(os.environ, {"NACHOBOT_CORE_TOKEN": "test-token"}):
            self.assertEqual(
                service.perceive("audio.transcribe.v1", b"wav", media_format="wav"), "转写结果"
            )
        self.assertEqual(requests[0][0], "/api/multimodal/perception")
        self.assertEqual(base64.b64decode(requests[0][1]["data"]), b"wav")
        self.assertEqual(requests[0][2], "Bearer test-token")
        Handler.degraded = True
        with self.assertRaises(ValueError):
            service.perceive("audio.transcribe.v1", b"wav")
        self.assertEqual(
            core_http_url("wss://example.invalid/prefix/ws"), "https://example.invalid/prefix"
        )

    def test_ptt_release_produces_bounded_wav_and_cancellation_invalidates_queued_text(self):
        service, events = self.service(speech=True)
        began = threading.Event()

        class Stream:
            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                self.callback(b"\1\0" * 3200, 3200, None, None)
                began.set()
                return self

            def __exit__(self, *args):
                pass

        fake = SimpleNamespace(RawInputStream=Stream, check_input_settings=lambda **kwargs: None)
        with (
            patch.dict("sys.modules", {"sounddevice": fake}),
            patch.object(service, "perceive", return_value="你好") as perceive,
        ):
            self.assertTrue(service.start_recording())
            self.assertTrue(began.wait(2))
            self.assertFalse(service.start_recording())
            service.stop_recording()
            service._record_thread.join(2)
        wav_data = perceive.call_args.args[1]
        with wave.open(io.BytesIO(wav_data), "rb") as wav:
            self.assertEqual(
                (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2)
            )
            self.assertEqual(wav.getnframes(), 3200)
        packet = next(value for name, value in events if name == "transcript")
        self.assertEqual(service.transcript_text(packet), "你好")
        service.stop_recording(cancel=True)
        self.assertIsNone(service.transcript_text(packet))

    def test_ui_coalesces_drag_updates_and_keeps_voice_on_normal_submission_path(self):
        chat = DesktopPetChat(
            DesktopChatConfig(), "pet", lambda *args: None, Mock(), backend=Mock()
        )
        chat._root = Mock()
        for x in range(120):
            chat.sync_anchor(
                (x, 0, x + 200, 300),
                (0, 0, 1920, 1080),
                visible=True,
                topmost=True,
                click_through=False,
            )
        with (
            patch.object(chat, "_apply_ui_scale") as scale,
            patch.object(chat, "_update_hover_visibility"),
        ):
            chat._pump_ui_queue()
        self.assertEqual(chat._anchor["pet_rect"][0], 119)
        scale.assert_called_once()
        chat._root.after.assert_called_once_with(16, chat._pump_ui_queue)
        window = Mock()
        DesktopPetChat._set_geometry_if_changed(window, "200x50+1+2")
        DesktopPetChat._set_geometry_if_changed(window, "200x50+1+2")
        window.geometry.assert_called_once()
        chat._perception = Mock(recording=False)
        chat._perception.transcript_text.return_value = "语音文本"
        chat._ui_queue.put(("transcript", {"record_id": 1, "text": "语音文本"}))
        with (
            patch.object(chat, "_submit_text") as submit,
            patch.object(chat, "_update_hover_visibility"),
        ):
            chat._pump_ui_queue()
        submit.assert_called_once_with("语音文本")

    def test_native_composer_has_a_pure_icon_mic_only_when_enabled(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            for enabled in (False, True):
                config = replace(
                    DesktopChatConfig(enabled=True, transport="core"),
                    perception=PerceptionConfig(speech=enabled),
                )
                chat = DesktopPetChat(config, "pet", lambda *args: None, Mock(), backend=Mock())
                chat._tk, chat._root = tk, root
                window = tk.Toplevel(root)
                window.withdraw()
                chat._build_composer_surface(window)
                self.assertEqual(chat._mic_canvas is not None, enabled)
                if enabled:
                    self.assertTrue(chat._mic_canvas.bind("<ButtonPress-1>"))
                    self.assertTrue(chat._mic_canvas.bind("<ButtonRelease-1>"))
                    chat._perception.close()
                window.destroy()
        finally:
            root.update_idletasks()
            root.destroy()

    def test_lost_mouse_capture_and_escape_always_close_the_ptt_gate(self):
        chat = DesktopPetChat(
            DesktopChatConfig(), "pet", lambda *args: None, Mock(), backend=Mock()
        )
        chat._perception = Mock(recording=True)
        with (
            patch("win32api.GetAsyncKeyState", return_value=0),
            patch.object(chat, "_end_microphone") as end,
        ):
            chat._poll_microphone_release()
        end.assert_called_once()
        with (
            patch("win32api.GetAsyncKeyState", return_value=0x8000),
            patch.object(chat, "_cancel_microphone") as cancel,
        ):
            chat._poll_microphone_release()
        cancel.assert_called_once()
