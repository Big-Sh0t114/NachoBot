from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from desktop_pet_adapter.chat import (
    ChatReply,
    DesktopPetBackendClient,
    DesktopPetBackendError,
    DesktopPetChat,
    PetChatHoverState,
    _top_level_geometry,
    _suppress_thread_window_activation,
    chat_ui_layout,
    composer_below_pet_position,
    docked_window_position,
    load_chat_history,
    pet_side_button_positions,
    save_chat_history,
)
from desktop_pet_adapter.config import DesktopChatConfig, PerceptionConfig


class StubLogger:
    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: None


class StubBackend:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def ask(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "zh",
    ) -> ChatReply:
        self.asked.append(text)
        audio = b"RIFF-audio" if include_audio else b""
        return ChatReply(f"回答：{text}（{tts_language}）", audio)

    def announce(
        self,
        text: str,
        *,
        include_audio: bool = True,
        tts_language: str = "zh",
    ) -> ChatReply:
        audio = b"RIFF-audio" if include_audio else b""
        return ChatReply(text, audio)


class FakeTkWidget:
    def __init__(self, kind: str, master: Any = None, **options: Any) -> None:
        self.kind = kind
        self.master = master
        self.options = options
        self.children: list[FakeTkWidget] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._state = "normal"
        self.title_text = ""
        if isinstance(master, FakeTkWidget):
            master.children.append(self)

    def __getattr__(self, name: str):
        if name == "winfo_exists":
            return lambda: self._state != "destroyed"
        if name == "state":
            return lambda: self._state
        if name in {"winfo_x", "winfo_y"}:
            return lambda: 100
        if name in {"winfo_width", "winfo_height"}:
            return lambda: 100
        if name == "winfo_id":
            return lambda: id(self) & 0xFFFF
        if name == "bbox":
            return lambda *_args: (0, 0, 420, 700)
        if name in {
            "create_window",
            "create_polygon",
            "create_text",
            "create_oval",
            "create_line",
        }:
            def record_call(*args: Any, **kwargs: Any) -> int:
                self.calls.append((name, args, kwargs))
                return 1

            return record_call
        return lambda *_args, **_kwargs: None

    def withdraw(self) -> None:
        self._state = "withdrawn"

    def deiconify(self) -> None:
        self._state = "normal"

    def title(self, value: str) -> None:
        self.title_text = value

    def destroy(self) -> None:
        self._state = "destroyed"


class FakeTkModule:
    TclError = RuntimeError

    def __init__(self) -> None:
        self.created: list[str] = []

    def __getattr__(self, name: str):
        if name == "StringVar":
            return lambda **_options: SimpleNamespace(value="")
        if name in {"Toplevel", "Frame", "Canvas", "Scrollbar", "Label", "Entry"}:
            def factory(master: Any = None, **options: Any) -> FakeTkWidget:
                self.created.append(name)
                return FakeTkWidget(name, master, **options)

            return factory
        raise AttributeError(name)


class ChatRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands: list[tuple[str, Any]] = []
        self.backend = StubBackend()
        self.chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda command, value: self.commands.append((command, value)),
            StubLogger(),
            backend=self.backend,
        )

    def test_plain_text_asks_backend_and_queues_speech(self) -> None:
        reply = self.chat.execute_for_test("你好")

        self.assertEqual(reply, "回答：你好（zh）")
        self.assertEqual(self.commands[0], ("state", "start_thinking"))
        self.assertIn(("emotion", "joy"), self.commands)
        self.assertIn(("play_chat_audio", b"RIFF-audio"), self.commands)

    def test_slash_text_is_forwarded_literally_without_desktop_side_effects(self) -> None:
        for text in ("/动作 开心", "/打开 calc", "/闭嘴", "/语言 日语"):
            self.commands.clear()
            self.chat.execute_for_test(text)
            self.assertEqual(self.backend.asked[-1], text)
            self.assertFalse(
                any(command.startswith("desktop_") for command, _value in self.commands)
            )

    def test_mute_mode_keeps_text_reply_and_stops_audio(self) -> None:
        self.chat._set_voice_enabled(False)
        self.commands.clear()

        reply = self.chat.execute_for_test("继续聊天")

        self.assertEqual(reply, "回答：继续聊天（zh）")
        self.assertNotIn(("play_chat_audio", b"RIFF-audio"), self.commands)
        self.assertIn(("state", "finish_reply"), self.commands)

    def test_pending_stream_keeps_renderer_replying_until_output_finishes(self) -> None:
        self.chat._present_reply(ChatReply("文字已到，语音准备中", audio_pending=True))

        self.assertNotIn(("state", "finish_reply"), self.commands)

    def test_language_control_changes_backend_language(self) -> None:
        self.chat._cycle_language()
        self.assertEqual(self.chat.execute_for_test("こんにちは"), "回答：こんにちは（ja）")

    def test_language_control_keeps_english_and_auto_available(self) -> None:
        self.chat._cycle_language()  # zh -> ja
        self.chat._cycle_language()  # ja -> en
        self.assertEqual(self.chat.execute_for_test("hello"), "回答：hello（en）")
        self.chat._cycle_language()  # en -> auto
        self.assertEqual(self.chat.execute_for_test("hello"), "回答：hello（auto）")

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
            (1480, 14),
        )

    def test_composer_uses_shared_band_and_moves_above_pet_at_bottom_edge(self) -> None:
        pet_rect = (900, 100, 1100, 700)
        position = composer_below_pet_position(
            pet_rect,
            (0, 0, 1920, 1080),
            (380, 44),
        )
        self.assertEqual(position, (810, 706))
        self.assertEqual(position[0] + 190, 1000)
        self.assertGreaterEqual(position[1], pet_rect[3])

        bottom_position = composer_below_pet_position(
            (900, 600, 1100, 1060),
            (0, 0, 1920, 1080),
            (380, 44),
        )
        self.assertEqual(bottom_position, (810, 550))
        self.assertLessEqual(bottom_position[1] + 44, 600 - 6)

    def test_toolbar_buttons_share_one_horizontal_row(self) -> None:
        pet_rect = (500, 100, 720, 500)
        positions = pet_side_button_positions(pet_rect, (0, 0, 1920, 1080))

        self.assertEqual(positions["settings"][1], positions["history"][1])
        self.assertEqual(positions["history"][0] - positions["settings"][0], 58)
        self.assertGreaterEqual(positions["settings"][0], 0)

    @staticmethod
    def _intersects(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
        return not (
            first[2] <= second[0]
            or first[0] >= second[2]
            or first[3] <= second[1]
            or first[1] >= second[3]
        )

    def test_chat_layout_rectangles_are_disjoint_at_edges_scales_and_counts(self) -> None:
        cases = (
            ((1000, 500, 1221, 676), (0, 0, 1920, 1080)),
            ((0, 20, 221, 196), (0, 0, 1920, 1080)),
            ((1000, 900, 1221, 1076), (0, 0, 1920, 1080)),
            ((0, 500, 221, 676), (0, 0, 1920, 1080)),
            ((1699, 500, 1920, 676), (0, 0, 1920, 1080)),
            ((-1800, -300, -1579, -124), (-1920, -1080, 0, 0)),
            # At 2x the vertical block cannot fit above or below, so it moves
            # to the side with enough work-area width.
            ((1000, 160, 1221, 336), (0, 0, 2200, 500)),
        )
        available_kinds = ("settings", "history", "language", "speech", "proactive")

        for scale in (1.0, 1.5, 2.0):
            for count in range(1, 6):
                kinds = available_kinds[:count]
                for pet_rect, work_area in cases:
                    with self.subTest(scale=scale, kinds=kinds, pet_rect=pet_rect):
                        layout = chat_ui_layout(pet_rect, work_area, scale, kinds)
                        self.assertTrue(layout.fits, layout)
                        self.assertFalse(self._intersects(layout.toolbar_rect, layout.composer_rect))
                        self.assertFalse(self._intersects(layout.bounds_rect, pet_rect))
                        self.assertGreaterEqual(layout.bounds_rect[0], work_area[0])
                        self.assertGreaterEqual(layout.bounds_rect[1], work_area[1])
                        self.assertLessEqual(layout.bounds_rect[2], work_area[2])
                        self.assertLessEqual(layout.bounds_rect[3], work_area[3])
                        self.assertEqual(layout.toolbar_rect[3] + round(6 * scale), layout.composer_rect[1])
                        button_width = round(44 * scale)
                        for index, kind in enumerate(kinds):
                            x, y = layout.toolbar_positions[kind]
                            button_rect = (x, y, x + button_width, y + button_width)
                            self.assertFalse(self._intersects(button_rect, pet_rect))
                            self.assertFalse(self._intersects(button_rect, layout.composer_rect))
                            if index:
                                previous_x, previous_y = layout.toolbar_positions[kinds[index - 1]]
                                self.assertEqual(y, previous_y)
                                self.assertEqual(x - previous_x, button_width + round(14 * scale))
        side_layout = chat_ui_layout(
            (1000, 160, 1221, 336),
            (0, 0, 2200, 500),
            2.0,
            available_kinds,
        )
        self.assertEqual(side_layout.placement, "left")

    def test_chat_layout_marks_physically_impossible_fallback(self) -> None:
        layout = chat_ui_layout(
            (0, 0, 80, 80),
            (0, 0, 100, 100),
            2.0,
            ("settings", "history", "language", "speech", "proactive"),
        )

        self.assertFalse(layout.fits)
        self.assertIn("constrained", layout.placement)

    def test_toolbar_definitions_match_settings_three_and_five_button_modes(self) -> None:
        cases = (
            (DesktopChatConfig(enabled=False), ("settings",)),
            (
                DesktopChatConfig(
                    enabled=True,
                    perception=PerceptionConfig(speech=False),
                ),
                ("settings", "history", "language"),
            ),
            (
                DesktopChatConfig(
                    enabled=True,
                    transport="core",
                    perception=PerceptionConfig(speech=True),
                ),
                ("settings", "history", "language", "speech", "proactive"),
            ),
        )
        for config, expected in cases:
            with self.subTest(expected=expected):
                chat = DesktopPetChat(
                    config,
                    "Hiyori",
                    lambda _command, _value: None,
                    StubLogger(),
                    backend=StubBackend(),
                )
                kinds = tuple(item[0] for item in chat._toolbar_definitions())
                self.assertEqual(kinds, expected)
                layout = chat_ui_layout(
                    (1000, 500, 1221, 676),
                    (0, 0, 1920, 1080),
                    1.0,
                    kinds,
                )
                self.assertEqual(tuple(layout.toolbar_positions), expected)
                self.assertFalse(self._intersects(layout.toolbar_rect, layout.composer_rect))

    def test_production_composer_toolbar_and_animation_share_layout(self) -> None:
        import desktop_pet_adapter.chat as chat_module

        chat = DesktopPetChat(
            DesktopChatConfig(
                enabled=True,
                transport="core",
                perception=PerceptionConfig(speech=True),
            ),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._ui_scale = 2.0
        chat._anchor = {
            "pet_rect": (1000, 160, 1221, 336),
            "work_area": (0, 0, 2200, 500),
            "topmost": True,
        }
        composer = FakeTkWidget("composer")
        composer._pet_topmost = True
        chat._window = composer
        toolbar_windows = {
            kind: FakeTkWidget(kind)
            for kind, _label, _command in chat._toolbar_definitions()
        }
        chat._toolbar_window = toolbar_windows["settings"]
        chat._history_button_window = toolbar_windows["history"]
        chat._language_button_window = toolbar_windows["language"]
        chat._speech_button_window = toolbar_windows["speech"]
        chat._proactive_button_window = toolbar_windows["proactive"]
        actual_geometries: dict[Any, str] = {}
        chat._set_geometry_if_changed = lambda window, geometry: actual_geometries.__setitem__(window, geometry)
        expected = chat._chat_ui_layout()
        self.assertIsNotNone(expected)

        with patch("desktop_pet_adapter.chat.chat_ui_layout", wraps=chat_module.chat_ui_layout) as layout_calls:
            chat._position_window()
            for kind in toolbar_windows:
                chat._position_toolbar(kind)
            self.assertEqual(layout_calls.call_count, len(toolbar_windows) + 1)

        self.assertEqual(
            actual_geometries[composer],
            _top_level_geometry(800, 120, *expected.composer_rect[:2]),
        )
        for kind, window in toolbar_windows.items():
            self.assertEqual(
                actual_geometries[window],
                _top_level_geometry(88, 88, *expected.toolbar_positions[kind]),
            )

        # Real popup animation must stay within one gap of each target even if
        # sibling callbacks have not landed on the same animation frame.
        chat._tk = SimpleNamespace(TclError=RuntimeError)
        for window in toolbar_windows.values():
            window.geometry = lambda value: None
            window.after = lambda _delay, _callback: "timer"
        with patch("desktop_pet_adapter.chat.chat_ui_layout", wraps=chat_module.chat_ui_layout) as layout_calls:
            for kind in toolbar_windows:
                chat._position_toolbar(kind, animate=True)
            self.assertEqual(layout_calls.call_count, len(toolbar_windows))
        gap = 28
        for kind, animation in chat._toolbar_animations.items():
            target_x = expected.toolbar_positions[kind][0]
            self.assertLessEqual(abs(animation["from_x"] - target_x), gap)

    def test_negative_monitor_geometry_uses_absolute_tk_position_form(self) -> None:
        self.assertEqual(_top_level_geometry(44, 44, -1800, -300), "44x44+-1800+-300")


class PetChatHoverStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = PetChatHoverState()

    def update(self, now: float, **kwargs: Any) -> bool:
        values = {
            "pet_available": True,
            "pet_hovered": False,
            "chat_hovered": False,
            "interaction_active": False,
        }
        values.update(kwargs)
        return self.state.update(now=now, **values)

    def test_initially_hidden_until_cursor_hits_visible_pet(self) -> None:
        self.assertFalse(self.state.visible)
        self.assertFalse(self.update(0.0))
        self.assertTrue(self.update(0.1, pet_hovered=True))

    def test_unrelated_open_request_does_not_bypass_pet_hover_gate(self) -> None:
        self.assertFalse(self.update(0.0, open_allowed=True))
        self.assertTrue(self.update(0.1, pet_hovered=True, open_allowed=True))

    def test_pet_to_chat_pointer_transfer_keeps_window_open(self) -> None:
        self.assertTrue(self.update(0.0, pet_hovered=True))
        self.assertTrue(self.update(0.1, chat_hovered=True))
        self.assertTrue(self.update(0.7, chat_hovered=True))

    def test_any_visible_hover_session_surface_keeps_group_open(self) -> None:
        self.assertTrue(self.update(0.0, pet_hovered=True))
        self.assertTrue(self.update(0.1, chat_hovered=True))
        self.assertTrue(self.update(10.0, chat_hovered=True))

    def test_inactive_leave_hides_after_grace_period(self) -> None:
        self.assertTrue(self.update(0.0, pet_hovered=True))
        self.assertTrue(self.update(0.1))
        self.assertTrue(self.update(0.59))
        self.assertFalse(self.update(0.6))

    def test_input_draft_focus_or_drag_keeps_chat_visible(self) -> None:
        self.assertTrue(self.update(0.0, pet_hovered=True))
        self.assertTrue(self.update(4.0, interaction_active=True))

    def test_escape_suppresses_reopen_until_pointer_leaves_and_reenters_pet(self) -> None:
        self.assertTrue(self.update(0.0, pet_hovered=True))
        self.state.hide_until_pet_leave()
        self.assertFalse(self.update(0.1, pet_hovered=True))
        self.assertFalse(self.update(0.2, pet_hovered=True, open_from_pet=True))
        self.assertFalse(self.update(0.3, pet_hovered=False))
        self.assertTrue(self.update(0.4, pet_hovered=True))

    def test_double_click_open_does_not_bypass_pet_hover(self) -> None:
        self.assertFalse(self.update(0.0, open_from_pet=True))
        self.assertTrue(self.update(0.1, open_from_pet=True, pet_hovered=True))

    def test_hidden_or_clickthrough_pet_cannot_reveal(self) -> None:
        self.assertFalse(
            self.update(0.0, pet_available=False, pet_hovered=False)
        )

    def test_background_reply_does_not_reveal_hidden_chat(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._append_message("bot", "后台回复")

        self.assertFalse(chat._hover_state.visible)
        self.assertIsNone(chat._window)
        self.assertIsNone(chat._toolbar_window)
        self.assertIsNone(chat._history_window)
        self.assertEqual(chat._history_messages[-1]["text"], "后台回复")

    def test_pet_hover_reveals_composer_and_toolbar_without_opening_panels(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        revealed: list[str] = []
        chat._show_window = lambda *, focus: revealed.append("composer")
        chat._show_toolbar = lambda: revealed.append("toolbar")
        chat._anchor = {
            "visible": True,
            "click_through": False,
            "hovered": True,
        }

        chat._update_hover_visibility()

        self.assertTrue(chat._hover_state.visible)
        self.assertEqual(revealed, ["composer", "toolbar"])
        self.assertIsNone(chat._history_window)
        self.assertIsNone(chat._parameter_panel)

    def test_input_draft_alone_does_not_hold_group_but_focused_draft_does(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        window = FakeTkWidget("Toplevel")
        entry = FakeTkWidget("Entry", window)
        window.focus_get = lambda: entry
        chat._window = window
        chat._entry_widget = entry
        chat._input_variable = SimpleNamespace(get=lambda: "草稿")
        root = window.winfo_id()
        foreground = {"value": root + 1}
        fake_win32con = SimpleNamespace(GA_ROOT=2)
        fake_win32gui = SimpleNamespace(
            GetForegroundWindow=lambda: foreground["value"],
            GetAncestor=lambda hwnd, _flag: hwnd,
            IsChild=lambda _parent, _child: False,
        )
        with patch.dict(
            "sys.modules",
            {"win32con": fake_win32con, "win32gui": fake_win32gui},
        ), patch("desktop_pet_adapter.chat.time.monotonic", return_value=10.0):
            self.assertFalse(chat._chat_interaction_active())
            foreground["value"] = root
            self.assertTrue(chat._chat_interaction_active())
            chat._input_variable = SimpleNamespace(get=lambda: "")
            self.assertFalse(chat._chat_interaction_active())

    def test_recent_input_edit_holds_only_for_short_activity_grace(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._last_input_edit_at = 9.5
        with patch("desktop_pet_adapter.chat.time.monotonic", return_value=10.0):
            self.assertTrue(chat._chat_interaction_active())
        chat._last_input_edit_at = 8.0
        with patch("desktop_pet_adapter.chat.time.monotonic", return_value=10.0):
            self.assertFalse(chat._chat_interaction_active())

    def test_history_surface_is_readonly_and_separate_from_composer(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        fake_tk = FakeTkModule()
        chat._tk = fake_tk
        history_window = FakeTkWidget("Toplevel")

        chat._build_history_surface(history_window)

        self.assertNotIn("Entry", fake_tk.created)
        self.assertIsNone(chat._entry_widget)
        self.assertIsNotNone(chat._history_canvas)
        self.assertIsNotNone(chat._history_content)

    def test_composer_is_a_borderless_pill_with_only_one_entry(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        fake_tk = FakeTkModule()
        chat._tk = fake_tk
        window = FakeTkWidget("Toplevel")

        chat._build_composer_surface(window)
        chat._set_result("后台回答完成")
        chat._refresh_voice_controls()

        self.assertEqual(fake_tk.created, ["Canvas", "Entry"])
        self.assertEqual([child.kind for child in window.children], ["Canvas"])
        self.assertEqual([child.kind for child in window.children[0].children], ["Entry"])
        self.assertEqual(window.children[0].options["background"], "#ff00ff")
        self.assertEqual(
            {
                "relief": chat._entry_widget.options["relief"],
                "borderwidth": chat._entry_widget.options["borderwidth"],
                "highlightthickness": chat._entry_widget.options["highlightthickness"],
            },
            {"relief": "flat", "borderwidth": 0, "highlightthickness": 0},
        )
        self.assertIsNone(chat._result_widget)
        self.assertIsNone(chat._voice_button)
        self.assertIsNone(chat._language_button)

    def test_hover_buttons_are_independent_pet_side_windows(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._tk = FakeTkModule()
        chat._root = FakeTkWidget("Root")
        chat._anchor = {
            "pet_rect": (500, 100, 720, 500),
            "work_area": (0, 0, 1920, 1080),
            "topmost": True,
        }
        chat._hover_state.visible = True
        chat._set_window_no_activate = lambda *_args, **_kwargs: None

        chat._show_toolbar()

        self.assertIsNot(chat._toolbar_window, chat._history_button_window)
        self.assertEqual(chat._toolbar_window.title_text, "桌宠设置按钮")
        self.assertEqual(chat._history_button_window.title_text, "桌宠历史按钮")
        self.assertEqual(chat._toolbar_window.state(), "normal")
        self.assertEqual(chat._history_button_window.state(), "normal")
        for button in (chat._toolbar_window, chat._history_button_window):
            canvas = button.children[0]
            self.assertEqual(canvas.options["width"], 44)
            self.assertEqual(canvas.options["height"], 44)
            self.assertFalse(
                any(call[0] == "create_text" for call in canvas.calls)
            )
        self.assertIsNone(chat._history_window)
        self.assertIsNone(chat._parameter_panel)

    def test_hidden_tk_root_is_withdrawn_then_nonactivating_and_disabled(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        events: list[tuple[str, Any]] = []

        class Root:
            def withdraw(self) -> None:
                events.append(("withdraw", self))

            def update_idletasks(self) -> None:
                events.append(("flush", self))

            def winfo_id(self) -> int:
                return 12

        root = Root()
        chat._set_window_no_activate = lambda window, enabled: events.append(
            ("noactivate", (window, enabled))
        )
        win32con = SimpleNamespace(GA_ROOT=2)
        win32gui = SimpleNamespace(
            GetAncestor=lambda _hwnd, _flag: 120,
            EnableWindow=lambda hwnd, enabled: events.append(
                ("disable", (hwnd, enabled))
            ),
        )

        with patch.dict(
            sys.modules,
            {"win32con": win32con, "win32gui": win32gui},
        ):
            chat._prepare_hidden_root(root)

        self.assertEqual(
            events,
            [
                ("withdraw", root),
                ("flush", root),
                ("noactivate", (root, True)),
                ("disable", (120, False)),
            ],
        )

    def test_run_ui_keeps_dpi_context_through_mainloop_callbacks_and_cleanup(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        events: list[tuple[str, bool]] = []
        dpi_active = False

        class TrackingDpiContext:
            def __enter__(self) -> bool:
                nonlocal dpi_active
                if dpi_active:
                    raise AssertionError("DPI context entered twice")
                dpi_active = True
                events.append(("dpi_enter", dpi_active))
                return True

            def __exit__(self, *_exc_info: Any) -> bool:
                nonlocal dpi_active
                events.append(("dpi_exit", dpi_active))
                dpi_active = False
                return False

        class NoopContext:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_exc_info: Any) -> bool:
                return False

        class Root:
            callback: Any = None

            def after(self, delay: int, callback: Any) -> None:
                events.append(("after", dpi_active))
                self.callback = callback

            def mainloop(self) -> None:
                events.append(("mainloop", dpi_active))
                self.callback()

        root = Root()

        class FakeTkModule:
            @staticmethod
            def Tk() -> Root:
                events.append(("tk_root", dpi_active))
                return root

        class Perception:
            def start(self) -> None:
                events.append(("perception_start", dpi_active))

            def close(self) -> None:
                events.append(("perception_close", dpi_active))

        chat._perception = Perception()
        chat._prepare_hidden_root = lambda _root: events.append(
            ("hidden_root", dpi_active)
        )
        chat._pump_ui_queue = lambda: events.append(("ui_callback", dpi_active))

        with (
            patch(
                "desktop_pet_adapter.chat.per_monitor_dpi_context",
                return_value=TrackingDpiContext(),
            ),
            patch(
                "desktop_pet_adapter.chat._suppress_thread_window_activation",
                side_effect=NoopContext,
            ),
            patch.dict(sys.modules, {"tkinter": FakeTkModule}),
        ):
            chat._run_ui()

        self.assertFalse(dpi_active)
        self.assertEqual(
            events,
            [
                ("dpi_enter", True),
                ("tk_root", True),
                ("hidden_root", True),
                ("perception_start", True),
                ("after", True),
                ("mainloop", True),
                ("ui_callback", True),
                ("perception_close", True),
                ("dpi_exit", True),
            ],
        )
        self.assertTrue(chat._ready.is_set())
        self.assertIsNone(chat._root)

    def test_startup_activation_hook_precedes_tk_and_unhooks_after_constructor_error(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        events: list[tuple[Any, ...]] = []
        dpi_events: list[tuple[str, bool]] = []
        dpi_active = False

        class TrackingDpiContext:
            def __enter__(self) -> bool:
                nonlocal dpi_active
                dpi_active = True
                dpi_events.append(("dpi_enter", dpi_active))
                return True

            def __exit__(self, *_exc_info: Any) -> bool:
                nonlocal dpi_active
                dpi_events.append(("dpi_exit", dpi_active))
                dpi_active = False
                return False

        class FakeUser32:
            callback: Any = None

            def SetWindowsHookExW(
                _self,
                hook_id: int,
                callback: Any,
                module: Any,
                thread_id: int,
            ) -> int:
                events.append(("install", hook_id, module, thread_id))
                dpi_events.append(("hook_install", dpi_active))
                self.assertIsNotNone(callback)
                FakeUser32.callback = callback
                return 0x1234

            def CallNextHookEx(
                _self,
                hook: int,
                code: int,
                wparam: int,
                lparam: int,
            ) -> int:
                events.append(("forward", hook, code, wparam, lparam))
                dpi_events.append(("native_callback", dpi_active))
                return 70 + code

            def UnhookWindowsHookEx(_self, hook: int) -> bool:
                events.append(("unhook", hook))
                dpi_events.append(("hook_unhook", dpi_active))
                return True

        class FakeKernel32:
            def GetCurrentThreadId(_self) -> int:
                events.append(("thread_id", 4321))
                return 4321

        user32 = FakeUser32()
        kernel32 = FakeKernel32()

        def load_library(name: str, *, use_last_error: bool) -> Any:
            events.append(("load_library", name, use_last_error))
            return {"user32": user32, "kernel32": kernel32}[name]

        class FakeTkModule:
            TclError = RuntimeError

            @staticmethod
            def Tk() -> Any:
                events.append(("tk_constructor",))
                dpi_events.append(("tk_constructor", dpi_active))
                callback = FakeUser32.callback
                activation_result = callback(5, 0xA1, 0xB1)
                dpi_events.append(("activation_callback", dpi_active))
                other_result = callback(2, 0xA2, 0xB2)
                negative_result = callback(-1, 0xA3, 0xB3)
                events.append(
                    ("callback_results", activation_result, other_result, negative_result)
                )
                raise RuntimeError("simulated Tk constructor failure")

        class Perception:
            def close(self) -> None:
                dpi_events.append(("perception_cleanup", dpi_active))

        chat._perception = Perception()

        with (
            patch(
                "desktop_pet_adapter.chat.per_monitor_dpi_context",
                return_value=TrackingDpiContext(),
            ),
            patch("desktop_pet_adapter.chat.sys.platform", "win32"),
            patch.object(ctypes, "WinDLL", side_effect=load_library, create=True),
            patch.dict(sys.modules, {"tkinter": FakeTkModule}),
        ):
            chat._run_ui()

        install_index = next(i for i, event in enumerate(events) if event[0] == "install")
        constructor_index = next(
            i for i, event in enumerate(events) if event[0] == "tk_constructor"
        )
        unhook_index = next(i for i, event in enumerate(events) if event[0] == "unhook")
        self.assertLess(install_index, constructor_index)
        self.assertGreater(unhook_index, constructor_index)
        self.assertEqual(events[install_index], ("install", 5, None, 4321))
        self.assertIn(("callback_results", 1, 72, 69), events)
        self.assertIn(("forward", 0x1234, 2, 0xA2, 0xB2), events)
        self.assertIn(("forward", 0x1234, -1, 0xA3, 0xB3), events)
        self.assertIn(("unhook", 0x1234), events)
        self.assertTrue(chat._ready.is_set())
        self.assertIsNone(chat._root)
        self.assertFalse(dpi_active)
        self.assertEqual(
            dpi_events,
            [
                ("dpi_enter", True),
                ("hook_install", True),
                ("tk_constructor", True),
                ("activation_callback", True),
                ("native_callback", True),
                ("native_callback", True),
                ("hook_unhook", True),
                ("perception_cleanup", True),
                ("dpi_exit", True),
            ],
        )

    def test_failed_activation_hook_unhook_retains_callback_on_ui_thread(self) -> None:
        events: list[tuple[Any, ...]] = []

        class FakeUser32:
            callback_id: int | None = None

            def SetWindowsHookExW(
                _self,
                _hook_id: int,
                callback: Any,
                _module: Any,
                _thread_id: int,
            ) -> int:
                FakeUser32.callback_id = id(callback)
                return 0x4321

            def CallNextHookEx(_self, _hook: int, code: int, *_args: int) -> int:
                return code

            def UnhookWindowsHookEx(_self, hook: int) -> bool:
                events.append(("unhook_failed", hook))
                return False

        class FakeKernel32:
            def GetCurrentThreadId(_self) -> int:
                return 8765

        user32 = FakeUser32()
        kernel32 = FakeKernel32()

        def load_library(name: str, *, use_last_error: bool) -> Any:
            events.append(("load_library", name, use_last_error))
            return {"user32": user32, "kernel32": kernel32}[name]

        ui_thread = threading.current_thread()
        retention_attribute = "_nachobot_live2d_cbt_hooks"
        previous_retained = getattr(ui_thread, retention_attribute, None)
        if hasattr(ui_thread, retention_attribute):
            delattr(ui_thread, retention_attribute)
        try:
            with (
                patch("desktop_pet_adapter.chat.sys.platform", "win32"),
                patch.object(ctypes, "WinDLL", side_effect=load_library, create=True),
                self.assertRaisesRegex(OSError, "UnhookWindowsHookEx failed"),
            ):
                with _suppress_thread_window_activation():
                    pass

            retained = getattr(ui_thread, retention_attribute)
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0][0], 0x4321)
            self.assertEqual(id(retained[0][1]), FakeUser32.callback_id)
            self.assertEqual(events[-1], ("unhook_failed", 0x4321))
        finally:
            if hasattr(ui_thread, retention_attribute):
                delattr(ui_thread, retention_attribute)
            if previous_retained is not None:
                setattr(ui_thread, retention_attribute, previous_retained)

    def test_history_opens_only_after_explicit_action_during_hover_session(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._tk = FakeTkModule()
        chat._root = FakeTkWidget("Root")
        chat._anchor = {
            "pet_rect": (400, 100, 700, 500),
            "work_area": (0, 0, 1920, 1080),
            "topmost": True,
        }
        chat._position_history_window = lambda: None

        chat._show_history_window()
        self.assertIsNone(chat._history_window)

        chat._hover_state.visible = True
        chat._show_history_window()

        self.assertIsNotNone(chat._history_window)
        self.assertIsNone(chat._window)
        self.assertIsNone(chat._entry_widget)

    def test_history_close_does_not_hide_composer_or_clear_draft(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        composer = FakeTkWidget("Toplevel")
        history = FakeTkWidget("Toplevel")
        chat._window = composer
        chat._history_window = history
        chat._input_variable = SimpleNamespace(get=lambda: "保留的草稿")

        chat._hide_history_window()

        self.assertEqual(composer.state(), "normal")
        self.assertEqual(history.state(), "withdrawn")
        self.assertEqual(chat._input_variable.get(), "保留的草稿")

    def test_session_hide_withdraws_surfaces_but_keeps_draft(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        windows = [FakeTkWidget(name) for name in ("composer", "toolbar", "history", "panel")]
        chat._window, chat._toolbar_window, chat._history_window = windows[:3]
        chat._input_variable = SimpleNamespace(get=lambda: "保留的草稿")

        class Panel:
            window = windows[3]

            def hide(self) -> None:
                self.window.withdraw()

        chat._parameter_panel = Panel()
        chat._hide_session_windows()

        self.assertTrue(all(window.state() == "withdrawn" for window in windows))
        self.assertEqual(chat._input_variable.get(), "保留的草稿")



    def test_unfocused_draft_hides_entire_group_after_leave_and_is_preserved(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        windows = [FakeTkWidget(name) for name in ("composer", "toolbar", "history", "panel")]
        chat._window, chat._toolbar_window, chat._history_window = windows[:3]
        chat._input_variable = SimpleNamespace(get=lambda: "草稿不要丢")
        chat._cursor_over_chat = lambda: False
        chat._show_toolbar = lambda: None

        class Panel:
            window = windows[3]

            def is_interacting(self) -> bool:
                return False

            def hide(self) -> None:
                self.window.withdraw()

        chat._parameter_panel = Panel()
        chat._anchor = {
            "pet_rect": (400, 100, 700, 500),
            "work_area": (0, 0, 1920, 1080),
            "visible": True,
            "topmost": True,
            "click_through": False,
            "hovered": True,
        }
        ticks = iter((0.0, 0.0, 0.1, 0.1, 0.6, 0.6))
        with patch("desktop_pet_adapter.chat.time.monotonic", side_effect=lambda: next(ticks)):
            chat._update_hover_visibility()
            chat._anchor["hovered"] = False
            chat._update_hover_visibility()
            self.assertTrue(chat._hover_state.visible)
            chat._update_hover_visibility()

        self.assertFalse(chat._hover_state.visible)
        self.assertTrue(all(window.state() == "withdrawn" for window in windows))
        self.assertEqual(chat._input_variable.get(), "草稿不要丢")

    def test_disabled_chat_still_reveals_toolbar_for_model_controls(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=False, transport="core"),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
        )
        revealed: list[str] = []
        chat._show_toolbar = lambda: revealed.append("toolbar")
        chat._show_window = lambda **_kwargs: revealed.append("composer")
        chat._anchor = {
            "visible": True,
            "click_through": False,
            "hovered": True,
        }

        chat.open(from_pet=True)
        chat._update_hover_visibility(open_from_pet=True)

        self.assertIsNone(chat._thread)
        self.assertTrue(chat._hover_state.visible)
        self.assertEqual(revealed, ["toolbar"])
        self.assertIsNone(chat._window)
        with self.assertRaisesRegex(DesktopPetBackendError, "未启用"):
            chat.backend.ask("不可达")

    def test_disabled_chat_can_open_parameter_panel_only_inside_hover_group(self) -> None:
        class Panel:
            def __init__(self, _root: Any, _change: Any, _reset: Any, _logger: Any) -> None:
                self.window = FakeTkWidget("Toplevel")
                self.shown: list[list[dict[str, Any]]] = []

            def show(self, specs: list[dict[str, Any]]) -> None:
                self.shown.append(specs)
                self.window.deiconify()

        chat = DesktopPetChat(
            DesktopChatConfig(enabled=False, transport="core"),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            parameter_panel_factory=Panel,
        )
        chat._show_parameter_panel()
        self.assertIsNone(chat._parameter_panel)
        chat._hover_state.visible = True
        chat._show_parameter_panel()

        self.assertIsNotNone(chat._parameter_panel)
        self.assertEqual(chat._parameter_panel.window.state(), "normal")

    def test_independent_position_does_not_discard_fresh_pet_anchor(self) -> None:
        chat = DesktopPetChat(
            DesktopChatConfig(enabled=True),
            "Hiyori",
            lambda _command, _value: None,
            StubLogger(),
            backend=StubBackend(),
        )
        chat._history_position = (700, 300)
        chat.sync_anchor(
            (1200, 100, 1500, 700),
            (0, 0, 1920, 1080),
            visible=True,
            topmost=True,
            click_through=False,
            hovered=True,
        )

        action, value = chat._ui_queue.get_nowait()
        self.assertEqual(action, "anchor")
        self.assertEqual(value["pet_rect"], (1200, 100, 1500, 700))
        self.assertTrue(value["hovered"])


class LocalHostHandler(BaseHTTPRequestHandler):
    replies = 0
    latest_reply = ""
    audio_version = 0
    streamed_audio_version = 0
    last_speak = None
    reply_error = ""

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
                    "reply_error": self.__class__.reply_error,
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
        if self.path == "/api/respond" and self.__class__.reply_error:
            self._json({"ok": True, "request_id": "test-request"})
            return
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
        LocalHostHandler.reply_error = ""
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

    def test_ask_stops_when_backend_reports_expired_reply(self) -> None:
        LocalHostHandler.reply_error = "AI 回复超过 45 秒，已取消"

        with self.assertRaisesRegex(DesktopPetBackendError, "超过 45 秒"):
            self.client.ask("不会无限等待")


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
