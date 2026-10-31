from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from live2d_adapter.chat import DesktopPetChat
from live2d_adapter.config import ConfigError, DesktopChatConfig, PerceptionConfig, load_config
from live2d_adapter.push_to_talk import HoldToTalkKey, parse_ptt_key


class PushToTalkTests(unittest.TestCase):
    def test_single_keys_chords_empty_and_invalid_config(self):
        self.assertEqual(parse_ptt_key(" F8 "), (0x77,))
        self.assertEqual(parse_ptt_key("Ctrl + ALT + V"), (0x11, 0x12, 0x56))
        self.assertEqual(parse_ptt_key("ctrl_r"), (0xA3,))
        self.assertEqual(parse_ptt_key("space"), (0x20,))
        self.assertEqual(parse_ptt_key(""), ())
        for value in (True, 8, "ctrl++v", "ctrl+ctrl", "F25", "Escape", "mouse4", "unknown"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ptt_key(value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            prefix = '[renderer]\nmodel_path="avatar.model3.json"\n[desktop_pet.chat]\ntransport="core"\n[desktop_pet.perception]\nspeech=true\n'
            path.write_text(prefix + 'push_to_talk_key="ctrl+alt+v"', encoding="utf-8")
            self.assertEqual(
                load_config(path).desktop_pet.chat.perception.push_to_talk_key, "ctrl+alt+v"
            )
            path.write_text(prefix + 'push_to_talk_key="Escape"', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_physical_edges_ignore_startup_hold_and_repeat_until_released(self):
        binding = HoldToTalkKey("F8")

        def sample(down):
            return binding.poll(lambda key: 0x8000 if down else 0)

        self.assertIsNone(sample(True))
        self.assertIsNone(sample(True))
        self.assertEqual(sample(False), "release")
        self.assertEqual(sample(True), "press")
        self.assertIsNone(sample(True))
        self.assertEqual(sample(False), "release")
        self.assertIsNone(sample(False))

    def test_chords_require_all_keys_and_release_on_any_key_up(self):
        binding = HoldToTalkKey("ctrl+alt+v")
        pressed = set()
        poll = lambda: binding.poll(lambda key: 0x8000 if key in pressed else 0)
        self.assertIsNone(poll())
        pressed.update((0x11, 0x12))
        self.assertIsNone(poll())
        pressed.add(0x56)
        self.assertEqual(poll(), "press")
        pressed.remove(0x11)
        self.assertEqual(poll(), "release")

    def chat(self, *, speech=True, key="F8", mode="hold"):
        config = DesktopChatConfig(
            enabled=True,
            transport="core",
            play_audio=False,
            perception=PerceptionConfig(speech=speech, push_to_talk_key=key, push_to_talk_mode=mode),
        )
        chat = DesktopPetChat(config, "pet", lambda *args: None, Mock(), backend=Mock())
        if chat._perception is not None:
            chat._perception.close()
        chat._perception = Mock(recording=True)
        chat._perception.start_recording.return_value = True
        return chat

    def test_toggle_keyboard_release_keeps_listening_and_next_press_closes(self):
        chat = self.chat(mode="toggle")
        pressed = set()
        with patch("win32api.GetAsyncKeyState", side_effect=lambda key: 0x8000 if key in pressed else 0):
            chat._poll_keyboard_microphone()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            chat._poll_keyboard_microphone()
            pressed.clear()
            chat._poll_keyboard_microphone()
            chat._poll_microphone_release()
            chat._perception.stop_recording.assert_not_called()
            chat._perception.start_recording.assert_called_once_with(continuous=True)
            chat._end_microphone()
            chat._perception.stop_recording.assert_not_called()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            chat._perception.stop_recording.assert_called_once_with(cancel=True)
            self.assertIsNone(chat._microphone_source)
            chat._poll_keyboard_microphone()
            chat._perception.start_recording.assert_called_once()

    def test_toggle_mouse_and_escape_cancel(self):
        chat = self.chat(mode="toggle")
        chat._begin_microphone()
        chat._end_microphone()
        with patch("win32api.GetAsyncKeyState", return_value=0):
            chat._poll_microphone_release()
        chat._perception.stop_recording.assert_not_called()
        chat._begin_microphone()
        chat._perception.stop_recording.assert_called_once_with(cancel=True)
        chat._begin_microphone()
        with patch("win32api.GetAsyncKeyState", side_effect=lambda key: 0x8000 if key == 0x1B else 0):
            chat._poll_microphone_release()
        self.assertIsNone(chat._microphone_source)
        self.assertEqual(chat._perception.stop_recording.call_args.kwargs, {"cancel": True})

    def test_continuous_listening_survives_ui_unmap_and_can_be_closed_from_another_control(self):
        chat = self.chat(mode="toggle")
        chat._begin_microphone()
        chat._on_composer_unmap()
        self.assertEqual(chat._microphone_source, "mouse")
        chat._perception.stop_recording.assert_not_called()
        chat._continuous_transcripts.append({"text": "pending"})
        chat._microphone_press("keyboard")
        chat._perception.stop_recording.assert_called_once_with(cancel=True)
        self.assertFalse(chat._continuous_transcripts)
        self.assertIsNone(chat._microphone_source)

    def test_multiple_continuous_transcripts_wait_for_core_in_order(self):
        chat = self.chat(mode="toggle")
        chat._root = Mock()
        chat._perception.transcript_text.side_effect = lambda value: value["text"]
        chat._update_hover_visibility = Mock()
        chat._submit_text = Mock()
        chat._work_lock.acquire()
        for text in ("第一句", "第二句"):
            chat._ui_queue.put(("transcript", {"record_id": 1, "text": text, "continuous": True}))
        chat._pump_ui_queue()
        chat._submit_text.assert_not_called()
        chat._work_lock.release()
        chat._pump_ui_queue()
        chat._pump_ui_queue()
        self.assertEqual([call.args[0] for call in chat._submit_text.call_args_list], ["第一句", "第二句"])

    def test_mode_switch_cancels_pending_results_and_requires_a_fresh_press(self):
        chat = self.chat()
        chat._microphone_source = "keyboard"
        chat._pending_transcript = {"text": "discard"}
        chat._toggle_speech_mode()
        self.assertEqual(chat._ptt_mode, "toggle")
        self.assertIsNone(chat._pending_transcript)
        chat._perception.stop_recording.assert_called_once_with(cancel=True)
        with patch("win32api.GetAsyncKeyState", return_value=0x8000):
            chat._poll_keyboard_microphone()
            chat._poll_keyboard_microphone()
        chat._perception.start_recording.assert_not_called()
        chat._toggle_speech_mode()
        self.assertEqual(chat._ptt_mode, "hold")

    def test_mode_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            prefix = '[renderer]\nmodel_path="avatar.model3.json"\n[desktop_pet.perception]\n'
            for value in ('"hold"', '"toggle"', '"continuous"', 'true', '[]'):
                path.write_text(prefix + f'push_to_talk_mode={value}', encoding="utf-8")
                if value in ('"hold"', '"toggle"'):
                    self.assertEqual(load_config(path).desktop_pet.chat.perception.push_to_talk_mode, value.strip('"'))
                else:
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_pet_open_and_microphone_click_do_not_request_keyboard_focus(self):
        from types import SimpleNamespace
        chat = self.chat()
        chat._anchor = {"visible": True, "click_through": False, "hovered": True}
        chat._cursor_over_chat = lambda: False
        chat._chat_interaction_active = lambda: False
        chat._show_window = Mock()
        chat._show_toolbar = Mock()
        chat._update_hover_visibility(open_requested=True, open_from_pet=True)
        chat._show_window.assert_called_once_with(focus=False)
        chat._window = Mock()
        chat._entry_widget = Mock()
        chat._entry_widget.winfo_toplevel.return_value = chat._window
        chat._set_window_no_activate = Mock()
        chat._activate_chat_from_pointer(SimpleNamespace(widget=Mock()))
        chat._window.focus_force.assert_not_called()
        chat._activate_chat_from_pointer(SimpleNamespace(widget=chat._entry_widget))
        chat._window.focus_force.assert_called_once()
        chat._set_window_no_activate.assert_called_once_with(chat._window, enabled=False)

    def test_native_sidebar_mode_button_scales_and_hides_with_pet(self):
        import tkinter as tk
        from live2d_adapter.ui_theme import scale_widgets

        root = tk.Tk()
        root.withdraw()
        chat = self.chat()
        chat._root, chat._tk = root, tk
        chat._anchor = {"topmost": True, "pet_rect": (500, 100, 700, 600), "work_area": (0, 0, 1920, 1080)}
        try:
            button = chat._create_toolbar_button("speech", "语音模式", chat._toggle_speech_mode)
            canvas = button._mode_canvas
            button.deiconify()
            root.update()
            self.assertIn(button, chat._owned_interaction_windows())
            for factor in (1.0, 0.7, 1.25):
                scale_widgets(button, factor)
                root.update()
                x, y = round(20 * factor), round(20 * factor)
                canvas.event_generate("<ButtonPress-1>", x=x, y=y)
                root.update()
                tag = "mode-toggle" if chat._ptt_mode == "toggle" else "mode-hold"
                self.assertEqual(canvas.itemcget(canvas.find_withtag(tag)[0], "state"), "normal")
                self.assertFalse(any(canvas.type(item) == "text" for item in canvas.find_all()))
            chat._hide_session_windows()
            self.assertEqual(button.state(), "withdrawn")
        finally:
            root.update_idletasks()
            root.destroy()
            # Collect Tk callback cycles on their owner thread before later tests
            # start background UI threads.
            chat._root = chat._speech_button_window = None
            del canvas, button, root, chat
            import gc
            gc.collect()

    def test_keyboard_works_without_a_visible_composer_and_mouse_release_cannot_stop_it(self):
        chat = self.chat()
        self.assertIsNone(chat._mic_canvas)
        pressed = set()
        with patch(
            "win32api.GetAsyncKeyState", side_effect=lambda key: 0x8000 if key in pressed else 0
        ):
            chat._poll_keyboard_microphone()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            chat._poll_keyboard_microphone()
            chat._perception.start_recording.assert_called_once()
            self.assertEqual(chat._microphone_source, "keyboard")
            chat._end_microphone()  # release of the unrelated mouse button
            chat._poll_microphone_release()
            chat._perception.stop_recording.assert_not_called()
            pressed.clear()
            chat._poll_keyboard_microphone()
            chat._perception.stop_recording.assert_called_once_with()

    def test_cancelled_hold_does_not_restart_until_a_new_press_and_busy_turns_are_not_delayed(self):
        chat = self.chat()
        pressed = set()
        with patch(
            "win32api.GetAsyncKeyState", side_effect=lambda key: 0x8000 if key in pressed else 0
        ):
            chat._poll_keyboard_microphone()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            chat._cancel_microphone()
            chat._poll_keyboard_microphone()
            chat._perception.start_recording.assert_called_once()
            pressed.clear()
            chat._poll_keyboard_microphone()
            chat._work_lock.acquire()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            chat._work_lock.release()
            chat._poll_keyboard_microphone()
            chat._perception.start_recording.assert_called_once()
            pressed.clear()
            chat._poll_keyboard_microphone()
            pressed.add(0x77)
            chat._poll_keyboard_microphone()
            self.assertEqual(chat._perception.start_recording.call_count, 2)

    def test_keyboard_release_does_not_stop_mouse_recording_and_disabled_binding_is_never_polled(
        self,
    ):
        chat = self.chat()
        chat._microphone_source = "mouse"
        chat._end_microphone(source="keyboard")
        chat._perception.stop_recording.assert_not_called()
        for speech, key in ((False, "F8"), (True, "")):
            chat = self.chat(speech=speech, key=key)
            with patch("win32api.GetAsyncKeyState") as read:
                chat._poll_keyboard_microphone()
            read.assert_not_called()

    def test_key_state_failure_cancels_its_recording(self):
        chat = self.chat()
        chat._microphone_source = "keyboard"
        with patch("win32api.GetAsyncKeyState", side_effect=OSError("unavailable")):
            chat._poll_keyboard_microphone()
        chat._perception.stop_recording.assert_called_once_with(cancel=True)
