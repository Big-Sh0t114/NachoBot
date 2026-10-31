from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from websockets.sync.server import serve

from live2d_adapter.chat import ChatReply, DesktopPetChat
from live2d_adapter.config import ConfigError, DesktopChatConfig, load_config
from live2d_adapter.core_chat import CoreChatClient, incoming_idle_event
from tests.test_core_chat import response


class ProactiveTests(unittest.TestCase):
    def setUp(self):
        # Scheduling tests take the success branch unless testing the draw itself.
        self.random = self.enterContext(patch("live2d_adapter.chat.random.random", return_value=0.0))

    def chat(self, **overrides):
        values = dict(enabled=True, transport="core", play_audio=False, proactive=True, proactive_idle_seconds=30)
        values.update(overrides)
        config = DesktopChatConfig(**values)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1000):
            chat = DesktopPetChat(config, "pet", lambda *_: None, Mock(), backend=Mock())
        chat.backend.proactive.return_value = ChatReply("这个画面挺有意思。")
        chat._root = Mock()
        chat._anchor = {"visible": True}
        chat._update_hover_visibility = Mock()
        chat._present_reply = Mock()
        return chat

    def test_five_minute_threshold_and_exact_probability_boundary(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            with patch("live2d_adapter.chat.time.monotonic", return_value=1299):
                chat._poll_proactive()
                self.random.assert_not_called()
            self.random.return_value = 0.25  # The 25% boundary is outside the success range.
            with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
                chat._poll_proactive()
                chat._poll_proactive()
            self.assertEqual(chat._next_proactive_at, 1330)
            self.random.assert_called_once()
            worker.assert_not_called()
            with patch("live2d_adapter.chat.time.monotonic", return_value=1329):
                chat._poll_proactive()
            self.random.assert_called_once()
            self.random.return_value = 0.249999
            with patch("live2d_adapter.chat.time.monotonic", return_value=1330):
                chat._poll_proactive()
                chat._process_proactive(*worker.call_args.kwargs["args"])
                chat._pump_ui_queue()
            self.assertEqual(self.random.call_count, 2)
            self.assertEqual(chat._next_proactive_at, 1360)
            self.assertEqual(chat._last_user_activity_at, 1000)
            self.assertEqual(chat._last_bot_activity_at, 1330)

    def test_sidebar_toggle_enables_config_disabled_feature_without_rewriting_config(self):
        chat = self.chat(proactive=False, proactive_idle_seconds=300)
        self.assertFalse(chat._proactive_enabled)
        self.assertIn("proactive", [kind for kind, *_ in chat._toolbar_definitions()])
        self.assertNotIn("speech", [kind for kind, *_ in chat._toolbar_definitions()])
        with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
            chat._toggle_proactive()
        self.assertTrue(chat._proactive_enabled)
        self.assertFalse(chat.config.proactive)
        self.assertEqual(chat._next_proactive_at, 1600)
        self.assertEqual(chat._last_user_activity_at, 1000)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1600), patch("live2d_adapter.chat.threading.Thread") as worker:
            chat._poll_proactive()
            worker.assert_called_once()
            chat._toggle_proactive()
            self.assertTrue(chat._proactive_cancel.is_set())
            chat._process_proactive(*worker.call_args.kwargs["args"])
        self.assertFalse(chat._proactive_enabled)
        self.assertFalse(chat._work_lock.locked())
        chat.backend.set_voice_enabled.assert_called_once()

    def test_off_then_on_discards_already_queued_reply_and_preserves_backoff(self):
        chat = self.chat()
        chat._unanswered_proactive_count = 4
        chat._proactive_interval = 270
        chat._proactive_chance = 1 / 9
        chat._ui_queue.put(("proactive_reply", (chat._user_activity_epoch, ChatReply("旧回复"))))
        with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
            chat._toggle_proactive()
            chat._toggle_proactive()
            chat._pump_ui_queue()
        chat._present_reply.assert_not_called()
        self.assertFalse(chat._history_messages)
        self.assertEqual(chat._unanswered_proactive_count, 4)
        self.assertEqual(chat._proactive_interval, 270)

    def test_native_sidebar_proactive_button_click_zoom_and_hover_hide(self):
        import tkinter as tk
        from live2d_adapter.chat import pet_side_button_positions
        from live2d_adapter.ui_theme import scale_widgets
        root = tk.Tk()
        root.withdraw()
        chat = self.chat(proactive=False)
        chat._root, chat._tk = root, tk
        chat._anchor = {"topmost": True, "pet_rect": (500, 100, 700, 600), "work_area": (0, 0, 1920, 1080)}
        try:
            button = chat._create_toolbar_button("proactive", "主动对话", chat._toggle_proactive)
            canvas = button._proactive_canvas
            button.deiconify()
            root.update()
            self.assertIn(button, chat._owned_interaction_windows())
            for factor in (1.0, 0.7, 1.25):
                scale_widgets(button, factor)
                root.update()
                before = chat._proactive_enabled
                canvas.event_generate("<ButtonPress-1>", x=round(20 * factor), y=round(20 * factor))
                root.update()
                self.assertEqual(chat._proactive_enabled, not before)
                self.assertFalse(any(canvas.type(item) == "text" for item in canvas.find_all()))
                expected = "#0f9d8a" if chat._proactive_enabled else "#98a2b3"
                self.assertEqual(canvas.itemcget(canvas.find_withtag("proactive-dot")[0], "fill"), expected)
            kinds = tuple(item[0] for item in chat._toolbar_definitions())
            self.assertEqual(kinds, ("settings", "history", "proactive"))
            positions = pet_side_button_positions((500, 100, 700, 600), (0, 0, 1920, 1080), kinds=kinds)
            self.assertEqual(positions["proactive"][1] - positions["history"][1], 58)
            chat._hide_session_windows()
            self.assertEqual(button.state(), "withdrawn")
        finally:
            root.update_idletasks()
            root.destroy()
            chat._root = chat._proactive_button_window = None
            del canvas, button, root, chat
            import gc
            gc.collect()

    def test_standalone_has_no_proactive_sidebar_switch(self):
        chat = self.chat(enabled=False)
        self.assertEqual([kind for kind, *_ in chat._toolbar_definitions()], ["settings"])
        chat._toggle_proactive()
        self.assertFalse(chat._proactive_enabled)

    def test_repeated_proactive_replies_only_reset_bot_cooldown(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            for now, interval in ((1300, 30), (1330, 30), (1360, 90)):
                with patch("live2d_adapter.chat.time.monotonic", return_value=now):
                    chat._poll_proactive()
                    self.assertTrue(chat._work_lock.locked())
                    self.assertEqual(worker.call_args.kwargs["args"][1], now - 1000)
                    chat._process_proactive(*worker.call_args.kwargs["args"])
                    chat._pump_ui_queue()
                    self.assertEqual(chat._last_user_activity_at, 1000)
                    self.assertEqual(chat._last_bot_activity_at, now)
                    self.assertEqual(chat._next_proactive_at, now + interval)
                with patch("live2d_adapter.chat.time.monotonic", return_value=now + 29):
                    chat._poll_proactive()
                    self.assertFalse(chat._work_lock.locked())
            self.assertEqual(worker.call_count, 3)
        self.assertEqual(len(chat._history_messages), 3)

    def test_third_unanswered_reply_starts_cumulative_triple_interval_backoff(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            for count, (now, interval) in enumerate(((1300, 30), (1330, 30), (1360, 90), (1450, 270), (1720, 810)), 1):
                with patch("live2d_adapter.chat.time.monotonic", return_value=now):
                    chat._poll_proactive()
                    chat._process_proactive(*worker.call_args.kwargs["args"])
                    chat._pump_ui_queue()
                self.assertEqual(chat._unanswered_proactive_count, count)
                self.assertEqual(chat._proactive_interval, interval)
                self.assertAlmostEqual(chat._proactive_chance, 0.25 * (2 / 3) ** max(0, count - 2))
                self.assertEqual(chat._next_proactive_at, now + interval)
                self.assertEqual(chat._last_user_activity_at, 1000)
        self.assertEqual(worker.call_count, 5)

    def test_misses_and_failures_use_current_backoff_without_incrementing_it(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
            for _ in range(3):
                chat._note_bot_activity(proactive=True)
        self.random.return_value = 0.2  # Below the old 25%, above the reduced 16.67%.
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            with patch("live2d_adapter.chat.time.monotonic", return_value=1390):
                chat._poll_proactive()
            worker.assert_not_called()
            self.assertEqual(chat._next_proactive_at, 1480)
            self.assertEqual(chat._unanswered_proactive_count, 3)
            self.random.return_value = 0.1
            chat.backend.proactive.side_effect = RuntimeError("unavailable")
            with patch("live2d_adapter.chat.time.monotonic", return_value=1480):
                chat._poll_proactive()
                chat._process_proactive(*worker.call_args.kwargs["args"])
                chat._pump_ui_queue()
            self.assertEqual(chat._next_proactive_at, 1570)
        self.assertEqual(chat._unanswered_proactive_count, 3)
        self.assertEqual(chat._proactive_interval, 90)
        self.assertAlmostEqual(chat._proactive_chance, 1 / 6)

    def test_only_actual_user_response_resets_backoff(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
            for _ in range(3):
                chat._note_bot_activity(proactive=True)
            chat._note_input_activity()
            chat._start_microphone("keyboard")  # Opening a mic alone is not a response.
            chat._note_bot_activity()  # Ordinary bot messages do not advance/reset it.
            self.assertEqual(chat._unanswered_proactive_count, 3)
            self.assertEqual(chat._proactive_interval, 90)
            with patch("live2d_adapter.chat.threading.Thread"):
                chat._submit_text("我在呢")
        chat._work_lock.release()  # The mocked input worker never ran.
        self.assertEqual(chat._unanswered_proactive_count, 0)
        self.assertEqual(chat._proactive_interval, 30)
        self.assertEqual(chat._proactive_chance, 0.25)
        self.assertEqual(chat._next_proactive_at, 1600)

    def test_detected_voice_resets_backoff_without_waiting_for_asr(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
            for _ in range(3):
                chat._note_bot_activity(proactive=True)
        chat._perception = Mock(speech_activity=lambda: (1400, True))
        with patch("live2d_adapter.chat.time.monotonic", return_value=1401):
            chat._poll_proactive()
        self.assertEqual(chat._unanswered_proactive_count, 0)
        self.assertEqual(chat._proactive_interval, 30)
        self.assertEqual(chat._proactive_chance, 0.25)
        self.assertEqual(chat._next_proactive_at, 1701)

    def test_normal_bot_reply_does_not_postpone_user_silence_deadline(self):
        chat = self.chat(proactive_idle_seconds=300)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1190):
            chat._ui_queue.put(("message", ("bot", "普通回复")))
            chat._pump_ui_queue()
        self.assertEqual(chat._last_bot_activity_at, 1190)
        self.assertEqual(chat._last_user_activity_at, 1000)
        self.assertEqual(chat._next_proactive_at, 1300)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1200):
            chat._note_user_activity()
        self.assertEqual(chat._next_proactive_at, 1500)
        self.assertEqual(chat._last_bot_activity_at, 1190)
        with patch("live2d_adapter.chat.time.monotonic", return_value=1210):
            chat._ui_queue.put(("message", ("bot", "后续回复")))
            chat._pump_ui_queue()
        self.assertEqual(chat._last_user_activity_at, 1200)
        self.assertEqual(chat._last_bot_activity_at, 1210)
        self.assertEqual(chat._next_proactive_at, 1500)

    def test_misses_repeat_only_every_thirty_seconds_without_accumulating_chance(self):
        chat = self.chat()
        self.random.side_effect = [0.9, 0.8, 0.1]
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            for now in (1030, 1031, 1059, 1060, 1061, 1089):
                with patch("live2d_adapter.chat.time.monotonic", return_value=now):
                    chat._poll_proactive()
            self.assertEqual(self.random.call_count, 2)
            worker.assert_not_called()
            with patch("live2d_adapter.chat.time.monotonic", return_value=1090):
                chat._poll_proactive()
            worker.assert_called_once()
            self.assertEqual(self.random.call_count, 3)
        chat._work_lock.release()

    def test_blocked_slot_is_skipped_and_failure_retries_after_thirty_seconds(self):
        chat = self.chat(proactive_idle_seconds=300)
        chat._output_speaking = True
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            with patch("live2d_adapter.chat.time.monotonic", return_value=1300):
                chat._poll_proactive()
            chat._output_speaking = False
            with patch("live2d_adapter.chat.time.monotonic", return_value=1301):
                chat._poll_proactive()
            self.random.assert_not_called()
            worker.assert_not_called()
            chat.backend.proactive.side_effect = RuntimeError("unavailable")
            with patch("live2d_adapter.chat.time.monotonic", return_value=1330):
                chat._poll_proactive()
                chat._process_proactive(*worker.call_args.kwargs["args"])
                chat._pump_ui_queue()
            self.assertEqual(chat._next_proactive_at, 1360)
        self.assertFalse(chat._work_lock.locked())

    def test_config_defaults_validation_and_standalone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = '[renderer]\nmodel_path="avatar.model3.json"\n[desktop_pet.chat]\nenabled=true\ntransport="core"\n'
            path.write_text(base, encoding="utf-8")
            self.assertFalse(load_config(path).desktop_pet.chat.proactive)
            for value in ('proactive="true"', 'proactive_idle_seconds=true', 'proactive_idle_seconds=0', 'proactive_idle_seconds=nan'):
                path.write_text(base + value, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text('[components]\ncore=false\n' + base + 'proactive=true\nproactive_idle_seconds=90', encoding="utf-8")
            config = load_config(path)
            self.assertFalse(config.desktop_pet.chat.proactive)
            self.assertFalse(config.desktop_pet.chat.enabled)

    def test_one_idle_request_and_bot_only_history_with_cooldown(self):
        chat = self.chat()
        with patch("live2d_adapter.chat.threading.Thread") as worker:
            with patch("live2d_adapter.chat.time.monotonic", return_value=1029):
                chat._poll_proactive()
                worker.assert_not_called()
            with patch("live2d_adapter.chat.time.monotonic", return_value=1031):
                chat._poll_proactive()
                chat._poll_proactive()
                worker.assert_called_once()
                self.assertTrue(chat._work_lock.locked())
                chat._process_proactive(*worker.call_args.kwargs["args"])
                chat._pump_ui_queue()
                self.assertEqual(chat._next_proactive_at, 1061)
        self.assertFalse(chat._work_lock.locked())
        self.assertEqual([(m["role"], m["text"]) for m in chat._history_messages], [("bot", "这个画面挺有意思。")])
        chat._present_reply.assert_called_once()
        chat.backend.proactive.assert_called_once()
        self.assertFalse(chat.backend.proactive.call_args.kwargs["include_audio"])

    def test_busy_speaking_hidden_draft_and_voice_recognition_defer(self):
        for blocker in ("busy", "playing", "hidden", "draft", "speech"):
            chat = self.chat()
            if blocker == "busy":
                chat._work_lock.acquire()
            elif blocker == "playing":
                chat._output_speaking = True
            elif blocker == "hidden":
                chat._anchor["visible"] = False
            elif blocker == "draft":
                chat._input_variable = Mock(get=lambda: "未发送的草稿")
            else:
                chat._perception = Mock(speech_activity=lambda: (0, True))
            with patch("live2d_adapter.chat.time.monotonic", return_value=1031), patch("live2d_adapter.chat.threading.Thread") as worker:
                chat._poll_proactive()
                worker.assert_not_called()
        # An open but silent continuous microphone does not block proactive chat.
        chat = self.chat()
        chat._perception = Mock(recording=True, speech_activity=lambda: (0, False))
        with patch("live2d_adapter.chat.time.monotonic", return_value=1031), patch("live2d_adapter.chat.threading.Thread") as worker:
            chat._poll_proactive()
            worker.assert_called_once()
        chat._work_lock.release()

    def test_new_user_turn_cancels_proactive_and_is_not_lost(self):
        chat = self.chat()
        with patch("live2d_adapter.chat.time.monotonic", return_value=1031), patch("live2d_adapter.chat.threading.Thread") as worker:
            chat._poll_proactive()
            args = worker.call_args.kwargs["args"]
            chat._submit_text("我有个问题")
            self.assertTrue(chat._proactive_cancel.is_set())
            self.assertEqual(chat._deferred_user_input, ("我有个问题", False))
            chat._process_proactive(*args)
            chat._pump_ui_queue()
        chat._present_reply.assert_not_called()
        self.assertEqual(chat._history_messages[-1]["text"], "我有个问题")
        self.assertFalse(any(m["role"] == "bot" for m in chat._history_messages))
        chat._work_lock.release()  # mocked normal-input worker did not run

    def test_user_activity_after_generation_discards_queued_proactive_before_tts(self):
        chat = self.chat()
        chat._voice_enabled = True
        with patch("live2d_adapter.chat.time.monotonic", return_value=1031), patch("live2d_adapter.chat.threading.Thread") as worker:
            chat._poll_proactive()
            chat._process_proactive(*worker.call_args.kwargs["args"])
            chat._note_input_activity()
            chat._pump_ui_queue()
        chat.backend.announce.assert_not_called()
        chat._present_reply.assert_not_called()
        self.assertFalse(chat._history_messages)

    def test_speech_starting_before_presentation_discards_proactive_reply(self):
        chat = self.chat()
        with patch("live2d_adapter.chat.time.monotonic", return_value=1031), patch("live2d_adapter.chat.threading.Thread") as worker:
            chat._poll_proactive()
            chat._process_proactive(*worker.call_args.kwargs["args"])
            chat._perception = Mock(recording=True, speech_activity=lambda: (1031, True))
            chat._pump_ui_queue()
        chat.backend.announce.assert_not_called()
        chat._present_reply.assert_not_called()
        self.assertFalse(chat._history_messages)

    def test_real_wire_sends_senderless_idle_event_with_optional_visual_context(self):
        received = []
        def handler(socket):
            packet = json.loads(socket.recv())
            received.append(packet)
            socket.send(json.dumps(response(packet["message_info"]["message_id"], "在看代码呀？")))
        server = serve(handler, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        context = {"schema_version": 1, "captured_at": 123, "summary": "代码编辑器"}
        client = CoreChatClient(DesktopChatConfig(core_url=f"ws://127.0.0.1:{server.socket.getsockname()[1]}", play_audio=False),
                                Mock(), Mock(), Mock(), context_provider=lambda: context)
        try:
            reply = client.proactive(300, include_audio=False)
            self.assertEqual(reply.text, "在看代码呀？")
            info = received[0]["message_info"]
            self.assertNotIn("user_info", info)
            self.assertEqual(info["additional_config"]["system_event"]["type"], "desktop.idle")
            self.assertEqual(info["additional_config"]["system_event_route"]["peer"]["user_id"], "host")
            self.assertEqual(info["additional_config"]["desktop_context"], context)
            self.assertEqual(received[0]["message_segment"]["data"], "")
            plain = incoming_idle_event("id", 300)
            self.assertNotIn("desktop_context", plain["message_info"]["additional_config"])
        finally:
            client.close()
            server.shutdown()
            worker.join(2)
